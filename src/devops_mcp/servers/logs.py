"""Logs MCP server: tail, search and summarise application log files inside the allowed roots.

Run:  python -m devops_mcp.servers.logs
Dev:  mcp dev src/devops_mcp/servers/logs.py

summarize_errors is the tool that keeps an agent from reading 10k lines: it groups repeated
tracebacks and error lines into "N occurrences of X, first/last seen at T".
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from devops_mcp.config import SKIP_DIRS
from devops_mcp.safety import display_path, is_binary, redact, resolve_in_roots, truncate

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

MAX_TAIL = 5000
MAX_ANALYZE_LINES = 200_000
MAX_LINE_CHARS = 300
SAMPLE_LINES = 14
LOG_GLOBS = ("*.log", "*.log.*", "*.out", "*.err", "*.txt")

mcp = MCPServer(
    "devops-logs",
    instructions=(
        "Read-only log analysis for files inside the allowed roots. Start with summarize_errors "
        "to see which errors dominate and when they started, then search_logs with context around "
        "a specific message, then read_log (tail) for the raw recent lines. Secrets are redacted."
    ),
)

# --------------------------------------------------------------------------- line parsing

# Leading timestamp in the common shapes: 2026-09-11T13:27:28.302Z | 2026-09-11 13:27:28,302 | [2026-09-11 13:27:28,302]
_TS_RE = re.compile(
    r"""^\s*\[?
        (?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)
        \]?[ \t]?""",
    re.VERBOSE,
)
_LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|FATAL|PANIC|SEVERE|EMERG)\b")
_LEVEL_ALIAS = {"WARN": "WARNING", "SEVERE": "ERROR", "EMERG": "CRITICAL", "PANIC": "CRITICAL", "FATAL": "CRITICAL"}
_ERROR_LEVELS = {"ERROR", "CRITICAL"}
_SEVERITY = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2}
_TRACEBACK_START = "Traceback (most recent call last):"
_CHAIN_MARKERS = ("During handling of the above exception", "The above exception was the direct cause")
_EXC_LINE_RE = re.compile(r"^(?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception|Interrupt|Exit|Fault|Warning)\b")
_PY_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<fn>\S+))?')
_JS_FRAME_RE = re.compile(r"^\s*at .*?\(?(?P<file>[^\s():]+):(?P<line>\d+)(?::\d+)?\)?")
_NORMALISE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "N"),
]


def _split_ts(line: str) -> tuple[str | None, str]:
    m = _TS_RE.match(line)
    if not m:
        return None, line
    return m.group("ts"), line[m.end() :]


def _level_of(rest: str) -> str | None:
    m = _LEVEL_RE.search(rest[:80])
    if not m:
        return None
    lvl = m.group(1)
    return _LEVEL_ALIAS.get(lvl, lvl)


def _is_continuation(rest: str) -> bool:
    if not rest.strip():
        return False
    return rest.startswith((" ", "\t", "^", "~")) or rest.strip().startswith(("at ", "Caused by:", "... "))


def _normalise(msg: str) -> str:
    for pattern, repl in _NORMALISE:
        msg = pattern.sub(repl, msg)
    return msg.strip()


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


# --------------------------------------------------------------------------- file helpers


def _suggest_logs(directory: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if Path(dirpath).relative_to(directory).parts[2:]:
            dirnames[:] = []  # depth 2 max
        for fn in filenames:
            if any(Path(fn).match(g) for g in LOG_GLOBS):
                found.append(display_path(Path(dirpath) / fn))
        if len(found) >= 20:
            break
    return sorted(found)


def _log_file(path: str) -> Path:
    target = resolve_in_roots(path)
    if not target.exists():
        raise ToolError(f"File does not exist: {display_path(target)}")
    if target.is_dir():
        found = _suggest_logs(target)
        hint = f" Log-like files found: {', '.join(found)}" if found else ""
        raise ToolError(f"{display_path(target)} is a directory; pass a log file.{hint}")
    with target.open("rb") as fh:
        if is_binary(fh.read(8192)):
            raise ToolError(f"{display_path(target)} looks binary; refusing to read it.")
    return target


def _tail_lines(path: Path, n: int) -> tuple[list[str], int | None]:
    """Last *n* lines without reading the whole file. Returns (lines, total_lines or None if unknown)."""
    size = path.stat().st_size
    if size <= 8 * 1024 * 1024:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:], len(lines)
    # Big file: read blocks from the end until we have enough newlines.
    block = 64 * 1024
    data = b""
    with path.open("rb") as fh:
        pos = size
        while pos > 0 and data.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n:], None


def _all_lines(path: Path, max_lines: int) -> tuple[list[str], int | None, int]:
    """Up to the last *max_lines* lines. Returns (lines, total_or_None, first_line_number)."""
    lines, total = _tail_lines(path, max_lines)
    first_no = (total - len(lines) + 1) if total is not None else 1
    return lines, total, first_no


# --------------------------------------------------------------------------- read_log


@mcp.tool(annotations=READ_ONLY)
def read_log(path: str, tail: int = 200) -> str:
    """Return the last *tail* lines of a log file (default 200), line-numbered and redacted.

    Use after summarize_errors/search_logs when you want the raw recent output. For older
    regions use the filesystem read_file tool with a line range.
    """
    if tail < 1 or tail > MAX_TAIL:
        raise ToolError(f"tail must be between 1 and {MAX_TAIL}.")
    target = _log_file(path)
    lines, total = _tail_lines(target, tail)
    if not lines:
        return f"# {display_path(target)}\n[empty file]"
    first_no = (total - len(lines) + 1) if total is not None else None
    if first_no is not None:
        width = len(str(total))
        numbered = "\n".join(f"{first_no + i:>{width}} | {_clip(ln)}" for i, ln in enumerate(lines))
        header = f"# {display_path(target)}  (lines {first_no}-{total} of {total})"
    else:
        numbered = "\n".join(_clip(ln) for ln in lines)
        header = f"# {display_path(target)}  (last {len(lines)} lines; file too large to count)"
    body, note = truncate(redact(numbered), hint="Reduce tail, or use search_logs / summarize_errors.")
    return header + "\n" + body + (f"\n{note}" if note else "")


# --------------------------------------------------------------------------- search_logs


@dataclass
class LogHit:
    line: int
    text: str
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


@dataclass
class LogSearchResult:
    path: str
    pattern: str
    lines_scanned: int
    hits: list[LogHit] = field(default_factory=list)
    total_matches: int = 0
    truncated: bool = False
    note: str | None = None


@mcp.tool(annotations=READ_ONLY)
def search_logs(
    path: str,
    pattern: str,
    context: int = 2,
    max_results: int = 30,
    ignore_case: bool = True,
    max_lines: int = 50_000,
) -> LogSearchResult:
    """Regex search within one log file, returning each hit with *context* lines before/after.

    Scans the last *max_lines* lines. Case-insensitive by default. total_matches counts every
    match even when only the first max_results are returned (a good "how often" signal).
    """
    if not pattern:
        raise ToolError("pattern must not be empty.")
    if context < 0 or context > 20:
        raise ToolError("context must be between 0 and 20.")
    if max_results < 1 or max_results > 500:
        raise ToolError("max_results must be between 1 and 500.")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression {pattern!r}: {exc}") from exc

    target = _log_file(path)
    lines, _total, first_no = _all_lines(target, min(max_lines, MAX_ANALYZE_LINES))
    result = LogSearchResult(path=display_path(target), pattern=pattern, lines_scanned=len(lines))

    for i, line in enumerate(lines):
        if not regex.search(line):
            continue
        result.total_matches += 1
        if len(result.hits) >= max_results:
            result.truncated = True
            continue
        result.hits.append(
            LogHit(
                line=first_no + i,
                text=redact(_clip(line)),
                before=[redact(_clip(ln)) for ln in lines[max(0, i - context) : i]],
                after=[redact(_clip(ln)) for ln in lines[i + 1 : i + 1 + context]],
            )
        )
    if result.truncated:
        result.note = f"{result.total_matches} matches; showing first {max_results}. Narrow the pattern or raise max_results."
    elif not result.hits:
        result.note = f"No matches in the last {len(lines)} lines."
    return result


# --------------------------------------------------------------------------- summarize_errors


@dataclass
class _Event:
    level: str
    message: str
    ts: str | None
    start: int
    lines: list[str]
    location: str | None = None


@dataclass
class ErrorGroup:
    level: str
    count: int
    message: str
    location: str | None
    first_seen: str | None
    last_seen: str | None
    first_line: int
    last_line: int
    sample: str


@dataclass
class ErrorSummary:
    path: str
    lines_analyzed: int
    level_counts: dict[str, int]
    time_range: str | None
    groups: list[ErrorGroup] = field(default_factory=list)
    note: str | None = None


def _location_of(lines: list[str]) -> str | None:
    """Innermost frame of a Python traceback, or first frame of a JS/Java-style trace."""
    py = [m for ln in lines for m in [_PY_FRAME_RE.search(ln)] if m]
    if py:
        m = py[-1]
        loc = f"{m.group('file')}:{m.group('line')}"
        return f"{loc} in {m.group('fn')}" if m.group("fn") else loc
    for ln in lines:
        m = _JS_FRAME_RE.match(_split_ts(ln)[1])
        if m:
            return f"{m.group('file')}:{m.group('line')}"
    return None


def _extract_events(lines: list[str]) -> tuple[list[_Event], Counter[str], str | None, str | None]:
    events: list[_Event] = []
    level_counts: Counter[str] = Counter()
    first_ts = last_ts = None
    last_seen_ts: str | None = None
    n = len(lines)
    i = 0

    def consume_block(start: int) -> int:
        """From a Traceback line, consume through the final exception line (and chained ones)."""
        j = start + 1
        while j < n:
            _, rest = _split_ts(lines[j])
            if _is_continuation(rest) or not rest.strip() or rest.startswith(_TRACEBACK_START):
                j += 1
                continue
            j += 1  # the exception line itself
            # chained traceback?
            k = j
            while k < n and not _split_ts(lines[k])[1].strip():
                k += 1
            if k < n and _split_ts(lines[k])[1].startswith(_CHAIN_MARKERS):
                k += 1
                while k < n and not _split_ts(lines[k])[1].strip():
                    k += 1
                if k < n and _split_ts(lines[k])[1].startswith(_TRACEBACK_START):
                    j = k
                    continue
            break
        return j

    while i < n:
        ts, rest = _split_ts(lines[i])
        if ts:
            last_seen_ts = ts
            first_ts = first_ts or ts
            last_ts = ts
        lvl = _level_of(rest)
        if lvl:
            level_counts[lvl] += 1

        if rest.startswith(_TRACEBACK_START):
            end = consume_block(i)
            block = lines[i:end]
            exc_line = _split_ts(block[-1])[1].strip() if len(block) > 1 else _TRACEBACK_START
            prev = events[-1] if events else None
            if prev is not None and prev.start + len(prev.lines) == i and prev.level in _ERROR_LEVELS:
                prev.lines.extend(block)
                prev.message = f"{prev.message} -> {exc_line}"
                prev.location = _location_of(block)
            else:
                events.append(_Event("ERROR", exc_line, ts or last_seen_ts, i, block, _location_of(block)))
            i = end
            continue

        stripped = rest.strip()
        if lvl in _SEVERITY or (lvl is None and _EXC_LINE_RE.match(stripped)):
            ev = _Event(lvl or "ERROR", stripped, ts or last_seen_ts, i, [lines[i]])
            i += 1
            while i < n:
                nts, nrest = _split_ts(lines[i])
                if nrest.startswith(_TRACEBACK_START) or not _is_continuation(nrest):
                    break
                ev.lines.append(lines[i])
                if nts:
                    last_seen_ts = nts
                    last_ts = nts
                i += 1
            ev.location = _location_of(ev.lines[1:]) if len(ev.lines) > 1 else None
            events.append(ev)
            continue
        i += 1
    return events, level_counts, first_ts, last_ts


@mcp.tool(annotations=READ_ONLY)
def summarize_errors(path: str, max_groups: int = 10, max_lines: int = 50_000) -> ErrorSummary:
    """Group repeated errors, tracebacks and warnings in a log file: what, how many times, where in
    the code, and when it first/last happened.

    Call this first on any log: it turns thousands of lines into a ranked list of distinct problems.
    Then use search_logs/read_log to look at specific occurrences.
    """
    if max_groups < 1 or max_groups > 100:
        raise ToolError("max_groups must be between 1 and 100.")
    target = _log_file(path)
    lines, _total, first_no = _all_lines(target, min(max_lines, MAX_ANALYZE_LINES))
    events, level_counts, first_ts, last_ts = _extract_events(lines)

    buckets: dict[tuple[str, str, str | None], list[_Event]] = {}
    for ev in events:
        key = (ev.level, _normalise(ev.message), ev.location)
        buckets.setdefault(key, []).append(ev)

    groups: list[ErrorGroup] = []
    for (level, _norm, location), evs in buckets.items():
        first, last = evs[0], evs[-1]
        sample_lines = first.lines[:SAMPLE_LINES]
        sample = "\n".join(_clip(_split_ts(ln)[1].rstrip()) for ln in sample_lines)
        if len(first.lines) > SAMPLE_LINES:
            sample += f"\n… ({len(first.lines) - SAMPLE_LINES} more lines)"
        groups.append(
            ErrorGroup(
                level=level,
                count=len(evs),
                message=redact(_clip(first.message)),
                location=location,
                first_seen=first.ts,
                last_seen=last.ts,
                first_line=first_no + first.start,
                last_line=first_no + last.start,
                sample=redact(sample),
            )
        )
    groups.sort(key=lambda g: (_SEVERITY.get(g.level, 9), -g.count, g.first_line))

    summary = ErrorSummary(
        path=display_path(target),
        lines_analyzed=len(lines),
        level_counts=dict(sorted(level_counts.items(), key=lambda kv: _SEVERITY.get(kv[0], 9))),
        time_range=f"{first_ts} .. {last_ts}" if first_ts else None,
        groups=groups[:max_groups],
    )
    if not groups:
        summary.note = f"No error, warning or traceback lines found in the last {len(lines)} lines."
    elif len(groups) > max_groups:
        summary.note = f"{len(groups)} distinct problems; showing the top {max_groups}. Raise max_groups to see more."
    return summary


if __name__ == "__main__":
    mcp.run()

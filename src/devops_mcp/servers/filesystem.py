"""Filesystem MCP server: list, read and search project files inside the allowed roots.

Run:  python -m devops_mcp.servers.filesystem
Dev:  mcp dev src/devops_mcp/servers/filesystem.py
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pathspec
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from devops_mcp.config import SKIP_DIRS, get_settings
from devops_mcp.safety import display_path, is_binary, redact, resolve_in_roots, truncate

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

MAX_TREE_ENTRIES = 300
MAX_READ_BYTES = 5 * 1024 * 1024  # refuse whole-file reads above this without a line range
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_LINE_CHARS = 300
BINARY_SNIFF_BYTES = 8192

mcp = MCPServer(
    "devops-filesystem",
    instructions=(
        "Read-only access to project files inside the allowed roots. Paths are relative to the "
        "first root. Start with list_files for orientation, search_files to locate symbols or "
        "error strings, then read_file with a line range to inspect specifics. Results are "
        "size-capped and secrets are redacted."
    ),
)


# --------------------------------------------------------------------------- helpers


def _load_gitignore(root: Path) -> pathspec.PathSpec | None:
    gi = root / ".gitignore"
    if not gi.is_file():
        return None
    try:
        lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _ignored(spec: pathspec.PathSpec | None, rel_posix: str, is_dir: bool) -> bool:
    if spec is None:
        return False
    return spec.match_file(rel_posix + "/" if is_dir else rel_posix)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _sniff_text(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(BINARY_SNIFF_BYTES)


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


# --------------------------------------------------------------------------- tools


@mcp.tool(annotations=READ_ONLY)
def list_files(path: str = ".", depth: int = 2, include_hidden: bool = False) -> str:
    """List the directory tree under *path* (relative to the project root).

    Shows file sizes. Skips VCS/dependency/cache directories and anything matched by the
    project's .gitignore. Use depth=1 for a quick top-level look, larger depth to drill in.
    """
    if depth < 1 or depth > 8:
        raise ToolError("depth must be between 1 and 8.")

    root = resolve_in_roots(path)
    if not root.exists():
        raise ToolError(f"Path does not exist: {display_path(root)}")
    if root.is_file():
        st = root.stat()
        return f"{display_path(root)}  ({_human_size(st.st_size)})  [file]"

    spec = _load_gitignore(root)
    lines: list[str] = [f"{display_path(root)}/"]
    count = 0
    truncated = False

    def walk(directory: Path, level: int) -> None:
        nonlocal count, truncated
        if truncated:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except (PermissionError, OSError) as exc:
            lines.append(f"{'  ' * level}[unreadable: {exc.strerror or exc}]")
            return

        skipped_here = 0
        for entry in entries:
            name = entry.name
            is_dir = entry.is_dir(follow_symlinks=False)
            if not include_hidden and name.startswith(".") and name != ".env":
                skipped_here += 1
                continue
            if is_dir and name in SKIP_DIRS:
                skipped_here += 1
                continue
            rel = Path(entry.path).relative_to(root).as_posix()
            if _ignored(spec, rel, is_dir):
                skipped_here += 1
                continue

            if count >= MAX_TREE_ENTRIES:
                truncated = True
                return
            count += 1
            indent = "  " * level
            if is_dir:
                lines.append(f"{indent}{name}/")
                if level < depth:
                    walk(Path(entry.path), level + 1)
                else:
                    try:
                        n = sum(1 for _ in os.scandir(entry.path))
                    except OSError:
                        n = 0
                    if n:
                        lines[-1] = f"{indent}{name}/  [{n} entries, increase depth to expand]"
            elif entry.is_symlink():
                lines.append(f"{indent}{name} -> (symlink)")
            else:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                    lines.append(f"{indent}{name}  ({_human_size(size)})")
                except OSError:
                    lines.append(f"{indent}{name}")
        if skipped_here:
            lines.append(f"{'  ' * level}[{skipped_here} hidden/ignored entries not shown]")

    walk(root, 1)
    if truncated:
        lines.append(f"[truncated at {MAX_TREE_ENTRIES} entries: list a subdirectory or reduce depth]")
    body, note = truncate("\n".join(lines), hint="List a subdirectory or reduce depth.")
    return body + (f"\n{note}" if note else "")


@mcp.tool(annotations=READ_ONLY)
def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    """Read a text file with line numbers. Lines are 1-indexed and *end_line* is inclusive.

    Large files are truncated; use start_line/end_line to page through. Secret-looking values
    (passwords, tokens, keys) are redacted, so config files are safe to read.
    """
    target = resolve_in_roots(path)
    if not target.exists():
        raise ToolError(f"File does not exist: {display_path(target)}")
    if target.is_dir():
        raise ToolError(f"{display_path(target)} is a directory; use list_files.")
    if start_line is not None and start_line < 1:
        raise ToolError("start_line must be >= 1.")
    if end_line is not None and start_line is not None and end_line < start_line:
        raise ToolError("end_line must be >= start_line.")

    size = target.stat().st_size
    if size > MAX_READ_BYTES and start_line is None:
        raise ToolError(
            f"{display_path(target)} is {_human_size(size)}; too large to read whole. "
            "Pass start_line/end_line, or use search_files to find the relevant region."
        )
    if is_binary(_sniff_text(target)):
        raise ToolError(f"{display_path(target)} looks binary ({_human_size(size)}); refusing to read it.")

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"Cannot read {display_path(target)}: {exc}") from exc

    all_lines = text.splitlines()
    total = len(all_lines)
    first = start_line or 1
    last = min(end_line or total, total)
    if first > total and total > 0:
        raise ToolError(f"start_line {first} is past the end of the file ({total} lines).")

    selected = all_lines[first - 1 : last]
    width = len(str(last)) if last else 1
    numbered = "\n".join(f"{first + i:>{width}} | {_clip(line)}" for i, line in enumerate(selected))
    numbered = redact(numbered)

    body, note = truncate(numbered, hint="Pass start_line/end_line to read a narrower range.")
    shown_last = first + body.count("\n") if body else first
    header = f"# {display_path(target)}  (lines {first}-{min(shown_last, last)} of {total}, {_human_size(size)})"
    if total == 0:
        return header + "\n[empty file]"
    return header + "\n" + body + (f"\n{note}" if note else "")


@dataclass
class SearchHit:
    path: str
    line: int
    text: str


@dataclass
class SearchResult:
    pattern: str
    root: str
    hits: list[SearchHit] = field(default_factory=list)
    files_scanned: int = 0
    files_matched: int = 0
    truncated: bool = False
    note: str | None = None


@mcp.tool(annotations=READ_ONLY)
def search_files(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    max_results: int = 50,
    ignore_case: bool = False,
) -> SearchResult:
    """Search file contents for a regular expression, like grep -rn.

    *glob* filters by file name or relative path (e.g. "*.py", "src/**/*.ts", "docker-compose*").
    Good for locating an error string from a log, a config key, or where a function is defined.
    Returns path, 1-indexed line number and the matching line for each hit.
    """
    if not pattern:
        raise ToolError("pattern must not be empty.")
    if max_results < 1 or max_results > 500:
        raise ToolError("max_results must be between 1 and 500.")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression {pattern!r}: {exc}") from exc

    root = resolve_in_roots(path)
    if not root.exists():
        raise ToolError(f"Path does not exist: {display_path(root)}")

    spec = _load_gitignore(root) if root.is_dir() else None
    result = SearchResult(pattern=pattern, root=display_path(root))

    def matches_glob(rel_posix: str, name: str) -> bool:
        if not glob:
            return True
        return fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(rel_posix, glob)

    def candidate_files():
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dp = Path(dirpath)
            rel_dir = dp.relative_to(root).as_posix()
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in SKIP_DIRS
                and not d.startswith(".")
                and not _ignored(spec, f"{rel_dir}/{d}" if rel_dir != "." else d, True)
            )
            for fn in sorted(filenames):
                rel = f"{rel_dir}/{fn}" if rel_dir != "." else fn
                if _ignored(spec, rel, False) or not matches_glob(rel, fn):
                    continue
                yield dp / fn

    for file in candidate_files():
        if result.truncated:
            break
        try:
            if file.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            sample = _sniff_text(file)
        except OSError:
            continue
        if is_binary(sample):
            continue
        result.files_scanned += 1
        matched_this_file = False
        try:
            with file.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if regex.search(line):
                        if len(result.hits) >= max_results:
                            result.truncated = True
                            break
                        matched_this_file = True
                        result.hits.append(
                            SearchHit(path=display_path(file), line=lineno, text=redact(_clip(line.rstrip("\r\n"))))
                        )
        except OSError:
            continue
        if matched_this_file:
            result.files_matched += 1

    if result.truncated:
        result.note = (
            f"Stopped after {max_results} hits. Narrow the pattern, add a glob, search a subdirectory, "
            "or raise max_results."
        )
    elif not result.hits:
        result.note = f"No matches in {result.files_scanned} files scanned."
    return result


if __name__ == "__main__":
    mcp.run()

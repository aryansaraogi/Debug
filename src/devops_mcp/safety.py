"""The security boundary shared by every server.

Three guarantees:
  * resolve_in_roots  - no tool touches a path outside the configured roots (symlinks included).
  * redact            - secret-shaped values never reach the model, even from config files.
  * truncate          - no tool result exceeds the configured size, and the model is told how to narrow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from mcp.server.mcpserver.exceptions import ToolError

from .config import get_settings

REDACTED = "***REDACTED***"

# --------------------------------------------------------------------------- paths


def resolve_in_roots(path: str | Path, roots: Iterable[Path] | None = None) -> Path:
    """Resolve *path* and ensure it lies inside one of the allowed roots.

    Relative paths are resolved against the *first* root, not the process cwd - the server's
    cwd is whatever the MCP host happened to launch it from and is meaningless to the agent.
    """
    roots = tuple(roots) if roots is not None else get_settings().roots
    if not roots:
        raise ToolError("No allowed roots configured (set DEVOPS_MCP_ROOTS).")

    raw = Path(str(path)).expanduser()
    candidate = raw if raw.is_absolute() else roots[0] / raw
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:  # broken symlink loops etc.
        raise ToolError(f"Cannot resolve path {path!r}: {exc}") from exc

    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue

    allowed = ", ".join(str(r) for r in roots)
    raise ToolError(
        f"Access denied: {path!r} resolves to {resolved}, which is outside the allowed roots "
        f"({allowed}). Paths are relative to the first root."
    )


def display_path(resolved: Path, roots: Iterable[Path] | None = None) -> str:
    """Render a resolved path relative to its root for compact, stable output."""
    roots = tuple(roots) if roots is not None else get_settings().roots
    for root in roots:
        try:
            rel = resolved.relative_to(root)
            return rel.as_posix() or "."
        except ValueError:
            continue
    return resolved.as_posix()


# --------------------------------------------------------------------------- binary detection


def is_binary(sample: bytes) -> bool:
    """Heuristic: NUL byte in the first chunk, or a high ratio of non-text bytes."""
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f\x1b"
    high = sum(1 for b in sample if b >= 128)
    nontext = sum(1 for b in sample if b < 128 and b not in text_chars)
    # UTF-8 text has plenty of >=128 bytes; only count control garbage as evidence.
    return nontext / len(sample) > 0.10 and high / len(sample) < 0.5


# --------------------------------------------------------------------------- redaction

_SECRET_KEY_WORDS = (
    r"secret|token|password|passwd|pwd|api[_-]?key|apikey|private[_-]?key|"
    r"credential|access[_-]?key|auth|session[_-]?key|signing[_-]?key|client[_-]?secret"
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # KEY=value / KEY: value / "KEY": "value"   (env files, yaml, json, ini, toml)
    (
        re.compile(
            rf"""(?ix)
            (?P<prefix>
                (?:^|(?<=[\s,{{]))
                (?:export\s+)?
                (?P<q1>["']?)
                [A-Za-z0-9_.\-]*(?:{_SECRET_KEY_WORDS})[A-Za-z0-9_.\-]*
                (?P=q1)
                \s*[=:]\s*
            )
            (?P<q2>["']?)
            (?P<value>[^\s"',;}}]+)
            """,
            re.MULTILINE,
        ),
        rf"\g<prefix>\g<q2>{REDACTED}",
    ),
    # scheme://user:password@host
    (re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s]+)(@)", re.IGNORECASE), rf"\1{REDACTED}\3"),
    # PEM private keys
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        f"-----BEGIN PRIVATE KEY-----\n{REDACTED}\n-----END PRIVATE KEY-----",
    ),
    # Authorization headers
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]+=*"), rf"\1 {REDACTED}"),
    # Well-known token shapes
    (re.compile(r"\bsk-(?:[a-z]+-)?[A-Za-z0-9_\-]{20,}"), REDACTED),  # OpenAI / Anthropic style
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{10,}"), REDACTED),  # Stripe
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),  # GitHub
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),  # AWS access key id
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), REDACTED),  # Slack
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), REDACTED),  # JWT
]


def redact(text: str) -> str:
    """Mask secret-looking values. Errs on the side of over-redaction."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------- truncation


def truncate(
    text: str,
    max_lines: int | None = None,
    max_bytes: int | None = None,
    hint: str = "",
) -> tuple[str, str | None]:
    """Cut *text* to the configured budget.

    Returns (body, note). *note* is None when nothing was cut, otherwise a one-line
    explanation the caller should append so the model knows the result is partial.
    """
    settings = get_settings()
    max_lines = max_lines or settings.max_lines
    max_bytes = max_bytes or settings.max_bytes

    lines = text.splitlines()
    total_lines = len(lines)
    cut_reason = None

    if total_lines > max_lines:
        lines = lines[:max_lines]
        cut_reason = f"showing first {max_lines} of {total_lines} lines"

    body = "\n".join(lines)
    if len(body.encode("utf-8", errors="replace")) > max_bytes:
        encoded = body.encode("utf-8", errors="replace")[:max_bytes]
        body = encoded.decode("utf-8", errors="ignore")
        # Don't end on a partial line.
        body = body.rsplit("\n", 1)[0] if "\n" in body else body
        shown = body.count("\n") + 1
        cut_reason = f"showing first {shown} of {total_lines} lines ({max_bytes} byte limit)"

    if cut_reason is None:
        return body, None
    note = f"[truncated: {cut_reason}"
    if hint:
        note += f". {hint}"
    return body, note + "]"

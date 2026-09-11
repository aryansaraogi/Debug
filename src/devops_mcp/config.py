"""Runtime settings, read from environment variables on every call.

Environment variables:
    DEVOPS_MCP_ROOTS      os.pathsep-separated list of directories the tools may touch.
                          Defaults to CLAUDE_PROJECT_DIR, then the current working directory.
    DEVOPS_MCP_MAX_LINES  Max lines any single tool result may contain (default 400).
    DEVOPS_MCP_MAX_BYTES  Max bytes any single tool result may contain (default 65536).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_LINES = 400
DEFAULT_MAX_BYTES = 64 * 1024

# Directories that are never worth showing an agent when it asks for a project overview.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".next",
        ".cache",
        "target",
    }
)


@dataclass(frozen=True)
class Settings:
    roots: tuple[Path, ...]
    max_lines: int
    max_bytes: int


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _roots_from_env() -> tuple[Path, ...]:
    raw = os.environ.get("DEVOPS_MCP_ROOTS", "")
    parts = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
    if not parts:
        parts = [os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()]
    return tuple(Path(p).expanduser().resolve() for p in parts)


def get_settings() -> Settings:
    """Build settings from the environment. Cheap; called per tool invocation so tests can override env."""
    return Settings(
        roots=_roots_from_env(),
        max_lines=_int_env("DEVOPS_MCP_MAX_LINES", DEFAULT_MAX_LINES),
        max_bytes=_int_env("DEVOPS_MCP_MAX_BYTES", DEFAULT_MAX_BYTES),
    )

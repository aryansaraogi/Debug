"""Subprocess wrapper used by the git and docker servers.

Always argv lists (never shell=True), always a timeout, never raises on non-zero exit -
the caller decides whether a non-zero code is an error the model should see.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from mcp.server.mcpserver.exceptions import ToolError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def which(executable: str) -> str | None:
    return shutil.which(executable)


def run(argv: Sequence[str], cwd: Path | None = None, timeout: float = 30.0, env: dict[str, str] | None = None) -> CommandResult:
    """Run *argv* and capture output. Raises ToolError only for 'could not run at all' situations."""
    argv = tuple(str(a) for a in argv)
    if not argv:
        raise ToolError("Empty command.")
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Executable not found: {argv[0]!r}. Is it installed and on PATH?") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"Command timed out after {timeout:.0f}s: {' '.join(argv)}") from exc
    return CommandResult(argv=argv, returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")

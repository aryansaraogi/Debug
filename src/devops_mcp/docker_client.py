"""Shared docker CLI plumbing used by the read-only and the action servers.

Keeping this in one place means both servers report a missing CLI, a stopped daemon and an
unknown container name the same way - as a ToolError the model can act on, never a crash.

`run_docker` is the single seam every docker invocation goes through; tests monkeypatch it.
"""

from __future__ import annotations

import re

from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp.shell import CommandResult, run, which

DOCKER_TIMEOUT = 30.0
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

_DAEMON_DOWN_MARKERS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "docker daemon is not running",
    "the system cannot find the file specified",
    "open //./pipe/docker",
    "is the docker daemon running",
)


def docker_exe() -> str:
    exe = which("docker")
    if not exe:
        raise ToolError("Docker CLI not found on PATH. Is Docker Desktop installed?")
    return exe


def run_docker(*args: str, timeout: float = DOCKER_TIMEOUT, cwd=None) -> CommandResult:
    """Single seam for every docker invocation; tests monkeypatch this."""
    return run([docker_exe(), *args], timeout=timeout, cwd=cwd)


def daemon_down(stderr: str) -> bool:
    s = stderr.lower()
    return any(marker in s for marker in _DAEMON_DOWN_MARKERS)


def known_names() -> str:
    """Comma-separated container names, for 'did you mean' errors. Never raises."""
    try:
        result = run_docker("ps", "-a", "--format", "{{.Names}}")
    except ToolError:
        return ""
    return ", ".join(sorted(n for n in result.stdout.split() if n)) if result.ok else ""


def raise_for(result: CommandResult, subcommand: str) -> None:
    """Translate a failed docker invocation into an actionable ToolError."""
    err = (result.stderr or result.stdout).strip()
    first = err.splitlines()[0] if err else f"exit code {result.returncode}"
    if daemon_down(err):
        raise ToolError("Docker daemon is not reachable. Is Docker Desktop running?")
    if "no such container" in err.lower():
        raise ToolError(f"{first}. Known containers: {known_names() or 'none'}")
    raise ToolError(f"docker {subcommand} failed: {first}")


def docker(*args: str, timeout: float = DOCKER_TIMEOUT, cwd=None) -> str:
    result = run_docker(*args, timeout=timeout, cwd=cwd)
    if not result.ok:
        raise_for(result, args[0])
    return result.stdout


def safe_name(name: str) -> str:
    """Reject anything that isn't a plain container name/id (no flags, no shell metacharacters)."""
    name = name.strip()
    if not name or not _NAME_RE.match(name):
        raise ToolError(f"Invalid container name or id {name!r}.")
    return name


def container_state(name: str) -> str:
    """Current state word (running/exited/restarting/...), or 'absent' if it no longer exists."""
    result = run_docker("inspect", "--type", "container", "--format", "{{.State.Status}}", name)
    return result.stdout.strip() if result.ok else "absent"

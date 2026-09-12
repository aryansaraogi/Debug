"""Actions MCP server: the small set of *mutating* Docker operations, each gated by human approval.

Run:  python -m devops_mcp.servers.actions

This is deliberately a separate server from devops-docker. Registering the read-only servers
without this one means the agent physically cannot mutate anything - the tools aren't in its
tool list at all. Three layers guard what is exposed here:

  1. Opt-in by registration  - the server has to be added to .mcp.json to exist.
  2. Permission control      - DEVOPS_MCP_ALLOW_ACTIONS=0 disables every tool, and
                               DEVOPS_MCP_ACTION_CONTAINERS scopes them to matching names.
  3. Human approval per call - every tool carries `anthropic/requiresUserInteraction`, which
                               forces a prompt on EVERY call with no "don't ask again", even in
                               auto-accept modes. The agent cannot batch past it.

Every tool reports state before and after, so the agent can verify the action did what it wanted.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from devops_mcp import docker_client as dc
from devops_mcp.safety import display_path, redact, resolve_in_roots, truncate

# readOnlyHint=False + destructiveHint=True tells any MCP host this changes the world.
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
# Forces a permission prompt on every single call, with no "don't ask again" option.
NEEDS_APPROVAL = {"anthropic/requiresUserInteraction": True}

BUILD_TIMEOUT = 600.0
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

mcp = MCPServer(
    "devops-actions",
    instructions=(
        "Mutating Docker actions. Every call requires explicit human approval, so propose one "
        "action at a time and say why. Diagnose with the read-only servers FIRST - never restart "
        "a container before reading its logs, because restarting can destroy the evidence. After "
        "editing source, rebuild_service is required for the change to take effect in a container "
        "that has no source mount."
    ),
)


# --------------------------------------------------------------------------- permission control


def _actions_enabled() -> None:
    if os.environ.get("DEVOPS_MCP_ALLOW_ACTIONS", "1").strip().lower() in ("0", "false", "no", "off"):
        raise ToolError("Actions are disabled (DEVOPS_MCP_ALLOW_ACTIONS=0). Ask the operator to enable them.")


def _check_container(name: str) -> str:
    """Validate the name, then enforce the optional allowlist."""
    _actions_enabled()
    name = dc.safe_name(name)
    raw = os.environ.get("DEVOPS_MCP_ACTION_CONTAINERS", "").strip()
    if raw:
        patterns = [p.strip() for p in raw.split(",") if p.strip()]
        if not any(fnmatch.fnmatch(name, p) for p in patterns):
            raise ToolError(
                f"Container {name!r} is outside the permitted scope "
                f"(DEVOPS_MCP_ACTION_CONTAINERS={', '.join(patterns)})."
            )
    return name


# --------------------------------------------------------------------------- results


@dataclass
class ActionResult:
    action: str
    target: str
    state_before: str
    state_after: str
    succeeded: bool
    message: str
    next_step: str | None = None


def _act(action: str, name: str, argv: list[str], expect: str, next_step: str) -> ActionResult:
    before = dc.container_state(name)
    if before == "absent":
        raise ToolError(f"No such container: {name}. Known containers: {dc.known_names() or 'none'}")
    result = dc.run_docker(*argv, timeout=90.0)
    if not result.ok:
        dc.raise_for(result, argv[0])
    after = dc.container_state(name)
    ok = after == expect
    return ActionResult(
        action=action,
        target=name,
        state_before=before,
        state_after=after,
        succeeded=ok,
        message=(
            f"{name}: {before} -> {after}."
            if ok
            else f"{name}: {before} -> {after}, but {expect!r} was expected. It may be crash-looping."
        ),
        next_step=None if ok else next_step,
    )


# --------------------------------------------------------------------------- tools


@mcp.tool(annotations=DESTRUCTIVE, meta=NEEDS_APPROVAL)
def restart_container(name: str, timeout: int = 10) -> ActionResult:
    """Restart a container. REQUIRES HUMAN APPROVAL.

    Read its logs first: restarting rotates away the evidence of why it failed. Use this to apply
    a config or env change, or to recover a wedged process, not as a blind fix attempt.
    """
    if timeout < 0 or timeout > 300:
        raise ToolError("timeout must be between 0 and 300 seconds.")
    name = _check_container(name)
    return _act(
        "restart",
        name,
        ["restart", "--time", str(timeout), name],
        expect="running",
        next_step="Check get_container_logs for why it is not staying up.",
    )


@mcp.tool(annotations=DESTRUCTIVE, meta=NEEDS_APPROVAL)
def stop_container(name: str, timeout: int = 10) -> ActionResult:
    """Stop a running container (SIGTERM, then SIGKILL after *timeout*). REQUIRES HUMAN APPROVAL.

    The container is stopped, not removed, so start_container brings it back with its state intact.
    """
    if timeout < 0 or timeout > 300:
        raise ToolError("timeout must be between 0 and 300 seconds.")
    name = _check_container(name)
    return _act(
        "stop",
        name,
        ["stop", "--time", str(timeout), name],
        expect="exited",
        next_step="Inspect the container to see why it did not stop cleanly.",
    )


@mcp.tool(annotations=DESTRUCTIVE, meta=NEEDS_APPROVAL)
def start_container(name: str) -> ActionResult:
    """Start a stopped container. REQUIRES HUMAN APPROVAL."""
    name = _check_container(name)
    return _act(
        "start",
        name,
        ["start", name],
        expect="running",
        next_step="It exited immediately; read the logs for the startup failure.",
    )


@dataclass
class RebuildResult:
    action: str
    compose_dir: str
    services: list[str]
    succeeded: bool
    output: str
    containers: list[str] = field(default_factory=list)
    next_step: str | None = None


def _compose_dir(path: str) -> Path:
    directory = resolve_in_roots(path)
    if directory.is_file() and directory.name in COMPOSE_FILES:
        directory = directory.parent
    if not directory.is_dir():
        raise ToolError(f"{display_path(directory)} is not a directory.")
    if not any((directory / f).is_file() for f in COMPOSE_FILES):
        raise ToolError(f"No compose file in {display_path(directory)} (looked for {', '.join(COMPOSE_FILES)}).")
    return directory


@mcp.tool(annotations=DESTRUCTIVE, meta=NEEDS_APPROVAL)
def rebuild_service(compose_dir: str, service: str | None = None, no_cache: bool = False) -> RebuildResult:
    """Rebuild and recreate a Compose service via `docker compose up -d --build`. REQUIRES HUMAN APPROVAL.

    This is the step that makes a source edit take effect when the container has no bind mount;
    restarting alone keeps running the old image. *compose_dir* must be inside the allowed roots.
    Omit *service* to rebuild every service in the file. Can take several minutes.
    """
    _actions_enabled()
    directory = _compose_dir(compose_dir)
    argv = ["compose", "up", "-d", "--build"]
    if no_cache:
        argv.append("--no-cache")
    if service:
        argv.append(dc.safe_name(service))

    result = dc.run_docker(*argv, timeout=BUILD_TIMEOUT, cwd=directory)
    # Compose writes progress to stderr even on success, so merge both streams.
    combined = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    body, note = truncate(redact(combined), hint="Check the build output for the failing step.")
    output = body + (f"\n{note}" if note else "")

    if not result.ok:
        if dc.daemon_down(combined):
            raise ToolError("Docker daemon is not reachable. Is Docker Desktop running?")
        raise ToolError(f"docker compose up --build failed:\n{output}")

    ps = dc.run_docker("compose", "ps", "--format", "{{.Name}} {{.State}}", timeout=60.0, cwd=directory)
    containers = [ln.strip() for ln in ps.stdout.splitlines() if ln.strip()] if ps.ok else []
    unhealthy = [c for c in containers if not c.endswith(("running", "healthy"))]
    return RebuildResult(
        action="rebuild",
        compose_dir=display_path(directory),
        services=[service] if service else ["(all)"],
        succeeded=not unhealthy,
        output=output,
        containers=containers,
        next_step=(f"These are not running: {', '.join(unhealthy)}. Read their logs." if unhealthy else None),
    )


if __name__ == "__main__":
    mcp.run()

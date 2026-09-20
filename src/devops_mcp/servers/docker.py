"""Docker MCP server: inspect containers and the services they serve, via the docker CLI.

Run:  python -m devops_mcp.servers.docker
Dev:  mcp dev src/devops_mcp/servers/docker.py

Degrades gracefully: if the CLI is missing or the daemon is not running, every tool raises a
ToolError that says so, rather than crashing the server.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from devops_mcp import docker_client as dc
from devops_mcp.http_probe import HttpResult, probe
from devops_mcp.safety import redact, truncate

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

MAX_TAIL = 5000
_SINCE_RE = re.compile(r"^[A-Za-z0-9:.+\-TZ]+$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z?\s")

mcp = MCPServer(
    "devops-docker",
    instructions=(
        "Read-only Docker inspection through the local daemon. For 'why is my container "
        "crashing/restarting/unhealthy': list_containers to find it, inspect_container for exit "
        "code, OOM, restart count, health and config, then get_container_logs (small tail first). "
        "After a fix, probe_url checks whether the service actually answers again - always "
        "verify rather than assuming a rebuild worked. Env values and log secrets are redacted."
    ),
)


# --------------------------------------------------------------------------- list_containers


@dataclass
class ContainerSummary:
    name: str
    id: str
    image: str
    state: str  # running, exited, restarting, paused, created, dead
    status: str  # human string, e.g. "Exited (3) 2 minutes ago"
    ports: str
    created: str
    compose_project: str | None = None
    compose_service: str | None = None


@dataclass
class ContainerList:
    containers: list[ContainerSummary] = field(default_factory=list)
    note: str | None = None


def _compose_labels(labels: Any) -> tuple[str | None, str | None]:
    if isinstance(labels, str):
        pairs = dict(kv.split("=", 1) for kv in labels.split(",") if "=" in kv)
    elif isinstance(labels, dict):
        pairs = labels
    else:
        pairs = {}
    return pairs.get("com.docker.compose.project"), pairs.get("com.docker.compose.service")


def _parse_ps(raw: str) -> list[ContainerSummary]:
    out: list[ContainerSummary] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        project, service = _compose_labels(d.get("Labels"))
        out.append(
            ContainerSummary(
                name=d.get("Names", ""),
                id=(d.get("ID") or "")[:12],
                image=d.get("Image", ""),
                state=d.get("State", ""),
                status=d.get("Status", ""),
                ports=d.get("Ports", "") or "",
                created=d.get("CreatedAt", ""),
                compose_project=project,
                compose_service=service,
            )
        )
    # Problem containers first: anything not running is more interesting to a debugger.
    out.sort(key=lambda c: (c.state == "running", c.name))
    return out


@mcp.tool(annotations=READ_ONLY)
def list_containers(all: bool = True) -> ContainerList:
    """List containers with state, status (exit code, uptime), image and ports.

    all=True (default) includes stopped/exited containers, which is usually what you want when
    something is crashing. Non-running containers are listed first.
    """
    args = ["ps", "--no-trunc", "--format", "{{json .}}"]
    if all:
        args.insert(1, "-a")
    result = ContainerList(containers=_parse_ps(dc.docker(*args)))
    if not result.containers:
        result.note = "No containers found." + ("" if all else " Try all=True to include stopped ones.")
    return result


# --------------------------------------------------------------------------- inspect_container


@dataclass
class ContainerState:
    status: str
    running: bool
    exit_code: int
    oom_killed: bool
    error: str
    started_at: str
    finished_at: str
    health_status: str | None = None
    health_failing_streak: int | None = None
    health_log: list[str] = field(default_factory=list)


@dataclass
class ContainerDetails:
    name: str
    id: str
    image: str
    created: str
    state: ContainerState
    restart_count: int
    restart_policy: str
    command: str
    entrypoint: str | None
    working_dir: str
    user: str
    env: list[str]
    ports: list[str]
    mounts: list[str]
    networks: list[str]
    compose_project: str | None
    compose_service: str | None
    memory_limit: str | None
    diagnosis_hints: list[str] = field(default_factory=list)


def _fmt_ports(ports: Any) -> list[str]:
    out: list[str] = []
    for cport, bindings in (ports or {}).items():
        if not bindings:
            out.append(f"{cport} (not published)")
            continue
        for b in bindings:
            out.append(f"{b.get('HostIp', '')}:{b.get('HostPort', '')}->{cport}")
    return sorted(out)


def _fmt_mounts(mounts: Any) -> list[str]:
    out: list[str] = []
    for m in mounts or []:
        src = m.get("Source") or m.get("Name") or "?"
        mode = "rw" if m.get("RW", True) else "ro"
        out.append(f"{m.get('Type', '?')} {src} -> {m.get('Destination', '?')} ({mode})")
    return out


def _fmt_networks(networks: Any) -> list[str]:
    return [f"{name} {info.get('IPAddress') or '(no ip)'}" for name, info in (networks or {}).items()]


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _summarise_inspect(d: dict[str, Any]) -> ContainerDetails:
    st = d.get("State") or {}
    health = st.get("Health") or {}
    hc = d.get("HostConfig") or {}
    cfg = d.get("Config") or {}
    ns = d.get("NetworkSettings") or {}
    policy = hc.get("RestartPolicy") or {}
    policy_name = policy.get("Name") or "no"
    if policy.get("MaximumRetryCount"):
        policy_name += f" (max {policy['MaximumRetryCount']})"
    project, service = _compose_labels(cfg.get("Labels"))
    entrypoint = cfg.get("Entrypoint")
    memory = hc.get("Memory") or 0

    state = ContainerState(
        status=st.get("Status", "unknown"),
        running=bool(st.get("Running")),
        exit_code=int(st.get("ExitCode") or 0),
        oom_killed=bool(st.get("OOMKilled")),
        error=st.get("Error") or "",
        started_at=st.get("StartedAt") or "",
        finished_at=st.get("FinishedAt") or "",
        health_status=health.get("Status"),
        health_failing_streak=health.get("FailingStreak"),
        health_log=[redact((entry.get("Output") or "").strip())[:300] for entry in (health.get("Log") or [])[-3:]],
    )

    details = ContainerDetails(
        name=(d.get("Name") or "").lstrip("/"),
        id=(d.get("Id") or "")[:12],
        image=cfg.get("Image") or "",
        created=d.get("Created") or "",
        state=state,
        restart_count=int(d.get("RestartCount") or 0),
        restart_policy=policy_name,
        command=" ".join([d.get("Path") or "", *(d.get("Args") or [])]).strip(),
        entrypoint=" ".join(entrypoint) if isinstance(entrypoint, list) else entrypoint,
        working_dir=cfg.get("WorkingDir") or "",
        user=cfg.get("User") or "",
        env=[redact(e) for e in (cfg.get("Env") or [])],
        ports=_fmt_ports(ns.get("Ports")),
        mounts=_fmt_mounts(d.get("Mounts")),
        networks=_fmt_networks(ns.get("Networks")),
        compose_project=project,
        compose_service=service,
        memory_limit=_human_bytes(memory) if memory else None,
    )

    hints = details.diagnosis_hints
    if state.oom_killed:
        hints.append("Container was OOM-killed: it exceeded its memory limit.")
    if not state.running and state.exit_code:
        hints.append(f"Exited with code {state.exit_code}; check get_container_logs for the failure.")
    if state.exit_code == 137 and not state.oom_killed:
        hints.append("Exit 137 = SIGKILL (often OOM or `docker kill`).")
    if state.exit_code == 139:
        hints.append("Exit 139 = segmentation fault.")
    if state.error:
        hints.append(f"Daemon-reported error: {state.error}")
    if details.restart_count >= 3:
        hints.append(f"Restarted {details.restart_count} times: likely a crash loop. Look at the first lines of the logs.")
    if state.health_status == "unhealthy":
        hints.append("Health check is failing; see health_log for the probe output.")
    if state.status == "restarting":
        hints.append("Currently restarting; the process is dying shortly after start.")
    return details


@mcp.tool(annotations=READ_ONLY)
def inspect_container(name: str) -> ContainerDetails:
    """Diagnostic summary of one container (by name or id): state, exit code, OOM flag, restart
    count and policy, health check results, command, env (redacted), ports, mounts, networks.

    The diagnosis_hints field flags the usual suspects (crash loop, OOM, unhealthy) so you know
    where to look next.
    """
    raw = dc.docker("inspect", "--type", "container", dc.safe_name(name))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Unexpected docker inspect output: {exc}") from exc
    if not data:
        raise ToolError(f"No such container: {name}. Known containers: {dc.known_names() or 'none'}")
    return _summarise_inspect(data[0])


# --------------------------------------------------------------------------- get_container_logs


def _merge_streams(stdout: str, stderr: str, timestamps: bool) -> str:
    out_lines = stdout.splitlines()
    err_lines = stderr.splitlines()
    if not err_lines:
        return "\n".join(out_lines)
    if not out_lines:
        return "\n".join(err_lines)
    if timestamps and all(_TS_RE.match(ln) for ln in out_lines[:5] + err_lines[:5]):
        # Both streams carry RFC3339 prefixes: interleave chronologically (stable for ties).
        merged = sorted(
            [(ln.split(" ", 1)[0], i, ln) for i, ln in enumerate(out_lines)]
            + [(ln.split(" ", 1)[0], i, ln) for i, ln in enumerate(err_lines)],
            key=lambda t: (t[0], t[1]),
        )
        return "\n".join(ln for _, _, ln in merged)
    return "\n".join(["--- stdout ---", *out_lines, "--- stderr ---", *err_lines])


@mcp.tool(annotations=READ_ONLY)
def get_container_logs(
    name: str,
    tail: int = 200,
    since: str | None = None,
    timestamps: bool = True,
    stderr_only: bool = False,
) -> str:
    """Fetch recent log lines from a container (stdout and stderr interleaved by timestamp).

    tail: number of most-recent lines (default 200). since: only lines after a duration ("10m",
    "2h") or RFC3339 time. stderr_only=True isolates the error stream. For a crash loop, a small
    tail shows the last failure; the first lines after a restart usually hold the root cause.
    """
    if tail < 1 or tail > MAX_TAIL:
        raise ToolError(f"tail must be between 1 and {MAX_TAIL}.")
    args = ["logs", f"--tail={tail}"]
    if since:
        since = since.strip()
        if not _SINCE_RE.match(since) or since.startswith("-"):
            raise ToolError(f"Invalid since value {since!r}; use a duration like 10m or an RFC3339 timestamp.")
        args.append(f"--since={since}")
    if timestamps:
        args.append("--timestamps")
    args.append(dc.safe_name(name))

    result = dc.run_docker(*args)
    if not result.ok:
        err = (result.stderr or "").strip()
        if dc.daemon_down(err):
            raise ToolError("Docker daemon is not reachable. Is Docker Desktop running?")
        if "no such container" in err.lower():
            raise ToolError(f"{err.splitlines()[0]}. Known containers: {dc.known_names() or 'none'}")
        raise ToolError(f"docker logs failed: {err.splitlines()[0] if err else result.returncode}")

    text = result.stderr if stderr_only else _merge_streams(result.stdout, result.stderr, timestamps)
    if not text.strip():
        return f"(no {'stderr ' if stderr_only else ''}log output for {name})"
    body, note = truncate(redact(text), hint="Reduce tail, pass since=, or use stderr_only=True.")
    return f"# logs: {name} (tail={tail}{', since=' + since if since else ''})\n" + body + (f"\n{note}" if note else "")


# --------------------------------------------------------------------------- probe_url


@mcp.tool(annotations=READ_ONLY)
def probe_url(url: str, method: str = "GET", timeout: float = 10.0) -> HttpResult:
    """Make one HTTP request to a local service and report status, timing, headers and body.

    Use this to confirm a service is actually up, and to verify a fix worked after rebuild_service
    rather than assuming it did. Returns a `hint` naming the next thing to check when the response
    is an error.

    Only GET and HEAD are allowed, and only hosts resolving to loopback or private addresses, so
    this cannot change server state or reach the public internet. Redirects are reported, not
    followed.
    """
    return probe(url, method=method, timeout=timeout)


if __name__ == "__main__":
    mcp.run()

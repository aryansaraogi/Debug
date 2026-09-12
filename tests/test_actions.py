from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp import docker_client as dc
from devops_mcp.servers import actions as ac
from devops_mcp.shell import CommandResult


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(argv=("docker",), returncode=0, stdout=stdout, stderr=stderr)


def fail(stderr: str, rc: int = 1) -> CommandResult:
    return CommandResult(argv=("docker",), returncode=rc, stdout="", stderr=stderr)


@pytest.fixture
def fake(monkeypatch):
    """Queue handlers per docker subcommand; `inspect` drives the before/after state readings."""
    calls: list[tuple] = []
    handlers: dict[str, object] = {}
    states: list[str] = []

    def _run(*args, timeout=30.0, cwd=None):
        calls.append((args, cwd))
        if args[0] == "inspect" and states:
            nxt = states.pop(0)
            return fail("No such container") if nxt == "absent" else ok(nxt + "\n")
        h = handlers.get(args[0])
        if h is None:
            raise AssertionError(f"unexpected docker call: {args}")
        return h(args) if callable(h) else h

    monkeypatch.setattr(dc, "run_docker", _run)
    monkeypatch.delenv("DEVOPS_MCP_ALLOW_ACTIONS", raising=False)
    monkeypatch.delenv("DEVOPS_MCP_ACTION_CONTAINERS", raising=False)
    handlers["calls"] = calls  # type: ignore[assignment]
    handlers["states"] = states  # type: ignore[assignment]
    return handlers


# --------------------------------------------------------------------------- approval metadata


@pytest.mark.parametrize("tool", ["restart_container", "stop_container", "start_container", "rebuild_service"])
def test_every_tool_is_destructive_and_needs_approval(tool):
    """The whole point of this server: no tool may be silently callable."""
    assert ac.DESTRUCTIVE.read_only_hint is False
    assert ac.DESTRUCTIVE.destructive_hint is True
    assert ac.NEEDS_APPROVAL == {"anthropic/requiresUserInteraction": True}
    assert callable(getattr(ac, tool))


@pytest.mark.anyio
async def test_approval_flag_reaches_the_wire():
    """Verify the host actually receives the flag, not just that we set a constant."""
    tools = await ac.mcp.list_tools()
    assert {t.name for t in tools} == {"restart_container", "stop_container", "start_container", "rebuild_service"}
    for t in tools:
        assert (t.meta or {}).get("anthropic/requiresUserInteraction") is True, t.name
        assert t.annotations and t.annotations.destructive_hint is True
        assert t.annotations.read_only_hint is False


# --------------------------------------------------------------------------- permission control


def test_kill_switch_blocks_every_tool(fake, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_ALLOW_ACTIONS", "0")
    for call in (
        lambda: ac.restart_container("x"),
        lambda: ac.stop_container("x"),
        lambda: ac.start_container("x"),
        lambda: ac.rebuild_service("."),
    ):
        with pytest.raises(ToolError, match="Actions are disabled"):
            call()
    assert fake["calls"] == []  # nothing reached docker


@pytest.mark.parametrize("value", ["0", "false", "no", "OFF"])
def test_kill_switch_accepts_common_spellings(fake, monkeypatch, value):
    monkeypatch.setenv("DEVOPS_MCP_ALLOW_ACTIONS", value)
    with pytest.raises(ToolError, match="Actions are disabled"):
        ac.restart_container("x")


def test_allowlist_scopes_containers(fake, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_ACTION_CONTAINERS", "broken_app-*,broken-backend")
    with pytest.raises(ToolError, match="outside the permitted scope"):
        ac.restart_container("prod-database")
    assert fake["calls"] == []
    # a permitted name gets through to docker
    fake["states"].extend(["running", "running"])
    fake["restart"] = ok()
    assert ac.restart_container("broken-backend").succeeded is True


def test_allowlist_empty_means_allow_all(fake, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_ACTION_CONTAINERS", "   ")
    fake["states"].extend(["running", "running"])
    fake["restart"] = ok()
    assert ac.restart_container("anything").succeeded is True


@pytest.mark.parametrize("bad", ["-f", "--format={{.Id}}", "a b", "", "x;rm -rf /"])
def test_injection_rejected(fake, bad):
    with pytest.raises(ToolError, match="Invalid container name"):
        ac.restart_container(bad)
    assert fake["calls"] == []


# --------------------------------------------------------------------------- restart / stop / start


def test_restart_reports_before_and_after(fake):
    fake["states"].extend(["exited", "running"])
    fake["restart"] = ok()
    res = ac.restart_container("broken-backend", timeout=5)
    assert (res.action, res.target) == ("restart", "broken-backend")
    assert (res.state_before, res.state_after) == ("exited", "running")
    assert res.succeeded is True and res.next_step is None
    assert "exited -> running" in res.message
    argv = [c[0] for c in fake["calls"] if c[0][0] == "restart"][0]
    assert argv == ("restart", "--time", "5", "broken-backend")


def test_restart_that_does_not_stay_up_flags_next_step(fake):
    fake["states"].extend(["running", "restarting"])
    fake["restart"] = ok()
    res = ac.restart_container("crashy")
    assert res.succeeded is False
    assert "crash-looping" in res.message
    assert res.next_step and "get_container_logs" in res.next_step


def test_stop_and_start(fake):
    fake["states"].extend(["running", "exited"])
    fake["stop"] = ok()
    res = ac.stop_container("c", timeout=3)
    assert (res.action, res.state_after, res.succeeded) == ("stop", "exited", True)
    assert ("stop", "--time", "3", "c") in [c[0] for c in fake["calls"]]

    fake["states"].extend(["exited", "running"])
    fake["start"] = ok()
    res = ac.start_container("c")
    assert (res.action, res.state_after, res.succeeded) == ("start", "running", True)


def test_absent_container_lists_known_names(fake):
    fake["states"].append("absent")
    fake["ps"] = ok("broken-backend\nother\n")
    with pytest.raises(ToolError, match="No such container: ghost. Known containers: broken-backend, other"):
        ac.restart_container("ghost")


def test_daemon_down_during_action(fake):
    fake["states"].append("running")
    fake["restart"] = fail("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
    with pytest.raises(ToolError, match="daemon is not reachable"):
        ac.restart_container("c")


def test_bad_timeout(fake):
    with pytest.raises(ToolError, match="timeout must be"):
        ac.restart_container("c", timeout=999)
    with pytest.raises(ToolError, match="timeout must be"):
        ac.stop_container("c", timeout=-1)


# --------------------------------------------------------------------------- rebuild_service


@pytest.fixture
def compose_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(tmp_path))
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "docker-compose.yml").write_text("services:\n  web:\n    image: alpine\n")
    (tmp_path / "notstack").mkdir()
    return stack


def test_rebuild_runs_in_compose_dir(fake, compose_root: Path):
    fake["compose"] = lambda args: ok("web Running\n") if args[1] == "ps" else ok(stderr="Container web Started\n")
    res = ac.rebuild_service("stack", service="web")
    assert res.action == "rebuild" and res.compose_dir == "stack" and res.services == ["web"]
    assert res.succeeded is True and res.next_step is None
    assert res.containers == ["web Running"]
    assert "Container web Started" in res.output
    up = [c for c in fake["calls"] if c[0][:2] == ("compose", "up")][0]
    assert up[0] == ("compose", "up", "-d", "--build", "web")
    assert up[1] == compose_root  # cwd


def test_rebuild_all_services_and_no_cache(fake, compose_root: Path):
    fake["compose"] = lambda args: ok("web Running\n") if args[1] == "ps" else ok("done")
    res = ac.rebuild_service("stack", no_cache=True)
    assert res.services == ["(all)"]
    up = [c[0] for c in fake["calls"] if c[0][:2] == ("compose", "up")][0]
    assert up == ("compose", "up", "-d", "--build", "--no-cache")


def test_rebuild_accepts_the_compose_file_itself(fake, compose_root: Path):
    fake["compose"] = lambda args: ok("web Running\n") if args[1] == "ps" else ok("ok")
    assert ac.rebuild_service("stack/docker-compose.yml").compose_dir == "stack"


def test_rebuild_flags_services_that_did_not_come_up(fake, compose_root: Path):
    fake["compose"] = lambda args: ok("web exited\ndb Running\n") if args[1] == "ps" else ok("ok")
    res = ac.rebuild_service("stack")
    assert res.succeeded is False
    assert res.next_step and "web exited" in res.next_step


def test_rebuild_build_failure_is_tool_error(fake, compose_root: Path):
    fake["compose"] = fail("failed to solve: process /bin/sh -c pip install returned a non-zero code: 1")
    with pytest.raises(ToolError, match="failed to solve"):
        ac.rebuild_service("stack")


def test_rebuild_rejects_paths_outside_roots(fake, compose_root: Path):
    with pytest.raises(ToolError, match="Access denied"):
        ac.rebuild_service("../elsewhere")
    assert fake["calls"] == []


def test_rebuild_requires_a_compose_file(fake, compose_root: Path):
    with pytest.raises(ToolError, match="No compose file"):
        ac.rebuild_service("notstack")


def test_rebuild_redacts_build_output(fake, compose_root: Path):
    fake["compose"] = lambda args: ok("web Running\n") if args[1] == "ps" else ok("ok", stderr="using DB_PASSWORD=hunter2 for build\n")
    res = ac.rebuild_service("stack")
    assert "hunter2" not in res.output


# --------------------------------------------------------------------------- integration (real daemon)


def _daemon_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker
@pytest.mark.skipif(not _daemon_available(), reason="Docker daemon not reachable")
def test_integration_stop_start_restart():
    name = f"devops-mcp-act-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "alpine", "sh", "-c", "while true; do sleep 1; done"],
        capture_output=True,
        timeout=120,
    )
    try:
        stopped = ac.stop_container(name, timeout=2)
        assert (stopped.state_before, stopped.state_after, stopped.succeeded) == ("running", "exited", True)

        started = ac.start_container(name)
        assert (started.state_before, started.state_after, started.succeeded) == ("exited", "running", True)

        restarted = ac.restart_container(name, timeout=2)
        assert restarted.state_after == "running" and restarted.succeeded is True
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)

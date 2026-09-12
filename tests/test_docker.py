from __future__ import annotations

import json
import subprocess
import uuid

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp.safety import REDACTED
from devops_mcp import docker_client as dc
from devops_mcp.servers import docker as dk
from devops_mcp.shell import CommandResult

# --------------------------------------------------------------------------- recorded output (Docker 29.7.2)

PS_RUNNING = {
    "Command": '"gunicorn -b 0.0.0.0:8000 app:create_app()"',
    "CreatedAt": "2026-09-11 18:49:33 +0530 IST",
    "ID": "aaaaaaaaaaaa1111111111111111111111111111111111111111111111111111",
    "Image": "broken_app-backend",
    "Labels": "com.docker.compose.project=broken_app,com.docker.compose.service=backend",
    "Names": "broken-backend",
    "Ports": "0.0.0.0:8000->8000/tcp",
    "State": "running",
    "Status": "Up 4 minutes",
}
PS_EXITED = {
    "Command": "\"sh -c 'echo boom >&2; exit 3'\"",
    "CreatedAt": "2026-09-11 18:49:33 +0530 IST",
    "ID": "686070a1b9d6b24e37a12fd0d75184c1ccccce170e3e9bc4f2ce86a8a4cc4a17",
    "Image": "alpine",
    "Labels": "com.docker.compose.project=probe,com.docker.compose.service=web",
    "Names": "devops-mcp-probe",
    "Ports": "",
    "State": "exited",
    "Status": "Exited (3) 2 minutes ago",
}

INSPECT_CRASHED = {
    "Id": "686070a1b9d6b24e37a12fd0d75184c1ccccce170e3e9bc4f2ce86a8a4cc4a17",
    "Name": "/devops-mcp-probe",
    "Created": "2026-09-11T13:19:33.9300843Z",
    "Path": "sh",
    "Args": ["-c", "echo boom >&2; exit 3"],
    "RestartCount": 5,
    "State": {
        "Status": "restarting",
        "Running": True,
        "Restarting": True,
        "OOMKilled": False,
        "ExitCode": 3,
        "Error": "",
        "StartedAt": "2026-09-11T13:19:34.1996033Z",
        "FinishedAt": "2026-09-11T13:19:34.5975443Z",
        "Health": {
            "Status": "unhealthy",
            "FailingStreak": 4,
            "Log": [
                {"ExitCode": 1, "Output": "curl: (7) Failed to connect"},
                {"ExitCode": 1, "Output": "curl: (7) Failed to connect again"},
                {"ExitCode": 1, "Output": "curl: (7) Failed to connect yet again"},
                {"ExitCode": 1, "Output": "curl: (7) Failed to connect final token=abc123secret"},
            ],
        },
    },
    "HostConfig": {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 10}, "Memory": 268435456},
    "Config": {
        "Image": "alpine",
        "Env": ["DB_PASSWORD=hunter2", "APP_ENV=dev", "PATH=/usr/bin:/bin"],
        "Cmd": ["sh", "-c", "echo boom >&2; exit 3"],
        "Entrypoint": None,
        "WorkingDir": "/srv",
        "User": "",
        "Labels": {"com.docker.compose.project": "probe", "com.docker.compose.service": "web"},
    },
    "NetworkSettings": {
        "Ports": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8000"}], "9/tcp": None},
        "Networks": {"bridge": {"IPAddress": "172.17.0.2"}},
    },
    "Mounts": [{"Type": "bind", "Source": "C:\\src\\app", "Destination": "/srv/app", "RW": True}],
}


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(argv=("docker",), returncode=0, stdout=stdout, stderr=stderr)


def fail(stderr: str, rc: int = 1) -> CommandResult:
    return CommandResult(argv=("docker",), returncode=rc, stdout="", stderr=stderr)


@pytest.fixture
def fake(monkeypatch):
    """Route docker_client.run_docker through a dict of handlers keyed by the subcommand."""
    calls: list[tuple[str, ...]] = []
    handlers: dict[str, object] = {}

    def _run(*args, timeout=30.0, cwd=None):
        calls.append(args)
        h = handlers.get(args[0])
        if h is None:
            raise AssertionError(f"unexpected docker call: {args}")
        return h(args) if callable(h) else h

    monkeypatch.setattr(dc, "run_docker", _run)
    handlers["calls"] = calls  # type: ignore[assignment]
    return handlers


# --------------------------------------------------------------------------- list_containers


def test_list_parses_and_sorts_problem_first(fake):
    fake["ps"] = ok(json.dumps(PS_RUNNING) + "\n" + json.dumps(PS_EXITED) + "\n")
    res = dk.list_containers()
    assert [c.name for c in res.containers] == ["devops-mcp-probe", "broken-backend"]
    probe = res.containers[0]
    assert probe.id == "686070a1b9d6"
    assert probe.state == "exited" and probe.status.startswith("Exited (3)")
    assert (probe.compose_project, probe.compose_service) == ("probe", "web")
    assert res.containers[1].ports == "0.0.0.0:8000->8000/tcp"
    assert res.note is None
    assert "-a" in fake["calls"][0]


def test_list_all_false_omits_flag_and_notes_empty(fake):
    fake["ps"] = ok("")
    res = dk.list_containers(all=False)
    assert res.containers == []
    assert res.note and "all=True" in res.note
    assert "-a" not in fake["calls"][0]


def test_daemon_down_is_tool_error(fake):
    fake["ps"] = fail("error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.")
    with pytest.raises(ToolError, match="daemon is not reachable"):
        dk.list_containers()


def test_cli_missing_is_tool_error(monkeypatch):
    monkeypatch.setattr(dc, "which", lambda _: None)
    with pytest.raises(ToolError, match="Docker CLI not found"):
        dk.list_containers()


# --------------------------------------------------------------------------- inspect_container


def test_inspect_summary_and_hints(fake):
    fake["inspect"] = ok(json.dumps([INSPECT_CRASHED]))
    d = dk.inspect_container("devops-mcp-probe")
    assert d.name == "devops-mcp-probe" and d.id == "686070a1b9d6"
    assert d.state.status == "restarting" and d.state.exit_code == 3
    assert d.restart_count == 5 and d.restart_policy == "on-failure (max 10)"
    assert d.command == "sh -c echo boom >&2; exit 3"
    assert d.ports == ["0.0.0.0:8000->8000/tcp", "9/tcp (not published)"]
    assert d.mounts == ["bind C:\\src\\app -> /srv/app (rw)"]
    assert d.networks == ["bridge 172.17.0.2"]
    assert d.memory_limit == "256.0 MB"
    assert (d.compose_project, d.compose_service) == ("probe", "web")
    assert d.state.health_status == "unhealthy" and len(d.state.health_log) == 3
    hints = " ".join(d.diagnosis_hints)
    assert "crash loop" in hints and "Health check is failing" in hints and "restarting" in hints


def test_inspect_redacts_env_and_health_output(fake):
    fake["inspect"] = ok(json.dumps([INSPECT_CRASHED]))
    d = dk.inspect_container("devops-mcp-probe")
    assert "hunter2" not in " ".join(d.env)
    assert any(e.startswith(f"DB_PASSWORD={REDACTED}") for e in d.env)
    assert "APP_ENV=dev" in d.env
    assert "abc123secret" not in " ".join(d.state.health_log)


def test_inspect_oom_hint(fake):
    data = json.loads(json.dumps(INSPECT_CRASHED))
    data["State"].update({"Status": "exited", "Running": False, "Restarting": False, "OOMKilled": True, "ExitCode": 137})
    data["RestartCount"] = 0
    data["State"].pop("Health")
    fake["inspect"] = ok(json.dumps([data]))
    d = dk.inspect_container("x")
    assert d.state.oom_killed is True
    assert any("OOM-killed" in h for h in d.diagnosis_hints)
    assert not any("crash loop" in h for h in d.diagnosis_hints)


def test_inspect_unknown_lists_known_names(fake):
    fake["inspect"] = fail("Error response from daemon: No such container: nope")
    fake["ps"] = ok("broken-backend\ndevops-mcp-probe\n")
    with pytest.raises(ToolError, match=r"No such container: nope\. Known containers: broken-backend, devops-mcp-probe"):
        dk.inspect_container("nope")


@pytest.mark.parametrize("bad", ["-p", "--format={{.Id}}", "a b", "", "x;rm"])
def test_inspect_rejects_bad_names(fake, bad):
    with pytest.raises(ToolError, match="Invalid container name"):
        dk.inspect_container(bad)
    assert fake["calls"] == []


# --------------------------------------------------------------------------- get_container_logs


def test_logs_interleave_by_timestamp_and_redact(fake):
    fake["logs"] = ok(
        stdout="2026-09-11T13:19:34.100Z starting\n2026-09-11T13:19:34.300Z DATABASE_URL=postgres://u:pw@db/app\n",
        stderr="2026-09-11T13:19:34.200Z boom\n",
    )
    out = dk.get_container_logs("devops-mcp-probe", tail=50)
    lines = out.splitlines()
    assert lines[0] == "# logs: devops-mcp-probe (tail=50)"
    assert [ln.split(" ", 1)[1] for ln in lines[1:]] == ["starting", "boom", f"DATABASE_URL=postgres://u:{REDACTED}@db/app"]
    assert "--tail=50" in fake["calls"][0] and "--timestamps" in fake["calls"][0]


def test_logs_without_timestamps_sections(fake):
    fake["logs"] = ok(stdout="a\n", stderr="b\n")
    out = dk.get_container_logs("c", timestamps=False)
    assert "--- stdout ---\na\n--- stderr ---\nb" in out
    assert "--timestamps" not in fake["calls"][0]


def test_logs_stderr_only_and_since(fake):
    fake["logs"] = ok(stdout="2026-09-11T13:19:34.100Z fine\n", stderr="2026-09-11T13:19:34.200Z boom\n")
    out = dk.get_container_logs("c", since="10m", stderr_only=True)
    assert "boom" in out and "fine" not in out
    assert "--since=10m" in fake["calls"][0]


def test_logs_empty(fake):
    fake["logs"] = ok()
    assert "(no log output for c)" in dk.get_container_logs("c")


def test_logs_truncates_with_hint(fake, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_MAX_LINES", "5")
    fake["logs"] = ok(stdout="\n".join(f"2026-09-11T13:19:{i:02d}Z line {i}" for i in range(30)) + "\n")
    out = dk.get_container_logs("c")
    assert "[truncated: showing first 5 of 30 lines" in out and "since=" in out


def test_logs_bad_args(fake):
    with pytest.raises(ToolError, match="tail must be"):
        dk.get_container_logs("c", tail=0)
    with pytest.raises(ToolError, match="Invalid since"):
        dk.get_container_logs("c", since="--follow")
    with pytest.raises(ToolError, match="Invalid since"):
        dk.get_container_logs("c", since="10m; rm")


def test_logs_unknown_container(fake):
    fake["logs"] = fail("Error response from daemon: No such container: ghost")
    fake["ps"] = ok("one\n")
    with pytest.raises(ToolError, match="No such container: ghost. Known containers: one"):
        dk.get_container_logs("ghost")


# --------------------------------------------------------------------------- integration (real daemon)


def _daemon_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker
@pytest.mark.skipif(not _daemon_available(), reason="Docker daemon not reachable")
def test_integration_real_container():
    name = f"devops-mcp-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "--name", name, "-e", "API_TOKEN=abc123456789", "alpine", "sh", "-c", "echo hello; echo boom >&2; exit 3"],
        capture_output=True,
        timeout=120,
    )
    try:
        listed = dk.list_containers()
        mine = next(c for c in listed.containers if c.name == name)
        assert mine.state == "exited" and "Exited (3)" in mine.status

        d = dk.inspect_container(name)
        assert d.state.exit_code == 3 and d.state.running is False
        assert any(e.startswith(f"API_TOKEN={REDACTED}") for e in d.env)
        assert any("Exited with code 3" in h for h in d.diagnosis_hints)

        logs = dk.get_container_logs(name, tail=10)
        assert "hello" in logs and "boom" in logs
        assert "hello" not in dk.get_container_logs(name, stderr_only=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)

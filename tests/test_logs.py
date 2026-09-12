from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp.safety import REDACTED
from devops_mcp.servers import logs as lg

LOG = "logs/app.log"


# --------------------------------------------------------------------------- read_log


def test_read_log_tail_numbered(broken_app: Path):
    out = lg.read_log(LOG, tail=3)
    lines = out.splitlines()
    assert lines[0].startswith("# logs/app.log  (lines 349-351 of 351)")
    assert lines[1].startswith("349 | ")
    assert "redis.exceptions.ConnectionError" in lines[-1]


def test_read_log_redacts(broken_app: Path):
    (broken_app / "logs" / "boot.log").write_text("connecting with DATABASE_URL=postgres://app:pw123@db/app\n")
    out = lg.read_log("logs/boot.log")
    assert "pw123" not in out and REDACTED in out


def test_read_log_bad_args(broken_app: Path):
    with pytest.raises(ToolError, match="tail must be"):
        lg.read_log(LOG, tail=0)
    with pytest.raises(ToolError, match="does not exist"):
        lg.read_log("logs/nope.log")
    with pytest.raises(ToolError, match="Access denied"):
        lg.read_log("../outside/secret.txt")


def test_read_log_directory_suggests_files(broken_app: Path):
    with pytest.raises(ToolError, match=r"is a directory.*logs/app\.log"):
        lg.read_log("logs")


def test_read_log_empty(broken_app: Path):
    (broken_app / "logs" / "empty.log").write_text("")
    assert "[empty file]" in lg.read_log("logs/empty.log")


def test_tail_lines_big_file(tmp_path: Path):
    big = tmp_path / "big.log"
    with big.open("w") as fh:
        for i in range(400_000):
            fh.write(f"line {i} {'x' * 20}\n")
    lines, total = lg._tail_lines(big, 3)
    assert lines == [f"line {i} {'x' * 20}" for i in (399_997, 399_998, 399_999)]
    assert total is None  # too big to count cheaply


# --------------------------------------------------------------------------- search_logs


def test_search_with_context(broken_app: Path):
    res = lg.search_logs(LOG, r"KeyError: 'email'", context=2, max_results=1)
    assert res.path == "logs/app.log"
    assert res.total_matches > 10
    assert res.truncated is True and res.note and "matches" in res.note
    hit = res.hits[0]
    assert hit.text == "KeyError: 'email'"
    assert len(hit.before) == 2 and 'email=user["email"]' in hit.before[-1]
    assert len(hit.after) == 2 and "500" in hit.after[0]


def test_search_case_and_no_match(broken_app: Path):
    assert lg.search_logs(LOG, "keyerror").total_matches > 0
    assert lg.search_logs(LOG, "keyerror", ignore_case=False).total_matches == 0
    res = lg.search_logs(LOG, "zzz-not-here")
    assert res.hits == [] and res.note and "No matches" in res.note


def test_search_bad_args(broken_app: Path):
    with pytest.raises(ToolError, match="Invalid regular expression"):
        lg.search_logs(LOG, "(")
    with pytest.raises(ToolError, match="context must"):
        lg.search_logs(LOG, "x", context=99)


def test_search_redacts(broken_app: Path):
    (broken_app / "logs" / "s.log").write_text("info\nauth token=abcdef0123456789\nafter\n")
    res = lg.search_logs("logs/s.log", "token", context=1)
    assert res.hits[0].text.endswith(REDACTED)
    assert res.hits[0].before == ["info"] and res.hits[0].after == ["after"]


# --------------------------------------------------------------------------- summarize_errors


def test_summarize_fixture_log(broken_app: Path):
    s = lg.summarize_errors(LOG)
    assert s.path == "logs/app.log" and s.lines_analyzed == 351
    assert s.level_counts["ERROR"] > 10 and s.level_counts["WARNING"] == 1 and s.level_counts["INFO"] > 30
    assert s.time_range and s.time_range.startswith("2026-09-10 14:02:11")

    top = s.groups[0]
    assert top.level == "ERROR"
    assert top.count == s.level_counts["ERROR"]  # every ERROR line is the same problem
    assert "[ERROR] Exception on /users/" in top.message and top.message.endswith("-> KeyError: 'email'")
    assert top.location == "/srv/app/routes.py:21 in user_detail"
    assert top.first_seen and top.last_seen and top.first_seen < top.last_seen
    assert "Traceback (most recent call last):" in top.sample and "KeyError: 'email'" in top.sample

    warn = next(g for g in s.groups if g.level == "WARNING")
    assert warn.count == 1 and "redis.exceptions.ConnectionError" in warn.message
    assert warn.last_line == 351
    assert len(s.groups) == 2


def test_summarize_groups_by_normalised_message(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(tmp_path))
    log = tmp_path / "svc.log"
    log.write_text(
        "2026-09-12T10:00:00Z ERROR request 1234 failed: timeout after 30s\n"
        "2026-09-12T10:00:05Z ERROR request 9876 failed: timeout after 31s\n"
        "2026-09-12T10:00:09Z ERROR request 5555 failed: timeout after 30s\n"
        "2026-09-12T10:01:00Z ERROR disk full on /var/lib/data\n"
        "2026-09-12T10:02:00Z WARNING slow query id=0xdeadbeef\n"
        "2026-09-12T10:02:01Z WARNING slow query id=0xcafebabe\n"
        "2026-09-12T10:03:00Z CRITICAL out of memory\n"
        "2026-09-12T10:03:01Z INFO all good\n"
    )
    s = lg.summarize_errors("svc.log")
    assert [(g.level, g.count) for g in s.groups] == [("CRITICAL", 1), ("ERROR", 3), ("ERROR", 1), ("WARNING", 2)]
    timeouts = s.groups[1]
    assert timeouts.message == "ERROR request 1234 failed: timeout after 30s"
    assert (timeouts.first_seen, timeouts.last_seen) == ("2026-09-12T10:00:00Z", "2026-09-12T10:00:09Z")
    assert (timeouts.first_line, timeouts.last_line) == (1, 3)
    assert s.level_counts == {"CRITICAL": 1, "ERROR": 4, "WARNING": 2, "INFO": 1}


def test_summarize_docker_style_traceback(tmp_path: Path, monkeypatch):
    """Every line carries a docker --timestamps prefix, including traceback frames."""
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(tmp_path))
    ts = "2026-09-11T13:27:28.30Z "
    log = tmp_path / "docker.log"
    log.write_text(
        ts + "[INFO] Booting worker\n"
        + ts + "[ERROR] Exception on /users/1 [GET]\n"
        + ts + "Traceback (most recent call last):\n"
        + ts + '  File "/srv/app/routes.py", line 21, in user_detail\n'
        + ts + '    email=user["email"],\n'
        + ts + "          ~~~~^^^^^^^^^\n"
        + ts + "KeyError: 'email'\n"
        + ts + '10.0.0.3 "GET /users/1 HTTP/1.1" 500 265\n'
        + ts + "Traceback (most recent call last):\n"
        + ts + '  File "/srv/app/x.py", line 3, in boom\n'
        + ts + "ValueError: bad\n"
    )
    s = lg.summarize_errors("docker.log")
    assert len(s.groups) == 2
    assert s.groups[0].message.endswith("-> KeyError: 'email'") and s.groups[0].location == "/srv/app/routes.py:21 in user_detail"
    assert s.groups[1].message == "ValueError: bad" and s.groups[1].location == "/srv/app/x.py:3 in boom"
    assert "~~~~^^^^^^^^^" in s.groups[0].sample


def test_summarize_chained_traceback_and_js_trace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(tmp_path))
    log = tmp_path / "mixed.log"
    log.write_text(
        "Traceback (most recent call last):\n"
        '  File "a.py", line 1, in f\n'
        "    inner()\n"
        "OSError: disk\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "b.py", line 9, in g\n'
        "    handle()\n"
        "RuntimeError: wrapped\n"
        "2026-09-12T10:00:00Z ERROR TypeError: Cannot read properties of undefined\n"
        "    at handler (/app/src/server.js:42:15)\n"
        "    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)\n"
        "2026-09-12T10:00:01Z INFO done\n"
    )
    s = lg.summarize_errors("mixed.log")
    py = next(g for g in s.groups if "RuntimeError" in g.message)
    assert py.message == "RuntimeError: wrapped" and py.location == "b.py:9 in g"
    assert "OSError: disk" in py.sample and "During handling" in py.sample
    js = next(g for g in s.groups if "TypeError" in g.message)
    assert js.location == "/app/src/server.js:42" and js.count == 1


def test_summarize_no_errors_and_limits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(tmp_path))
    (tmp_path / "quiet.log").write_text("2026-09-12T10:00:00Z INFO ok\nINFO still ok\n")
    s = lg.summarize_errors("quiet.log")
    assert s.groups == [] and s.note and "No error" in s.note
    (tmp_path / "many.log").write_text("".join(f"ERROR problem kind {chr(65 + i)}\n" for i in range(12)))
    s = lg.summarize_errors("many.log", max_groups=3)
    assert len(s.groups) == 3 and s.note and "12 distinct problems" in s.note
    with pytest.raises(ToolError, match="max_groups"):
        lg.summarize_errors("many.log", max_groups=0)


def test_summarize_redacts_messages(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(tmp_path))
    (tmp_path / "r.log").write_text("ERROR connect failed for postgres://app:s3cret@db/app\n")
    s = lg.summarize_errors("r.log")
    assert "s3cret" not in s.groups[0].message and REDACTED in s.groups[0].message
    assert "s3cret" not in s.groups[0].sample

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp.safety import REDACTED
from devops_mcp.servers import filesystem as fs


# --------------------------------------------------------------------------- list_files


def test_list_files_tree(broken_app: Path):
    out = fs.list_files(".", depth=2)
    assert out.startswith("./")
    assert "app/" in out
    assert "routes.py" in out
    assert "docker-compose.yml" in out
    assert ".env" in out  # .env is shown even though hidden: it's the #1 thing to inspect
    assert ".gitignore" not in out  # other dotfiles hidden by default


def test_list_files_include_hidden(broken_app: Path):
    assert ".gitignore" in fs.list_files(".", include_hidden=True)


def test_list_files_skips_junk_and_gitignore(broken_app: Path):
    (broken_app / "node_modules").mkdir()
    (broken_app / "node_modules" / "x.js").write_text("x")
    (broken_app / "app" / "__pycache__").mkdir()
    (broken_app / "app" / "cached.pyc").write_bytes(b"\x00")
    out = fs.list_files(".", depth=3)
    assert "node_modules" not in out
    assert "__pycache__" not in out
    assert "cached.pyc" not in out  # *.pyc in .gitignore


def test_list_files_depth_collapses(broken_app: Path):
    out = fs.list_files(".", depth=1)
    assert "app/  [" in out and "increase depth" in out
    assert "routes.py" not in out


def test_list_files_outside_root(broken_app: Path):
    with pytest.raises(ToolError, match="Access denied"):
        fs.list_files("..")


def test_list_files_missing(broken_app: Path):
    with pytest.raises(ToolError, match="does not exist"):
        fs.list_files("nope")


def test_list_files_bad_depth(broken_app: Path):
    with pytest.raises(ToolError):
        fs.list_files(".", depth=0)


# --------------------------------------------------------------------------- read_file


def test_read_file_numbered(broken_app: Path):
    out = fs.read_file("app/routes.py")
    assert out.startswith("# app/routes.py  (lines 1-")
    assert ' 1 | from flask import' in out
    assert 'email=user["email"]' in out


def test_read_file_range(broken_app: Path):
    out = fs.read_file("app/routes.py", start_line=19, end_line=21)
    lines = out.splitlines()
    assert "(lines 19-21 of" in lines[0]
    assert lines[1].startswith("19 | ")
    assert len(lines) == 4


def test_read_file_redacts_env(broken_app: Path):
    out = fs.read_file(".env")
    assert "supersecret-pg-pass" not in out
    assert "8f3a9c1e2b7d4f6a0c5e9b1d3f7a2c4e" not in out
    assert "4eC39HqLyjWDarjtT1zdp7dc" not in out
    assert "FLASK_ENV=development" in out
    assert REDACTED in out


def test_read_file_redacts_compose(broken_app: Path):
    out = fs.read_file("docker-compose.yml")
    assert "supersecret-pg-pass" not in out
    assert "POSTGRES_PASSWORD:" in out


def test_read_file_truncates_with_hint(broken_app: Path, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_MAX_LINES", "5")
    out = fs.read_file("logs/app.log")
    assert "[truncated: showing first 5 of" in out
    assert "start_line/end_line" in out
    assert out.count("\n") == 6  # header + 5 lines + note


def test_read_file_bad_ranges(broken_app: Path):
    with pytest.raises(ToolError):
        fs.read_file("app/routes.py", start_line=0)
    with pytest.raises(ToolError):
        fs.read_file("app/routes.py", start_line=5, end_line=2)
    with pytest.raises(ToolError, match="past the end"):
        fs.read_file("app/routes.py", start_line=9999)


def test_read_file_directory(broken_app: Path):
    with pytest.raises(ToolError, match="use list_files"):
        fs.read_file("app")


def test_read_file_binary(broken_app: Path):
    (broken_app / "blob.bin").write_bytes(bytes(range(256)) * 10)
    with pytest.raises(ToolError, match="binary"):
        fs.read_file("blob.bin")


def test_read_file_empty(broken_app: Path):
    (broken_app / "empty.txt").write_text("")
    assert "[empty file]" in fs.read_file("empty.txt")


def test_read_file_outside_root(broken_app: Path):
    with pytest.raises(ToolError, match="Access denied"):
        fs.read_file("../../etc/passwd")


# --------------------------------------------------------------------------- search_files


def test_search_finds_bug_line(broken_app: Path):
    res = fs.search_files(r'user\["email"\]', glob="*.py")
    assert res.files_matched == 1
    assert res.hits[0].path == "app/routes.py"
    assert res.hits[0].line == 21
    assert "BUG" in res.hits[0].text
    # Without the glob, the same string is also found in the log traceback and README
    assert fs.search_files(r'user\["email"\]').files_matched == 3


def test_search_glob_by_name(broken_app: Path):
    res = fs.search_files("email", glob="*.py")
    assert {h.path for h in res.hits} == {"app/routes.py", "app/repository.py"}
    res2 = fs.search_files("email", glob="*.log")
    assert res2.hits and all(h.path == "logs/app.log" for h in res2.hits)


def test_search_glob_by_path(broken_app: Path):
    res = fs.search_files("def ", glob="app/*.py")
    assert res.hits and all(h.path.startswith("app/") for h in res.hits)


def test_search_ignore_case(broken_app: Path):
    assert fs.search_files("KEYERROR").hits == []
    assert fs.search_files("KEYERROR", ignore_case=True).hits


def test_search_max_results_truncates(broken_app: Path):
    res = fs.search_files("KeyError", max_results=3)
    assert len(res.hits) == 3
    assert res.truncated is True
    assert res.note and "max_results" in res.note


def test_search_redacts_hits(broken_app: Path):
    res = fs.search_files("PASSWORD", ignore_case=True)
    assert res.hits
    assert all("supersecret" not in h.text for h in res.hits)


def test_search_no_match_note(broken_app: Path):
    res = fs.search_files("definitely-not-present-zzz")
    assert res.hits == []
    assert res.note and "No matches" in res.note
    assert res.files_scanned > 0


def test_search_bad_regex(broken_app: Path):
    with pytest.raises(ToolError, match="Invalid regular expression"):
        fs.search_files("(unclosed")


def test_search_skips_binary_and_junk(broken_app: Path):
    (broken_app / "node_modules").mkdir()
    (broken_app / "node_modules" / "x.js").write_text("KeyError")
    (broken_app / "blob.bin").write_bytes(b"KeyError\x00\x00")
    res = fs.search_files("KeyError")
    assert not any("node_modules" in h.path or h.path == "blob.bin" for h in res.hits)


def test_search_single_file(broken_app: Path):
    res = fs.search_files("KeyError", path="logs/app.log")
    assert res.hits and all(h.path == "logs/app.log" for h in res.hits)


def test_search_outside_root(broken_app: Path):
    with pytest.raises(ToolError, match="Access denied"):
        fs.search_files("x", path="../outside")

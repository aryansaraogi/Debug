from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp.safety import REDACTED
from devops_mcp.servers import git as g

GIT_ID = ["-c", "user.name=Test User", "-c", "user.email=test@example.com"]


def git(repo: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env_extra or {})}
    out = subprocess.run(["git", *GIT_ID, "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True)
    return out.stdout


@pytest.fixture
def repo(roots) -> Path:
    """A repo with 3 commits and a dirty tree, living inside the first allowed root."""
    root_a, _ = roots
    r = root_a / "svc"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")

    (r / "app.py").write_text("def handler(user):\n    return user['email']\n")
    (r / ".env").write_text("DB_PASSWORD=oldsecret\n")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "Initial service")

    (r / "repo.py").write_text("ROWS = {1: {'mail': 'a@b.c'}}\n")
    git(r, "add", "repo.py")
    git(r, "commit", "-q", "-m", "Rename users.email column to mail")

    (r / ".env").write_text("DB_PASSWORD=newsecret\nLOG_LEVEL=INFO\n")
    git(r, "add", ".env")
    git(r, "commit", "-q", "-m", "Rotate db password")

    # dirty tree: one staged, one unstaged, one untracked
    (r / "app.py").write_text("def handler(user):\n    return user['mail']\n")
    git(r, "add", "app.py")
    (r / "repo.py").write_text("ROWS = {1: {'mail': 'a@b.c'}, 2: {'mail': 'x@y.z'}}\n")
    (r / "notes.txt").write_text("todo\n")
    return r


# --------------------------------------------------------------------------- plumbing / safety


def test_non_repo_dir_is_tool_error(roots):
    root_a, _ = roots
    (root_a / "plain").mkdir()
    with pytest.raises(ToolError, match="not inside a git repository"):
        g.git_status("plain")


def test_outside_roots_rejected(repo):
    with pytest.raises(ToolError, match="Access denied"):
        g.git_status("../outside")


@pytest.mark.parametrize("bad", ["--output=x", "-p", "--exec=evil", "HEAD; rm -rf /", "a b"])
def test_option_injection_rejected(repo, bad):
    with pytest.raises(ToolError, match="Invalid ref"):
        g.git_diff("svc", ref=bad)
    with pytest.raises(ToolError, match="Invalid ref"):
        g.git_show("svc", ref=bad)


def test_path_injection_rejected(repo):
    with pytest.raises(ToolError, match="Invalid path"):
        g.git_diff("svc", path="--output=x")
    with pytest.raises(ToolError, match="Invalid path"):
        g.git_log("svc", path="-p")


def test_repo_accepts_file_inside_repo(repo):
    st = g.git_status("svc/app.py")
    assert st.repo == "svc"


def test_repo_toplevel_must_be_inside_roots(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q")
    (outer / "inner").mkdir()
    (outer / "inner" / "f").write_text("x")
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(outer / "inner"))
    with pytest.raises(ToolError, match="outside the allowed roots"):
        g.git_status(".")


# --------------------------------------------------------------------------- git_status


def test_status_structure(repo):
    st = g.git_status("svc")
    assert st.branch == "main"
    assert st.clean is False
    assert [c.path for c in st.staged] == ["app.py"]
    assert st.staged[0].change == "modified"
    assert [c.path for c in st.unstaged] == ["repo.py"]
    assert [c.path for c in st.untracked] == ["notes.txt"]
    assert st.conflicted == []
    assert st.ahead == 0 and st.behind == 0


def test_status_clean_repo(repo):
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "wip")
    st = g.git_status("svc")
    assert st.clean is True
    assert st.staged == st.unstaged == st.untracked == []


def test_parse_porcelain_rename_and_conflict():
    raw = (
        "# branch.oid abc\n# branch.head feature\n# branch.upstream origin/feature\n# branch.ab +2 -1\n"
        "2 R. N... 100644 100644 100644 abc def R100 new.py\told.py\n"
        "u UU N... 100644 100644 100644 100644 a b c conflict.txt\n"
        "? junk\n"
    )
    st = g._parse_porcelain_v2(raw, ".")
    assert (st.branch, st.upstream, st.ahead, st.behind) == ("feature", "origin/feature", 2, 1)
    assert st.staged[0].path == "new.py" and st.staged[0].original_path == "old.py" and st.staged[0].change == "renamed"
    assert st.conflicted[0].path == "conflict.txt"
    assert st.untracked[0].path == "junk"


# --------------------------------------------------------------------------- git_diff


def test_diff_unstaged_default(repo):
    out = g.git_diff("svc")
    assert "repo.py" in out
    assert "+ROWS" in out and "x@y.z" in out
    assert "app.py" not in out  # staged, so not in the default diff


def test_diff_staged(repo):
    out = g.git_diff("svc", staged=True)
    assert "app.py" in out and "-    return user['email']" in out and "+    return user['mail']" in out


def test_diff_ref_range_and_path(repo):
    out = g.git_diff("svc", ref="HEAD~2..HEAD~1")
    assert "repo.py" in out and "'mail'" in out
    out2 = g.git_diff("svc", ref="HEAD~2..HEAD", path="app.py")
    assert out2 == "(no changes)"  # app.py unchanged across those commits


def test_diff_redacts_secrets(repo):
    out = g.git_diff("svc", ref="HEAD~1..HEAD")
    assert "oldsecret" not in out and "newsecret" not in out
    assert REDACTED in out
    assert "+LOG_LEVEL=INFO" in out


def test_diff_stat_only(repo):
    out = g.git_diff("svc", stat_only=True)
    assert "repo.py" in out and "1 file changed" in out
    assert "+ROWS" not in out


def test_diff_falls_back_to_stat_when_huge(repo, monkeypatch):
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(2000)))
    git(repo, "add", "big.txt")
    git(repo, "commit", "-q", "-m", "big")
    (repo / "big.txt").write_text("\n".join(f"LINE {i}" for i in range(2000)))
    monkeypatch.setenv("DEVOPS_MCP_MAX_LINES", "50")
    out = g.git_diff("svc", path="big.txt")
    assert out.startswith("[diff too large")
    assert "big.txt" in out and "2000 insertions" in out
    assert "+LINE 5" not in out


def test_diff_no_changes(repo):
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "wip")
    assert g.git_diff("svc") == "(no changes)"


# --------------------------------------------------------------------------- git_log


def test_log_structure(repo):
    log = g.git_log("svc")
    assert log.repo == "svc" and log.ref == "HEAD"
    assert [c.subject for c in log.commits] == ["Rotate db password", "Rename users.email column to mail", "Initial service"]
    c = log.commits[1]
    assert len(c.sha) == 40 and c.short == c.sha[: len(c.short)]
    assert c.author == "Test User"
    assert c.date.startswith("20")
    assert c.stat and "1 file changed" in c.stat


def test_log_limit_and_note(repo):
    log = g.git_log("svc", limit=2)
    assert len(log.commits) == 2
    assert log.note and "raise limit" in log.note


def test_log_path_filter(repo):
    log = g.git_log("svc", path="repo.py")
    assert [c.subject for c in log.commits] == ["Rename users.email column to mail"]


def test_log_ref(repo):
    log = g.git_log("svc", ref="HEAD~1")
    assert log.commits[0].subject == "Rename users.email column to mail"


def test_log_bad_limit(repo):
    with pytest.raises(ToolError):
        g.git_log("svc", limit=0)


def test_log_unknown_ref(repo):
    with pytest.raises(ToolError, match="git log failed"):
        g.git_log("svc", ref="no-such-branch")


# --------------------------------------------------------------------------- git_show


def test_show_patch(repo):
    log = g.git_log("svc", path="repo.py")
    out = g.git_show("svc", ref=log.commits[0].sha)
    assert out.startswith("commit " + log.commits[0].sha)
    assert "author Test User <test@example.com>" in out
    assert "Rename users.email column to mail" in out
    assert "+ROWS = {1: {'mail': 'a@b.c'}}" in out


def test_show_stat_only_and_redaction(repo):
    out = g.git_show("svc", ref="HEAD", stat_only=True)
    assert ".env" in out and "1 file changed" in out
    assert "+DB_PASSWORD" not in out
    full = g.git_show("svc", ref="HEAD")
    assert "newsecret" not in full and REDACTED in full


def test_show_relative_ref(repo):
    assert "Initial service" in g.git_show("svc", ref="HEAD~2")

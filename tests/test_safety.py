from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp.safety import REDACTED, display_path, is_binary, redact, resolve_in_roots, truncate


# --------------------------------------------------------------------------- resolve_in_roots


def test_relative_path_resolves_against_first_root(roots):
    root_a, _ = roots
    (root_a / "a.txt").write_text("x")
    assert resolve_in_roots("a.txt") == (root_a / "a.txt").resolve()


def test_absolute_path_inside_second_root_allowed(roots):
    _, root_b = roots
    (root_b / "b.txt").write_text("x")
    assert resolve_in_roots(str(root_b / "b.txt")) == (root_b / "b.txt").resolve()


def test_dotdot_traversal_rejected(roots):
    with pytest.raises(ToolError, match="Access denied"):
        resolve_in_roots("../outside/secret.txt")


def test_deep_traversal_rejected(roots):
    with pytest.raises(ToolError, match="Access denied"):
        resolve_in_roots("sub/../../../../Windows/System32/drivers/etc/hosts")


def test_absolute_outside_rejected(roots):
    root_a, _ = roots
    outside = root_a.parent / "outside" / "secret.txt"
    with pytest.raises(ToolError, match="outside the allowed roots"):
        resolve_in_roots(str(outside))


def test_root_itself_allowed(roots):
    root_a, _ = roots
    assert resolve_in_roots(".") == root_a.resolve()
    assert resolve_in_roots(str(root_a)) == root_a.resolve()


def test_sibling_with_root_as_prefix_rejected(roots):
    # /tmp/project vs /tmp/project-evil must not pass a naive startswith check
    root_a, _ = roots
    evil = root_a.parent / (root_a.name + "-evil")
    evil.mkdir()
    (evil / "x").write_text("x")
    with pytest.raises(ToolError):
        resolve_in_roots(str(evil / "x"))


def test_symlink_escape_rejected_when_possible(roots):
    root_a, _ = roots
    link = root_a / "link"
    try:
        link.symlink_to(root_a.parent / "outside", target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(ToolError, match="Access denied"):
        resolve_in_roots("link/secret.txt")


def test_display_path_relative_to_root(roots):
    root_a, _ = roots
    p = (root_a / "src" / "x.py")
    assert display_path(p.resolve()) == "src/x.py"
    assert display_path(root_a.resolve()) == "."


# --------------------------------------------------------------------------- redact


@pytest.mark.parametrize(
    "line, must_hide",
    [
        ("DATABASE_URL=postgres://app:supersecret-pg-pass@db:5432/app", "supersecret-pg-pass"),
        ("SECRET_KEY=8f3a9c1e2b7d4f6a", "8f3a9c1e2b7d4f6a"),
        ("export API_KEY='abc-123'", "abc-123"),
        ('  "db_password": "hunter2",', "hunter2"),
        ("  POSTGRES_PASSWORD: supersecret", "supersecret"),
        ("STRIPE_API_KEY=sk_test_4eC39HqLyjWDarjtT1zdp7dc", "4eC39HqLyjWDarjtT1zdp7dc"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "eyJhbGci"),
        ("token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "ABCDEFGHIJKLMNOP"),
        ("aws_access_key_id = AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ],
)
def test_redact_hides_secret_values(line, must_hide):
    out = redact(line)
    assert must_hide not in out
    assert REDACTED in out


def test_redact_keeps_key_names_and_plain_values():
    text = "FLASK_ENV=development\nLOG_LEVEL=INFO\nDATABASE_URL=postgres://app:pw@db/app\n"
    out = redact(text)
    assert "FLASK_ENV=development" in out
    assert "LOG_LEVEL=INFO" in out
    assert "DATABASE_URL=postgres://app:" in out  # key and host survive, password does not
    assert "@db/app" in out
    assert ":pw@" not in out


def test_redact_pem_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nABCDEF\n-----END RSA PRIVATE KEY-----"
    out = redact(pem)
    assert "MIIEow" not in out
    assert "BEGIN PRIVATE KEY" in out


def test_redact_is_idempotent():
    once = redact("PASSWORD=abc\n")
    assert redact(once) == once


# --------------------------------------------------------------------------- truncate


def test_truncate_no_cut_returns_none_note(roots):
    body, note = truncate("a\nb\nc")
    assert body == "a\nb\nc"
    assert note is None


def test_truncate_by_lines(roots, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_MAX_LINES", "3")
    body, note = truncate("\n".join(str(i) for i in range(10)), hint="use tail")
    assert body == "0\n1\n2"
    assert note is not None
    assert "first 3 of 10 lines" in note
    assert "use tail" in note


def test_truncate_by_bytes(roots, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_MAX_BYTES", "20")
    body, note = truncate("\n".join("x" * 8 for _ in range(10)))
    assert len(body.encode()) <= 20
    assert note is not None and "byte limit" in note
    assert not body.endswith("xxxx\nx")  # no partial line


def test_truncate_explicit_overrides(roots):
    body, note = truncate("1\n2\n3\n4", max_lines=2)
    assert body == "1\n2"
    assert note


# --------------------------------------------------------------------------- is_binary


def test_is_binary():
    assert is_binary(b"\x00\x01\x02")
    assert not is_binary(b"hello world\n")
    assert not is_binary("héllo wörld ✓\n".encode("utf-8"))
    assert not is_binary(b"")
    assert is_binary(bytes(range(1, 32)) * 4)

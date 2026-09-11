from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Two allowed roots plus an 'outside' directory, with env pointing at the roots."""
    root_a = tmp_path / "project"
    root_b = tmp_path / "other"
    outside = tmp_path / "outside"
    for d in (root_a, root_b, outside):
        d.mkdir()
    (outside / "secret.txt").write_text("you should never see this\n")
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", f"{root_a}{__import__('os').pathsep}{root_b}")
    monkeypatch.delenv("DEVOPS_MCP_MAX_LINES", raising=False)
    monkeypatch.delenv("DEVOPS_MCP_MAX_BYTES", raising=False)
    return root_a, root_b


@pytest.fixture
def broken_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of fixtures/broken_app as the single allowed root."""
    dest = tmp_path / "broken_app"
    shutil.copytree(FIXTURES / "broken_app", dest)
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(dest))
    monkeypatch.delenv("DEVOPS_MCP_MAX_LINES", raising=False)
    monkeypatch.delenv("DEVOPS_MCP_MAX_BYTES", raising=False)
    return dest

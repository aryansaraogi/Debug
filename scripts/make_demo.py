"""Build demo/broken_app: a git repo whose history tells the story of the planted bug.

    python scripts/make_demo.py

Commits (oldest first):
  1. Initial backend service            - repository rows keyed `email`, app works
  2. Rename users.email column to mail  - touches only app/repository.py  <-- the culprit
  3. Tune gunicorn workers              - unrelated Dockerfile change, the most recent commit

logs/app.log is copied in but left untracked, like a real captured log.
The directory is gitignored so it never becomes a nested repo of this project.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SRC = PROJECT / "fixtures" / "broken_app"
DEST = PROJECT / "demo" / "broken_app"

IDENTITY = ["-c", "user.name=Dev Example", "-c", "user.email=dev@example.com", "-c", "core.autocrlf=false"]

REPOSITORY_BEFORE = '''"""Thin data-access layer. Column names come straight from the users table."""

from typing import Any

FAKE_ROWS = {
    1: {"id": 1, "name": "Ada", "email": "ada@example.com"},
    2: {"id": 2, "name": "Grace", "email": "grace@example.com"},
}


def get_user(user_id: int) -> dict[str, Any] | None:
    return FAKE_ROWS.get(user_id)
'''

ROUTES_BEFORE_MARKER = '        email=user["email"],  # BUG: column was renamed to `mail`\n'
ROUTES_BEFORE_CLEAN = '        email=user["email"],\n'


def git(*args: str, when: datetime | None = None) -> None:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if when is not None:
        stamp = when.isoformat()
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(["git", *IDENTITY, "-C", str(DEST), *args], check=True, env=env, stdout=subprocess.DEVNULL)


def main() -> None:
    if not SRC.is_dir():
        sys.exit(f"fixture not found: {SRC}")
    if DEST.exists():
        shutil.rmtree(DEST, onexc=_force_remove) if sys.version_info >= (3, 12) else shutil.rmtree(DEST)
    shutil.copytree(SRC, DEST, ignore=shutil.ignore_patterns("logs", "__pycache__"))

    t0 = datetime.now(timezone.utc) - timedelta(days=3)
    git("init", "-q", "-b", "main")

    # 1. working service
    (DEST / "app" / "repository.py").write_text(REPOSITORY_BEFORE, encoding="utf-8", newline="\n")
    routes = DEST / "app" / "routes.py"
    routes.write_text(routes.read_text(encoding="utf-8").replace(ROUTES_BEFORE_MARKER, ROUTES_BEFORE_CLEAN), encoding="utf-8", newline="\n")
    # .env must never be committed; keep it untracked like a real project
    (DEST / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\nnode_modules/\n.env\nlogs/\n", encoding="utf-8", newline="\n")
    git("add", ".")
    git("commit", "-q", "-m", "Initial backend service", when=t0)

    # 2. the culprit: rename the column in the data layer only
    shutil.copyfile(SRC / "app" / "repository.py", DEST / "app" / "repository.py")
    git("add", "app/repository.py")
    git(
        "commit",
        "-q",
        "-m",
        "Rename users.email column to mail (migration 0007)\n\n"
        "Aligns the repository layer with the new schema from migration 0007.",
        when=t0 + timedelta(days=1, hours=2),
    )

    # 3. an unrelated, most-recent commit so the culprit isn't simply HEAD
    dockerfile = DEST / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace('CMD ["gunicorn",', 'CMD ["gunicorn", "-w", "2",'),
        encoding="utf-8",
        newline="\n",
    )
    git("add", "Dockerfile")
    git("commit", "-q", "-m", "Tune gunicorn workers", when=t0 + timedelta(days=2, hours=5))

    # captured log, untracked
    shutil.copytree(SRC / "logs", DEST / "logs")

    log = subprocess.run(["git", "-C", str(DEST), "log", "--oneline"], capture_output=True, text=True, check=True).stdout
    print(f"demo repo ready at {DEST}\n{log}")


def _force_remove(func, path, exc):  # read-only files (.git objects) on Windows
    os.chmod(path, 0o666)
    func(path)


if __name__ == "__main__":
    main()

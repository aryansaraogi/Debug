"""Git MCP server: status, diff, log and show for repositories inside the allowed roots.

Run:  python -m devops_mcp.servers.git
Dev:  mcp dev src/devops_mcp/servers/git.py
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from devops_mcp.safety import display_path, redact, resolve_in_roots, truncate
from devops_mcp.shell import run, which

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

mcp = MCPServer(
    "devops-git",
    instructions=(
        "Read-only git inspection for repositories inside the allowed roots. Typical flow when "
        "something broke 'after recent changes': git_status for uncommitted work, git_log to see "
        "recent commits (optionally for one path), git_show on a suspicious commit, git_diff to "
        "compare refs or see working-tree edits. Output is redacted and size-capped."
    ),
)

_REF_RE = re.compile(r"^[\w./~^@{}:\-]+$")
_RS = "\x1e"  # record separator used to split git log output
_GIT_ENV_OVERRIDES = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}


# --------------------------------------------------------------------------- plumbing


def _git_exe() -> str:
    exe = which("git")
    if not exe:
        raise ToolError("git executable not found on PATH.")
    return exe


def _safe_ref(ref: str, what: str = "ref") -> str:
    """Refuse anything git might parse as an option, or that isn't ref-shaped."""
    ref = ref.strip()
    if not ref:
        raise ToolError(f"{what} must not be empty.")
    if ref.startswith("-"):
        raise ToolError(f"Invalid {what} {ref!r}: must not start with '-'.")
    if not _REF_RE.match(ref):
        raise ToolError(f"Invalid {what} {ref!r}: only branch/tag/sha/range characters are allowed.")
    return ref


def _safe_path(path: str) -> str:
    path = path.strip()
    if not path:
        raise ToolError("path must not be empty.")
    if path.startswith("-"):
        raise ToolError(f"Invalid path {path!r}: must not start with '-'.")
    return path


def _repo(path: str) -> Path:
    """Resolve *path* inside the roots and return the repository top-level (also inside the roots)."""
    where = resolve_in_roots(path)
    if not where.exists():
        raise ToolError(f"Path does not exist: {display_path(where)}")
    if where.is_file():
        where = where.parent
    result = run([_git_exe(), "-C", str(where), "rev-parse", "--show-toplevel"], env=_env())
    if not result.ok:
        raise ToolError(
            f"{display_path(where)} is not inside a git repository "
            f"({(result.stderr or result.stdout).strip() or 'git rev-parse failed'})."
        )
    top = Path(result.stdout.strip())
    # The work tree must itself be inside the roots, otherwise diffs could leak sibling files.
    return resolve_in_roots(top)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_GIT_ENV_OVERRIDES)
    return env


def _git(repo: Path, *args: str, timeout: float = 30.0) -> str:
    argv = [_git_exe(), "-c", "core.pager=cat", "-c", "color.ui=false", "-C", str(repo), *args]
    result = run(argv, env=_env(), timeout=timeout)
    if not result.ok:
        msg = (result.stderr or result.stdout).strip().splitlines()
        detail = msg[0] if msg else f"exit code {result.returncode}"
        raise ToolError(f"git {args[0]} failed: {detail}")
    return result.stdout


def _finish(text: str, hint: str) -> tuple[str, bool]:
    """Redact + truncate; returns (body_with_note, was_truncated)."""
    body, note = truncate(redact(text), hint=hint)
    return (body + (f"\n{note}" if note else "")), note is not None


# --------------------------------------------------------------------------- git_status


@dataclass
class FileChange:
    path: str
    change: str  # e.g. "modified", "added", "deleted", "renamed", "untracked", "conflict"
    original_path: str | None = None


@dataclass
class GitStatus:
    repo: str
    branch: str | None
    upstream: str | None
    ahead: int
    behind: int
    staged: list[FileChange] = field(default_factory=list)
    unstaged: list[FileChange] = field(default_factory=list)
    untracked: list[FileChange] = field(default_factory=list)
    conflicted: list[FileChange] = field(default_factory=list)
    clean: bool = True
    note: str | None = None


_XY_WORDS = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "T": "type-changed"}


def _parse_porcelain_v2(text: str, repo_display: str) -> GitStatus:
    st = GitStatus(repo=repo_display, branch=None, upstream=None, ahead=0, behind=0)
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("# branch.head "):
            head = line.split(" ", 2)[2]
            st.branch = None if head == "(detached)" else head
        elif line.startswith("# branch.upstream "):
            st.upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            _, _, ab = line.split(" ", 2)
            plus, minus = ab.split()
            st.ahead, st.behind = int(plus[1:]), int(minus[1:])
        elif line[0] in "12":
            parts = line.split(" ", 8 if line[0] == "1" else 9)
            xy = parts[1]
            if line[0] == "1":
                path, orig = parts[8], None
            else:
                path, _, orig = parts[9].partition("\t")
            x, y = xy[0], xy[1]
            if x != ".":
                st.staged.append(FileChange(path, _XY_WORDS.get(x, x), orig))
            if y != ".":
                st.unstaged.append(FileChange(path, _XY_WORDS.get(y, y), orig))
        elif line[0] == "u":
            path = line.split(" ", 10)[10]
            st.conflicted.append(FileChange(path, "conflict"))
        elif line[0] == "?":
            st.untracked.append(FileChange(line[2:], "untracked"))
    st.clean = not (st.staged or st.unstaged or st.untracked or st.conflicted)
    return st


@mcp.tool(annotations=READ_ONLY)
def git_status(repo: str = ".") -> GitStatus:
    """Show the working-tree state of a repository: branch, ahead/behind, staged, unstaged,
    untracked and conflicted files. Start here to see whether there is uncommitted work."""
    top = _repo(repo)
    raw = _git(top, "status", "--porcelain=v2", "--branch", "--untracked-files=all")
    status = _parse_porcelain_v2(raw, display_path(top))
    if status.branch is None:
        status.note = "HEAD is detached."
    cap = 200
    total = len(status.untracked)
    if total > cap:
        status.untracked = status.untracked[:cap]
        status.note = f"{total} untracked files; showing first {cap}."
    return status


# --------------------------------------------------------------------------- git_diff


@mcp.tool(annotations=READ_ONLY)
def git_diff(
    repo: str = ".",
    staged: bool = False,
    ref: str | None = None,
    path: str | None = None,
    stat_only: bool = False,
) -> str:
    """Show changes as a unified diff.

    Default: unstaged working-tree changes. staged=True: what is in the index. ref: compare the
    working tree against a commit ("HEAD~1"), or give a range ("main..feature", "abc123..HEAD").
    path narrows to one file or directory. If the diff is too large, a --stat summary is
    returned instead; call again with path= to see a specific file in full.
    """
    top = _repo(repo)
    args: list[str] = ["diff"]
    if staged:
        args.append("--cached")
    if ref:
        args.append(_safe_ref(ref))
    tail: list[str] = ["--", _safe_path(path)] if path else []

    if stat_only:
        out = _git(top, *args, "--stat", *tail)
        return out.strip() or "(no changes)"

    out = _git(top, *args, *tail)
    if not out.strip():
        return "(no changes)"
    body, was_truncated = _finish(out, hint="Pass path= to diff a single file.")
    if not was_truncated:
        return body
    stat = _git(top, *args, "--stat", *tail)
    return (
        "[diff too large to show in full; --stat summary instead. Pass path=<file> for one file's diff]\n"
        + stat.strip()
    )


# --------------------------------------------------------------------------- git_log


@dataclass
class Commit:
    sha: str
    short: str
    author: str
    date: str
    subject: str
    stat: str | None = None


@dataclass
class GitLog:
    repo: str
    ref: str
    path: str | None
    commits: list[Commit] = field(default_factory=list)
    note: str | None = None


def _parse_log(raw: str) -> list[Commit]:
    commits: list[Commit] = []
    for block in raw.split(_RS):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        header = lines[0]
        fields = header.split("\x00")
        if len(fields) < 5:
            continue
        sha, short, author, date, subject = fields[:5]
        stat = " ".join(ln.strip() for ln in lines[1:]) or None
        commits.append(Commit(sha=sha, short=short, author=author, date=date, subject=subject, stat=stat))
    return commits


@mcp.tool(annotations=READ_ONLY)
def git_log(repo: str = ".", limit: int = 15, ref: str | None = None, path: str | None = None) -> GitLog:
    """List recent commits (newest first) with author, date, subject and a files-changed summary.

    ref: start point or range ("main", "v1.2..HEAD"). path: only commits touching that file or
    directory, which is the fastest way to find who last changed something.
    """
    if limit < 1 or limit > 200:
        raise ToolError("limit must be between 1 and 200.")
    top = _repo(repo)
    args = ["log", f"--max-count={limit}", f"--format={_RS}%H%x00%h%x00%an%x00%aI%x00%s", "--shortstat"]
    if ref:
        args.append(_safe_ref(ref))
    if path:
        args += ["--", _safe_path(path)]
    raw = _git(top, *args)
    log = GitLog(repo=display_path(top), ref=ref or "HEAD", path=path, commits=_parse_log(raw))
    for c in log.commits:
        c.subject = redact(c.subject)
    if not log.commits:
        log.note = "No commits found for that ref/path."
    elif len(log.commits) == limit:
        log.note = f"Showing the {limit} most recent; raise limit or pass ref to go further back."
    return log


# --------------------------------------------------------------------------- git_show


@mcp.tool(annotations=READ_ONLY)
def git_show(repo: str = ".", ref: str = "HEAD", stat_only: bool = False) -> str:
    """Show one commit: metadata, message and the full patch (or just --stat with stat_only).

    Use after git_log to see exactly what a suspicious commit changed.
    """
    top = _repo(repo)
    safe = _safe_ref(ref)
    fmt = "--format=commit %H%nauthor %an <%ae>%ndate   %aI%n%n    %s%n%n%b"
    if stat_only:
        out = _git(top, "show", fmt, "--stat", safe)
        return redact(out.strip())
    out = _git(top, "show", fmt, "--patch", safe)
    body, was_truncated = _finish(out, hint="Use stat_only=True, then git_diff with path= for one file.")
    if not was_truncated:
        return body
    stat = _git(top, "show", fmt, "--stat", safe)
    return (
        redact(stat.strip())
        + f"\n[patch too large to show in full; use git_diff(ref='{safe}~1..{safe}', path=<file>) for one file]"
    )


if __name__ == "__main__":
    mcp.run()

"""Render the README's terminal screenshots to PNG.

    python scripts/render_docs.py            # writes docs/img/*.png

The transcripts below are verbatim captures from a real run on 2026-09-20
(Windows 10, Python 3.14.3, Docker 29.7.2). Nothing here is illustrative: if you
change a tool's output, re-run the command, paste the new text in, and re-render
so the docs cannot drift away from the code.

Inline colour markup is [[k]]...[[/]] where k is a key of PALETTE.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "img"

SCALE = 2  # render at 2x so the images stay crisp on high-DPI displays

FONT_SIZE = 15 * SCALE
LINE_H = 23 * SCALE
PAD_X = 22 * SCALE
PAD_TOP = 52 * SCALE  # room for the title bar
PAD_BOT = 20 * SCALE
BAR_H = 38 * SCALE
RADIUS = 10 * SCALE

BG = "#0d1117"
BAR = "#161b22"
BORDER = "#30363d"
FG = "#c9d1d9"

PALETTE = {
    "fg": FG,
    "dim": "#7d8590",
    "g": "#3fb950",  # success
    "r": "#f85149",  # error
    "y": "#d29922",  # warning / approval
    "b": "#58a6ff",  # paths, commands
    "m": "#bc8cff",  # tool names
    "c": "#39c5cf",  # server names
    "w": "#ffffff",
}

FONT = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", FONT_SIZE)
FONT_B = ImageFont.truetype("C:/Windows/Fonts/consolab.ttf", FONT_SIZE)

TAG = re.compile(r"\[\[(\w+|/)\]\]")


def _parse(body: str) -> list[list[tuple[str, str]]]:
    """Markup -> per-line (text, colour) spans.

    The colour state is carried across newlines, so a [[dim]] block may wrap
    several lines without being reopened on each one.
    """
    rows: list[list[tuple[str, str]]] = [[]]
    colour = FG
    pos = 0
    for m in TAG.finditer(body):
        chunk = body[pos:m.start()]
        for i, piece in enumerate(chunk.split("\n")):
            if i:
                rows.append([])
            if piece:
                rows[-1].append((piece, colour))
        colour = FG if m.group(1) == "/" else PALETTE.get(m.group(1), FG)
        pos = m.end()
    for i, piece in enumerate(body[pos:].split("\n")):
        if i:
            rows.append([])
        if piece:
            rows[-1].append((piece, colour))
    return rows


def _check_glyphs(body: str, title: str) -> None:
    """Fail loudly rather than silently rendering a missing glyph as a box."""
    def bitmap(ch: str) -> bytes:
        im = Image.new("L", (FONT_SIZE * 2, FONT_SIZE * 2), 0)
        ImageDraw.Draw(im).text((1, 1), ch, font=FONT, fill=255)
        return im.tobytes()

    ref = bitmap("￿")  # guaranteed-absent codepoint: the "no glyph" box
    bad = {ch for ch in TAG.sub("", body) + title
           if ord(ch) > 0x7F and bitmap(ch) == ref}
    if bad:
        raise SystemExit("Consolas has no glyph for: "
                         + ", ".join(f"U+{ord(c):04X}" for c in sorted(bad)))


def render(name: str, title: str, body: str) -> Path:
    body = body.strip("\n")
    _check_glyphs(body, title)
    rows = _parse(body)
    dots_right = PAD_X + 2 * 15 * SCALE + 5 * SCALE
    # the title is centred, but never allowed to run into the window dots
    widest = max([sum(FONT.getlength(t) for t, _ in row) for row in rows]
                 + [FONT_B.getlength(title) + (dots_right + 20 * SCALE) * 2 - PAD_X * 2])
    w = int(widest + PAD_X * 2)
    h = int(PAD_TOP + len(rows) * LINE_H + PAD_BOT)

    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    # window chrome
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=RADIUS, fill=BG, outline=BORDER, width=SCALE)
    d.rounded_rectangle([0, 0, w - 1, BAR_H], radius=RADIUS, fill=BAR)
    d.rectangle([0, BAR_H - RADIUS, w - 1, BAR_H], fill=BAR)
    d.line([0, BAR_H, w - 1, BAR_H], fill=BORDER, width=SCALE)
    for i, dot in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        cx = PAD_X + i * 15 * SCALE
        cy = BAR_H // 2
        r = 5 * SCALE
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dot)

    tw = FONT_B.getlength(title)
    tx = max((w - tw) / 2, dots_right + 20 * SCALE)
    d.text((tx, BAR_H / 2 - FONT_SIZE * 0.62), title, font=FONT_B, fill="#8b949e")

    y = PAD_TOP
    for row in rows:
        x = PAD_X
        for text, colour in row:
            d.text((x, y), text, font=FONT, fill=colour)
            x += FONT.getlength(text)
        y += LINE_H

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    img.save(path, optimize=True)
    return path


# --------------------------------------------------------------------------
# Verbatim captures
# --------------------------------------------------------------------------

SHOTS: list[tuple[str, str, str]] = [
    (
        "agent-check",
        "python -m devops_mcp.agent --check",
        """
[[w]]devops agent[[/]]  model=[[b]]claude-opus-5[[/]]  [[g]]17 tools[[/]] from [[g]]5 servers[[/]]  ([[y]]4 need approval[[/]])
[[dim]]roots: C:\\Users\\user\\Desktop\\MCP server[[/]]

  [[c]]devops-docker     [[/]]  [[m]]get_container_logs[[/]]
  [[c]]devops-git        [[/]]  [[m]]git_diff[[/]]
  [[c]]devops-git        [[/]]  [[m]]git_log[[/]]
  [[c]]devops-git        [[/]]  [[m]]git_show[[/]]
  [[c]]devops-git        [[/]]  [[m]]git_status[[/]]
  [[c]]devops-docker     [[/]]  [[m]]inspect_container[[/]]
  [[c]]devops-docker     [[/]]  [[m]]list_containers[[/]]
  [[c]]devops-filesystem [[/]]  [[m]]list_files[[/]]
  [[c]]devops-filesystem [[/]]  [[m]]read_file[[/]]
  [[c]]devops-logs       [[/]]  [[m]]read_log[[/]]
  [[c]]devops-actions    [[/]]  [[m]]rebuild_service[[/]] [[y]][approval][[/]]
  [[c]]devops-actions    [[/]]  [[m]]restart_container[[/]] [[y]][approval][[/]]
  [[c]]devops-filesystem [[/]]  [[m]]search_files[[/]]
  [[c]]devops-logs       [[/]]  [[m]]search_logs[[/]]
  [[c]]devops-actions    [[/]]  [[m]]start_container[[/]] [[y]][approval][[/]]
  [[c]]devops-actions    [[/]]  [[m]]stop_container[[/]] [[y]][approval][[/]]
  [[c]]devops-logs       [[/]]  [[m]]summarize_errors[[/]]

ANTHROPIC_KEY: [[r]]NOT SET[[/]] [[dim]](looked in the environment and ...\\MCP server\\.env)[[/]]
""",
    ),
    (
        "investigation",
        'Claude Code  —  "The broken-backend container is returning 500s. Why?"',
        """
[[dim]]The model chose each of these calls itself. Nothing was scripted.[[/]]

 [[g]]●[[/]] [[m]]list_containers[[/]]      [[dim]]all=true[[/]]
   [[dim]]→[[/]] broken-backend [[g]]running[[/]], 0 restarts  [[dim]]— so not a crash loop[[/]]

 [[g]]●[[/]] [[m]]inspect_container[[/]]    [[dim]]name=broken-backend[[/]]
   [[dim]]→[[/]] exit_code 0, oom_killed false, [[y]]mounts: [][[/]]  [[dim]]— not infrastructure[[/]]

 [[g]]●[[/]] [[m]]summarize_errors[[/]]     [[dim]]path=demo/broken_app/logs/app.log[[/]]
   [[dim]]→[[/]] [[r]]26 ×  KeyError: 'email'[[/]]  at [[b]]/srv/app/routes.py:21[[/]]
   [[dim]]→[[/]]  1 ×  redis ConnectionError [[dim]](unrelated)[[/]]

 [[g]]●[[/]] [[m]]search_files[[/]]         [[dim]]pattern="mail" path=demo/broken_app/app[[/]]
   [[dim]]→[[/]] [[b]]repository.py:6[[/]]  {"id": 1, "name": "Ada", [[y]]"mail"[[/]]: "ada@example.com"}

 [[g]]●[[/]] [[m]]git_log[[/]]              [[dim]]path=app/repository.py limit=5[[/]]
   [[dim]]→[[/]] [[y]]661536c[[/]]  Rename users.email column to mail (migration 0007)

 [[g]]●[[/]] [[m]]git_show[[/]]             [[dim]]ref=661536c[[/]]
   [[dim]]→[[/]] renamed the key in the data layer, never touched its consumer

[[w]]Diagnosis[[/]]  Commit [[y]]661536c[[/]] changed [[b]]repository.py[[/]] but not [[b]]routes.py:21[[/]],
which still reads user["email"]. Every [[b]]/users/<id>[[/]] request raises KeyError.
[[b]]/health[[/]] still works because it never touches user rows. The container has
[[y]]no source mount[[/]], so fix the line and [[m]]rebuild_service[[/]] — a restart keeps
the old image.
""",
    ),
    (
        "demo-500",
        "docker compose up -d --build  &&  curl",
        """
[[dim]]$[[/]] [[b]]cd demo/broken_app && docker compose up -d --build[[/]]
 [[g]]●[[/]] Image broken_app-backend    [[g]]Built[[/]]
 [[g]]●[[/]] Container broken_app-db-1   [[g]]Started[[/]]
 [[g]]●[[/]] Container broken_app-cache-1 [[g]]Started[[/]]
 [[g]]●[[/]] Container broken-backend    [[g]]Started[[/]]

[[dim]]$[[/]] [[b]]curl.exe -s -o /dev/null -w "%{http_code}" http://localhost:8000/health[[/]]
GET /health   -> [[g]]200[[/]]

[[dim]]$[[/]] [[b]]curl.exe -s -o /dev/null -w "%{http_code}" http://localhost:8000/users/1[[/]]
GET /users/1  -> [[r]]500[[/]]     [[dim]]← the planted bug, reproduced[[/]]
""",
    ),
    (
        "redaction",
        'inspect_container(name="broken-backend")',
        """
[[dim]]Every value that looks like a secret is masked before it reaches the model.[[/]]

"env": [
  "STRIPE_API_KEY=[[g]]***REDACTED***[[/]]",
  "DATABASE_URL=postgres://app:[[g]]***REDACTED***[[/]]@db:5432/app",   [[dim]]← mid-URL[[/]]
  "SECRET_KEY=[[g]]***REDACTED***[[/]]",
  "SENDGRID_TOKEN=[[g]]***REDACTED***[[/]]",
  "DB_PASSWORD=[[g]]***REDACTED***[[/]]",
  "REDIS_URL=redis://cache:6379/0",        [[dim]]← no credential, kept readable[[/]]
  "FLASK_ENV=development",
  "DB_HOST=db",
  "LOG_LEVEL=INFO"
],
"restart_count": 0,
"restart_policy": "on-failure",
"[[y]]mounts[[/]]": [],                              [[dim]]← why a restart cannot fix a source edit[[/]]
"diagnosis_hints": []
""",
    ),
    (
        "sandbox",
        'read_file(path="../../../Windows/System32/drivers/etc/hosts")',
        """
[[dim]]Paths are resolved through symlinks first, then checked against the roots.[[/]]

[[r]]Error executing tool read_file:[[/]] [[y]]Access denied[[/]]:
  '../../../Windows/System32/drivers/etc/hosts' resolves to
  [[b]]C:\\Users\\Windows\\System32\\drivers\\etc\\hosts[[/]], which is outside the
  allowed roots ([[b]]C:\\Users\\user\\Desktop\\MCP server[[/]]).
  Paths are relative to the first root.

[[dim]]The refusal names the resolved path, so the model can correct itself
instead of retrying blind.[[/]]
""",
    ),
    (
        "approval",
        "restart_container  —  the write-action gate",
        """
[[dim]]The tool carries destructiveHint=true and anthropic/requiresUserInteraction,
so the loop stops here before anything is touched.[[/]]

  [[y]]▲ WRITE ACTION[[/]] [[w]]restart_container[[/]](name='broken-backend')
  Approve? [y/N] [[g]]y[[/]]

[[g]]●[[/]] {
    "action": "restart",
    "target": "broken-backend",
    "state_before": "running",
    "state_after": "running",
    "succeeded": [[g]]true[[/]]
  }

[[dim]]Decline instead, and the model is told not to retry — the investigation
carries on read-only. With no approver wired at all, the call is refused.[[/]]
""",
    ),
    (
        "tests",
        "python -m pytest -q",
        """
...............................s........................................ [ 41%]
s....................................................................... [ 82%]
.........s.....................                                          [100%]

[[g]]172 passed[[/]], [[y]]3 skipped[[/]] in 67.94s (0:01:07)
[[dim]]2 skips need a live Docker daemon, 1 needs symlink privileges;
with Docker up the same suite reports 174 passed, 1 skipped[[/]]
""",
    ),
]


def main() -> None:
    for name, title, body in SHOTS:
        path = render(name, title, body)
        img = Image.open(path)
        print(f"{path.relative_to(OUT_DIR.parent.parent)}  {img.width}x{img.height}  "
              f"{path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()

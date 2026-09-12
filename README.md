# AI DevOps Assistant

**Natural-language debugging of a local development environment, powered by MCP.**

Instead of manually checking containers, tailing logs, grepping source and reading git history,
you ask a question in plain English and an AI agent investigates it for you:

> *My API started returning 500 errors after my latest changes. Find out why.*

The agent decides which tools it needs, calls them through the Model Context Protocol, and comes
back with a diagnosis. It never receives a dump of your system — it gets a set of capabilities and
chooses among them, one call at a time.

---

## What it actually does

A real investigation against the bundled demo app, unedited:

```
You:   The broken-backend container is returning 500s. Why?

Agent: list_containers        → finds broken-backend, running, no restarts
       inspect_container      → exit code 0, no OOM, health fine, so not infrastructure
       get_container_logs     → KeyError: 'email' at /srv/app/routes.py line 21
       summarize_errors       → 26 occurrences of that one error; 1 unrelated Redis warning
       read_file routes.py    → reads user["email"]
       read_file repository.py→ but rows are keyed "mail"
       git_log path=repo.py   → commit 661536c "Rename users.email column to mail"
       git_show 661536c       → renamed the key, never updated the route

Diagnosis: commit 661536c changed the data layer but not its consumer. Every request to
/users/<id> raises KeyError. /health still works because it never touches user rows.
Fix routes.py line 21, then rebuild — the image has no source mount, so a restart won't help.
```

That chain is the agent's own. Nothing in it was scripted.

---

## How it works

```
        You  ──ask──▶  AI Agent  ──MCP──▶  5 stdio servers  ──▶  your environment
                          │                                        docker / git /
                          └────────  diagnosis  ◀──────────────    files / logs
```

**MCP is the bridge.** Each server is a standalone Python process speaking the Model Context
Protocol over stdin/stdout. The host (Claude Code, or any MCP client) starts them, collects their
tool definitions, and hands those to the model. The model picks tools; the host routes the calls.

**Why five servers instead of one.** Each is independently registrable. Running only the four
read-only servers means the agent has *no* mutating tools at all — not disabled ones, absent ones.
That property is worth more than the convenience of a single process.

**The design constraint that shapes everything: context is scarce.** A tool that dumps a
10,000-line log is worse than no tool, because it buries the answer. So every tool caps its own
output, says so when it truncates, and tells the model how to narrow the next call. That's why
`summarize_errors` exists — it turns thousands of log lines into a ranked list of distinct
problems.

---

## The tools

Seventeen tools across five servers. Every parameter below is optional unless marked required.

### `devops-filesystem` — project inspection

| Tool | Parameters | Returns |
|---|---|---|
| `list_files` | `path="."`, `depth=2`, `include_hidden=False` | Directory tree with sizes. Skips VCS, dependency and cache dirs, and honours `.gitignore`. |
| `read_file` | **`path`**, `start_line`, `end_line` | Line-numbered text so the model can cite locations. Refuses binaries; secrets redacted. |
| `search_files` | **`pattern`**, `path="."`, `glob`, `max_results=50`, `ignore_case=False` | Regex search like `grep -rn`. Structured hits of path, line, text. `glob` filters by name or path. |

### `devops-git` — change history

| Tool | Parameters | Returns |
|---|---|---|
| `git_status` | `repo="."` | Branch, ahead/behind, and staged, unstaged, untracked and conflicted files. |
| `git_diff` | `repo="."`, `staged=False`, `ref`, `path`, `stat_only=False` | Unified diff. Falls back to a `--stat` summary when the patch exceeds the output budget. |
| `git_log` | `repo="."`, `limit=15`, `ref`, `path` | Commits with author, date, subject and files-changed. `path` finds who last touched a file. |
| `git_show` | `repo="."`, `ref="HEAD"`, `stat_only=False` | One commit: metadata, message and patch. |

### `devops-docker` — container inspection

| Tool | Parameters | Returns |
|---|---|---|
| `list_containers` | `all=True` | Name, state, status, image, ports. Non-running listed first, since those are the interesting ones. |
| `inspect_container` | **`name`** | The diagnostic subset of `docker inspect`: exit code, OOM flag, restart count and policy, health log, command, env (redacted), ports, mounts, networks. Plus `diagnosis_hints` flagging crash loops, OOM kills, exit 137/139 and failing health checks. |
| `get_container_logs` | **`name`**, `tail=200`, `since`, `timestamps=True`, `stderr_only=False` | Stdout and stderr interleaved by timestamp. |

### `devops-logs` — log analysis

| Tool | Parameters | Returns |
|---|---|---|
| `summarize_errors` | **`path`**, `max_groups=10`, `max_lines=50000` | **Start here.** Groups repeated errors and tracebacks into distinct problems, ranked by severity and count, each with the innermost code location and first/last seen timestamps. |
| `search_logs` | **`path`**, **`pattern`**, `context=2`, `max_results=30`, `ignore_case=True`, `max_lines=50000` | Regex hits with surrounding context lines, plus a total match count. |
| `read_log` | **`path`**, `tail=200` | Raw recent lines, numbered. Reads large files from the end without loading them whole. |

`summarize_errors` understands Python tracebacks including chained ones, Docker-style logs where
every line carries a timestamp prefix, and JavaScript or Java stack frames. It normalises request
IDs, UUIDs and hex values so one problem doesn't fragment into a hundred groups.

### `devops-actions` — write operations, **human approval required**

Every tool here prompts for explicit approval on every single call. See [Safety](#safety) below.

| Tool | Parameters | Returns |
|---|---|---|
| `restart_container` | **`name`**, `timeout=10` | State before and after, and whether it stayed up. |
| `stop_container` | **`name`**, `timeout=10` | SIGTERM then SIGKILL after `timeout`. Container is stopped, not removed. |
| `start_container` | **`name`** | State before and after. |
| `rebuild_service` | **`compose_dir`**, `service`, `no_cache=False` | `docker compose up -d --build`. The step that makes a source edit take effect when a container has no bind mount — restarting alone keeps the old image. |

---

## Quick start

```bash
pip install -e ".[dev]"
python -m pytest                                # 155 tests; Docker ones skip without a daemon
mcp dev src/devops_mcp/servers/filesystem.py    # open the MCP Inspector
```

The servers are registered for Claude Code in [`.mcp.json`](.mcp.json) at project scope. Open this
folder in Claude Code, approve the servers when prompted, then check `/mcp`.

### Try it

Build the demo target — a git repo whose history contains a deliberately planted bug:

```bash
python scripts/make_demo.py      # creates demo/broken_app (gitignored)
```

Ask, without Docker:

> The app in `demo/broken_app` started returning 500s after recent changes. What broke?

Or run the real thing:

```bash
cd demo/broken_app
docker compose up -d --build
curl.exe http://localhost:8000/users/1     # 500; use curl.exe on PowerShell, not the alias
```

> The broken-backend container is returning 500s. Why?

Add *"use only the devops MCP tools"* to the question. Otherwise the agent may reach for its
built-in file tools, which is faster but proves nothing about these servers. Tear down with
`docker compose down`.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DEVOPS_MCP_ROOTS` | `CLAUDE_PROJECT_DIR`, else cwd | Directories the tools may touch, separated by `;` on Windows and `:` elsewhere. Relative paths resolve against the first. |
| `DEVOPS_MCP_MAX_LINES` | `400` | Max lines in any tool result. |
| `DEVOPS_MCP_MAX_BYTES` | `65536` | Max bytes in any tool result. |
| `DEVOPS_MCP_ALLOW_ACTIONS` | `1` | Set to `0` to disable every write tool. |
| `DEVOPS_MCP_ACTION_CONTAINERS` | *(all)* | Glob allowlist scoping which containers write tools may touch, e.g. `broken_app-*,broken-backend`. |

Point `DEVOPS_MCP_ROOTS` at a real project to investigate it.

---

## Safety

The boundary lives in one place, [`src/devops_mcp/safety.py`](src/devops_mcp/safety.py), and every
server routes through it.

- **Path sandbox.** Every path is fully resolved, symlinks included, and must land inside an allowed
  root. Traversal, absolute escapes and symlink escapes are refused with an error the model can read
  and correct.
- **Secret redaction.** Secret-shaped keys (`*PASSWORD*`, `*TOKEN*`, `*API_KEY*`, …), credentials
  embedded in URLs, PEM blocks, bearer tokens and known token shapes are masked in every file,
  search, log, diff and container-env result. Config files stay readable, which matters because
  that's usually where the bug is.
- **Output budget.** No result exceeds the configured size. Truncated results say so and explain how
  to narrow the query.
- **No option injection.** Model-supplied refs, paths and container names that reach `git` or
  `docker` are validated (no leading `-`, no shell metacharacters) and paths always follow `--`.
  Commands are argv lists with timeouts; no shell is ever invoked.
- **Read-only by default.** All 13 inspection tools are annotated `readOnlyHint`.

### Write actions

`devops-actions` is the only server that changes anything, and three independent guards stack:

1. **Opt-in by registration.** Remove it from `.mcp.json` and the agent has no mutating tools at all.
2. **Operator control.** `DEVOPS_MCP_ALLOW_ACTIONS=0` disables every tool;
   `DEVOPS_MCP_ACTION_CONTAINERS` scopes them to named containers, so the agent can be allowed to
   restart your dev stack but never a database you happen to be running locally.
3. **Human approval per call.** Each tool is annotated `destructiveHint` and carries
   `anthropic/requiresUserInteraction`, which is intended to force a prompt on every call with no
   "don't ask again" option.

Each action reports container state before and after, so the agent verifies the result instead of
assuming it.

> **Known issue.** In the current Claude Code build these four tools are filtered out of the model's
> tool list rather than being offered behind a prompt. The server connects and serves them
> correctly over a direct MCP session, so the cause is host-side. `scripts/probe_flags.py` is a
> diagnostic server that isolates which attribute triggers the filtering; it is temporary and will
> be removed once resolved. The other four servers are unaffected.

---

## Layout

```
src/devops_mcp/
  config.py          settings read from the environment
  safety.py          path sandbox, secret redaction, output truncation
  shell.py           subprocess wrapper — argv lists, timeouts, never a shell
  docker_client.py   shared docker CLI plumbing and error translation
  servers/
    filesystem.py    list_files / read_file / search_files
    git.py           git_status / git_diff / git_log / git_show
    docker.py        list_containers / inspect_container / get_container_logs
    logs.py          read_log / search_logs / summarize_errors
    actions.py       restart / stop / start / rebuild   (approval-gated)
fixtures/broken_app/ deliberately broken Flask app used by tests and demos
scripts/
  make_demo.py       builds demo/broken_app with a bug-introducing commit in its history
  probe_flags.py     temporary diagnostic (see Known issue above)
tests/               155 tests, mostly on the safety boundary
```

Each server runs standalone: `python -m devops_mcp.servers.<name>`.

Requires Python 3.10+ and the `mcp` SDK 2.x. Git and Docker are invoked through their CLIs, so
neither needs a client library, and both degrade to a readable error when unavailable.

# AI DevOps Assistant (MCP)

MCP servers that give an AI agent **controlled, read-only** access to a local development
environment, so it can investigate questions like *"why is my backend container crashing?"*
by itself: listing files, reading config, grepping for the error string, checking git history
and container logs, then explaining the cause.

The agent doesn't get a dump of your system. It gets tools, and decides which ones to call.

## Status

| Server | Tools | State |
|---|---|---|
| `devops-filesystem` | `list_files`, `read_file`, `search_files` | **done** |
| `devops-git` | `git_status`, `git_diff`, `git_log`, `git_show` | **done** |
| `devops-docker` | `list_containers`, `inspect_container`, `get_container_logs` | **done** |
| `devops-logs` | `read_log`, `search_logs`, `summarize_errors` | **done** |

## Quick start

```bash
pip install -e ".[dev]"
python -m pytest            # unit tests; the one Docker integration test skips if no daemon
mcp dev src/devops_mcp/servers/filesystem.py   # opens the MCP Inspector
```

The servers are registered for Claude Code in [`.mcp.json`](.mcp.json) (project scope).
Open this folder in Claude Code, approve the project server when prompted, then check `/mcp`.

Build the demo target, a git repo whose history contains the bug-introducing commit:

```bash
python scripts/make_demo.py      # creates demo/broken_app (gitignored)
```

Then ask:

> The app in `demo/broken_app` started returning 500s after recent changes. What broke?

The agent should chain `search_files` → `git_log` → `git_show` and land on the
"Rename users.email column to mail" commit.

For the live Docker version, run the demo stack and generate some failing requests:

```bash
cd demo/broken_app
docker compose up -d --build
curl localhost:8000/users/1        # 500
```

Then ask:

> The broken-backend container is returning 500s. Why?

The agent should go `list_containers` → `get_container_logs` → `search_files` → `git_log` → `git_show`.
Tear down with `docker compose down`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DEVOPS_MCP_ROOTS` | `CLAUDE_PROJECT_DIR`, else cwd | `;`-separated (`:` on Unix) directories the tools may touch. Relative paths resolve against the first one. |
| `DEVOPS_MCP_MAX_LINES` | `400` | Max lines in any tool result. |
| `DEVOPS_MCP_MAX_BYTES` | `65536` | Max bytes in any tool result. |

Point `DEVOPS_MCP_ROOTS` at a real project to investigate it.

## Safety model

Everything lives in [`src/devops_mcp/safety.py`](src/devops_mcp/safety.py):

- **Path sandbox.** Every path is resolved (symlinks included) and must sit inside an allowed root.
  `..` traversal, absolute escapes and symlink escapes are refused with an error the model can read.
- **Secret redaction.** Values of keys that look like secrets (`*PASSWORD*`, `*TOKEN*`, `*API_KEY*`, ...),
  credentials embedded in URLs, PEM blocks, bearer tokens and well-known token shapes are masked in
  every file, search and log result. Config files stay readable, which is usually where the bug is.
- **Output budget.** No result exceeds the configured size. Truncated results say so and tell the
  model how to narrow the query (line ranges, globs, `max_results`).
- **Read-only.** Every tool is annotated `readOnlyHint`. Mutating actions (restart container, apply
  patch) are a later phase and will require explicit human approval per call.
- **No option injection.** Model-supplied refs and paths handed to `git` are validated (no leading
  `-`, ref-shaped characters only) and paths always follow `--`.

## Layout

```
src/devops_mcp/
  config.py        settings from env
  safety.py        path sandbox, redaction, truncation
  shell.py         subprocess wrapper (git / docker servers)
  servers/
    filesystem.py  list_files / read_file / search_files
    git.py         git_status / git_diff / git_log / git_show
    docker.py      list_containers / inspect_container / get_container_logs
    logs.py        read_log / search_logs / summarize_errors
fixtures/broken_app/   deliberately broken Flask app used by tests and demos
scripts/make_demo.py   builds demo/broken_app with a telling git history
tests/
```

Each server is a standalone stdio process (`python -m devops_mcp.servers.<name>`).

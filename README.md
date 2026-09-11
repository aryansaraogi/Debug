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
| `devops-git` | `git_status`, `git_diff`, `git_log`, `git_show` | planned |
| `devops-docker` | `list_containers`, `inspect_container`, `get_container_logs` | planned |
| `devops-logs` | `read_log`, `search_logs`, `summarize_errors` | planned |

## Quick start

```bash
pip install -e ".[dev]"
python -m pytest            # unit tests, no Docker needed
mcp dev src/devops_mcp/servers/filesystem.py   # opens the MCP Inspector
```

The servers are registered for Claude Code in [`.mcp.json`](.mcp.json) (project scope).
Open this folder in Claude Code, approve the project server when prompted, then check `/mcp`.

Try it on the bundled broken app:

> The app in `fixtures/broken_app` is returning 500s. Find out why.

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

## Layout

```
src/devops_mcp/
  config.py        settings from env
  safety.py        path sandbox, redaction, truncation
  shell.py         subprocess wrapper (git / docker servers)
  servers/
    filesystem.py  MCP server entry point
fixtures/broken_app/   deliberately broken Flask app used by tests and demos
tests/
```

Each server is a standalone stdio process (`python -m devops_mcp.servers.<name>`).

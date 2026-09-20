# Guide

How to use this thing, and what is actually happening when you do.

The [README](README.md) is the reference: every tool, every flag, every setting. This is the
walkthrough. It follows one investigation from a cold checkout to a diagnosis, and stops at each
step to explain the mechanism and the reason it works that way.

**Contents**

1. [What you are actually running](#1-what-you-are-actually-running)
2. [Setup](#2-setup)
3. [Your first investigation](#3-your-first-investigation)
4. [What happens inside one turn](#4-what-happens-inside-one-turn)
5. [Reading the output](#5-reading-the-output)
6. [Write actions and the approval gate](#6-write-actions-and-the-approval-gate)
7. [Pointing it at your own project](#7-pointing-it-at-your-own-project)
8. [Asking questions that get good answers](#8-asking-questions-that-get-good-answers)
9. [When it goes wrong](#9-when-it-goes-wrong)
10. [The two ways to run it](#10-the-two-ways-to-run-it)

---

## 1. What you are actually running

Three separate pieces. They are easy to confuse, and knowing which one you are talking to explains
most of the surprising behaviour.

**The five MCP servers** are ordinary Python processes. They know nothing about AI. Each one
exposes a handful of functions over stdin/stdout using the Model Context Protocol, and each
enforces its own limits on what it will return. You can run one by hand and poke at it with the MCP
Inspector; no API key is involved.

**The host** is whatever starts those processes and talks to a language model. That is either
Claude Code, or the bundled agent (`python -m devops_mcp.agent`). The host collects the tool
definitions, sends them to the model, and routes the calls the model asks for.

**The model** never touches your machine. It sees tool *names, descriptions and schemas* — never
your files, never your containers. It asks for things; the host decides whether to run them and
the server decides what to hand back.

That separation is the whole design. The model is treated as an untrusted planner: it chooses
**what** to look at, and never **whether it is allowed to**.

```
   you ──▶ host ──▶ model          "I want read_file(path='app/routes.py')"
             │  ◀──
             ▼
          server  ── resolves the path, checks it is inside the sandbox,
                     reads it, redacts secrets, truncates, hands back text
```

---

## 2. Setup

### Install

```bash
pip install -e ".[dev,agent]"
```

Two extras, and the split matters. `dev` gets you the test suite. `agent` pulls in the Anthropic
SDK, which **only the agent host needs** — the five servers never import it. If you only want to
use these servers from Claude Code, you never need the `agent` extra and never need an API key.

### Check it works before involving a model

```bash
python -m devops_mcp.agent --check
```

This spawns all five servers, asks each for its tool list, prints what it found, and reports
credential status. It deliberately **works without a key**, because it is testing plumbing, not
the model. If something is broken, this tells you which half.

A healthy run lists 18 tools across 5 servers, with 4 marked `[approval]`.

If every server fails at once, it is almost always the interpreter. `.mcp.json` launches them with
`${DEVOPS_MCP_PYTHON:-python}`, so they inherit whatever `python` means on your `PATH`. If that is
a different environment from the one you installed into, each server starts, fails to import
`devops_mcp`, and exits immediately. Point `DEVOPS_MCP_PYTHON` at the right interpreter rather
than editing the committed file.

### Credentials (agent host only)

```bash
cp .env.example .env     # then put your key in it
```

The variable is **`ANTHROPIC_KEY`**, not the SDK's usual `ANTHROPIC_API_KEY`. That looks like a
gratuitous difference and it is not. The SDK picks up `ANTHROPIC_API_KEY` from the environment
implicitly, so a value left over in your shell from something else could silently decide which
account gets billed. Using a different name forces the key to be passed to the client explicitly,
where you can see it happen. `.env` is gitignored.

---

## 3. Your first investigation

Use the demo. It exists so you can watch the system work on a bug whose answer is already known,
before pointing it at code you care about.

```bash
python scripts/make_demo.py
```

This builds `demo/broken_app` — a small Flask service, plus a **git history with a planted bug**.
Three commits: a working service, then *"Rename users.email column to mail (migration 0007)"*
which changes the data layer and forgets the route that consumes it, then an unrelated commit on
top so the culprit is not simply `HEAD`. The directory is gitignored, and the script rebuilds it
from scratch, so you can break it freely.

Then either run it for real:

```bash
cd demo/broken_app && docker compose up -d --build
curl.exe http://localhost:8000/users/1        # 500
```

(On PowerShell it must be `curl.exe`. Plain `curl` is an alias for `Invoke-WebRequest`, which
mis-parses the URL and treats a 500 as a terminating error.)

Or skip Docker entirely — the filesystem, git and logs servers work on the checked-in log file.

### Ask

```bash
python -m devops_mcp.agent "The broken-backend container is returning 500s. Why?"
```

What you will see, and why it happens in this order:

| The agent calls | Because |
|---|---|
| `list_containers` | Find the thing. Non-running containers sort first, since those are usually the problem. |
| `inspect_container` | Rule out infrastructure before reading logs. Exit code 0, no OOM, no restarts means this is an application bug, not a crash. |
| `get_container_logs` | Now the logs are worth reading, and it knows what it is looking for. |
| `summarize_errors` | 351 log lines collapse into two facts: 26 identical `KeyError: 'email'` at `routes.py:21`, and one unrelated Redis warning. |
| `read_file` ×2 | The route reads `user["email"]`; the repository returns rows keyed `mail`. |
| `git_log --path` | Who last touched the data layer. |
| `git_show` | That commit renamed the key and never updated the consumer. |

The interesting part is the Redis warning. It is real, it looks alarming, and it has nothing to do
with the 500s. A naive grep for errors surfaces it as a co-equal suspect. `summarize_errors` groups
by *distinct problem*, so one appears 26 times and the other once, and the ranking makes the
irrelevance obvious.

---

## 4. What happens inside one turn

Worth understanding, because it explains both the costs and the limits.

1. **Discover.** The host spawns each server as a subprocess and asks for its tools: names,
   JSON schemas, descriptions, annotations. Nothing has run yet.
2. **Offer.** All tools are flattened into one namespace and sent to the model with your question.
   A name collision between two servers is refused loudly rather than silently shadowed.
3. **Choose.** The model replies with `tool_use` blocks. It may ask for several at once.
4. **Gate.** Any tool marked `requiresUserInteraction` stops here and asks you. Everything else
   proceeds.
5. **Execute.** The call routes to the owning server, which validates arguments, resolves paths
   against the sandbox, runs the work, then redacts and truncates the result.
6. **Feed back.** All results return as a single user message, and the loop repeats until the model
   stops asking for tools.

The host runs this loop by hand rather than using the SDK's tool runner. Two reasons: the tools are
discovered at runtime from the servers rather than declared in code, and every write tool has to
pass through a human gate before it executes.

`--max-turns` (default 40) is the stop for a model that keeps calling tools without converging.

---

## 5. Reading the output

```
  ● summarize_errors path=demo/broken_app/logs/app.log
  ● git_log repo=demo/broken_app path=app/repository.py
  ⚠ WRITE ACTION restart_container(name='broken-backend')
  ⊘ restart_container declined by operator
  ✗ read_file Access denied: '../../etc/passwd' resolves to ...

────────────────────────────────────────────────────────────
<the diagnosis>

[7 turns, 9 tool calls, 48,213 in / 3,104 out tokens]
```

| Marker | Meaning |
|---|---|
| `●` | A tool ran. |
| `⚠` | A write action is asking for approval. |
| `⊘` | You declined it. The agent is told, and carries on. |
| `✗` | The tool refused. This is normal and often useful. |

**A `✗` is not a crash.** Tools raise errors *written to be read by the model* — naming the
resolved path, listing the containers that do exist, saying which flag to use instead. The agent
is expected to correct itself and retry. Seeing one or two is healthy.

**The token counts are the real cost signal.** Input dwarfs output because every tool result is
resent on every subsequent turn. That is exactly why each tool caps its own output: one
unbounded log dump does not just waste one call, it is re-billed on every turn after it.

---

## 6. Write actions and the approval gate

Four tools can change your environment: `restart_container`, `stop_container`, `start_container`,
`rebuild_service`. Each one stops and asks, every single time:

```
  ⚠ WRITE ACTION restart_container(name='broken-backend')
  Approve? [y/N]
```

Three independent guards sit behind that prompt, and they are deliberately redundant.

**The server has to be registered at all.** Run with `--read-only` and `devops-actions` never
starts. The agent does not have disabled write tools — it has *no* write tools. A capability that
was never loaded cannot be invoked by a confused model, a prompt injection, or a bug in the
approval logic. This is the strongest guarantee of the three, because it does not depend on any
code being correct.

**The operator can narrow the blast radius.** `DEVOPS_MCP_ALLOW_ACTIONS=0` turns every write tool
off. `DEVOPS_MCP_ACTION_CONTAINERS=broken_app-*` restricts them to matching names, so the agent
can be allowed to restart your dev stack but never a database you happen to be running locally.

**Every call asks a human.** Non-interactive runs decline by default rather than proceeding.
`--yes` auto-approves; think before using it.

Two behaviours worth knowing:

- **Diagnose before restarting.** The agent is told this explicitly, because restarting a container
  rotates away the logs that explain why it failed. If it proposes a restart before reading
  anything, decline and tell it to look first.
- **A restart usually will not apply a code fix.** The demo image has no bind mount, so the
  container keeps running the old image. `rebuild_service` is the tool that makes a source edit
  take effect. The tool descriptions say so, which is part of why the agent reaches for the right
  one.

After any action, the tool reports container state *before and after*, so the agent can verify what
happened instead of assuming. `probe_url` closes the same loop at the HTTP level — it is how the
agent checks that a fix actually worked rather than declaring victory.

`probe_url` is restricted to GET and HEAD, and to hosts resolving to loopback or private addresses.
Both restrictions are load-bearing: an unrestricted HTTP client would let the model change server
state with a POST and walk straight around this entire approval gate.

---

## 7. Pointing it at your own project

```bash
python -m devops_mcp.agent --roots ~/code/my-service "Why is the worker crashing?"
```

`--roots` sets the sandbox. Every path any tool touches is fully resolved — symlinks included —
and must land inside it. Traversal, absolute escapes and symlink escapes are refused with an error
naming the resolved path, so the model can correct itself rather than retrying blind.

You can pass `--roots` more than once. Relative paths resolve against the first.

Three things to expect the first time:

**Secrets come back masked.** Values under keys like `*PASSWORD*`, `*TOKEN*`, `*API_KEY*`,
credentials inside URLs, PEM blocks and known token shapes are redacted from every file, search,
log, diff and container-env result. Keys and structure survive. This is on purpose: config files
are where bugs live, so they have to stay readable, but their contents are about to be sent to an
API and land in a transcript.

**Results get truncated, loudly.** Every result says when it was cut and how to narrow the next
call. If you see a lot of truncation, the fix is usually a more specific question rather than a
bigger budget — though `DEVOPS_MCP_MAX_LINES` and `DEVOPS_MCP_MAX_BYTES` exist.

**`repo=` and `path=` are different things.** `repo=` chooses *which* repository; `path=` narrows
to a file *within* it. A project in a subdirectory is usually its own repo, so reach it with
`repo=<that directory>`. Passing it as `path=` searches the outer repo for a path it does not
track, and returns nothing — which reads like "no history" but is not.

Start with `--read-only` on anything you care about. Add write tools once you trust it.

---

## 8. Asking questions that get good answers

The quality of the diagnosis depends more on the question than on anything else you control.

**Say what you observed, not what you think is wrong.** *"The API returns 500 on /users/1"* beats
*"the database connection is broken"* — the second sends it chasing your hypothesis instead of the
evidence.

**Include the timeframe if there is one.** *"after my latest changes"* is the phrase that puts the
git tools in play, which is usually the fastest route to a root cause.

**Name the thing if you know it.** Container name, file path, endpoint. It saves several turns of
searching, and turns are tokens.

**Ask for one investigation at a time.** Two unrelated questions in one prompt produce a worse
answer to both.

**In Claude Code, add "use only the devops MCP tools."** Otherwise the agent may reach for its
built-in file tools, which is faster but tells you nothing about whether *these* servers work.

Good: *"The broken-backend container started returning 500s on /users/1 after my last few commits.
Find the cause and cite the commit."*

Weak: *"is my app ok?"*

---

## 9. When it goes wrong

| Symptom | What is actually happening |
|---|---|
| All five servers fail to connect | Wrong interpreter. `.mcp.json` uses `${DEVOPS_MCP_PYTHON:-python}`; that Python has no `devops_mcp` installed, so each server exits on import. |
| One server is missing its tools | You may be over the host's MCP tool limit. Six servers silently hid one server's tools entirely while still reporting "Connected". Unregister something you are not using. |
| `Docker daemon is not reachable` | Docker Desktop is not running. The Docker tools degrade to a readable error rather than crashing; everything else keeps working. |
| `Access denied: ... outside the allowed roots` | Working as designed. Widen `--roots` if the path is genuinely in scope. |
| Agent says "no history" for a subdirectory | Almost always `repo=` versus `path=`. See [section 7](#7-pointing-it-at-your-own-project). |
| `No ANTHROPIC_KEY found` | Agent host only. Claude Code needs no key. Copy `.env.example` to `.env`. |
| Write action declined without a prompt | Non-interactive session. There is no terminal to ask on, so it declines rather than proceeding. `--yes` overrides. |
| Everything is truncated | Ask something narrower. The budget is doing its job. |
| Tests skip | Expected. Docker-marked tests skip without a daemon, and one symlink test skips without Windows privileges. |

---

## 10. The two ways to run it

**Claude Code as the host.** Open this folder, approve the servers, ask in chat. No API key, and
you get the built-in permission UI for write actions. Best for day-to-day use and for trying the
servers out.

**The standalone agent.** `python -m devops_mcp.agent "..."`. Needs a key. Scriptable, prints its
own tool trace and token counts, and the approval gate is enforced by ~400 lines you can read.
Best when you want the safety model to be *visible* rather than taken on trust, or when you want
to run an investigation from CI or a script.

They share all five servers. Nothing is duplicated between them.

---

## Where to look next

- [README](README.md) — full tool reference, every flag and setting, the safety model in detail.
- `src/devops_mcp/safety.py` — the entire security boundary, in one file. Most of the 200 tests
  aim at it.
- `src/devops_mcp/agent/session.py` — the loop and the system prompt that shapes how the agent
  investigates.
- `scripts/make_demo.py` — how the planted bug is constructed, if you want to plant your own.

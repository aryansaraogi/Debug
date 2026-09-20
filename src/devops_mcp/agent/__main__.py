"""CLI entry point.

    python -m devops_mcp.agent "The broken-backend container is returning 500s. Why?"
    python -m devops_mcp.agent --roots demo/broken_app "What broke after recent changes?"
    python -m devops_mcp.agent --read-only "..."     # no write tools exist at all
    python -m devops_mcp.agent --check              # verify credentials and tool discovery
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import anyio

from devops_mcp import llm
from devops_mcp.agent.bridge import READ_ONLY_SERVERS, SERVERS, MCPBridge
from devops_mcp.agent.session import AgentSession, TurnEvent

# Kept narrow on purpose: a terminal that does not do colour still reads fine.
DIM, BOLD, YELLOW, RED, GREEN, RESET = "\033[2m", "\033[1m", "\033[33m", "\033[31m", "\033[32m", "\033[0m"


def _colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _colour() else text


def make_printer() -> Any:
    def printer(ev: TurnEvent) -> None:
        if ev.kind == "tool":
            print(f"  {_c('●', GREEN)} {_c(ev.name, BOLD)} {_c(ev.detail, DIM)}", flush=True)
        elif ev.kind == "error":
            print(f"  {_c('✗', RED)} {ev.name} {_c(ev.detail, DIM)}", flush=True)
        elif ev.kind == "denied":
            print(f"  {_c('⊘', YELLOW)} {ev.name} declined by operator", flush=True)
        elif ev.kind == "text" and ev.detail.strip():
            print(f"  {_c(ev.detail.strip(), DIM)}", flush=True)
    return printer


def make_approver(auto_yes: bool) -> Any:
    def approve(name: str, args: dict[str, Any]) -> bool:
        rendered = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "(no arguments)"
        print(f"\n  {_c('⚠ WRITE ACTION', YELLOW)} {_c(name, BOLD)}({rendered})")
        if auto_yes:
            print(f"  {_c('auto-approved (--yes)', DIM)}")
            return True
        if not sys.stdin.isatty():
            print(f"  {_c('declined: no terminal to ask on (pass --yes to allow)', DIM)}")
            return False
        try:
            answer = input("  Approve? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")
    return approve


async def run(args: argparse.Namespace) -> int:
    servers = READ_ONLY_SERVERS if args.read_only else tuple(SERVERS)
    if args.servers:
        servers = tuple(s.strip() for s in args.servers.split(",") if s.strip())

    env = dict(os.environ)
    if args.roots:
        resolved = [str(Path(p).expanduser().resolve()) for p in args.roots]
        env["DEVOPS_MCP_ROOTS"] = os.pathsep.join(resolved)

    async with MCPBridge(servers=servers, env=env) as bridge:
        writable = [n for n, t in bridge.tools.items() if t.requires_approval]
        print(
            f"{_c('devops agent', BOLD)}  model={llm.get_model()}  "
            f"{len(bridge.tools)} tools from {len(servers)} servers"
            + (f"  ({len(writable)} need approval)" if writable else "  (read-only)")
        )
        print(f"{_c('roots: ' + env.get('DEVOPS_MCP_ROOTS', os.getcwd()), DIM)}\n")

        if args.check:
            for name in sorted(bridge.tools):
                t = bridge.tools[name]
                flag = _c(" [approval]", YELLOW) if t.requires_approval else ""
                print(f"  {t.server:20} {name}{flag}")
            print(f"\n{llm.describe()}")
            return 0

        session = AgentSession(
            bridge=bridge,
            max_turns=args.max_turns,
            approve=make_approver(args.yes),
            on_event=make_printer(),
        )
        if args.model:
            session.model = args.model

        result = await session.run(args.question)

        print(f"\n{_c('─' * 60, DIM)}")
        print(result.answer)
        print(
            _c(
                f"\n[{result.turns} turns, {result.tool_calls} tool calls, "
                f"{result.input_tokens:,} in / {result.output_tokens:,} out tokens]",
                DIM,
            )
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m devops_mcp.agent",
        description="Investigate a local dev environment by asking a question in plain English.",
    )
    parser.add_argument("question", nargs="?", help="What you want investigated.")
    parser.add_argument("--roots", action="append", metavar="PATH",
                        help="Directory the tools may touch (repeatable). Sets DEVOPS_MCP_ROOTS.")
    parser.add_argument("--read-only", action="store_true",
                        help="Do not start devops-actions: the agent has no write tools at all.")
    parser.add_argument("--servers", metavar="A,B", help="Explicit server list (overrides --read-only).")
    parser.add_argument("--yes", action="store_true",
                        help="Auto-approve write actions. Think before using this.")
    parser.add_argument("--model", help=f"Override the model (default {llm.DEFAULT_MODEL}).")
    parser.add_argument("--max-turns", type=int, default=40, help="Safety stop (default 40).")
    parser.add_argument("--check", action="store_true",
                        help="List discovered tools and credential status, then exit.")
    args = parser.parse_args()

    if not args.question and not args.check:
        parser.error("give a question, or --check")

    # --check is a plumbing test: it must work before a key exists.
    if not args.check:
        try:
            llm.get_api_key()
        except llm.MissingAPIKey as exc:
            print(f"{_c('error:', RED)} {exc}", file=sys.stderr)
            return 2

    try:
        return anyio.run(run, args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

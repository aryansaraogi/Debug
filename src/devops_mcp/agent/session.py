"""The agent loop: ask Claude a question, let it drive the MCP tools, return a diagnosis.

A manual tool loop rather than the SDK's tool runner, because the tools are
discovered from the MCP servers at runtime and every write tool has to pass
through a human approval gate before it executes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from devops_mcp import llm
from devops_mcp.agent.bridge import MCPBridge

MAX_TOKENS = 32_000
DEFAULT_MAX_TURNS = 40

SYSTEM_PROMPT = """You are a DevOps debugging assistant investigating a local development environment.

You work only through the tools you have been given. You cannot see the machine except
through them, so gather evidence before concluding anything.

How to investigate:
- Start broad, then narrow. summarize_errors before read_log; list_containers before inspect_container.
- Every tool caps its own output and tells you when a result was truncated. When it does, follow the
  hint and make a narrower call rather than guessing at what was cut.
- When something broke "after recent changes", the git tools are usually the fastest route: git_log
  finds who last touched a file, git_show explains what that commit did. Mind the two parameters:
  `repo=` chooses WHICH repository, `path=` narrows to a file WITHIN it. A project sitting in a
  subdirectory is usually its own repository, so reach it with `repo=<that directory>` - passing it
  as `path=` searches the outer repo for a path it does not track and returns nothing.
- An empty or failed result is evidence about your query at least as often as about the system.
  Before concluding "there is no history" or "this is not tracked", try the obvious variant of the
  call - a different `repo=`, a wider `limit`, a parent directory. Do not build an argument on top
  of a single empty result.
- Correlate. A stack trace gives you a file and a line; read that file; then ask git why it looks
  that way. A diagnosis that names a root cause and the commit that introduced it beats one that
  only restates the error.

Write actions (restart, stop, start, rebuild) change the environment and each needs the operator's
approval. Diagnose first - restarting a container can destroy the evidence of why it failed. Propose
one action at a time, and say what you expect it to prove or fix.

Finish with a short diagnosis: what is broken, the evidence, the root cause, and the fix. Cite
files as path:line. If the evidence does not support a conclusion, say what you would need instead
of guessing."""


@dataclass
class TurnEvent:
    """Something worth showing the user as the agent works."""

    kind: str  # "text" | "tool" | "approval" | "denied" | "error"
    name: str = ""
    detail: str = ""


@dataclass
class AgentResult:
    answer: str
    turns: int
    tool_calls: int
    stop_reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0


def _render_args(args: dict[str, Any]) -> str:
    """Compact one-line rendering of tool arguments for the console."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = json.dumps(v) if not isinstance(v, str) else v
        parts.append(f"{k}={s if len(s) <= 60 else s[:57] + '...'}")
    return " ".join(parts)


@dataclass
class AgentSession:
    bridge: MCPBridge
    model: str = field(default_factory=llm.get_model)
    max_turns: int = DEFAULT_MAX_TURNS
    approve: Callable[[str, dict[str, Any]], bool] | None = None
    on_event: Callable[[TurnEvent], None] | None = None

    def _emit(self, kind: str, name: str = "", detail: str = "") -> None:
        if self.on_event:
            self.on_event(TurnEvent(kind=kind, name=name, detail=detail))

    def _decide(self, name: str, args: dict[str, Any]) -> bool:
        """Approval gate. Absent an approver, a tool that demands one is refused."""
        bound = self.bridge.get(name)
        if bound is None or not bound.requires_approval:
            return True
        if self.approve is None:
            return False
        return self.approve(name, args)

    async def run(self, question: str) -> AgentResult:
        client = llm.get_async_client()
        tools = self.bridge.anthropic_tools()
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

        turns = 0
        tool_calls = 0
        in_tok = out_tok = 0
        stop_reason: str | None = None

        while turns < self.max_turns:
            turns += 1
            async with client.messages.stream(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tools,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
            ) as stream:
                response = await stream.get_final_message()

            in_tok += response.usage.input_tokens
            out_tok += response.usage.output_tokens
            stop_reason = response.stop_reason

            if stop_reason == "refusal":
                detail = getattr(response.stop_details, "explanation", "") or "no explanation given"
                return AgentResult(
                    answer=f"The model declined this request ({detail}).",
                    turns=turns, tool_calls=tool_calls, stop_reason=stop_reason,
                    input_tokens=in_tok, output_tokens=out_tok,
                )

            # Append the whole content list: thinking blocks must be echoed back unchanged.
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            # Emit interstitial narration only. On the final turn the text IS the answer,
            # and the caller prints that - emitting it here too would print it twice.
            if tool_uses:
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        self._emit("text", detail=block.text)

            if not tool_uses:
                answer = "\n".join(b.text for b in response.content if b.type == "text").strip()
                if stop_reason == "max_tokens":
                    answer += "\n\n[stopped at the max_tokens limit; the answer may be cut off]"
                return AgentResult(
                    answer=answer, turns=turns, tool_calls=tool_calls, stop_reason=stop_reason,
                    input_tokens=in_tok, output_tokens=out_tok,
                )

            # All results for one assistant turn go back in a SINGLE user message.
            results: list[dict[str, Any]] = []
            for use in tool_uses:
                args = use.input if isinstance(use.input, dict) else {}
                if not self._decide(use.name, args):
                    self._emit("denied", name=use.name, detail=_render_args(args))
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": (
                            "The operator declined this action. Do not retry it. Continue with "
                            "read-only investigation, or explain what this action would have shown."
                        ),
                        "is_error": True,
                    })
                    continue

                self._emit("tool", name=use.name, detail=_render_args(args))
                text, is_error = await self.bridge.call(use.name, args)
                tool_calls += 1
                if is_error:
                    self._emit("error", name=use.name, detail=text.splitlines()[0] if text else "")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": text,
                    "is_error": is_error,
                })

            messages.append({"role": "user", "content": results})

        return AgentResult(
            answer=f"Stopped after {self.max_turns} turns without reaching a conclusion. "
                   "Raise --max-turns or ask a narrower question.",
            turns=turns, tool_calls=tool_calls, stop_reason="max_turns",
            input_tokens=in_tok, output_tokens=out_tok,
        )

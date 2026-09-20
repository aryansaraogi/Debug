"""Tests for the agent host: the MCP bridge, the approval gate and the tool loop.

The bridge tests spawn the real servers over stdio. The loop tests stub the
Anthropic client - no API key, no network, no spend.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from mcp.types import Tool

from devops_mcp.agent.bridge import READ_ONLY_SERVERS, SERVERS, BoundTool, MCPBridge
from devops_mcp.agent.session import AgentSession, TurnEvent, _render_args


# --------------------------------------------------------------------------- bridge (real servers)


@pytest.fixture
def bridge_env(monkeypatch):
    monkeypatch.delenv("DEVOPS_MCP_ROOTS", raising=False)
    return None


@pytest.mark.anyio
async def test_bridge_discovers_every_tool(bridge_env):
    async with MCPBridge(python=sys.executable) as bridge:
        # Assert on names, not a count: a count only tells you a number moved, never which tool
        # went missing, and it breaks on every legitimate addition.
        assert set(bridge.tools) == {
            "list_files", "read_file", "search_files",
            "git_status", "git_diff", "git_log", "git_show",
            "list_containers", "inspect_container", "get_container_logs", "probe_url",
            "read_log", "search_logs", "summarize_errors",
            "restart_container", "stop_container", "start_container", "rebuild_service",
        }
        assert {t.server for t in bridge.tools.values()} == set(SERVERS)


@pytest.mark.anyio
async def test_read_only_bridge_has_no_write_tools(bridge_env):
    """The capability boundary: not disabled write tools - absent ones."""
    async with MCPBridge(servers=READ_ONLY_SERVERS, python=sys.executable) as bridge:
        assert not [n for n, t in bridge.tools.items() if t.requires_approval]
        assert not [n for n, t in bridge.tools.items() if t.destructive]
        for absent in ("restart_container", "stop_container", "start_container", "rebuild_service"):
            assert absent not in bridge.tools
        # the read-only servers still contribute their full inventory, probe_url included
        assert {"list_files", "git_log", "list_containers", "probe_url", "summarize_errors"} <= set(bridge.tools)


@pytest.mark.anyio
async def test_action_tools_carry_approval_and_destructive_flags(bridge_env):
    async with MCPBridge(servers=("devops-actions",), python=sys.executable) as bridge:
        assert set(bridge.tools) == {"restart_container", "stop_container", "start_container", "rebuild_service"}
        for tool in bridge.tools.values():
            assert tool.requires_approval, tool.name
            assert tool.destructive, tool.name


@pytest.mark.anyio
async def test_anthropic_tool_definitions_are_well_formed(bridge_env):
    async with MCPBridge(servers=("devops-logs",), python=sys.executable) as bridge:
        defs = bridge.anthropic_tools()
        assert [d["name"] for d in defs] == sorted(d["name"] for d in defs)
        for d in defs:
            assert set(d) == {"name", "description", "input_schema"}
            assert d["description"]
            assert d["input_schema"]["type"] == "object"


@pytest.mark.anyio
async def test_bridge_executes_a_real_tool(bridge_env, monkeypatch):
    monkeypatch.setenv("DEVOPS_MCP_ROOTS", str(__import__("pathlib").Path.cwd()))
    import os

    async with MCPBridge(servers=("devops-logs",), python=sys.executable, env=dict(os.environ)) as bridge:
        text, is_error = await bridge.call(
            "summarize_errors", {"path": "fixtures/broken_app/logs/app.log", "max_groups": 3}
        )
        assert is_error is False
        assert "KeyError" in text


@pytest.mark.anyio
async def test_unknown_tool_is_an_error_not_a_crash(bridge_env):
    async with MCPBridge(servers=("devops-logs",), python=sys.executable) as bridge:
        text, is_error = await bridge.call("no_such_tool", {})
        assert is_error is True
        assert "No such tool" in text


# --------------------------------------------------------------------------- stub Anthropic client


class _Block:
    def __init__(self, type: str, **kw: Any) -> None:
        self.type = type
        self.__dict__.update(kw)


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 10
        self.output_tokens = 5


class _Response:
    def __init__(self, content: list[_Block], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()
        self.stop_details = None


class _Stream:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get_final_message(self) -> _Response:
        return self._response


class _Messages:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _Stream:
        # The session mutates one messages list across turns; snapshot it so each
        # recorded request reflects what was actually sent at that point.
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs["messages"])
        self.requests.append(snapshot)
        return _Stream(self._responses.pop(0))


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _Messages(responses)


def _tool(name: str, approval: bool = False) -> BoundTool:
    payload: dict[str, Any] = {
        "name": name,
        "description": f"does {name}",
        "inputSchema": {"type": "object", "properties": {}},
    }
    if approval:
        payload["_meta"] = {"anthropic/requiresUserInteraction": True}
    return BoundTool(server="stub", tool=Tool.model_validate(payload))


class FakeBridge:
    """Implements just the surface AgentSession uses."""

    def __init__(self, tools: dict[str, BoundTool], result: tuple[str, bool] = ("ok", False)) -> None:
        self.tools = tools
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [t.to_anthropic() for t in self.tools.values()]

    def get(self, name: str) -> BoundTool | None:
        return self.tools.get(name)

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, arguments))
        return self._result


@pytest.fixture
def stub(monkeypatch):
    def install(responses: list[_Response]) -> _Client:
        client = _Client(responses)
        monkeypatch.setattr("devops_mcp.llm.get_async_client", lambda: client)
        return client

    return install


# --------------------------------------------------------------------------- the loop


@pytest.mark.anyio
async def test_plain_answer_returns_without_calling_tools(stub):
    stub([_Response([_Block("text", text="Nothing is wrong.")], "end_turn")])
    bridge = FakeBridge({"read_log": _tool("read_log")})
    result = await AgentSession(bridge=bridge, model="m").run("status?")
    assert result.answer == "Nothing is wrong."
    assert result.tool_calls == 0
    assert bridge.calls == []


@pytest.mark.anyio
async def test_tool_result_is_fed_back_and_loop_continues(stub):
    client = stub([
        _Response([_Block("tool_use", id="t1", name="read_log", input={"path": "a.log"})], "tool_use"),
        _Response([_Block("text", text="Found it.")], "end_turn"),
    ])
    bridge = FakeBridge({"read_log": _tool("read_log")}, result=("line one", False))
    result = await AgentSession(bridge=bridge, model="m").run("why?")

    assert result.answer == "Found it."
    assert result.tool_calls == 1
    assert bridge.calls == [("read_log", {"path": "a.log"})]
    # second request carries the tool_result in a single user message
    followup = client.messages.requests[1]["messages"][-1]
    assert followup["role"] == "user"
    assert followup["content"][0]["type"] == "tool_result"
    assert followup["content"][0]["content"] == "line one"
    assert followup["content"][0]["is_error"] is False


@pytest.mark.anyio
async def test_parallel_tool_results_go_back_in_one_message(stub):
    client = stub([
        _Response(
            [
                _Block("tool_use", id="t1", name="read_log", input={}),
                _Block("tool_use", id="t2", name="read_log", input={}),
            ],
            "tool_use",
        ),
        _Response([_Block("text", text="done")], "end_turn"),
    ])
    bridge = FakeBridge({"read_log": _tool("read_log")})
    await AgentSession(bridge=bridge, model="m").run("q")

    followup = client.messages.requests[1]["messages"][-1]
    assert len(followup["content"]) == 2
    assert [b["tool_use_id"] for b in followup["content"]] == ["t1", "t2"]


@pytest.mark.anyio
async def test_write_tool_is_refused_when_no_approver_is_wired(stub):
    """Absent an approver, an approval-gated tool must not execute."""
    client = stub([
        _Response([_Block("tool_use", id="t1", name="restart_container", input={"name": "x"})], "tool_use"),
        _Response([_Block("text", text="ok, staying read-only")], "end_turn"),
    ])
    bridge = FakeBridge({"restart_container": _tool("restart_container", approval=True)})
    result = await AgentSession(bridge=bridge, model="m", approve=None).run("restart it")

    assert bridge.calls == []  # never executed
    assert result.tool_calls == 0
    block = client.messages.requests[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True
    assert "declined" in block["content"]


@pytest.mark.anyio
async def test_write_tool_runs_once_approved(stub):
    stub([
        _Response([_Block("tool_use", id="t1", name="restart_container", input={"name": "x"})], "tool_use"),
        _Response([_Block("text", text="restarted")], "end_turn"),
    ])
    bridge = FakeBridge({"restart_container": _tool("restart_container", approval=True)})
    asked: list[str] = []

    def approve(name: str, args: dict[str, Any]) -> bool:
        asked.append(name)
        return True

    result = await AgentSession(bridge=bridge, model="m", approve=approve).run("restart it")
    assert asked == ["restart_container"]
    assert bridge.calls == [("restart_container", {"name": "x"})]
    assert result.tool_calls == 1


@pytest.mark.anyio
async def test_read_only_tool_never_prompts(stub):
    stub([
        _Response([_Block("tool_use", id="t1", name="read_log", input={})], "tool_use"),
        _Response([_Block("text", text="done")], "end_turn"),
    ])
    bridge = FakeBridge({"read_log": _tool("read_log")})
    asked: list[str] = []
    await AgentSession(bridge=bridge, model="m", approve=lambda n, a: asked.append(n) or True).run("q")
    assert asked == []


@pytest.mark.anyio
async def test_tool_error_is_reported_not_raised(stub):
    client = stub([
        _Response([_Block("tool_use", id="t1", name="read_log", input={})], "tool_use"),
        _Response([_Block("text", text="handled")], "end_turn"),
    ])
    bridge = FakeBridge({"read_log": _tool("read_log")}, result=("boom", True))
    result = await AgentSession(bridge=bridge, model="m").run("q")
    assert result.answer == "handled"
    assert client.messages.requests[1]["messages"][-1]["content"][0]["is_error"] is True


@pytest.mark.anyio
async def test_thinking_blocks_are_echoed_back_unchanged(stub):
    """Required when continuing a turn on the same model."""
    thinking = _Block("thinking", thinking="hmm")
    client = stub([
        _Response([thinking, _Block("tool_use", id="t1", name="read_log", input={})], "tool_use"),
        _Response([_Block("text", text="done")], "end_turn"),
    ])
    bridge = FakeBridge({"read_log": _tool("read_log")})
    await AgentSession(bridge=bridge, model="m").run("q")

    assistant_turn = client.messages.requests[1]["messages"][1]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["content"][0] is thinking


@pytest.mark.anyio
async def test_refusal_is_surfaced(stub):
    stub([_Response([], "refusal")])
    bridge = FakeBridge({"read_log": _tool("read_log")})
    result = await AgentSession(bridge=bridge, model="m").run("q")
    assert result.stop_reason == "refusal"
    assert "declined" in result.answer


@pytest.mark.anyio
async def test_max_turns_stops_a_runaway_loop(stub):
    stub([_Response([_Block("tool_use", id=f"t{i}", name="read_log", input={})], "tool_use") for i in range(5)])
    bridge = FakeBridge({"read_log": _tool("read_log")})
    result = await AgentSession(bridge=bridge, model="m", max_turns=3).run("q")
    assert result.stop_reason == "max_turns"
    assert result.turns == 3


@pytest.mark.anyio
async def test_request_carries_model_tools_and_thinking(stub):
    client = stub([_Response([_Block("text", text="hi")], "end_turn")])
    bridge = FakeBridge({"read_log": _tool("read_log")})
    await AgentSession(bridge=bridge, model="claude-opus-5").run("q")

    req = client.messages.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"] == {"effort": "high"}
    assert [t["name"] for t in req["tools"]] == ["read_log"]


# --------------------------------------------------------------------------- misc


def test_render_args_truncates_long_values():
    out = _render_args({"path": "x" * 200})
    assert out.endswith("...")
    assert len(out) < 80


def test_render_args_is_empty_for_no_arguments():
    assert _render_args({}) == ""


@pytest.mark.anyio
async def test_final_answer_is_not_also_emitted_as_an_event(stub):
    """The caller prints result.answer; emitting it as text too would double-print it."""
    stub([
        _Response(
            [_Block("text", text="Looking into it."), _Block("tool_use", id="t1", name="read_log", input={})],
            "tool_use",
        ),
        _Response([_Block("text", text="THE DIAGNOSIS")], "end_turn"),
    ])
    bridge = FakeBridge({"read_log": _tool("read_log")})
    events: list[TurnEvent] = []
    result = await AgentSession(
        bridge=bridge, model="m", on_event=events.append
    ).run("q")

    texts = [e.detail for e in events if e.kind == "text"]
    assert texts == ["Looking into it."]        # interstitial narration kept
    assert "THE DIAGNOSIS" not in texts         # final answer emitted once, by the caller
    assert result.answer == "THE DIAGNOSIS"

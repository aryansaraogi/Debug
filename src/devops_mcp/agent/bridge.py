"""Bridge between the local MCP servers and the Anthropic Messages API.

Spawns each devops-* server as a stdio subprocess, aggregates their tool
definitions into the shape `client.messages.create(tools=...)` expects, and
routes tool calls back to whichever server owns the name.

The API's built-in MCP connector only speaks to remote URL servers; these are
local stdio processes, so the bridge is the connector.
"""

from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.types import Tool

# Server name -> module to run. Order matters only for display.
SERVERS: dict[str, str] = {
    "devops-filesystem": "devops_mcp.servers.filesystem",
    "devops-git": "devops_mcp.servers.git",
    "devops-docker": "devops_mcp.servers.docker",
    "devops-logs": "devops_mcp.servers.logs",
    "devops-actions": "devops_mcp.servers.actions",
}
READ_ONLY_SERVERS = tuple(n for n in SERVERS if n != "devops-actions")

APPROVAL_KEY = "anthropic/requiresUserInteraction"
CALL_TIMEOUT = 900.0  # rebuild_service can legitimately take minutes


@dataclass
class BoundTool:
    """One MCP tool, plus which server serves it."""

    server: str
    tool: Tool

    @property
    def name(self) -> str:
        return self.tool.name

    @property
    def requires_approval(self) -> bool:
        return bool((self.tool.meta or {}).get(APPROVAL_KEY))

    @property
    def destructive(self) -> bool:
        ann = self.tool.annotations
        return bool(ann and ann.destructive_hint)

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.tool.name,
            "description": (self.tool.description or "").strip(),
            "input_schema": self.tool.input_schema,
        }


@dataclass
class MCPBridge:
    """Owns the server subprocesses for the lifetime of one agent run."""

    servers: tuple[str, ...] = tuple(SERVERS)
    python: str = field(default_factory=lambda: sys.executable)
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))

    _stack: AsyncExitStack | None = field(default=None, init=False, repr=False)
    _clients: dict[str, Client] = field(default_factory=dict, init=False, repr=False)
    tools: dict[str, BoundTool] = field(default_factory=dict, init=False, repr=False)

    async def __aenter__(self) -> "MCPBridge":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for name in self.servers:
            module = SERVERS.get(name)
            if module is None:
                raise ValueError(f"Unknown server {name!r}. Known: {', '.join(SERVERS)}")
            params = StdioServerParameters(
                command=self.python,
                args=["-m", module],
                # The default stdio environment is stripped down; pass ours through
                # so DEVOPS_MCP_ROOTS and the action guards actually reach the server.
                env=self.env,
            )
            client = await self._stack.enter_async_context(Client(params, read_timeout_seconds=CALL_TIMEOUT))
            self._clients[name] = client
            for tool in (await client.list_tools()).tools:
                if tool.name in self.tools:
                    existing = self.tools[tool.name].server
                    raise ValueError(f"Tool name collision: {tool.name!r} served by both {existing} and {name}.")
                self.tools[tool.name] = BoundTool(server=name, tool=tool)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self._stack is not None
        await self._stack.__aexit__(*exc)
        self._stack = None
        self._clients.clear()
        self.tools.clear()

    # ----------------------------------------------------------------- tools

    def anthropic_tools(self) -> list[dict[str, Any]]:
        """Tool definitions for the Messages API, in a stable order so the prompt cache holds."""
        return [self.tools[n].to_anthropic() for n in sorted(self.tools)]

    def get(self, name: str) -> BoundTool | None:
        return self.tools.get(name)

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (text, is_error); never raises for tool-level failures."""
        bound = self.tools.get(name)
        if bound is None:
            return (f"No such tool {name!r}. Available: {', '.join(sorted(self.tools))}", True)
        client = self._clients[bound.server]
        try:
            result = await client.call_tool(name, arguments)
        except Exception as exc:  # transport died, timeout, validation error
            return (f"{type(exc).__name__} calling {name}: {exc}", True)

        parts = [block.text for block in (result.content or []) if getattr(block, "type", None) == "text"]
        text = "\n".join(p for p in parts if p).strip()
        return (text or "(tool returned no output)", bool(result.is_error))

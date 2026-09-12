"""Diagnostic probe: which tool attribute makes an MCP host hide a tool?

Exposes four trivial tools covering the 2x2 of (destructive annotation) x (requiresUserInteraction
meta). After a restart, whichever tools appear in the model's tool list identifies the trigger:

    probe_plain        readOnly,    no meta     -> baseline; must always appear
    probe_destructive  destructive, no meta     -> appears? then annotations are not the cause
    probe_meta         readOnly,    meta        -> appears? then the meta flag is not the cause
    probe_both         destructive, meta        -> same shape as the real actions tools

Register temporarily in .mcp.json as "devops-probe", restart, then ask which probe_* tools exist.
Delete the entry when done. Nothing here touches the system.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
NEEDS_APPROVAL = {"anthropic/requiresUserInteraction": True}

mcp = MCPServer(
    "devops-probe",
    instructions="Diagnostic probe server. Each tool just echoes a string; none of them do anything.",
)


@mcp.tool(annotations=READ_ONLY)
def probe_plain() -> str:
    """Baseline probe: read-only, no approval metadata. Should always be visible."""
    return "probe_plain: readOnly=True, meta=none"


@mcp.tool(annotations=DESTRUCTIVE)
def probe_destructive() -> str:
    """Probe: destructive annotation only, no approval metadata."""
    return "probe_destructive: destructiveHint=True, meta=none"


@mcp.tool(annotations=READ_ONLY, meta=NEEDS_APPROVAL)
def probe_meta() -> str:
    """Probe: approval metadata only, read-only annotation."""
    return "probe_meta: readOnly=True, meta=requiresUserInteraction"


@mcp.tool(annotations=DESTRUCTIVE, meta=NEEDS_APPROVAL)
def probe_both() -> str:
    """Probe: destructive annotation AND approval metadata. Same shape as the real action tools."""
    return "probe_both: destructiveHint=True, meta=requiresUserInteraction"


if __name__ == "__main__":
    mcp.run()

"""Anthropic SDK credentials and client construction for the agent host.

The five MCP servers never import this module - they have no idea an LLM exists.
Only the agent that drives them does.

Key resolution, first match wins:
  1. ANTHROPIC_KEY   from the process environment
  2. ANTHROPIC_KEY   from the project's .env file (gitignored)

ANTHROPIC_KEY is deliberately NOT the SDK's default variable (ANTHROPIC_API_KEY),
so the key is always passed to the client explicitly rather than picked up
implicitly. That means a stray ANTHROPIC_API_KEY in the shell cannot silently
take over which account gets billed.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

KEY_VAR = "ANTHROPIC_KEY"
DEFAULT_MODEL = "claude-opus-5"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class MissingAPIKey(RuntimeError):
    """Raised when no Anthropic credential can be found."""


def load_env() -> None:
    """Load the project's .env into os.environ. Existing env vars win."""
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:  # agent extra not installed
        return
    if ENV_FILE.is_file():
        load_dotenv(ENV_FILE, override=False)


def get_api_key() -> str:
    """Return the Anthropic API key, or raise with instructions on how to set it."""
    load_env()
    key = (os.environ.get(KEY_VAR) or "").strip()
    if not key:
        raise MissingAPIKey(
            f"No {KEY_VAR} found.\n"
            f"  Add it to {ENV_FILE} (gitignored):  {KEY_VAR}=sk-ant-...\n"
            f"  or export it:                       export {KEY_VAR}=sk-ant-...\n"
            f"  Template: .env.example"
        )
    return key


def get_model() -> str:
    """Model id for the agent. Override with DEVOPS_MCP_MODEL."""
    load_env()
    return (os.environ.get("DEVOPS_MCP_MODEL") or "").strip() or DEFAULT_MODEL


@lru_cache(maxsize=1)
def get_client():
    """Construct the Anthropic client with the key passed explicitly."""
    import anthropic

    return anthropic.Anthropic(api_key=get_api_key())


@lru_cache(maxsize=1)
def get_async_client():
    """Async client, used by the agent loop (which is async because MCP is)."""
    import anthropic

    return anthropic.AsyncAnthropic(api_key=get_api_key())


def describe() -> str:
    """One-line credential status, safe to print - never reveals the key."""
    load_env()
    raw = (os.environ.get(KEY_VAR) or "").strip()
    if not raw:
        return f"{KEY_VAR}: NOT SET (looked in the environment and {ENV_FILE})"
    source = "environment" if ENV_FILE.is_file() is False else f"environment or {ENV_FILE.name}"
    return f"{KEY_VAR}: set, {len(raw)} chars, ends ...{raw[-4:]} (from {source}); model={get_model()}"

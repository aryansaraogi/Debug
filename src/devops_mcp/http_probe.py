"""HTTP probing for local services, with the guardrails an agent-driven tool needs.

Why this exists: the agent could already diagnose a broken service and rebuild it, but had no way
to check whether the fix worked. A human had to curl the endpoint. This closes that loop.

Two restrictions keep it from becoming a hole in the rest of the safety model:

  * **Safe methods only.** GET and HEAD. An unrestricted HTTP client would let the agent mutate
    state through a POST and bypass the approval gate on `devops-actions` entirely.
  * **Local targets only.** Hosts must resolve to loopback or private addresses, so the tool
    cannot reach the public internet or exfiltrate anything. `DEVOPS_MCP_HTTP_HOSTS` extends the
    allowlist when you genuinely need a public host.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp.safety import is_binary, redact, truncate

SAFE_METHODS = ("GET", "HEAD")
MAX_BODY_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 120.0
# Headers worth showing an agent diagnosing a service. Others are noise.
INTERESTING_HEADERS = (
    "content-type",
    "content-length",
    "location",
    "server",
    "retry-after",
    "x-request-id",
    "x-correlation-id",
    "cache-control",
    "www-authenticate",
)


@dataclass
class HttpResult:
    url: str
    method: str
    status: int | None
    reason: str
    elapsed_ms: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    body_truncated: bool = False
    error: str | None = None
    hint: str | None = None


# --------------------------------------------------------------------------- target validation


def _extra_allowed_hosts() -> set[str]:
    raw = os.environ.get("DEVOPS_MCP_HTTP_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_local_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _check_target(url: str) -> str:
    """Reject anything that isn't a safe, local http(s) URL."""
    url = url.strip()
    if not url:
        raise ToolError("url must not be empty.")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ToolError(f"Cannot parse url {url!r}: {exc}") from exc

    if parsed.scheme not in ("http", "https"):
        raise ToolError(f"Only http and https are supported, not {parsed.scheme or '(none)'!r}.")
    host = parsed.hostname
    if not host:
        raise ToolError(f"No host in url {url!r}. Did you mean http://localhost{url}?")

    if host.lower() in _extra_allowed_hosts():
        return url

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ToolError(f"Cannot resolve host {host!r}: {exc}") from exc

    addresses = {info[4][0] for info in infos}
    outside = sorted(a for a in addresses if not _is_local_address(a))
    if outside:
        raise ToolError(
            f"Refusing to probe {host!r}: it resolves to {', '.join(outside)}, which is not a "
            "loopback or private address. This tool only reaches local services. Add the host to "
            "DEVOPS_MCP_HTTP_HOSTS if you really need it."
        )
    return url


# --------------------------------------------------------------------------- probing


def _diagnose(status: int | None, error: str | None, url: str) -> str | None:
    """Turn the raw outcome into the next thing worth checking."""
    if error:
        low = error.lower()
        if "refused" in low:
            return (
                "Nothing is listening. Check the container is running (list_containers) and that "
                "the port is published."
            )
        if "timed out" in low or "timeout" in low:
            return "The service accepted the connection but did not answer. Check its logs for a hang or deadlock."
        if "name or service not known" in low or "resolve" in low:
            return "Hostname did not resolve. From the host, use localhost; service names only resolve inside the compose network."
        return None
    if status is None:
        return None
    if status >= 500:
        return "Server error. get_container_logs on the serving container should hold the traceback."
    if status == 404:
        return "Route not found. Check the path, and search_files for the route definition."
    if status in (401, 403):
        return "Authentication or authorization rejected the request, so the service itself is up."
    if 300 <= status < 400:
        return "Redirect. Follow the location header if you want the final response."
    return None


def probe(
    url: str,
    method: str = "GET",
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> HttpResult:
    """Issue one safe HTTP request against a local service and report what came back."""
    method = method.upper().strip()
    if method not in SAFE_METHODS:
        raise ToolError(
            f"{method!r} is not allowed. Only {' and '.join(SAFE_METHODS)} are permitted, so this "
            "tool cannot change server state. Use the devops-actions tools for changes."
        )
    if timeout <= 0 or timeout > MAX_TIMEOUT:
        raise ToolError(f"timeout must be between 0 and {MAX_TIMEOUT:.0f} seconds.")
    url = _check_target(url)

    request = urllib.request.Request(url, method=method)
    request.add_header("User-Agent", "devops-mcp/probe")
    for name, value in (headers or {}).items():
        request.add_header(name, value)

    started = time.monotonic()
    status: int | None = None
    reason = ""
    raw = b""
    resp_headers: dict[str, str] = {}
    error: str | None = None

    try:
        # No redirect following: a redirect is diagnostic information, not a detour.
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            reason = response.reason or ""
            resp_headers = dict(response.headers.items())
            if method != "HEAD":
                raw = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is a perfectly good answer, not a failure.
        status = exc.code
        reason = exc.reason or ""
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            raw = exc.read(MAX_BODY_BYTES + 1)
        except Exception:  # noqa: BLE001 - body is best-effort on an error response
            raw = b""
    except urllib.error.URLError as exc:
        error = str(exc.reason)
    except (TimeoutError, socket.timeout):
        error = f"timed out after {timeout:.0f}s"
    except OSError as exc:
        error = str(exc)

    elapsed_ms = int((time.monotonic() - started) * 1000)

    truncated = len(raw) > MAX_BODY_BYTES
    raw = raw[:MAX_BODY_BYTES]
    if raw and is_binary(raw[:8192]):
        body = f"[binary response, {len(raw)} bytes]"
    else:
        body, note = truncate(redact(raw.decode("utf-8", errors="replace")))
        if note:
            truncated = True
            body += f"\n{note}"

    return HttpResult(
        url=url,
        method=method,
        status=status,
        reason=reason,
        elapsed_ms=elapsed_ms,
        headers={k: redact(v) for k, v in resp_headers.items() if k.lower() in INTERESTING_HEADERS},
        body=body,
        body_truncated=truncated,
        error=error,
        hint=_diagnose(status, error, url),
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report redirects instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None

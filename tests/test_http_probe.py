from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devops_mcp import http_probe as hp
from devops_mcp.safety import REDACTED


class _Handler(BaseHTTPRequestHandler):
    """Routes covering the response shapes the probe has to make sense of."""

    def log_message(self, *args):  # silence the test run
        pass

    def _send(self, status: int, body: bytes = b"", extra: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ok":
            self._send(200, b'{"status":"ok"}')
        elif self.path == "/boom":
            self._send(500, b"Internal Server Error: KeyError 'email'")
        elif self.path == "/missing":
            self._send(404, b"not found")
        elif self.path == "/secret":
            self._send(200, b'{"DATABASE_URL":"postgres://app:hunter2@db/app"}')
        elif self.path == "/redirect":
            self._send(302, b"", {"Location": "/ok"})
        elif self.path == "/big":
            self._send(200, b"x" * (hp.MAX_BODY_BYTES + 5000))
        elif self.path == "/binary":
            self._send(200, bytes(range(256)) * 40)
        elif self.path == "/authed":
            self._send(401, b"nope", {"WWW-Authenticate": "Bearer realm=api"})
        else:
            self._send(404, b"no route")

    do_HEAD = do_GET


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


# --------------------------------------------------------------------------- happy paths


def test_probe_ok(server):
    r = hp.probe(f"{server}/ok")
    assert (r.status, r.method) == (200, "GET")
    assert '"status":"ok"' in r.body
    assert r.error is None and r.hint is None
    assert r.headers["Content-Type"] == "application/json"
    assert r.elapsed_ms >= 0


def test_probe_head_has_no_body(server):
    r = hp.probe(f"{server}/ok", method="HEAD")
    assert r.status == 200 and r.body == ""


def test_server_error_is_a_result_not_an_exception(server):
    r = hp.probe(f"{server}/boom")
    assert r.status == 500
    assert "KeyError" in r.body
    assert r.hint and "get_container_logs" in r.hint


def test_404_hint(server):
    assert "search_files" in hp.probe(f"{server}/missing").hint


def test_401_hint_says_service_is_up(server):
    r = hp.probe(f"{server}/authed")
    assert r.status == 401
    assert "service itself is up" in r.hint
    assert "WWW-Authenticate" in r.headers


def test_redirect_reported_not_followed(server):
    r = hp.probe(f"{server}/redirect")
    assert r.status == 302
    assert r.headers.get("Location") == "/ok"
    assert r.hint and "Redirect" in r.hint


# --------------------------------------------------------------------------- output discipline


def test_body_is_redacted(server):
    r = hp.probe(f"{server}/secret")
    assert "hunter2" not in r.body
    assert REDACTED in r.body


def test_large_body_truncated(server):
    r = hp.probe(f"{server}/big")
    assert r.body_truncated is True
    assert len(r.body.encode()) <= hp.MAX_BODY_BYTES + 200


def test_binary_body_not_dumped(server):
    r = hp.probe(f"{server}/binary")
    assert r.body.startswith("[binary response,")


def test_only_interesting_headers_kept(server):
    r = hp.probe(f"{server}/ok")
    assert "Server" not in r.headers or r.headers.get("Server")
    assert all(k.lower() in hp.INTERESTING_HEADERS for k in r.headers)


# --------------------------------------------------------------------------- guardrails


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "post"])
def test_unsafe_methods_refused(server, method):
    with pytest.raises(ToolError, match="not allowed"):
        hp.probe(f"{server}/ok", method=method)


@pytest.mark.parametrize("url", ["ftp://localhost/x", "file:///etc/passwd", "gopher://localhost"])
def test_non_http_schemes_refused(url):
    with pytest.raises(ToolError, match="Only http and https"):
        hp.probe(url)


def test_public_host_refused(monkeypatch):
    monkeypatch.delenv("DEVOPS_MCP_HTTP_HOSTS", raising=False)
    monkeypatch.setattr(hp.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))])
    with pytest.raises(ToolError, match="not a loopback or private address"):
        hp.probe("http://example.com/")


def test_public_host_allowed_when_explicitly_listed(monkeypatch, server):
    monkeypatch.setenv("DEVOPS_MCP_HTTP_HOSTS", "example.com")
    # allowlisted hosts skip resolution entirely; point it at the test server to prove it proceeds
    assert hp._check_target("http://example.com/x") == "http://example.com/x"


def test_private_addresses_allowed():
    assert hp._is_local_address("127.0.0.1")
    assert hp._is_local_address("10.1.2.3")
    assert hp._is_local_address("192.168.0.9")
    assert hp._is_local_address("::1")
    assert not hp._is_local_address("8.8.8.8")


def test_empty_and_hostless_urls(monkeypatch):
    with pytest.raises(ToolError, match="must not be empty"):
        hp.probe("   ")
    with pytest.raises(ToolError, match="No host in url"):
        hp.probe("http:///users/1")


def test_bad_timeout(server):
    with pytest.raises(ToolError, match="timeout must be"):
        hp.probe(f"{server}/ok", timeout=0)
    with pytest.raises(ToolError, match="timeout must be"):
        hp.probe(f"{server}/ok", timeout=9999)


def test_connection_refused_hint():
    # port 1 on loopback: nothing listens there
    r = hp.probe("http://127.0.0.1:1/", timeout=5)
    assert r.status is None and r.error
    assert r.hint and "Nothing is listening" in r.hint


def test_unresolvable_host_is_tool_error():
    with pytest.raises(ToolError, match="Cannot resolve host"):
        hp.probe("http://no-such-host-devops-mcp-test.invalid/")

"""Smoke tests for utils/web_viewer.py — end-to-end HTTP responses."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _read(url: str, timeout: float = 2.0):
    with urlopen(Request(url), timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


@pytest.fixture
def graph_with_data(tmp_path: Path, monkeypatch):
    """Spin up a fresh graph + viewer on a high port for each test."""
    import utils.execution_graph as eg
    import utils.web_viewer as wv

    monkeypatch.setattr(eg, "_GRAPH", eg.ExecutionGraph(tmp_path / "g.db"))
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)

    # Force a fresh viewer instance bound to a random-ish port
    monkeypatch.setattr(wv, "_SERVER", None)
    monkeypatch.setattr(wv, "_SERVER_THREAD", None)
    monkeypatch.setattr(wv, "_SERVER_PORT", None)
    monkeypatch.setattr(wv, "_DISABLED", False)
    monkeypatch.setattr(wv, "_AUTO_OPEN", False)  # don't open a tab during tests
    monkeypatch.setattr(wv, "_DEFAULT_PORT", 18765)

    url = wv.start_web_viewer()
    assert url is not None
    yield url, eg.get_graph()
    wv.stop_web_viewer()


def test_index_page_served(graph_with_data):
    url, _ = graph_with_data
    status, body = _read(url)
    assert status == 200
    assert "<title>PAL Execution Graph" in body


def test_health_endpoint(graph_with_data):
    url, _ = graph_with_data
    status, body = _read(url + "health")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def test_runs_endpoint_returns_list(graph_with_data):
    url, graph = graph_with_data
    a = graph.start_run("chat", args={"prompt": "hi"})
    graph.complete_run(a, cost_tier="oauth_free")
    b = graph.start_run("clink", args={"cli_name": "codex"})
    graph.fail_run(b, error="boom")

    status, body = _read(url + "runs")
    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["count"] == 2
    tools = {r["tool_name"] for r in payload["runs"]}
    assert tools == {"chat", "clink"}


def test_runs_endpoint_filters(graph_with_data):
    url, graph = graph_with_data
    a = graph.start_run("chat", args={})
    graph.complete_run(a)
    b = graph.start_run("clink", args={})
    graph.fail_run(b, error="x")
    c = graph.start_run("panel", args={})
    graph.complete_run(c)

    status, body = _read(url + "runs?status=failed")
    assert status == 200
    payload = json.loads(body)
    assert payload["count"] == 1
    assert payload["runs"][0]["tool_name"] == "clink"


def test_run_tree_endpoint_with_descendants(graph_with_data):
    url, graph = graph_with_data
    parent = graph.start_run("panel", args={"prompt": "x"})
    child = graph.start_run("clink", parent_run_id=parent, args={})
    graph.complete_run(child, cost_tier="oauth_free")
    graph.complete_run(parent)

    status, body = _read(url + f"runs/{parent}/tree")
    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["tree"]["run_id"] == parent
    assert len(payload["tree"]["children"]) == 1
    assert payload["cost_tier_rollup"].get("oauth_free") == 1


def test_invalid_limit_returns_400_not_crash(graph_with_data):
    """Pre-fix: int('abc') ran outside try/except → ValueError → daemon
    thread crashed. Now: 400 with a clear error message."""
    from urllib.error import HTTPError

    url, _ = graph_with_data
    try:
        _read(url + "runs?limit=abc")
        assert False, "expected 400"
    except HTTPError as exc:
        assert exc.code == 400
        body = exc.read().decode("utf-8")
        assert "invalid 'limit'" in body


def test_negative_limit_rejected(graph_with_data):
    """Pre-fix: ?limit=-1 → SQL `LIMIT -1` = unbounded dump of entire table."""
    from urllib.error import HTTPError

    url, _ = graph_with_data
    try:
        _read(url + "runs?limit=-1")
        assert False, "expected 400"
    except HTTPError as exc:
        assert exc.code == 400


def test_oversized_limit_rejected(graph_with_data):
    """Cap upper limit to keep one curl from dumping arbitrary rows."""
    from urllib.error import HTTPError

    url, _ = graph_with_data
    try:
        _read(url + "runs?limit=99999")
        assert False, "expected 400"
    except HTTPError as exc:
        assert exc.code == 400


def test_invalid_status_filter_rejected(graph_with_data):
    """Status filter is whitelisted to known states."""
    from urllib.error import HTTPError

    url, _ = graph_with_data
    try:
        _read(url + "runs?status=evil")
        assert False, "expected 400"
    except HTTPError as exc:
        assert exc.code == 400


def test_html_escape_in_run_label_does_not_xss(graph_with_data):
    """A panelist label containing <script> must NOT be rendered raw.
    The page escapes server-supplied strings client-side; we verify the
    JSON layer surfaces the raw label and the escaping logic is in the
    embedded HTML."""
    import json

    url, graph = graph_with_data
    rid = graph.start_run("chat", label="<img src=x onerror=alert(1)>")
    graph.complete_run(rid)

    # The raw label is in the JSON response (clients should escape on render)
    _, body = _read(url + "runs")
    payload = json.loads(body)
    raw_label = payload["runs"][0]["label"]
    assert "<img" in raw_label  # raw in JSON

    # The HTML page must contain the escapeHtml function and use it
    _, page = _read(url)
    assert "function escapeHtml" in page
    assert "function safeClass" in page
    # statusBadge / renderRunRow / renderTreeNode must call escapeHtml
    assert page.count("escapeHtml(") >= 6  # multiple sites


def test_unknown_run_returns_404(graph_with_data):
    url, _ = graph_with_data
    try:
        _read(url + "runs/deadbeef")
        assert False, "expected 404"
    except Exception as exc:
        # urllib raises HTTPError for non-2xx
        assert "404" in str(exc) or "Not Found" in str(exc)


def test_web_url_tool_returns_running_url(graph_with_data):
    """The MCP tool surfaces the URL Claude Code would show the user."""
    import asyncio

    from tools.web_url import WebUrlTool

    async def go():
        return await WebUrlTool().execute({})

    out = asyncio.run(go())
    payload = json.loads(out[0].text)
    assert payload["status"] == "ok"
    assert payload["url"].startswith("http://")


def test_viewer_lazy_starts_on_first_tool_call(tmp_path, monkeypatch):
    """Pre-fix: viewer started in main() at MCP server boot — popped a tab
    on every Claude Code launch even if the user never used PAL.
    Now: viewer starts only when execute_tool fires its first dispatch.
    """
    import utils.execution_graph as eg
    import utils.web_viewer as wv

    monkeypatch.setattr(eg, "_GRAPH", eg.ExecutionGraph(tmp_path / "g.db"))
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)
    monkeypatch.setattr(wv, "_SERVER", None)
    monkeypatch.setattr(wv, "_SERVER_THREAD", None)
    monkeypatch.setattr(wv, "_SERVER_PORT", None)
    monkeypatch.setattr(wv, "_DISABLED", False)
    monkeypatch.setattr(wv, "_AUTO_OPEN", False)
    monkeypatch.setattr(wv, "_DEFAULT_PORT", 18888)

    # Before any tool call: viewer not running
    assert wv.get_server_url() is None

    # First dispatch lazy-starts it
    import asyncio
    import server

    asyncio.run(server.execute_tool("version", {}))

    # Now running
    assert wv.get_server_url() is not None
    assert wv.get_server_url().startswith("http://")
    wv.stop_web_viewer()


def test_settings_endpoint_returns_snapshot(graph_with_data):
    """GET /api/settings returns the live + restart-required env vars,
    provider key presence, OAuth status, viewer info, graph version."""
    url, _ = graph_with_data
    status, body = _read(url + "api/settings")
    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    s = payload["settings"]
    assert "live" in s and "live_keys" in s
    assert "PAL_OPENAI_STREAM" in s["live"]
    assert "PAL_GEMINI_STREAM" in s["live"]
    assert "PAL_MULTIAUDIT_JUDGE" in s["live"]
    assert "restart_required" in s
    assert "PAL_MAX_CONCURRENT_API" in s["restart_required"]
    assert "provider_keys" in s
    assert "oauth_clis" in s
    assert "viewer" in s and s["viewer"]["url"] == url.rstrip("/") + "/"
    assert "graph" in s and s["graph"]["version"] is not None


def test_settings_post_mutates_whitelist(graph_with_data):
    """POST /api/settings with a whitelisted key updates os.environ
    immediately so per-call lookups see the new value."""
    import os
    from urllib.request import Request, urlopen

    url, _ = graph_with_data
    body = json.dumps({"key": "PAL_MULTIAUDIT_JUDGE", "value": "claude"}).encode()
    req = Request(url + "api/settings", data=body, method="POST",
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=2.0) as r:
        out = json.loads(r.read())
    assert out["status"] == "ok"
    assert os.environ.get("PAL_MULTIAUDIT_JUDGE") == "claude"
    # Cleanup so other tests don't see the mutation.
    os.environ.pop("PAL_MULTIAUDIT_JUDGE", None)


def test_settings_post_rejects_off_whitelist(graph_with_data):
    """A POST for a non-whitelisted key returns 400 and does not
    touch os.environ. Prevents arbitrary env mutation via the viewer."""
    import os
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    url, _ = graph_with_data
    body = json.dumps({"key": "OPENAI_API_KEY", "value": "leaked"}).encode()
    req = Request(url + "api/settings", data=body, method="POST",
                  headers={"Content-Type": "application/json"})
    try:
        urlopen(req, timeout=2.0)
        raised = False
    except HTTPError as exc:
        raised = True
        assert exc.code == 400
    assert raised, "off-whitelist mutation must be rejected"
    assert os.environ.get("OPENAI_API_KEY") != "leaked"


def test_settings_post_rejects_non_json_content_type(graph_with_data):
    """CSRF defense: simple-request CSRF (HTML form / no-cors fetch with
    text/plain) must be rejected. Settings POST requires explicit
    application/json so the browser is forced into a preflight that
    our handler will deny by default. Round-3 panel-flagged."""
    import json as _json
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    url, _ = graph_with_data
    body = _json.dumps({"key": "PAL_MULTIAUDIT_JUDGE", "value": "claude"}).encode()
    req = Request(url + "api/settings", data=body, method="POST",
                  headers={"Content-Type": "text/plain"})
    try:
        urlopen(req, timeout=2.0)
        raised = False
    except HTTPError as exc:
        raised = True
        assert exc.code == 415  # unsupported media type
    assert raised, "non-JSON Content-Type must be rejected"


def test_settings_post_rejects_oversized_body(graph_with_data):
    """A POST body larger than 4KB must be rejected with 413 to prevent
    memory / thread-time DoS via giant payloads. Round-3 panel-flagged."""
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    url, _ = graph_with_data
    big = b"x" * 5000
    req = Request(url + "api/settings", data=big, method="POST",
                  headers={"Content-Type": "application/json"})
    try:
        urlopen(req, timeout=2.0)
        raised = False
    except HTTPError as exc:
        raised = True
        assert exc.code == 413
    assert raised, "oversized body must be rejected"


def test_web_url_tool_reports_disabled(monkeypatch):
    """When the viewer isn't running the tool reports gracefully."""
    import asyncio

    import utils.web_viewer as wv
    from tools.web_url import WebUrlTool

    monkeypatch.setattr(wv, "_SERVER_PORT", None)

    async def go():
        return await WebUrlTool().execute({})

    out = asyncio.run(go())
    payload = json.loads(out[0].text)
    assert payload["status"] == "disabled"

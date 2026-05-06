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

"""Tests for utils/execution_graph.py — durable execution log."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest


def _fresh_graph(tmp_path: Path):
    from utils.execution_graph import ExecutionGraph

    return ExecutionGraph(tmp_path / "graph.db")


# ---------------------------------------------------------------------------
# Schema + basic CRUD
# ---------------------------------------------------------------------------


def test_open_creates_db_and_schema(tmp_path: Path):
    g = _fresh_graph(tmp_path)
    # The three tables must exist
    cur = g._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert {"runs", "events", "edges"}.issubset(tables)
    g.close()


def test_start_complete_roundtrip(tmp_path: Path):
    g = _fresh_graph(tmp_path)
    rid = g.start_run("chat", label="hello", args={"prompt": "hi"})
    assert isinstance(rid, str) and len(rid) == 32
    run = g.get_run(rid)
    assert run is not None
    assert run["tool_name"] == "chat"
    assert run["label"] == "hello"
    assert run["status"] == "running"

    g.complete_run(rid, result={"content": "hello back"}, cost_tier="oauth_free", model_used="codex")
    run = g.get_run(rid)
    assert run["status"] == "completed"
    assert run["cost_tier"] == "oauth_free"
    assert run["model_used"] == "codex"
    assert "hello back" in run["result_json"]
    g.close()


def test_fail_run_records_error_and_payload(tmp_path: Path):
    g = _fresh_graph(tmp_path)
    rid = g.start_run("chat", args={})
    g.fail_run(rid, error="boom", error_payload={"status": "error", "code": 42})
    run = g.get_run(rid)
    assert run["status"] == "failed"
    assert run["error"] == "boom"
    assert "42" in run["error_payload_json"]
    g.close()


def test_events_are_append_only_and_ordered(tmp_path: Path):
    g = _fresh_graph(tmp_path)
    rid = g.start_run("panel", args={})
    g.add_event(rid, event_type="progress", message="step 1", progress=0.25)
    g.add_event(rid, event_type="progress", message="step 2", progress=0.5)
    g.add_event(rid, event_type="progress", message="step 3", progress=0.75)
    g.complete_run(rid)

    events = g.get_run_events(rid)
    # start + 3 progress + complete = 5 events
    assert len(events) == 5
    types = [e["event_type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "complete"
    # Progress events ordered
    progress_events = [e for e in events if e["event_type"] == "progress"]
    assert [e["message"] for e in progress_events] == ["step 1", "step 2", "step 3"]
    g.close()


# ---------------------------------------------------------------------------
# Lineage: parent/child + recursive tree
# ---------------------------------------------------------------------------


def test_parent_child_edge_auto_created(tmp_path: Path):
    g = _fresh_graph(tmp_path)
    parent = g.start_run("panel", args={})
    child = g.start_run("clink", parent_run_id=parent, args={"cli_name": "codex"})
    cur = g._conn.execute(
        "SELECT parent_run_id, child_run_id, kind FROM edges WHERE parent_run_id=?",
        (parent,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0] == (parent, child, "spawn")
    g.close()


def test_get_run_tree_returns_full_descendants(tmp_path: Path):
    g = _fresh_graph(tmp_path)
    panel_run = g.start_run("panel", args={"prompt": "audit"})
    p1 = g.start_run("clink", parent_run_id=panel_run, args={"cli_name": "codex"})
    p2 = g.start_run("clink", parent_run_id=panel_run, args={"cli_name": "gemini"})
    # Fallback uses edge_kind='fallback' instead of the default 'spawn'
    # so the tree reflects the semantic relationship.
    fb = g.start_run("chat", parent_run_id=p2, args={"model": "gemini-3.1-pro-preview"}, edge_kind="fallback")
    g.complete_run(fb, cost_tier="oauth_fallback_paid")
    g.complete_run(p1, cost_tier="oauth_free")
    g.complete_run(p2, cost_tier="oauth_fallback_paid")
    g.complete_run(panel_run)

    tree = g.get_run_tree(panel_run)
    assert tree is not None
    assert tree["tool_name"] == "panel"
    assert len(tree["children"]) == 2
    # The clink that fell back to chat should have the chat as its descendant
    p2_subtree = next(c for c in tree["children"] if c["run_id"] == p2)
    assert len(p2_subtree["children"]) == 1
    fb_node = p2_subtree["children"][0]
    assert fb_node["tool_name"] == "chat"
    assert fb_node["edge_kind"] == "fallback"
    g.close()


# ---------------------------------------------------------------------------
# Listing / querying
# ---------------------------------------------------------------------------


def test_list_runs_filters(tmp_path: Path):
    g = _fresh_graph(tmp_path)
    a = g.start_run("chat", args={})
    g.complete_run(a)
    b = g.start_run("clink", args={})
    g.fail_run(b, error="boom")
    c = g.start_run("panel", args={})
    g.complete_run(c)

    completed = g.list_runs(status="completed")
    assert {r["run_id"] for r in completed} == {a, c}

    chats = g.list_runs(tool_name="chat")
    assert [r["run_id"] for r in chats] == [a]

    all_runs = g.list_runs(limit=10)
    # Most recent first
    assert [r["run_id"] for r in all_runs][0] == c
    g.close()


def test_args_redaction_strips_internal_fields(tmp_path: Path):
    """Internal fields (_model_context, _resolved_model_name, etc.) must not
    end up in the persisted args snapshot — they don't survive serialization
    cleanly and aren't useful for replay."""
    g = _fresh_graph(tmp_path)
    rid = g.start_run("chat", args={
        "prompt": "hi",
        "_model_context": object(),  # would crash json.dumps
        "_resolved_model_name": "codex",
        "model": "codex",
    })
    run = g.get_run(rid)
    assert "_model_context" not in run["args_json"]
    assert "_resolved_model_name" not in run["args_json"]
    assert "prompt" in run["args_json"]
    g.close()


def test_args_snapshot_caps_long_strings(tmp_path: Path, monkeypatch):
    """A 1MB prompt shouldn't bloat the DB."""
    import utils.execution_graph as eg

    monkeypatch.setattr(eg, "_SNAPSHOT_CAP", 200)
    g = eg.ExecutionGraph(tmp_path / "g.db")
    big = "X" * 50_000
    rid = g.start_run("chat", args={"prompt": big})
    run = g.get_run(rid)
    # Original was 50k chars; stored snapshot must be at most a few hundred.
    assert len(run["args_json"]) < 600
    assert big not in run["args_json"]
    g.close()


# ---------------------------------------------------------------------------
# Disabled / failure modes
# ---------------------------------------------------------------------------


def test_get_graph_returns_none_when_disabled(monkeypatch):
    """PAL_GRAPH_DB='' is the explicit opt-out."""
    import utils.execution_graph as eg

    monkeypatch.setattr(eg, "_GRAPH", None)
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)
    monkeypatch.setenv("PAL_GRAPH_DB", "")
    assert eg.get_graph() is None
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)


def test_get_graph_disables_silently_on_init_failure(monkeypatch, tmp_path):
    """If init crashes (read-only FS, bad path), the graph disables itself
    permanently for the process — never load-bearing."""
    import utils.execution_graph as eg

    monkeypatch.setattr(eg, "_GRAPH", None)
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)

    class _Boom(eg.ExecutionGraph):
        def __init__(self, *_a, **_k):
            raise RuntimeError("simulated init failure")

    monkeypatch.setattr(eg, "ExecutionGraph", _Boom)
    assert eg.get_graph() is None
    # Subsequent calls don't retry
    assert eg.get_graph() is None
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)


# ---------------------------------------------------------------------------
# run_context: contextvar parent tracking
# ---------------------------------------------------------------------------


def test_run_context_threads_parent_via_contextvar(tmp_path, monkeypatch):
    """Nested execute_tool dispatches should auto-derive parent from the
    contextvar, no explicit threading. This is the core ergonomics win."""
    import utils.execution_graph as eg

    monkeypatch.setattr(eg, "_GRAPH", eg.ExecutionGraph(tmp_path / "g.db"))
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)
    g = eg.get_graph()
    assert g is not None

    captured: dict[str, str | None] = {}

    async def go():
        with eg.run_context("panel", args={"prompt": "x"}) as outer_id:
            captured["outer"] = outer_id
            with eg.run_context("clink", args={"cli_name": "codex"}) as inner_id:
                captured["inner"] = inner_id
                # The inner run must list the outer as parent
                inner_run = g.get_run(inner_id)
                assert inner_run["parent_run_id"] == outer_id

    asyncio.run(go())
    assert captured["outer"] is not None
    assert captured["inner"] is not None
    assert captured["outer"] != captured["inner"]


def test_run_context_marks_failure_on_exception(tmp_path, monkeypatch):
    import utils.execution_graph as eg

    monkeypatch.setattr(eg, "_GRAPH", eg.ExecutionGraph(tmp_path / "g.db"))
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)
    g = eg.get_graph()

    captured = {}
    with pytest.raises(ValueError):
        with eg.run_context("chat", args={}) as rid:
            captured["rid"] = rid
            raise ValueError("simulated failure")

    run = g.get_run(captured["rid"])
    assert run["status"] == "failed"
    assert "simulated failure" in run["error"]


def test_run_context_marks_completed_on_clean_exit(tmp_path, monkeypatch):
    """If the caller doesn't explicitly complete_run, run_context marks
    completed on clean exit so dangling 'running' rows don't accumulate."""
    import utils.execution_graph as eg

    monkeypatch.setattr(eg, "_GRAPH", eg.ExecutionGraph(tmp_path / "g.db"))
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)
    g = eg.get_graph()
    captured = {}
    with eg.run_context("listmodels", args={}) as rid:
        captured["rid"] = rid
    run = g.get_run(captured["rid"])
    assert run["status"] == "completed"

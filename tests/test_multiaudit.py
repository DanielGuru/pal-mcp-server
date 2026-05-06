"""Tests for tools/multiaudit.py — git context capture + dispatch.

These don't fire a real panel (would cost paid tokens). They verify:
  - non-git directory rejected cleanly
  - empty diff returns a structured error
  - the dispatched start_task argv carries the audit prompt + panelist setup
  - the response payload has task_id, web URL, summary, next_steps

start_task is monkeypatched to return a synthetic task_id so we never touch
network / tokens.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest


def _git_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with a main branch + a feature branch carrying
    a real diff. Returns the repo path."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "feature.py").write_text("def hello():\n    return 'world'\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: add hello"], cwd=tmp_path, check=True
    )
    return tmp_path


def test_rejects_non_git_directory(tmp_path):
    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute(
            {"working_directory_absolute_path": str(tmp_path)}
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "error"
    assert "Not a git repository" in body["error"]


def test_rejects_repo_with_no_diff(tmp_path):
    """If branch == main and there are no uncommitted/staged changes, fail
    cleanly with an actionable message."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@l"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "x").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=tmp_path, check=True)

    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute(
            {"working_directory_absolute_path": str(tmp_path)}
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "error"
    assert "No changes to audit" in body["error"]


def test_dispatches_panel_with_audit_prompt(tmp_path, monkeypatch):
    """Happy path: real diff → start_task called with a panel arguments
    payload that contains the diff body and the requested panelists."""
    repo = _git_repo(tmp_path)

    captured: dict = {}

    async def fake_execute(name, arguments):
        # Stand in for server.execute_tool — capture what multiaudit dispatched.
        captured["name"] = name
        captured["arguments"] = arguments
        from mcp.types import TextContent
        return [TextContent(type="text", text=json.dumps({"status": "started", "task_id": "fake_task_id_123"}))]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute({
            "working_directory_absolute_path": str(repo),
            "panelists": ["codex", "gemini"],
            "judge": "codex",
            "debate_rounds": 0,
            "extra_context": "Pay attention to error handling.",
        })

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "started"
    assert body["task_id"] == "fake_task_id_123"
    assert body["panelists"] == ["codex", "gemini"]
    assert body["judge"] == "codex"
    assert body["debate_rounds"] == 0
    assert "feature.py" in body["files_changed"]
    assert "next_steps" in body and len(body["next_steps"]) >= 3

    # Verify the dispatched panel call
    assert captured["name"] == "start_task"
    assert captured["arguments"]["tool"] == "panel"
    panel_args = captured["arguments"]["arguments"]
    assert panel_args["panelists"] == ["codex", "gemini"]
    assert panel_args["judge"] == "codex"
    # The audit prompt must contain key sections + the diff
    prompt = panel_args["prompt"]
    assert "VERDICT" in prompt
    assert "BUGS" in prompt
    assert "WHAT YOU'D ATTACK" in prompt
    assert "feature.py" in prompt
    assert "def hello" in prompt  # actual diff body
    assert "Pay attention to error handling" in prompt  # extra_context inlined


def test_diff_truncation_marker_when_oversized(tmp_path, monkeypatch):
    """Massive diff gets capped + the panel prompt explicitly tells panelists
    they're seeing a subset."""
    repo = _git_repo(tmp_path)
    big_file = repo / "big.py"
    big_file.write_text("x = '" + ("Y" * 80_000) + "'\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "huge"], cwd=repo, check=True)

    captured: dict = {}

    async def fake_execute(name, arguments):
        captured["arguments"] = arguments
        from mcp.types import TextContent
        return [TextContent(type="text", text=json.dumps({"status": "started", "task_id": "t"}))]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute({
            "working_directory_absolute_path": str(repo),
        })

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["diff_truncated"] is True
    prompt = captured["arguments"]["arguments"]["prompt"]
    assert "truncated" in prompt.lower()


def test_includes_web_viewer_url_when_available(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)

    async def fake_execute(name, arguments):
        from mcp.types import TextContent
        return [TextContent(type="text", text=json.dumps({"status": "started", "task_id": "t"}))]

    import server
    import utils.web_viewer as wv

    monkeypatch.setattr(server, "execute_tool", fake_execute)
    monkeypatch.setattr(wv, "_SERVER_PORT", 18999)
    monkeypatch.setattr(wv, "_BIND_HOST", "127.0.0.1")

    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute({"working_directory_absolute_path": str(repo)})

    body = json.loads(asyncio.run(go())[0].text)
    assert body["web_viewer_url"] == "http://127.0.0.1:18999/"


def test_handles_web_viewer_disabled(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)

    async def fake_execute(name, arguments):
        from mcp.types import TextContent
        return [TextContent(type="text", text=json.dumps({"status": "started", "task_id": "t"}))]

    import server
    import utils.web_viewer as wv

    monkeypatch.setattr(server, "execute_tool", fake_execute)
    monkeypatch.setattr(wv, "_SERVER_PORT", None)

    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute({"working_directory_absolute_path": str(repo)})

    body = json.loads(asyncio.run(go())[0].text)
    assert body["web_viewer_url"] is None
    assert "web_viewer_note" in body

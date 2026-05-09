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
    they're seeing a subset.

    Patches _DIFF_CHAR_CAP to a small value rather than generating a 600 KB
    diff — the default cap was bumped to 600 KB ("don't be cheap" directive)
    and creating a real diff that size in CI would be slow. The behaviour we
    care about (cap fires → marker set → prompt warns panelists) is the same
    at any cap value.
    """
    repo = _git_repo(tmp_path)
    big_file = repo / "big.py"
    big_file.write_text("x = '" + ("Y" * 80_000) + "'\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "huge"], cwd=repo, check=True)

    # Force the cap small so the 80 KB file overflows it.
    from tools import multiaudit
    monkeypatch.setattr(multiaudit, "_DIFF_CHAR_CAP", 1000)

    captured: dict = {}

    async def fake_execute(name, arguments):
        captured["arguments"] = arguments
        from mcp.types import TextContent
        return [TextContent(type="text", text=json.dumps({"status": "started", "task_id": "t"}))]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    async def go():
        return await multiaudit.MultiauditTool().execute({
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


def test_web_viewer_url_deep_links_to_current_run(tmp_path, monkeypatch):
    """When run_context wraps the multiaudit dispatch with a known run_id,
    the returned URL should land on that run via ?run=<id>. This is what
    keeps the auto-opened browser tab from getting stuck on a stale run
    from a previous Panel session."""
    repo = _git_repo(tmp_path)

    async def fake_execute(name, arguments):
        from mcp.types import TextContent
        return [TextContent(type="text", text=json.dumps({"status": "started", "task_id": "t"}))]

    import server
    import utils.web_viewer as wv
    import utils.execution_graph as eg

    monkeypatch.setattr(server, "execute_tool", fake_execute)
    monkeypatch.setattr(wv, "_SERVER_PORT", 18999)
    monkeypatch.setattr(wv, "_BIND_HOST", "127.0.0.1")
    # Pretend we're inside a run with a known id — multiaudit reads this
    # via current_run_id() to deep-link the viewer URL at its own run.
    monkeypatch.setattr(eg, "current_run_id", lambda: "deadbeefcafe")

    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute({"working_directory_absolute_path": str(repo)})

    body = json.loads(asyncio.run(go())[0].text)
    assert body["web_viewer_url"] == "http://127.0.0.1:18999/?run=deadbeefcafe"


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


def test_rejects_base_branch_starting_with_dash(tmp_path):
    """Audit panel finding: a ref like '--upload-pack=evil' is parsed by
    git itself as an option flag (shell=False doesn't help). Reject at the
    multiaudit boundary."""
    repo = _git_repo(tmp_path)
    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute({
            "working_directory_absolute_path": str(repo),
            "base_branch": "--upload-pack=/tmp/evil",
        })

    body = json.loads(asyncio.run(go())[0].text)
    assert body["status"] == "error"
    assert "starts with '-'" in body["error"] or "starting with '-'" in body["error"]


def test_rejects_base_branch_with_invalid_chars(tmp_path):
    """Lightweight whitelist: refs must be alnum + /._- only."""
    repo = _git_repo(tmp_path)
    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute({
            "working_directory_absolute_path": str(repo),
            "base_branch": "main; rm -rf /",
        })

    body = json.loads(asyncio.run(go())[0].text)
    assert body["status"] == "error"
    assert "must contain only" in body["error"]


def test_default_panelist_set_includes_host(tmp_path, monkeypatch):
    """multiaudit pulls Claude Code into the debate by default via the
    'host' MCP-sampling agent — not just dispatching to other models."""
    repo = _git_repo(tmp_path)

    captured: dict = {}

    async def fake_execute(name, arguments):
        captured["arguments"] = arguments
        from mcp.types import TextContent
        return [TextContent(type="text", text=json.dumps({"status": "started", "task_id": "t"}))]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.multiaudit import MultiauditTool

    async def go():
        # No panelists override → default
        return await MultiauditTool().execute({"working_directory_absolute_path": str(repo)})

    asyncio.run(go())
    panelists = captured["arguments"]["arguments"]["panelists"]
    # 'host' is no longer a default panelist — it always fails on Claude
    # Code (no sampling) and polluted every audit with a "host failed" row.
    # Operators can opt back in by passing panelists=[...] explicitly.
    assert "host" not in panelists, (
        f"'host' should NOT be in default panelists (always-fails on Claude Code); got {panelists}"
    )
    # Panel must include all four current frontier families by default —
    # codex (OpenAI), gemini (Google), claude (Anthropic), grok-4.3 (xAI).
    # Anything missing means the audit only hears from a subset of vendors.
    for required in ("codex", "gemini", "claude", "grok-4.3"):
        assert required in panelists, f"expected '{required}' in defaults; got {panelists}"


def test_default_panelists_immutable_against_env_at_import(tmp_path, monkeypatch):
    """``DEFAULT_PANELISTS`` is now an immutable tuple. If env was set at
    server boot and later cleared, ``execute()`` must still fall back to
    the canonical 4-model list — not a stale mutated value. Mirrors the
    bugfind test; was the missed fix in the multiaudit audit (codex
    judge: ``no, fix multiaudit defaults first``)."""
    repo = _git_repo(tmp_path)

    monkeypatch.delenv("PANEL_MULTIAUDIT_PANELISTS", raising=False)
    monkeypatch.delenv("PANEL_MULTIAUDIT_JUDGE", raising=False)

    captured: dict = {}

    async def fake_execute(name, arguments):
        captured["arguments"] = arguments
        from mcp.types import TextContent

        return [
            TextContent(
                type="text",
                text=json.dumps({"status": "started", "task_id": "t"}),
            )
        ]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.multiaudit import DEFAULT_PANELISTS, MultiauditTool

    # Hard guard: must be a tuple (immutable), not a list. A future
    # regression that re-introduces module-level mutation would change
    # this back to a list and the assertion would fire.
    assert isinstance(DEFAULT_PANELISTS, tuple)

    async def go():
        return await MultiauditTool().execute(
            {"working_directory_absolute_path": str(repo)}
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["panelists"] == ["codex", "gemini", "claude", "grok-4.3"]


def test_propagates_start_task_error_no_false_success(tmp_path, monkeypatch):
    """When start_task returns a structured error payload (admission control,
    unknown wrapped tool, etc.) WITHOUT raising, multiaudit must surface
    that as an error — not return ``status: started`` with ``task_id: null``
    and ``next_steps`` telling the user to poll a nonexistent task. Mirrors
    bugfind regression test; was missing from multiaudit per the audit."""
    repo = _git_repo(tmp_path)

    async def fake_execute(name, arguments):
        from mcp.types import TextContent

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "status": "error",
                        "error": "too_many_active_tasks: 8/8 running",
                    }
                ),
            )
        ]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute(
            {"working_directory_absolute_path": str(repo)}
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "error"
    assert "too_many_active_tasks" in body["error"]
    # Critical: the user must NOT see status=started for a refused dispatch
    assert body.get("task_id") in (None, "")


def test_falls_back_to_last_commit_when_clean_main(tmp_path, monkeypatch):
    """When branch == base_branch and the working tree is clean, fall back
    to HEAD~1..HEAD so multiaudit still works after the change has already
    been committed (the common 'I just pushed, audit it' case)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@l"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("def a(): return 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: a"], cwd=tmp_path, check=True)
    (tmp_path / "b.py").write_text("def b(): return 2\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: b"], cwd=tmp_path, check=True)

    captured: dict = {}

    async def fake_execute(name, arguments):
        captured["arguments"] = arguments
        from mcp.types import TextContent
        return [TextContent(
            type="text",
            text=json.dumps({"status": "started", "task_id": "tid"}),
        )]

    import server
    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.multiaudit import MultiauditTool

    async def go():
        return await MultiauditTool().execute({
            "working_directory_absolute_path": str(tmp_path),
            "panelists": ["codex"],
            "debate_rounds": 0,
        })

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "started"
    assert body["diff_source"] == "last commit (HEAD)"
    assert "b.py" in body["files_changed"]
    prompt = captured["arguments"]["arguments"]["prompt"]
    assert "def b()" in prompt


def test_strip_null_bytes_replaces_with_visible_escape():
    """NUL bytes in a clink prompt must be replaced before subprocess
    dispatch — argv rejects them with ValueError, stdin can hang the CLI."""
    from clink.agents.base import _strip_null_bytes

    src = "before\x00PH1\x00middle\x00PH2\x00after"
    out = _strip_null_bytes(src, label="gemini")
    assert "\x00" not in out
    assert out == "before\\x00PH1\\x00middle\\x00PH2\\x00after"
    # No-op when clean — must return the same string instance for hot path.
    clean = "no nulls here"
    assert _strip_null_bytes(clean, label="codex") is clean

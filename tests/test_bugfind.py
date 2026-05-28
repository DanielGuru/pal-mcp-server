"""Tests for tools/bugfind.py — context capture + dispatch.

These don't fire a real panel (would cost paid tokens). They verify:
  - bug_description required and non-empty
  - non-existent working directory rejected cleanly
  - the dispatched start_task argv carries the bug rubric + bug description
  - recent commits are auto-attached when in a git repo
  - error log tail is auto-attached when logs/mcp_server.log exists
  - log tail filtered to ERROR / Traceback / Failed / Exception lines
  - skip_log_tail bypasses log attachment
  - attached_files are read and included
  - per-file cap and total cap fire correctly
  - response payload has task_id, web URL, summary, next_steps, context summary

start_task is monkeypatched so we never touch network / tokens.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with a couple of commits for context."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@l"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("readme\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=tmp_path, check=True)
    (tmp_path / "feature.py").write_text("def x(): return 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: add x() returning 1"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def _fake_dispatch(captured: dict):
    """Return an async function that stands in for server.execute_tool,
    recording what bugfind dispatched."""

    async def fake_execute(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        from mcp.types import TextContent

        return [
            TextContent(
                type="text",
                text=json.dumps({"status": "started", "task_id": "fake_bug_task_42"}),
            )
        ]

    return fake_execute


# ----------------------------------------------------------------------
# Input validation
# ----------------------------------------------------------------------


def test_rejects_empty_bug_description(tmp_path):
    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {"bug_description": "", "working_directory_absolute_path": str(tmp_path)}
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "error"
    assert "bug_description is required" in body["error"]


def test_rejects_whitespace_only_bug_description(tmp_path):
    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {"bug_description": "   \n\t  ", "working_directory_absolute_path": str(tmp_path)}
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "error"


def test_rejects_nonexistent_working_directory():
    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "the thing is broken",
                "working_directory_absolute_path": "/nope/this/does/not/exist",
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "error"
    assert "does not exist" in body["error"]


# ----------------------------------------------------------------------
# Happy path: dispatch carries the bug description + rubric
# ----------------------------------------------------------------------


def test_dispatches_panel_with_bug_rubric(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    captured: dict = {}

    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "the viewer header shows 'grok-4.3' instead of 'panel'",
                "working_directory_absolute_path": str(repo),
                "panelists": ["codex", "claude"],
                "judge": "claude",
                "debate_rounds": 0,
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "started"
    assert body["task_id"] == "fake_bug_task_42"
    assert body["panelists"] == ["codex", "claude"]
    assert body["judge"] == "claude"
    assert body["debate_rounds"] == 0
    assert "next_steps" in body and len(body["next_steps"]) >= 3

    # The dispatched panel prompt must carry the full bug rubric
    assert captured["name"] == "start_task"
    assert captured["arguments"]["tool"] == "panel"
    panel_args = captured["arguments"]["arguments"]
    assert panel_args["panelists"] == ["codex", "claude"]
    assert panel_args["judge"] == "claude"
    prompt = panel_args["prompt"]
    assert "REPRO" in prompt
    assert "ROOT CAUSE" in prompt
    assert "MINIMAL FIX" in prompt
    assert "REGRESSION TEST" in prompt
    assert "BLAST RADIUS" in prompt
    assert "WHAT YOU MISSED" in prompt
    # The user's bug description must appear verbatim
    assert "viewer header shows 'grok-4.3' instead of 'panel'" in prompt


def test_label_uses_first_line_of_bug_description(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    captured: dict = {}

    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "first short line\n\nlots of detail follows but the picker label should only show the first line",
                "working_directory_absolute_path": str(repo),
                "skip_log_tail": True,
            }
        )

    asyncio.run(go())
    label = captured["arguments"]["label"]
    assert label.startswith("bugfind:")
    assert "first short line" in label
    # Long second-line content must NOT bleed into the label
    assert "lots of detail" not in label


# ----------------------------------------------------------------------
# Auto-attached context: recent commits, error logs, files
# ----------------------------------------------------------------------


def test_recent_commits_attached_when_in_git_repo(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    captured: dict = {}

    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "x",
                "working_directory_absolute_path": str(repo),
                "skip_log_tail": True,
            }
        )

    asyncio.run(go())
    prompt = captured["arguments"]["arguments"]["prompt"]
    assert "RECENT COMMITS" in prompt
    assert "feat: add x() returning 1" in prompt


def test_log_tail_filtered_to_error_lines(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    log_dir = repo / "logs"
    log_dir.mkdir()
    log_path = log_dir / "mcp_server.log"
    log_path.write_text(
        "INFO: routine boot line that should NOT be in the bugfind context\n"
        "DEBUG: another routine line\n"
        "INFO: more routine\n"
        "ERROR: something failed in the auth flow\n"
        "Traceback (most recent call last):\n"
        "  File 'foo.py', line 42, in bar\n"
        "    raise ValueError('x')\n"
        "INFO: another routine line\n"
    )

    captured: dict = {}
    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "auth flow keeps failing",
                "working_directory_absolute_path": str(repo),
            }
        )

    asyncio.run(go())
    prompt = captured["arguments"]["arguments"]["prompt"]
    assert "ERROR LOG TAIL" in prompt
    assert "ERROR: something failed in the auth flow" in prompt
    assert "Traceback" in prompt
    # Routine lines must NOT have been attached
    assert "routine boot line that should NOT be" not in prompt


def test_skip_log_tail_bypasses_log_attachment(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    log_dir = repo / "logs"
    log_dir.mkdir()
    (log_dir / "mcp_server.log").write_text("ERROR: would normally be attached\n")

    captured: dict = {}
    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "ui bug, logs irrelevant",
                "working_directory_absolute_path": str(repo),
                "skip_log_tail": True,
            }
        )

    asyncio.run(go())
    prompt = captured["arguments"]["arguments"]["prompt"]
    assert "ERROR LOG TAIL" not in prompt
    assert "would normally be attached" not in prompt


def test_attached_files_are_inlined(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    target = repo / "broken_module.py"
    target.write_text("def buggy():\n    return None  # should return 1\n")

    captured: dict = {}
    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "buggy() returns None, should return 1",
                "working_directory_absolute_path": str(repo),
                "attached_files": [str(target)],
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    prompt = captured["arguments"]["arguments"]["prompt"]
    assert "ATTACHED FILES" in prompt
    assert str(target) in prompt
    assert "def buggy()" in prompt
    assert "should return 1" in prompt

    # Response context summary advertises the attachment
    body = json.loads(out[0].text)
    assert str(target) in body["context"]["attached_files"]


def test_attached_file_truncation_marker(tmp_path, monkeypatch):
    """Files larger than the per-file cap get truncated with a clear marker."""
    repo = _make_git_repo(tmp_path)
    big = repo / "huge.txt"
    # _FILE_CHAR_CAP is 200_000; write comfortably above it so the
    # truncation path fires deterministically regardless of small
    # future cap bumps.
    big.write_text("X" * 250_000)

    captured: dict = {}
    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "huge file is the problem",
                "working_directory_absolute_path": str(repo),
                "attached_files": [str(big)],
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    prompt = captured["arguments"]["arguments"]["prompt"]
    assert "[truncated]" in prompt or "truncated" in prompt
    body = json.loads(out[0].text)
    assert str(big) in body["context"]["attached_files_truncated"]


def test_nonexistent_attached_files_silently_skipped(tmp_path, monkeypatch):
    """Files that don't exist or aren't readable should be skipped without
    crashing the dispatch."""
    repo = _make_git_repo(tmp_path)
    captured: dict = {}
    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "x",
                "working_directory_absolute_path": str(repo),
                "attached_files": ["/nope/missing.py", "", "   "],
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "started"
    # Non-existent / empty / whitespace paths must NOT have been attached
    assert body["context"]["attached_files"] == []


# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------


def test_defaults_match_multiaudit(tmp_path, monkeypatch):
    """bugfind's default panelists / judge / debate_rounds mirror multiaudit
    so users don't have to learn a different default panel for the same
    cost profile."""
    repo = _make_git_repo(tmp_path)
    captured: dict = {}

    monkeypatch.delenv("PANEL_BUGFIND_PANELISTS", raising=False)
    monkeypatch.delenv("PANEL_BUGFIND_JUDGE", raising=False)

    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "x",
                "working_directory_absolute_path": str(repo),
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["panelists"] == [
        "codex",
        "gemini",
        {"agent": "claude-sonnet-4-6", "label": "sonnet"},
        {"agent": "claude-opus-4-8", "label": "opus"},
    ]
    assert body["judge"] == "codex"
    assert body["debate_rounds"] == 1


def test_env_overrides_for_panelists_and_judge(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    captured: dict = {}

    monkeypatch.setenv("PANEL_BUGFIND_PANELISTS", "codex,claude")
    monkeypatch.setenv("PANEL_BUGFIND_JUDGE", "claude")

    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "x",
                "working_directory_absolute_path": str(repo),
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["panelists"] == ["codex", "claude"]
    assert body["judge"] == "claude"


def test_per_call_override_wins_over_env(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    captured: dict = {}

    monkeypatch.setenv("PANEL_BUGFIND_PANELISTS", "codex,claude")
    monkeypatch.setenv("PANEL_BUGFIND_JUDGE", "claude")

    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "x",
                "working_directory_absolute_path": str(repo),
                "panelists": ["gemini"],
                "judge": "gemini",
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["panelists"] == ["gemini"]
    assert body["judge"] == "gemini"


# ----------------------------------------------------------------------
# Registration sanity
# ----------------------------------------------------------------------


def test_propagates_start_task_error_no_false_success(tmp_path, monkeypatch):
    """When start_task returns a structured error payload (admission control,
    unknown wrapped tool, etc.) WITHOUT raising, bugfind must surface that
    as an error — not return ``status: started`` with task_id=null and
    ``next_steps`` telling the user to poll a nonexistent task. Audit-flagged
    blocker fixed in this commit."""
    repo = _make_git_repo(tmp_path)

    async def fake_execute(name, arguments):
        from mcp.types import TextContent

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"status": "error", "error": "too_many_active_tasks: 8/8 running"}
                ),
            )
        ]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "x",
                "working_directory_absolute_path": str(repo),
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "error"
    assert "too_many_active_tasks" in body["error"]
    # Critical: the user must NOT see status=started for a refused dispatch
    assert body.get("task_id") in (None, "")


def test_rejects_file_as_working_directory(tmp_path):
    """An existing regular file at working_directory_absolute_path passes
    Path.exists() but would crash subprocess.run(..., cwd=cwd) later. Reject
    upfront. Audit-flagged."""
    a_file = tmp_path / "this_is_a_file.txt"
    a_file.write_text("not a directory\n")

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "x",
                "working_directory_absolute_path": str(a_file),
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["status"] == "error"
    assert "is not a directory" in body["error"]


def test_default_panelists_immutable_against_env_at_import(tmp_path, monkeypatch):
    """DEFAULT_PANELISTS is now an immutable tuple. If env was set at server
    boot and later cleared, execute() must still fall back to the canonical
    4-model list — not a stale mutated value. Audit-flagged."""
    repo = _make_git_repo(tmp_path)

    # Simulate: env was set at import (not actually relevant since the
    # tuple is immutable) and is now cleared.
    monkeypatch.delenv("PANEL_BUGFIND_PANELISTS", raising=False)
    monkeypatch.delenv("PANEL_BUGFIND_JUDGE", raising=False)

    captured: dict = {}
    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool, DEFAULT_PANELISTS

    # Hard guard: must be a tuple (immutable), not a list
    assert isinstance(DEFAULT_PANELISTS, tuple)

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "x",
                "working_directory_absolute_path": str(repo),
                "skip_log_tail": True,
            }
        )

    out = asyncio.run(go())
    body = json.loads(out[0].text)
    assert body["panelists"] == [
        "codex",
        "gemini",
        {"agent": "claude-sonnet-4-6", "label": "sonnet"},
        {"agent": "claude-opus-4-8", "label": "opus"},
    ]


def test_log_tail_redacts_secret_shapes_before_dispatch(tmp_path, monkeypatch):
    """The auto-attached log tail goes verbatim into a panel prompt that
    fans out to OpenAI / Anthropic / Gemini / xAI. Lines containing
    API-key shapes / Bearer headers / JWTs MUST be redacted before
    dispatch, otherwise a single unhandled exception that echoed a
    secret would broadcast it to all four provider request logs.
    Audit-flagged blocker (codex). Regression test."""
    repo = _make_git_repo(tmp_path)

    # Synthetic logs with every shape redact_secrets handles.
    log_dir = repo / "logs"
    log_dir.mkdir()
    (log_dir / "mcp_server.log").write_text(
        "ERROR: provider rejected key=sk-ant-realistic-test-key-1234567890abcdefghij\n"
        "Traceback (most recent call last):\n"
        "  File 'foo.py', line 42, in bar\n"
        "    raise AuthError('xai-realistic-test-key-1234567890abcdef')\n"
        "ERROR: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature\n"
        "ERROR: oauth check failed for AIzaSyAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "ERROR: home path was /Users/realname/.secrets/.env\n"
    )

    captured: dict = {}
    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "secrets in log shouldn't leave this process",
                "working_directory_absolute_path": str(repo),
                # skip_log_tail defaults False — we WANT the log path here
            }
        )

    asyncio.run(go())
    prompt = captured["arguments"]["arguments"]["prompt"]

    # Original secret strings must NOT appear in the dispatched prompt
    assert "sk-ant-realistic-test-key-1234567890abcdefghij" not in prompt
    assert "xai-realistic-test-key-1234567890abcdef" not in prompt
    assert "AIzaSyAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in prompt
    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in prompt
    assert "/Users/realname" not in prompt

    # Redaction markers ARE present (proves redaction fired, not that
    # log attachment was silently disabled)
    assert "[REDACTED_API_KEY]" in prompt or "[REDACTED" in prompt
    # The surrounding error context survives so panelists can still
    # diagnose — only the secret shapes were stripped.
    assert "provider rejected key=" in prompt
    assert "Traceback" in prompt


def test_attached_files_redact_secrets(tmp_path, monkeypatch):
    """Defence-in-depth: even though the schema warns users that
    ``attached_files`` go verbatim to panelist APIs, accidentally
    attaching a file containing API-key shapes shouldn't broadcast
    them. Apply the same redaction pass."""
    repo = _make_git_repo(tmp_path)
    secret_file = repo / "config_with_secret.py"
    secret_file.write_text(
        "API_KEY = 'sk-realistic-test-key-1234567890abcdefghijklmnopqrst'\n"
        "BEARER = 'Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature'\n"
    )

    captured: dict = {}
    import server

    monkeypatch.setattr(server, "execute_tool", _fake_dispatch(captured))

    from tools.bugfind import BugfindTool

    async def go():
        return await BugfindTool().execute(
            {
                "bug_description": "attached file has secrets in it",
                "working_directory_absolute_path": str(repo),
                "attached_files": [str(secret_file)],
                "skip_log_tail": True,
            }
        )

    asyncio.run(go())
    prompt = captured["arguments"]["arguments"]["prompt"]

    assert "sk-realistic-test-key-1234567890abcdefghijklmnopqrst" not in prompt
    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in prompt
    # The redaction marker proves the file was attached AND scrubbed
    assert "[REDACTED_API_KEY]" in prompt or "[REDACTED]" in prompt


def test_bugfind_registered_in_server_tools():
    import server

    assert "bugfind" in server.TOOLS
    tool = server.make_tool("bugfind")
    assert tool.get_name() == "bugfind"
    assert tool.requires_model() is False

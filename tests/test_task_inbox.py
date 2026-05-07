"""Tests for ``utils/task_inbox.py`` and ``panel_cli/`` (drain + install hooks).

Covers the load-bearing safety properties:
  - Atomic marker write: tmp staging file → atomic rename to final.
  - Concurrent drain: only one drain process claims a marker via the
    ``.processing.<pid>`` two-step rename.
  - Stale-processing reclaim: hook crash leaves ``.processing.<pid>`` for
    >TTL → next drain re-renames it back for retry.
  - Idempotent install / uninstall: re-running install on an already-
    configured settings.json is a no-op; uninstall removes only Panel-
    managed entries.
  - Backup before write: existing settings.json is copied to
    ``settings.json.panel-backup-<ts>`` on first modification.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


# --- task_inbox: atomic write -------------------------------------------------


def test_write_completion_marker_creates_atomic_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    from utils.task_inbox import write_completion_marker

    out = write_completion_marker(
        task_id="abc123",
        tool="panel",
        label="multiaudit:main",
        status="completed",
        created_at=1.0,
        completed_at=2.5,
        elapsed_seconds=1.5,
        run_id="run-xyz",
    )
    assert out is not None
    assert out.exists()
    assert out.name == "abc123.json"

    body = json.loads(out.read_text())
    assert body["schema_version"] == 1
    assert body["task_id"] == "abc123"
    assert body["status"] == "completed"
    assert body["elapsed_seconds"] == 1.5
    assert body["run_id"] == "run-xyz"
    assert "result_hint" in body

    # Staging dir should exist but be empty (tmp file was renamed out)
    staging = tmp_path / ".staging"
    assert staging.exists()
    assert list(staging.iterdir()) == []


def test_write_completion_marker_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    monkeypatch.setenv("PANEL_INBOX_DISABLE", "1")
    from utils.task_inbox import write_completion_marker

    out = write_completion_marker(
        task_id="abc",
        tool="panel",
        label=None,
        status="completed",
        created_at=None,
        completed_at=None,
        elapsed_seconds=None,
    )
    assert out is None
    assert list(tmp_path.iterdir()) == []  # nothing written


def test_write_completion_marker_overwrites_existing(tmp_path, monkeypatch):
    """If the same task_id is written twice (shouldn't happen but defend),
    the second write atomically replaces the first — no partial-state leak."""
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    from utils.task_inbox import write_completion_marker

    write_completion_marker(
        task_id="abc",
        tool="panel",
        label="first",
        status="completed",
        created_at=None,
        completed_at=None,
        elapsed_seconds=None,
    )
    out = write_completion_marker(
        task_id="abc",
        tool="panel",
        label="second",
        status="completed",
        created_at=None,
        completed_at=None,
        elapsed_seconds=None,
    )
    assert out is not None
    body = json.loads(out.read_text())
    assert body["label"] == "second"


def test_reclaim_stale_processing(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    from utils.task_inbox import reclaim_stale_processing

    # Simulate a crashed-hook leftover
    stale = tmp_path / "abc.json.processing.99999"
    stale.write_text("{}")
    # Backdate mtime so the TTL fires
    old = time.time() - 9999
    os.utime(stale, (old, old))

    count = reclaim_stale_processing(ttl_seconds=10.0)
    assert count == 1
    assert not stale.exists()
    assert (tmp_path / "abc.json").exists()


def test_reclaim_skips_recent_processing(tmp_path, monkeypatch):
    """Don't reclaim a .processing.* file that's actively in flight."""
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    from utils.task_inbox import reclaim_stale_processing

    fresh = tmp_path / "abc.json.processing.42"
    fresh.write_text("{}")  # mtime = now, age = 0

    count = reclaim_stale_processing(ttl_seconds=120.0)
    assert count == 0
    assert fresh.exists()


# --- panel_cli.inbox_drain: concurrent claim + format -------------------------


def _drop_marker(dir_: Path, task_id: str = "task1", **kwargs) -> Path:
    """Helper: write a completion marker the drain script will find."""
    marker = dir_ / f"{task_id}.json"
    payload = {
        "task_id": task_id,
        "label": kwargs.get("label", "multiaudit:main"),
        "tool": kwargs.get("tool", "panel"),
        "status": kwargs.get("status", "completed"),
        "elapsed_seconds": kwargs.get("elapsed_seconds", 12.3),
        "run_id": kwargs.get("run_id", "run-abc"),
    }
    marker.write_text(json.dumps(payload))
    return marker


def _run_drain(monkeypatch, stdin_payload: str = ""):
    """Run main() with optional stdin JSON simulating a hook-event payload.
    Returns (rc, stdout_text)."""
    import io
    from contextlib import redirect_stdout

    if stdin_payload:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_payload))
    else:
        # Empty stdin (no hook payload) — simulate a manual / unknown invoke
        monkeypatch.setattr("sys.stdin", io.StringIO(""))

    from panel_cli.inbox_drain import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main()
    return rc, buf.getvalue()


def test_inbox_drain_stop_event_exits_2_to_wake_model(tmp_path, monkeypatch):
    """Stop hook with asyncRewake: exit 2 wakes Claude with stdout as a
    system reminder. This is the load-bearing wake-up contract."""
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    marker = _drop_marker(tmp_path)

    rc, out = _run_drain(monkeypatch, stdin_payload=json.dumps({"hook_event_name": "Stop"}))

    assert rc == 2
    assert "<system-reminder>" in out
    assert "task1" in out
    assert "multiaudit:main" in out
    assert not marker.exists()


def test_inbox_drain_user_prompt_submit_exits_0(tmp_path, monkeypatch):
    """REGRESSION: UserPromptSubmit + exit 2 BLOCKS the user's prompt
    entirely. We must exit 0 here so stdout flows through as
    additionalContext rather than killing the user's message. Caught in
    the first live test — a prod-bug-shaped fix."""
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    marker = _drop_marker(tmp_path)

    rc, out = _run_drain(
        monkeypatch, stdin_payload=json.dumps({"hook_event_name": "UserPromptSubmit"})
    )

    assert rc == 0  # MUST be 0; exit 2 would block the prompt
    assert "<system-reminder>" in out  # but the reminder still flows on stdout
    assert "task1" in out
    assert not marker.exists()


def test_inbox_drain_unknown_event_defaults_to_exit_0(tmp_path, monkeypatch):
    """No hook payload (manual invocation, future event types) → exit 0.
    Safer default than 2 because exit 2 has hook-specific blocking
    semantics elsewhere."""
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    _drop_marker(tmp_path)

    rc, out = _run_drain(monkeypatch, stdin_payload="")

    assert rc == 0
    assert "<system-reminder>" in out


def test_inbox_drain_empty_inbox_exits_0_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path))
    # Inbox dir exists but is empty
    from panel_cli.inbox_drain import main

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main()
    assert rc == 0
    assert buf.getvalue() == ""


def test_inbox_drain_missing_inbox_exits_0(tmp_path, monkeypatch):
    """Hook fires globally — non-Panel projects must no-op cheaply."""
    monkeypatch.setenv("PANEL_INBOX_DIR", str(tmp_path / "does-not-exist"))
    from panel_cli.inbox_drain import main

    rc = main()
    assert rc == 0


# --- panel_cli.install_hooks: idempotent install/uninstall --------------------


def test_install_creates_settings_json_when_missing(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    from panel_cli.install_hooks import install

    summary = install(command="/usr/bin/panel-inbox-drain")
    assert summary["changed"] is True
    assert "Stop" in summary["actions"]
    assert "UserPromptSubmit" in summary["actions"]
    assert summary["actions"]["Stop"] == "added"
    assert summary["backup_path"] is None  # no prior file to back up

    body = json.loads(settings.read_text())
    stop_entries = body["hooks"]["Stop"]
    assert len(stop_entries) == 1
    # Wrapper shape (verified against /doctor's schema validator): every
    # hook event entry is {matcher, hooks: [...]} regardless of whether
    # the event uses the matcher.
    entry = stop_entries[0]
    assert entry["matcher"] == "*"
    assert isinstance(entry["hooks"], list) and len(entry["hooks"]) == 1
    h = entry["hooks"][0]
    assert h["type"] == "command"
    assert h["command"] == "/usr/bin/panel-inbox-drain"
    assert h["asyncRewake"] is True
    assert h["_panel_managed"] is True


def test_install_is_idempotent(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    from panel_cli.install_hooks import install

    s1 = install(command="/usr/bin/panel-inbox-drain")
    assert s1["changed"] is True

    s2 = install(command="/usr/bin/panel-inbox-drain")
    assert s2["changed"] is False
    for v in s2["actions"].values():
        assert v == "unchanged"


def test_install_preserves_user_hooks(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    # User has a pre-existing unrelated hook
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": "echo hello"}],
                        }
                    ]
                },
                "model": "sonnet",
            }
        )
    )
    from panel_cli.install_hooks import install

    install(command="/usr/bin/panel-inbox-drain")
    body = json.loads(settings.read_text())

    # Unrelated user hook still there
    stop_entries = body["hooks"]["Stop"]
    user_entries = [
        e
        for e in stop_entries
        if isinstance(e.get("hooks"), list)
        and any(h.get("command") == "echo hello" for h in e["hooks"])
    ]
    assert len(user_entries) == 1
    # Panel-managed entry added alongside (wrapper shape, marker on inner hook)
    panel_entries = [
        e
        for e in stop_entries
        if isinstance(e.get("hooks"), list)
        and any(h.get("_panel_managed") for h in e["hooks"])
    ]
    assert len(panel_entries) == 1
    # Top-level keys preserved
    assert body["model"] == "sonnet"


def test_install_backs_up_existing_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    settings.write_text(json.dumps({"model": "opus"}))
    from panel_cli.install_hooks import install

    summary = install(command="/usr/bin/panel-inbox-drain")
    assert summary["backup_path"] is not None
    backup = Path(summary["backup_path"])
    assert backup.exists()
    assert json.loads(backup.read_text()) == {"model": "opus"}


def test_uninstall_removes_only_managed_entries(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    from panel_cli.install_hooks import install, uninstall

    # User hook + install ours alongside
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"matcher": "*", "hooks": [{"type": "command", "command": "echo user"}]},
                    ]
                }
            }
        )
    )
    install(command="/usr/bin/panel-inbox-drain")

    # Verify both present
    body = json.loads(settings.read_text())
    assert len(body["hooks"]["Stop"]) == 2

    summary = uninstall()
    assert summary["changed"] is True
    body = json.loads(settings.read_text())
    # User hook still there (legacy nested shape, untouched)
    assert len(body["hooks"]["Stop"]) == 1
    user_remaining = body["hooks"]["Stop"][0]
    assert user_remaining["hooks"][0]["command"] == "echo user"
    # UserPromptSubmit was only ours, so its key is gone
    assert "UserPromptSubmit" not in body["hooks"]


def test_is_installed_detects_state(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    from panel_cli.install_hooks import install, is_installed, uninstall

    assert is_installed() is False
    install(command="/usr/bin/panel-inbox-drain")
    assert is_installed() is True
    uninstall()
    assert is_installed() is False


def test_ensure_installed_respects_env_flag(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("PANEL_AUTO_INSTALL_HOOKS", "0")
    from panel_cli.install_hooks import ensure_installed, is_installed

    summary = ensure_installed()
    assert summary is None
    assert is_installed() is False  # opt-out respected


def test_ensure_installed_idempotent_fast_path(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("PANEL_AUTO_INSTALL_HOOKS", "1")
    from panel_cli.install_hooks import ensure_installed

    s1 = ensure_installed()
    assert s1 is not None and s1["changed"] is True

    s2 = ensure_installed()
    # Already installed → fast path returns None without re-writing
    assert s2 is None


def test_install_migrates_broken_flat_panel_entry_to_wrapper(tmp_path, monkeypatch):
    """REGRESSION: An interim version of install() wrote Panel hooks in
    a FLAT shape (no matcher / hooks: [...] wrapper) based on a misread
    of the docs. Claude Code's schema validator rejects that with
    'hooks: Expected array, but received undefined' and skips the entire
    settings.json — silently disabling every other user hook in the file
    too. A re-install must detect the broken flat-shape entry and
    replace it with the correct wrapper shape, not duplicate it.

    Mirrors exactly what /doctor caught in live testing on 2026-05-07."""
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    # Simulate the broken interim flat-shape install output:
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "type": "command",
                            "command": "/old/panel-inbox-drain",
                            "_panel_managed": True,
                            "async": True,
                            "asyncRewake": True,
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "type": "command",
                            "command": "/old/panel-inbox-drain",
                            "_panel_managed": True,
                        }
                    ],
                }
            }
        )
    )
    from panel_cli.install_hooks import install

    summary = install(command="/usr/bin/panel-inbox-drain")
    assert summary["changed"] is True
    # Should have UPDATED in place, not appended a duplicate
    body = json.loads(settings.read_text())
    stop = body["hooks"]["Stop"]
    ups = body["hooks"]["UserPromptSubmit"]
    assert len(stop) == 1
    assert len(ups) == 1
    # New entries use the WRAPPER shape that /doctor accepts
    assert stop[0]["matcher"] == "*"
    assert isinstance(stop[0]["hooks"], list)
    inner = stop[0]["hooks"][0]
    assert inner["type"] == "command"
    assert inner["command"] == "/usr/bin/panel-inbox-drain"
    assert inner["asyncRewake"] is True
    assert ups[0]["matcher"] == "*"
    assert ups[0]["hooks"][0]["type"] == "command"


def test_install_refuses_corrupt_settings_json(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    settings.write_text("{ not valid json")
    from panel_cli.install_hooks import install

    with pytest.raises(RuntimeError, match="Cannot parse"):
        install(command="/usr/bin/panel-inbox-drain")

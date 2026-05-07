"""Atomic-rename completion-marker writer for Panel's task inbox.

When a background task finishes, Panel writes a small JSON marker into
``~/.panel/inbox/<task_id>.json``. A Claude Code hook (installed once via
``panel-install-hooks``) drains the inbox on Stop / UserPromptSubmit events,
prints a ``<system-reminder>`` block, and exits with code 2 to wake the
model. The hook lives in the user's ``~/.claude/settings.json``; this module
just produces the markers.

Why a separate module? The judge synthesis from the round-2 panel call
explicitly recommended a dedicated `utils/task_inbox.py` so the write
logic is testable in isolation from the TaskManager lifecycle. Keeping
the TaskManager free of filesystem boilerplate also keeps the hot path
in ``tools/tasks.py`` readable.

Atomicity contract (POSIX rename guarantees no partial-read):

    write payload    → ~/.panel/inbox/.staging/<task_id>.<pid>.json
    fsync + close
    os.replace(...)  → ~/.panel/inbox/<task_id>.json     (atomic)

The hook claims a marker by a SECOND atomic rename to
``<task_id>.processing.<pid>`` — that prevents two concurrently-firing
hooks (e.g. Stop + UserPromptSubmit firing together) from double-injecting
the same completion. The drain script reclaims abandoned ``.processing.*``
files older than a TTL in case a hook crashed mid-injection.

Disabling: set ``PANEL_INBOX_DISABLE=1`` to silently skip marker writes
(e.g. for tests, or for users who don't run Claude Code).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("panel.task_inbox")

SCHEMA_VERSION = 1


def inbox_dir(override: Optional[str] = None) -> Path:
    """Resolve the inbox directory. ``PANEL_INBOX_DIR`` env wins, then the
    explicit override arg, else ``~/.panel/inbox``."""
    env = os.environ.get("PANEL_INBOX_DIR")
    if env:
        return Path(env).expanduser()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".panel" / "inbox"


def is_disabled() -> bool:
    return os.environ.get("PANEL_INBOX_DISABLE", "").strip() not in ("", "0", "false", "False")


def write_completion_marker(
    *,
    task_id: str,
    tool: str,
    label: Optional[str],
    status: str,
    created_at: Optional[float],
    completed_at: Optional[float],
    elapsed_seconds: Optional[float],
    run_id: Optional[str] = None,
    error: Optional[str] = None,
    inbox_override: Optional[str] = None,
) -> Optional[Path]:
    """Atomically write a completion marker to the inbox.

    Returns the final marker path on success, or None when disabled / on
    failure. Best-effort: failures log at debug and return None — the live
    task path must NEVER break because we couldn't write a hook marker.
    """
    if is_disabled():
        return None

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": "panel.task.finished",
        "task_id": task_id,
        "tool": tool,
        "label": label,
        "status": status,
        "created_at": created_at,
        "completed_at": completed_at,
        "elapsed_seconds": elapsed_seconds,
        "run_id": run_id,
        "result_hint": (
            f"Call task_result('{task_id}') for the synthesised output, or "
            f"run_tree(<run_id>, mode='transcript') for panelist verdicts."
        ),
        "error": error,
    }

    try:
        base = inbox_dir(inbox_override)
        staging = base / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        # 0700 on the directories so other users on the box can't read
        # task labels / errors. Best effort — chmod silently ignored on
        # filesystems that don't support it (e.g. some Windows mounts).
        try:
            os.chmod(base, 0o700)
            os.chmod(staging, 0o700)
        except OSError:
            pass

        tmp_name = f"{task_id}.{os.getpid()}.json.tmp"
        tmp_path = staging / tmp_name
        final_path = base / f"{task_id}.json"

        # Write + fsync so the rename target is durable on disk before we
        # publish it. Without fsync, a crash between write and rename can
        # leave the inbox with an empty target on some filesystems.
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # mode=0o600 so the marker file isn't world-readable
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

        # ``os.replace`` is the cross-platform atomic-rename — replaces
        # final_path if it already exists (idempotent if the same
        # task_id is written twice for whatever reason).
        os.replace(tmp_path, final_path)
        return final_path
    except Exception as exc:  # noqa: BLE001 — best-effort; never fail the task path
        logger.debug("task_inbox: failed to write marker for %s: %s", task_id, exc)
        return None


def list_pending_markers(inbox_override: Optional[str] = None) -> list[Path]:
    """List ``<task_id>.json`` files currently in the inbox (excluding
    ``.staging/`` and ``.processing.*`` files). Used by the drain script
    and tests."""
    base = inbox_dir(inbox_override)
    if not base.exists():
        return []
    out: list[Path] = []
    for p in base.iterdir():
        if not p.is_file():
            continue
        # Skip claimed markers (.processing.<pid>) — those are mid-injection
        if ".processing." in p.name:
            continue
        if p.suffix != ".json":
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.stat().st_mtime if p.exists() else 0)


def reclaim_stale_processing(
    inbox_override: Optional[str] = None,
    ttl_seconds: float = 120.0,
) -> int:
    """Re-rename ``.processing.<pid>`` files back to ``<task_id>.json`` if
    they're older than ``ttl_seconds`` — a hook crashed before deletion.
    Returns the count reclaimed."""
    base = inbox_dir(inbox_override)
    if not base.exists():
        return 0
    now = time.time()
    reclaimed = 0
    for p in base.iterdir():
        if not p.is_file():
            continue
        # Match <task_id>.json.processing.<pid> OR <task_id>.processing.<pid>
        if ".processing." not in p.name:
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < ttl_seconds:
            continue
        # Strip the .processing.<pid> suffix to recover the original name.
        # Names look like "abc.json.processing.12345"; we restore "abc.json".
        original = p.name
        idx = original.find(".processing.")
        if idx <= 0:
            continue
        target = base / original[:idx]
        try:
            os.replace(p, target)
            reclaimed += 1
        except OSError:
            continue
    return reclaimed

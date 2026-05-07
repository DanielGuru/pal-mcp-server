"""Standalone hook script: drain Panel's task-completion inbox.

Wired into the user's ``~/.claude/settings.json`` as the command for a
``Stop`` and ``UserPromptSubmit`` hook by ``panel-install-hooks``. Runs on
every Stop / UserPromptSubmit event. Cheap fast-path when the inbox is
empty (the common case in non-Panel projects).

Behaviour:
1. If the inbox dir doesn't exist or is empty → exit 0 silently.
2. For each ``<task_id>.json`` marker, atomic-rename to
   ``<task_id>.json.processing.<pid>`` to claim it. Lost rename = another
   hook instance won; skip silently.
3. Read claimed marker, format a one-liner, delete the processing file
   on success.
4. Reclaim ``.processing.<pid>`` files older than 120s (a hook crashed
   mid-injection); they get re-renamed to ``<task_id>.json`` for the
   next drain to retry.
5. If anything was processed, print a single ``<system-reminder>`` block
   to stdout and exit with code 2 — Claude Code's hook contract treats
   exit-code-2 stdout as a system reminder injection, waking the agent.
6. Otherwise exit 0 (hook had nothing to inject; don't disturb the
   conversation).

This script is deliberately self-contained: no Panel imports, only Python
stdlib. That way it runs under whatever Python the user's hook environment
provides, even if Panel itself is uninstalled or in a venv.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

INBOX_DIR_DEFAULT = Path.home() / ".panel" / "inbox"
PROCESSING_TTL_S = 120.0


def _inbox_dir() -> Path:
    env = os.environ.get("PANEL_INBOX_DIR")
    if env:
        return Path(env).expanduser()
    return INBOX_DIR_DEFAULT


def _format_marker(payload: dict) -> str:
    """Build a single readable line for the system-reminder block."""
    task_id = payload.get("task_id", "?")
    label = payload.get("label") or payload.get("tool") or "panel task"
    status = payload.get("status", "?")
    elapsed = payload.get("elapsed_seconds")
    error = payload.get("error")
    elapsed_str = f"{elapsed:.0f}s" if isinstance(elapsed, (int, float)) else "?s"

    if error:
        return f"Panel task {task_id} ({label}) {status} after {elapsed_str} — error: {error}"
    return f"Panel task {task_id} ({label}) {status} in {elapsed_str}"


def _reclaim_stale(inbox: Path) -> None:
    """Re-rename .processing.<pid> files older than the TTL back to <id>.json."""
    if not inbox.exists():
        return
    now = time.time()
    for p in inbox.iterdir():
        if not p.is_file() or ".processing." not in p.name:
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < PROCESSING_TTL_S:
            continue
        idx = p.name.find(".processing.")
        if idx <= 0:
            continue
        target = inbox / p.name[:idx]
        try:
            os.replace(p, target)
        except OSError:
            pass


def main() -> int:
    inbox = _inbox_dir()
    if not inbox.exists() or not inbox.is_dir():
        return 0

    # Try to reclaim crashed-hook leftovers BEFORE listing — so a marker
    # stuck in .processing for >2min comes back into the queue this run.
    _reclaim_stale(inbox)

    pid = os.getpid()
    messages: list[str] = []
    seen_run_ids: list[str] = []

    for marker in sorted(inbox.glob("*.json")):
        # Skip our own staging directory's leftovers (shouldn't appear at
        # the top level but defend against anyone moving things around).
        if marker.name.startswith("."):
            continue

        # Atomic claim via second rename. ``.processing.<pid>`` makes it
        # unambiguous which drain instance is processing the marker —
        # lets the stale-reclaim logic above know who owns each file.
        claimed = inbox / f"{marker.name}.processing.{pid}"
        try:
            os.replace(marker, claimed)
        except (OSError, FileNotFoundError):
            # Another hook beat us to it. Move on.
            continue

        try:
            text = claimed.read_text(encoding="utf-8")
            payload = json.loads(text)
        except (OSError, json.JSONDecodeError):
            # Corrupt / unreadable. Leave it as .processing so the
            # stale-reclaim TTL re-tries later. After enough failures
            # the file will keep cycling — operator-visible.
            continue

        messages.append(_format_marker(payload))
        run_id = payload.get("run_id")
        if isinstance(run_id, str) and run_id:
            seen_run_ids.append(run_id)

        # Successful inject — drop the processing file.
        try:
            claimed.unlink()
        except OSError:
            pass

    if not messages:
        return 0

    # Build a single system-reminder block. Claude Code's hook contract
    # turns this into model-visible context when we exit 2.
    body = "\n".join(messages)
    if seen_run_ids:
        body += (
            "\n\nFetch results: task_result(<task_id>) for synthesised output, "
            "or run_tree('<run_id>', mode='transcript') for panelist verdicts."
        )
    sys.stdout.write(f"<system-reminder>\n{body}\n</system-reminder>\n")
    sys.stdout.flush()
    # exit 2 → Claude Code injects stdout as a system reminder
    return 2


if __name__ == "__main__":
    sys.exit(main())

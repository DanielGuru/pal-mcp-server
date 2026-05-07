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
   to stdout. Exit code is event-specific (Claude Code hook contract):
     - ``Stop`` (with ``asyncRewake: true``): exit 2 → wake the model and
       inject stdout as a system reminder.
     - ``UserPromptSubmit``: exit 0 → stdout is appended to the user's
       prompt as ``additionalContext``. Exit 2 here BLOCKS the user's
       prompt entirely (a pre-fix bug we hit on first install).
     - Anything else / no event info: exit 0 — safest default.
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

# Stop-hook watch loop: when the hook fires (right after Claude finishes
# its turn), the panel is usually still running and the inbox is empty.
# To make ``asyncRewake`` actually wake the model on completion, we poll
# the inbox for up to ``STOP_WATCH_TIMEOUT_S`` and exit 2 the moment a
# marker appears. UserPromptSubmit and unknown events drain-once because
# the user is already here — they don't want a 15-minute hang.
STOP_WATCH_TIMEOUT_S = float(os.environ.get("PANEL_STOP_WATCH_TIMEOUT_S", "900"))
STOP_WATCH_POLL_S = float(os.environ.get("PANEL_STOP_WATCH_POLL_S", "1.0"))

# Singleton-watcher lease (panel-flagged): without this, every Stop hook
# spawns its OWN 15-min poller. After N user turns in a long session, you
# end up with N concurrent pollers. Atomic per-marker claim prevents
# duplicate INJECTION but doesn't bound process count. Lease file lives
# next to the markers; first hook to call O_CREAT|O_EXCL wins, others
# drain-once and exit 0. PID liveness check (kill -0) lets us reclaim a
# stale lease left by a crashed/killed prior watcher.
WATCH_LOCK_NAME = ".watch.lock"


def _inbox_dir() -> Path:
    env = os.environ.get("PANEL_INBOX_DIR")
    if env:
        return Path(env).expanduser()
    return INBOX_DIR_DEFAULT


def _format_marker(payload: dict) -> str:
    """Format one marker as a system-reminder block. The header is always
    present (task_id + label + status + elapsed). When the marker carries
    a ``transcript_digest`` (panel-family results), append it after a
    blank line so the model lands with the verdict already in context —
    no follow-up ``task_result`` call needed. Long-running clink/chat
    background tasks that don't emit a digest just get the header line.
    """
    task_id = payload.get("task_id", "?")
    label = payload.get("label") or payload.get("tool") or "panel task"
    status = payload.get("status", "?")
    elapsed = payload.get("elapsed_seconds")
    error = payload.get("error")
    elapsed_str = f"{elapsed:.0f}s" if isinstance(elapsed, (int, float)) else "?s"

    if error:
        return f"Panel task {task_id} ({label}) {status} after {elapsed_str} — error: {error}"

    header = f"Panel task {task_id} ({label}) {status} in {elapsed_str}"
    digest = payload.get("transcript_digest")
    if isinstance(digest, str) and digest.strip():
        return f"{header}\n\n{digest.strip()}"
    return header


def _pid_is_alive(pid: int) -> bool:
    """``kill -0`` liveness check. Returns False for nonexistent processes,
    True for live ones (including cross-user where we can't signal but the
    PID is real). Platform handling:

    - **POSIX**: ``ProcessLookupError`` → dead. ``PermissionError`` → alive
      (real cross-user case — someone else's process). ``OSError`` with
      ESRCH → dead. Other ``OSError`` → dead (panel-flagged: previously
      "alive" was the safer default, but on Windows that meant a stale
      lease never got reclaimed).
    - **Windows**: ``os.kill(pid, 0)`` raises generic ``OSError`` /
      ``AttributeError`` for both nonexistent and live processes. Without
      a clean way to distinguish, treat as DEAD so a stale lease is
      always reclaimable (worst case: another live watcher loses its
      lease and re-acquires on the next poll tick — recoverable).
    """
    import errno
    import sys as _sys

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # POSIX: process exists but we can't signal it (different uid).
        # Alive for our purposes.
        return True
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ESRCH:
            return False
        # On Windows, generic OSError is the "no such process" failure
        # mode. Tighten: assume dead so stale locks get reclaimed.
        return False
    except AttributeError:
        # os.kill missing entirely (extreme edge cases). Be conservative
        # on POSIX (assume alive), aggressive on Windows (assume dead).
        return _sys.platform != "win32"


def _try_acquire_watch_lock(inbox: Path) -> Optional[Path]:
    """Try to become the singleton long-poll watcher. Returns the lock
    path on success (caller MUST release it via ``_release_watch_lock``);
    returns ``None`` if a live watcher already holds the lease.

    TOCTOU-safe reclaim path (panel finding):

    - The window between ``O_CREAT|O_EXCL`` succeeding and the holder
      writing its PID is small but real. A reclaiming process could
      observe an empty lock file mid-creation. Treat empty / unreadable
      lock contents as ALIVE (within a short grace window) rather than
      stale, so we don't race-delete a fresh lock.
    - Reclaim uses atomic ``os.replace`` to a unique
      ``.watch.lock.stale.<our-pid>.<ts>`` path — NOT ``unlink``. If
      another process raced and replaced the lock between our read and
      our reclaim, the rename either moves THEIR fresh lock to a
      uniquely-named stale file (we then re-attempt creation and find
      it gone, win) OR fails because the path no longer matches what we
      expected. Either way, no live lock gets blindly deleted.

    Reclaim is bounded: at most 2 attempts. If we keep losing the race,
    yield rather than spin."""
    lock_path = inbox / WATCH_LOCK_NAME
    pid = os.getpid()
    grace_age_s = 5.0  # treat lock files younger than this as "alive even
    # if PID is missing" — gives the holder time to write the PID.

    for attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Existing holder — alive, mid-creation, or stale?
            holder_alive = False
            try:
                contents = lock_path.read_text(encoding="utf-8").strip()
                # Empty or non-numeric → could be mid-creation. Use the
                # file's mtime as a coarse "is the holder still in the
                # write window?" check. Files older than the grace
                # period with empty/unparseable contents are stale.
                try:
                    holder_pid = int(contents)
                except ValueError:
                    age = max(0.0, time.time() - lock_path.stat().st_mtime)
                    if age < grace_age_s:
                        holder_alive = True  # presume mid-write
                        holder_pid = -1
                    else:
                        holder_pid = -1
                else:
                    if holder_pid > 0 and _pid_is_alive(holder_pid):
                        holder_alive = True
            except FileNotFoundError:
                # Disappeared between our open() and our read — retry
                # the whole acquire. The race is effectively resolved.
                continue
            except OSError:
                # Couldn't read it — be conservative within the grace
                # window. Outside it, treat as stale.
                try:
                    age = max(0.0, time.time() - lock_path.stat().st_mtime)
                    holder_alive = age < grace_age_s
                except OSError:
                    holder_alive = False
                holder_pid = -1
            if holder_alive:
                return None  # live watcher (or in-flight write)
            # Stale — atomic-rename to a unique stale name so we don't
            # race-delete a successor that just took over.
            stale_path = inbox / (
                f"{WATCH_LOCK_NAME}.stale.{pid}.{int(time.time() * 1000)}"
            )
            if attempt == 0:
                try:
                    os.replace(lock_path, stale_path)
                except FileNotFoundError:
                    pass  # someone else got there first; retry create
                except OSError:
                    return None
                else:
                    # Best-effort cleanup of the moved-aside lock.
                    try:
                        stale_path.unlink()
                    except OSError:
                        pass
                continue
            return None
        else:
            try:
                # Write PID immediately after creation so the
                # mid-creation grace window is as short as possible.
                os.write(fd, str(pid).encode("ascii"))
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
            return lock_path
    return None


def _release_watch_lock(lock_path: Path) -> None:
    """Release the lease. Verify ownership (PID match) before unlinking
    so a slow-shutting-down process doesn't accidentally wipe a fresh
    successor's lock."""
    try:
        contents = lock_path.read_text(encoding="utf-8").strip()
        holder_pid = int(contents)
    except (OSError, ValueError):
        return
    if holder_pid == os.getpid():
        try:
            lock_path.unlink()
        except OSError:
            pass


def _parent_orphaned() -> bool:
    """``os.getppid() == 1`` on POSIX means the parent (Claude Code) died
    and we got reparented to init. We should exit cleanly rather than keep
    polling on a dead conversation."""
    try:
        return os.getppid() == 1
    except (OSError, AttributeError):
        return False


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


def _detect_hook_event() -> str:
    """Claude Code passes a JSON object on stdin describing the hook event,
    including ``hook_event_name``. Read it best-effort. Returns the event
    name (e.g. ``"UserPromptSubmit"``, ``"Stop"``) or ``""`` if stdin isn't
    a pipe / payload missing / not JSON. Never raises."""
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return ""
    if not raw or not raw.strip():
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict):
        name = payload.get("hook_event_name")
        if isinstance(name, str):
            return name
    return ""


def _exit_code_for(event: str, did_inject: bool) -> int:
    """Pick the right exit code for the event we're running under.

    For ``Stop`` with ``asyncRewake: true``, exit 2 + stdout is the wake-up
    contract. For ``UserPromptSubmit``, exit 2 *blocks* the user's prompt —
    we must exit 0 there and let stdout flow through as additionalContext.
    Any unknown event defaults to exit 0 (safest)."""
    if not did_inject:
        return 0
    if event == "Stop":
        return 2
    return 0  # UserPromptSubmit, unknown, or no event info


def _drain_pending(inbox: Path) -> tuple[list[str], list[str]]:
    """Claim and process every marker currently in the inbox. Returns
    ``(messages, run_ids)``. Atomic .processing.<pid> claim so concurrent
    drain processes don't double-inject the same completion."""
    pid = os.getpid()
    messages: list[str] = []
    run_ids: list[str] = []

    for marker in sorted(inbox.glob("*.json")):
        # Skip dotfiles (staging dir, anything we deliberately tucked
        # away), the lease lock itself, and any moved-aside stale-lock
        # files from the reclaim path. None of these are completion
        # markers.
        if marker.name.startswith("."):
            continue
        if marker.name == WATCH_LOCK_NAME:
            continue
        if WATCH_LOCK_NAME + ".stale." in marker.name:
            continue
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
            # Leave the .processing file for the stale-reclaim TTL to
            # retry — repeated failures stay operator-visible.
            continue
        messages.append(_format_marker(payload))
        rid = payload.get("run_id")
        if isinstance(rid, str) and rid:
            run_ids.append(rid)
        try:
            claimed.unlink()
        except OSError:
            pass
    return messages, run_ids


def _emit_reminder(messages: list[str], run_ids: list[str]) -> None:
    """Write a single ``<system-reminder>`` block to stdout. Claude Code
    treats this as injectable context when the hook exits with the right
    code (2 for Stop+asyncRewake, 0 for UserPromptSubmit)."""
    body = "\n".join(messages)
    if run_ids:
        body += (
            "\n\nFetch results: task_result(<task_id>) for synthesised output, "
            "or run_tree('<run_id>', mode='transcript') for panelist verdicts."
        )
    sys.stdout.write(f"<system-reminder>\n{body}\n</system-reminder>\n")
    sys.stdout.flush()


def main() -> int:
    # Read the hook event FIRST so we know how to behave. Claude Code may
    # pass JSON on stdin even when there's nothing to drain — we still
    # consume it so future protocol additions don't leave bytes unread.
    event = _detect_hook_event()

    inbox = _inbox_dir()
    if not inbox.exists() or not inbox.is_dir():
        return 0

    # Stop hook with asyncRewake: at hook fire-time the panel is still
    # running and the inbox is empty. The first hook to acquire the
    # singleton lease enters the watch loop; subsequent Stop hooks (each
    # later turn fires a fresh one) drain-once-and-yield so we never
    # accumulate N concurrent pollers across a long session.
    if event == "Stop":
        lock_path = _try_acquire_watch_lock(inbox)
        if lock_path is None:
            # Another live watcher is holding the lease — just drain
            # whatever's already pending and exit cleanly. The lease
            # holder will catch any future arrivals.
            _reclaim_stale(inbox)
            messages, run_ids = _drain_pending(inbox)
            if messages:
                _emit_reminder(messages, run_ids)
                return 2  # still wake on already-completed work
            return 0

        try:
            deadline = time.time() + STOP_WATCH_TIMEOUT_S
            while True:
                if _parent_orphaned():
                    # Claude Code died; nobody listening for our exit-2.
                    return 0
                _reclaim_stale(inbox)
                messages, run_ids = _drain_pending(inbox)
                if messages:
                    _emit_reminder(messages, run_ids)
                    return 2  # asyncRewake wakes the model
                if time.time() >= deadline:
                    return 0  # nothing arrived within the watch window
                time.sleep(STOP_WATCH_POLL_S)
        finally:
            _release_watch_lock(lock_path)

    # UserPromptSubmit / unknown / no event info: drain-once. The user is
    # actively here — don't hang the prompt waiting for a possible future
    # marker. If there's nothing now, surface nothing and let them go.
    _reclaim_stale(inbox)
    messages, run_ids = _drain_pending(inbox)
    if not messages:
        return 0
    _emit_reminder(messages, run_ids)
    return _exit_code_for(event, did_inject=True)


if __name__ == "__main__":
    sys.exit(main())

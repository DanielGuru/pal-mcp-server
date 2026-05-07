"""Install / uninstall the Panel inbox-drain hook in ``~/.claude/settings.json``.

Wires Panel's task-completion notifications into Claude Code by adding two
hook entries:
  - ``Stop`` with ``asyncRewake: true`` — drains the inbox after Claude
    stops; exit-code-2 stdout wakes the model with a ``<system-reminder>``
    even when the user is idle. This is the "true push" path.
  - ``UserPromptSubmit`` — drains when the user types, in case
    ``asyncRewake`` isn't supported in the user's Claude Code version.
    Belt-and-braces.

Both hooks invoke the same ``panel-inbox-drain`` script (registered as a
console_scripts entry point in ``pyproject.toml``). The hooks are tagged
with ``"_panel_managed": true`` so we can find and update or remove them
idempotently across upgrades.

Auto-installed on first MCP server boot (default ON). Override with
``PANEL_AUTO_INSTALL_HOOKS=0``. Manual control via:

    panel-install-hooks       # add or refresh the hooks
    panel-uninstall-hooks     # remove only Panel-managed entries

Settings file is BACKED UP to
``~/.claude/settings.json.panel-backup-<unix-ts>`` before the first
modification each install run.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("panel.install_hooks")

# Marker we attach to entries so we can find and replace/remove our own
# hooks without disturbing user-installed ones. Older formats without the
# marker are detected by command shape — see ``_is_panel_drain_command``.
MANAGED_MARKER = "_panel_managed"
DRAIN_COMMAND_NAME = "panel-inbox-drain"

# Hook event names we install on. Stop is the primary wake-up; UserPromptSubmit
# is the no-asyncRewake fallback. Keep the list short — every hook adds
# overhead to every Claude turn.
HOOK_EVENTS = ("Stop", "UserPromptSubmit")


def settings_path(override: Optional[str] = None) -> Path:
    """Resolve the Claude Code settings.json path. ``CLAUDE_SETTINGS_PATH``
    env wins, then the explicit override, else the default
    ``~/.claude/settings.json``."""
    env = os.environ.get("CLAUDE_SETTINGS_PATH")
    if env:
        return Path(env).expanduser()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "settings.json"


def resolve_drain_command() -> str:
    """Where on PATH is ``panel-inbox-drain``? Prefer an absolute path so the
    hook works regardless of which shell/PATH Claude Code spawns the hook
    under. Falls back to the bare name if not on PATH yet (e.g. installed
    in a uv tool dir not currently exported)."""
    found = shutil.which(DRAIN_COMMAND_NAME)
    return found or DRAIN_COMMAND_NAME


def _is_panel_drain_command(cmd: str) -> bool:
    """Heuristic for older / hand-edited hook entries: does the command
    invoke our drain script? Used to avoid leaving stale entries when
    upgrading from a pre-marker version."""
    if not isinstance(cmd, str):
        return False
    # Match either the bare entry-point name or any path ending in it
    return cmd.endswith(DRAIN_COMMAND_NAME) or cmd.endswith(f"/{DRAIN_COMMAND_NAME}")


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        loaded = json.loads(text)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot parse {path}: {exc}. Refusing to overwrite — fix the file "
            "manually or delete it and re-run."
        ) from exc


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic JSON write via tmp + rename. Preserves indentation so the
    user can still hand-edit settings.json comfortably."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _backup(path: Path) -> Optional[Path]:
    """Snapshot settings.json before mutating. Returns the backup path or
    None if the source file didn't exist."""
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + f".panel-backup-{int(time.time())}")
    shutil.copy2(path, backup)
    return backup


def _build_hook_entry(event: str, command: str) -> dict[str, Any]:
    """Build a hook entry in the shape Claude Code expects for this event.

    Two distinct shapes per the official docs at code.claude.com/docs/en/hooks:

    1. **Flat shape** for ``UserPromptSubmit``, ``Stop``, ``Notification``,
       ``SessionStart`` etc. — events that always fire (no matcher):

       .. code-block:: json

          {"type": "command", "command": "..."}

    2. **Nested-with-matcher** for ``PreToolUse`` / ``PostToolUse`` etc.
       (here for completeness, not used by Panel today):

       .. code-block:: json

          {"matcher": "<tool>", "hooks": [{"type": "command", ...}]}

    Earlier versions of this module always used shape #2 — including for
    UserPromptSubmit/Stop. Claude Code silently skipped those entries
    because it looks for ``type`` at the top level on no-matcher events,
    didn't find it (saw ``matcher`` + ``hooks`` instead), and the hook
    never fired. Caught in live e2e test where a UserPromptSubmit prompt
    on a non-empty inbox didn't drain markers.

    The ``Stop`` variant adds ``asyncRewake: true`` to opt into idle wake-up
    (exit 2 + stdout wakes the model even if the user is idle).
    """
    hook_obj: dict[str, Any] = {
        "type": "command",
        "command": command,
        # Marker for managed-entry detection on subsequent installs/uninstalls.
        MANAGED_MARKER: True,
    }
    if event == "Stop":
        hook_obj["async"] = True
        hook_obj["asyncRewake"] = True
    # Flat shape: event entry IS the hook itself, not a wrapper.
    return hook_obj


def _is_managed_entry(entry: Any) -> bool:
    """Does this top-level ``hooks[event][i]`` entry belong to Panel?

    Handles both the new flat shape (entry = hook obj) and the legacy
    nested shape (entry = {matcher, hooks: [...]}) so an `uninstall` /
    re-install on an old broken settings.json can find and clean up the
    pre-fix entries instead of leaving them orphaned.
    """
    if not isinstance(entry, dict):
        return False
    # Flat shape (current) — entry IS the hook.
    if entry.get(MANAGED_MARKER) is True:
        return True
    if "command" in entry and _is_panel_drain_command(entry.get("command", "")):
        return True
    # Legacy nested shape — entry wraps a `hooks: [...]` list.
    nested = entry.get("hooks")
    if isinstance(nested, list) and nested:
        return all(
            isinstance(h, dict)
            and (h.get(MANAGED_MARKER) is True or _is_panel_drain_command(h.get("command", "")))
            for h in nested
        )
    return False


# Keep the old name as an alias so any external callers / tests don't break.
_is_managed_matcher = _is_managed_entry


def install(*, settings_override: Optional[str] = None, command: Optional[str] = None) -> dict[str, Any]:
    """Idempotent install. Returns a dict describing what changed.

    Behaviour:
    - If a managed entry already exists with the same command and shape,
      report no-op.
    - If a managed entry exists but is stale (different command path,
      missing marker), update it in place.
    - If no managed entry exists, append one for each event in HOOK_EVENTS.
    """
    path = settings_path(settings_override)
    cmd = command or resolve_drain_command()
    settings = _load_settings(path)

    hooks_root = settings.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        raise RuntimeError(
            f"Refusing to mutate {path}: 'hooks' is present but not an object "
            f"(found {type(hooks_root).__name__}). Fix manually."
        )

    summary: dict[str, Any] = {
        "settings_path": str(path),
        "command": cmd,
        "events": [],
        "actions": {},
        "backup_path": None,
    }
    changed = False

    for event in HOOK_EVENTS:
        entries = hooks_root.setdefault(event, [])
        if not isinstance(entries, list):
            raise RuntimeError(
                f"Refusing to mutate {path}: hooks.{event} is not a list "
                f"(found {type(entries).__name__}). Fix manually."
            )

        managed_index = None
        for i, e in enumerate(entries):
            if _is_managed_matcher(e):
                managed_index = i
                break

        new_entry = _build_hook_entry(event, cmd)

        if managed_index is None:
            entries.append(new_entry)
            summary["actions"][event] = "added"
            changed = True
        else:
            existing = entries[managed_index]
            if existing != new_entry:
                entries[managed_index] = new_entry
                summary["actions"][event] = "updated"
                changed = True
            else:
                summary["actions"][event] = "unchanged"
        summary["events"].append(event)

    if changed:
        backup = _backup(path)
        if backup is not None:
            summary["backup_path"] = str(backup)
        _atomic_write_json(path, settings)
        summary["changed"] = True
    else:
        summary["changed"] = False
    return summary


def uninstall(*, settings_override: Optional[str] = None) -> dict[str, Any]:
    """Remove all Panel-managed hook entries. Returns a dict describing
    what was removed. If the settings file doesn't exist, no-op."""
    path = settings_path(settings_override)
    summary: dict[str, Any] = {"settings_path": str(path), "removed": {}, "changed": False, "backup_path": None}
    if not path.exists():
        return summary

    settings = _load_settings(path)
    hooks_root = settings.get("hooks")
    if not isinstance(hooks_root, dict):
        return summary

    changed = False
    for event in list(hooks_root.keys()):
        entries = hooks_root.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not _is_managed_matcher(e)]
        removed = len(entries) - len(kept)
        if removed > 0:
            if kept:
                hooks_root[event] = kept
            else:
                # No remaining entries for this event — drop the empty list
                hooks_root.pop(event, None)
            summary["removed"][event] = removed
            changed = True

    # If we emptied the whole hooks block, drop it for tidy diffs.
    if changed and not hooks_root:
        settings.pop("hooks", None)

    if changed:
        backup = _backup(path)
        if backup is not None:
            summary["backup_path"] = str(backup)
        _atomic_write_json(path, settings)
        summary["changed"] = True
    return summary


def is_installed(settings_override: Optional[str] = None) -> bool:
    """Quick check used by ``ensure_installed`` so MCP boot doesn't churn
    when the hook is already wired."""
    path = settings_path(settings_override)
    if not path.exists():
        return False
    try:
        settings = _load_settings(path)
    except RuntimeError:
        return False
    hooks_root = settings.get("hooks")
    if not isinstance(hooks_root, dict):
        return False
    for event in HOOK_EVENTS:
        entries = hooks_root.get(event)
        if not isinstance(entries, list) or not any(_is_managed_matcher(e) for e in entries):
            return False
    return True


def ensure_installed(*, force: bool = False) -> Optional[dict[str, Any]]:
    """Called from ``server.run()`` on MCP boot. Skips silently when:
      - ``PANEL_AUTO_INSTALL_HOOKS`` is set to a falsey value
      - The hook is already installed (idempotent fast path)

    Logs a single one-liner on first install so the user knows why their
    settings.json gained an entry. Returns the install summary dict if
    something changed, else None.
    """
    if not force:
        flag = os.environ.get("PANEL_AUTO_INSTALL_HOOKS", "1").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return None
    if is_installed():
        return None
    try:
        summary = install()
    except Exception as exc:  # noqa: BLE001 — boot must never fail on this
        logger.warning(
            "panel: could not auto-install completion hook in %s: %s "
            "(set PANEL_AUTO_INSTALL_HOOKS=0 to silence; "
            "run `panel-install-hooks` manually to retry)",
            settings_path(),
            exc,
        )
        return None

    if summary.get("changed"):
        actions = ", ".join(f"{k}={v}" for k, v in summary.get("actions", {}).items())
        backup_note = (
            f"; backup at {summary['backup_path']}" if summary.get("backup_path") else ""
        )
        logger.info(
            "panel: auto-installed completion hook (%s) in %s%s. "
            "Disable with PANEL_AUTO_INSTALL_HOOKS=0 or run `panel-uninstall-hooks`.",
            actions,
            summary["settings_path"],
            backup_note,
        )
    return summary


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _print_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, indent=2))


def install_main() -> int:
    """Entry point for ``panel-install-hooks``."""
    try:
        summary = install()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_summary(summary)
    return 0


def uninstall_main() -> int:
    """Entry point for ``panel-uninstall-hooks``."""
    try:
        summary = uninstall()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    if cmd == "install":
        sys.exit(install_main())
    if cmd == "uninstall":
        sys.exit(uninstall_main())
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    sys.exit(2)

"""Persistent boot-time preferences for Panel.

Some settings — like ``PANEL_WEB_AUTO_OPEN`` — are read once at server
startup. Flipping them on the live settings tab is pointless because
the auto-open already happened. But forcing the user to edit
``~/.claude.json`` and restart Claude Code every time they want to
change one is the wrong UX too.

This module owns ``~/.panel/preferences.json``: a tiny JSON file that
the server reads at boot and merges into ``os.environ`` BEFORE
``configure_providers`` runs. The settings tab can then persist a value
to this file with one HTTP POST, and the next Claude Code restart
picks it up — no config-file editing required.

Precedence (lowest → highest):
  1. Default behavior in code.
  2. Value in ``~/.panel/preferences.json``.
  3. Explicit env var passed by the MCP client (``~/.claude.json``
     env block, shell env, etc.). Always wins so users who set a
     value in their MCP config aren't silently overridden by a stale
     preferences file.

Best-effort throughout: missing file, unreadable JSON, permission
errors, or weird types all fall back silently to "no preferences" —
the server boots normally and nothing breaks. Same observability
philosophy as the execution graph.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Whitelist of keys the preferences file can set. Anything outside this
# list is ignored — prevents a malformed or malicious preferences file
# from poisoning every env var the server reads. Also: the settings tab
# can ONLY persist values for keys on this whitelist (see the POST
# handler in utils/web_viewer.py).
PERSISTENT_KEY_WHITELIST: tuple[str, ...] = (
    # Boot-time viewer behaviour — auto-open + port + host bind.
    "PANEL_WEB_AUTO_OPEN",
    "PANEL_WEB_PORT",
    "PANEL_WEB_HOST",
    "PANEL_WEB_DISABLE",
    "PANEL_WEB_ALLOW_REMOTE",
    # Default model / disabled tools — operators commonly set once.
    "DEFAULT_MODEL",
    "DISABLED_TOOLS",
    # Multiaudit / bugfind defaults — already live-editable, but
    # persisting them here means the choice survives Panel restart.
    "PANEL_MULTIAUDIT_JUDGE",
    "PANEL_MULTIAUDIT_PANELISTS",
    "PANEL_BUGFIND_JUDGE",
    "PANEL_BUGFIND_PANELISTS",
    # Streaming on/off.
    "PANEL_OPENAI_STREAM",
    "PANEL_GEMINI_STREAM",
    # OAuth-first switch.
    "PANEL_OAUTH_FIRST",
)


def _preferences_path() -> Path:
    """Return the canonical preferences file path. Per-user, NOT per-repo
    — these are operator preferences (auto-open, etc.) that should
    apply across every project the user opens, not bound to a specific
    repo's ``.panel/`` directory."""

    return Path.home() / ".panel" / "preferences.json"


# Lock-protected reader/writer so a burst of POSTs from the settings UI
# doesn't corrupt the file mid-write.
_PREFS_LOCK = threading.Lock()


def load_preferences_into_env() -> dict[str, str]:
    """Merge persisted preferences into ``os.environ`` for keys that
    aren't already set. Called once at server startup, before
    ``configure_providers``. Returns the merged dict for logging.

    Explicit env vars from the MCP client always win — if the user has
    ``PANEL_WEB_AUTO_OPEN=0`` in their ``~/.claude.json`` env block,
    that value is already in ``os.environ`` and we skip it here. The
    preferences file is the fallback default, not the override.
    """

    path = _preferences_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("preferences: load failed (%s): %s", path, exc)
        return {}

    if not isinstance(data, dict):
        logger.debug("preferences: top-level shape is not a dict; ignoring")
        return {}

    merged: dict[str, str] = {}
    for key, value in data.items():
        if key not in PERSISTENT_KEY_WHITELIST:
            continue
        # Only fill in if the user hasn't set it explicitly via env.
        if key in os.environ and os.environ[key]:
            continue
        if value is None:
            continue
        # Coerce scalars to string; refuse complex shapes.
        if isinstance(value, bool):
            value_str = "1" if value else "0"
        elif isinstance(value, (int, float, str)):
            value_str = str(value)
        else:
            continue
        os.environ[key] = value_str
        merged[key] = value_str
    return merged


def write_preference(key: str, value: Any) -> tuple[bool, str | None]:
    """Persist a single preference to ``~/.panel/preferences.json``.

    Returns ``(ok, error_or_None)``. The settings POST handler uses
    this to save a value the user toggled in the UI. Idempotent;
    creates the file + parent directory on first call.

    Setting ``value`` to ``None`` or empty string clears the key
    from the persistent file (useful for "revert to default").
    """

    if key not in PERSISTENT_KEY_WHITELIST:
        return False, f"key {key!r} is not in PERSISTENT_KEY_WHITELIST"

    path = _preferences_path()
    with _PREFS_LOCK:
        # Read existing content (best-effort) so we preserve other keys.
        data: dict[str, Any] = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    parsed = json.load(f)
                if isinstance(parsed, dict):
                    data = parsed
            except (OSError, json.JSONDecodeError):
                # Corrupted — start fresh; the user's choice to write
                # is more recent than whatever's on disk.
                data = {}

        if value is None or value == "":
            data.pop(key, None)
        else:
            # Normalise to string — preferences are env-var-shaped,
            # not arbitrary JSON.
            if isinstance(value, bool):
                data[key] = "1" if value else "0"
            elif isinstance(value, (int, float, str)):
                data[key] = str(value)
            else:
                return False, "value must be a scalar (str/int/float/bool/None)"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            tmp.replace(path)
        except OSError as exc:
            return False, f"failed to write {path}: {exc}"

    return True, None


def read_preferences() -> dict[str, Any]:
    """Return the current persisted preferences (or empty dict). Used
    by the settings GET handler to show the "saved value" alongside
    the live ``os.environ`` value."""

    path = _preferences_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in PERSISTENT_KEY_WHITELIST}

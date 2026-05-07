"""Tests for the persistent preferences layer.

``utils/preferences.py`` lets the settings tab persist boot-time
toggles (like ``PANEL_WEB_AUTO_OPEN``) to ``~/.panel/preferences.json``
so the next server launch picks them up — without forcing the user
to edit ``~/.claude.json``.

These tests cover:
  - Whitelist enforcement (off-whitelist keys rejected on write,
    silently ignored on load).
  - File write idempotency: existing keys preserved when adding a
    new one; clearing a key removes it but leaves others alone.
  - Boot-time merge precedence: explicit env var always wins; the
    preferences file fills only unset/empty keys.
  - Graceful failure: missing file, corrupt JSON, non-dict shapes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ~ to a temp dir for the duration of the test, then
    reload the preferences module so its module-level path resolution
    picks up the new HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib
    import utils.preferences as prefs

    importlib.reload(prefs)
    yield tmp_path, prefs


# ----------------------------------------------------------------------
# Whitelist enforcement
# ----------------------------------------------------------------------


def test_write_preference_accepts_whitelisted_key(fake_home):
    _, prefs = fake_home
    ok, err = prefs.write_preference("PANEL_WEB_AUTO_OPEN", "0")
    assert ok is True
    assert err is None


def test_write_preference_rejects_off_whitelist_key(fake_home):
    _, prefs = fake_home
    ok, err = prefs.write_preference("ARBITRARY_VAR", "anything")
    assert ok is False
    assert "PERSISTENT_KEY_WHITELIST" in err


def test_write_preference_rejects_complex_value(fake_home):
    _, prefs = fake_home
    ok, err = prefs.write_preference("PANEL_WEB_AUTO_OPEN", {"nested": "dict"})
    assert ok is False
    assert "scalar" in err


def test_load_preferences_silently_ignores_off_whitelist_keys(fake_home, monkeypatch):
    """A corrupt or hand-crafted preferences file with extra keys
    shouldn't poison os.environ."""
    home, prefs = fake_home
    pref_path = home / ".panel" / "preferences.json"
    pref_path.parent.mkdir(parents=True)
    pref_path.write_text(
        json.dumps(
            {
                "PANEL_WEB_AUTO_OPEN": "0",
                "MALICIOUS_KEY": "would-not-want-this",
                "PATH": "/etc/passwd",
            }
        )
    )

    # Ensure os.environ is clean for our test keys
    monkeypatch.delenv("PANEL_WEB_AUTO_OPEN", raising=False)
    monkeypatch.delenv("MALICIOUS_KEY", raising=False)

    merged = prefs.load_preferences_into_env()
    assert "PANEL_WEB_AUTO_OPEN" in merged
    assert "MALICIOUS_KEY" not in merged
    assert os.environ.get("MALICIOUS_KEY") is None
    # PATH definitely not overwritten — it's not on the whitelist
    assert os.environ.get("PATH") != "/etc/passwd"


# ----------------------------------------------------------------------
# Idempotency / file shape preservation
# ----------------------------------------------------------------------


def test_write_preserves_other_keys(fake_home):
    _, prefs = fake_home
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", "0")
    prefs.write_preference("PANEL_OAUTH_FIRST", "1")
    out = prefs.read_preferences()
    assert out["PANEL_WEB_AUTO_OPEN"] == "0"
    assert out["PANEL_OAUTH_FIRST"] == "1"


def test_write_empty_value_clears_key(fake_home):
    _, prefs = fake_home
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", "0")
    prefs.write_preference("DEFAULT_MODEL", "auto")
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", "")
    out = prefs.read_preferences()
    assert "PANEL_WEB_AUTO_OPEN" not in out
    assert out["DEFAULT_MODEL"] == "auto"


def test_write_none_value_clears_key(fake_home):
    _, prefs = fake_home
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", "0")
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", None)
    out = prefs.read_preferences()
    assert "PANEL_WEB_AUTO_OPEN" not in out


def test_write_bool_normalised_to_01(fake_home):
    """Bools normalise to ``"1"`` / ``"0"`` so the env-var consumers
    get a string they can parse."""
    _, prefs = fake_home
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", False)
    out = prefs.read_preferences()
    assert out["PANEL_WEB_AUTO_OPEN"] == "0"
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", True)
    out = prefs.read_preferences()
    assert out["PANEL_WEB_AUTO_OPEN"] == "1"


# ----------------------------------------------------------------------
# Boot-time merge precedence
# ----------------------------------------------------------------------


def test_explicit_env_wins_over_preferences(fake_home, monkeypatch):
    """If the user has ``PANEL_WEB_AUTO_OPEN=1`` in their MCP config,
    a stale preferences file value of ``"0"`` must NOT override it."""

    home, prefs = fake_home
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", "0")
    monkeypatch.setenv("PANEL_WEB_AUTO_OPEN", "1")

    merged = prefs.load_preferences_into_env()
    assert "PANEL_WEB_AUTO_OPEN" not in merged  # not merged because env wins
    assert os.environ["PANEL_WEB_AUTO_OPEN"] == "1"


def test_preferences_fill_unset_env(fake_home, monkeypatch):
    home, prefs = fake_home
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", "0")
    monkeypatch.delenv("PANEL_WEB_AUTO_OPEN", raising=False)

    merged = prefs.load_preferences_into_env()
    assert merged["PANEL_WEB_AUTO_OPEN"] == "0"
    assert os.environ["PANEL_WEB_AUTO_OPEN"] == "0"


def test_empty_env_string_treated_as_unset(fake_home, monkeypatch):
    """Some MCP clients set env vars to empty string for "default". The
    preferences file should fill those too."""
    home, prefs = fake_home
    prefs.write_preference("PANEL_WEB_AUTO_OPEN", "0")
    monkeypatch.setenv("PANEL_WEB_AUTO_OPEN", "")

    merged = prefs.load_preferences_into_env()
    assert merged["PANEL_WEB_AUTO_OPEN"] == "0"


# ----------------------------------------------------------------------
# Graceful failure
# ----------------------------------------------------------------------


def test_missing_file_returns_empty(fake_home, monkeypatch):
    home, prefs = fake_home
    # No preferences.json written
    monkeypatch.delenv("PANEL_WEB_AUTO_OPEN", raising=False)
    merged = prefs.load_preferences_into_env()
    assert merged == {}


def test_corrupt_json_returns_empty(fake_home, monkeypatch):
    home, prefs = fake_home
    pref_path = home / ".panel" / "preferences.json"
    pref_path.parent.mkdir(parents=True)
    pref_path.write_text("not valid json {{{")

    monkeypatch.delenv("PANEL_WEB_AUTO_OPEN", raising=False)
    merged = prefs.load_preferences_into_env()
    assert merged == {}


def test_non_dict_top_level_returns_empty(fake_home, monkeypatch):
    home, prefs = fake_home
    pref_path = home / ".panel" / "preferences.json"
    pref_path.parent.mkdir(parents=True)
    pref_path.write_text(json.dumps(["this is", "a list, not a dict"]))

    merged = prefs.load_preferences_into_env()
    assert merged == {}


def test_corrupt_file_doesnt_block_subsequent_writes(fake_home):
    """If preferences.json is corrupted, the next write should
    succeed (overwriting the bad content) rather than refusing
    to save anything ever again."""
    home, prefs = fake_home
    pref_path = home / ".panel" / "preferences.json"
    pref_path.parent.mkdir(parents=True)
    pref_path.write_text("garbage")

    ok, err = prefs.write_preference("PANEL_WEB_AUTO_OPEN", "0")
    assert ok
    assert err is None
    out = prefs.read_preferences()
    assert out["PANEL_WEB_AUTO_OPEN"] == "0"

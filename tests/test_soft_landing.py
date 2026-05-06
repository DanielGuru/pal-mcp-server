"""Tests for the zero-provider soft-landing path in ``configure_providers``.

Used to be a hard ``ValueError`` that crashed server startup if no API
keys were configured — even when OAuth CLIs were available, even though
``listmodels`` / ``version`` / ``web_url`` / graph queries don't need
any provider at all. Now the server starts and surfaces a friendly
capability summary.

These tests cover the three startup configurations:

1. **Zero everything** (no API keys, no OAuth CLIs on PATH):
   ``configure_providers`` does not raise; logs a multi-line warning
   listing what's available, what's blocked, and how to unlock more.

2. **OAuth CLIs only** (no API keys, but at least one of
   codex/gemini/claude on PATH): logs an INFO summary noting clink /
   panel / multiaudit work via OAuth, and which API-needing tools are
   still blocked.

3. **Normal path** (≥ 1 API key): unchanged behaviour, INFO log of
   registered providers + (if any CLIs are present) OAuth CLI summary.
"""

from __future__ import annotations

import logging

import pytest


def _reset_registry():
    """Clear the registry singleton between tests so each
    ``configure_providers`` call starts fresh."""

    from providers.registry import ModelProviderRegistry

    inst = ModelProviderRegistry()
    inst._providers.clear()
    inst._initialized_providers.clear()


@pytest.fixture(autouse=True)
def isolate_registry_and_env(monkeypatch):
    """Each test sees a clean registry + minimal env. Tests opt in to
    specific keys/CLIs by re-setting them inside the test."""

    _reset_registry()
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "OPENROUTER_API_KEY",
        "DIAL_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "CUSTOM_API_URL",
        "OPENAI_ALLOWED_MODELS",
        "GOOGLE_ALLOWED_MODELS",
        "ANTHROPIC_ALLOWED_MODELS",
        "XAI_ALLOWED_MODELS",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    _reset_registry()


def test_soft_landing_no_keys_no_clis_does_not_raise(monkeypatch, capsys):
    """Zero-everything: no ValueError, server proceeds to start.
    The user gets a friendly multi-line warning telling them how to
    unlock more functionality.

    Asserts directly against the ``server`` logger's emitted output by
    attaching a fresh handler — caplog can't see it because the module
    configures ``propagate=False`` to keep MCP-protocol stderr clean.
    """

    import io as _io
    import server

    # No CLIs on PATH
    monkeypatch.setattr("shutil.which", lambda _name: None)

    # Attach our own handler to the server logger
    server_logger = logging.getLogger("server")
    buffer = _io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.WARNING)
    server_logger.addHandler(handler)
    try:
        # Used to raise ValueError; now must complete cleanly
        server.configure_providers()
    finally:
        server_logger.removeHandler(handler)

    log_text = buffer.getvalue()
    assert "limited functionality" in log_text
    assert "ANTHROPIC_API_KEY" in log_text
    assert "codex login" in log_text


def test_soft_landing_no_keys_oauth_cli_present(caplog, monkeypatch):
    """OAuth CLIs available, no API keys: log an INFO summary noting
    clink / panel work via OAuth, and which tools remain blocked."""

    import server

    # Exactly codex available; gemini/claude not on PATH
    def _which(name):
        return f"/fake/path/{name}" if name == "codex" else None

    monkeypatch.setattr("shutil.which", _which)

    with caplog.at_level(logging.INFO, logger="server"):
        server.configure_providers()

    log_text = "\n".join(r.message for r in caplog.records)
    assert "OAuth CLIs are available" in log_text
    assert "codex" in log_text
    assert "clink / panel / multiaudit will work" in log_text


def test_normal_path_one_api_key_unchanged(caplog, monkeypatch):
    """Regression: with at least one API key, the existing capability
    log still fires (Available providers: ...). The new code path
    only kicks in when valid_providers is empty."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    monkeypatch.setattr("shutil.which", lambda _name: None)

    import server

    with caplog.at_level(logging.INFO, logger="server"):
        server.configure_providers()

    log_text = "\n".join(r.message for r in caplog.records)
    assert "Available providers:" in log_text
    assert "Anthropic" in log_text


def test_auto_mode_no_models_does_not_raise(monkeypatch, caplog):
    """When auto mode is on but no providers are registered (because
    no keys), the auto-mode validation used to ``raise ValueError``
    and crash the server. Now logs a warning and lets the server start;
    per-call errors handle the missing-model case gracefully."""

    monkeypatch.setattr("shutil.which", lambda _name: None)

    import server

    with caplog.at_level(logging.WARNING, logger="server"):
        server.configure_providers()  # must NOT raise

    log_text = "\n".join(r.message for r in caplog.records)
    # Either the capability-summary warning OR the auto-mode warning
    # is enough — both indicate the soft-landing path is alive.
    assert (
        "limited functionality" in log_text
        or "no models are available after applying restrictions" in log_text
    )


def test_listmodels_works_with_zero_providers(monkeypatch):
    """Even with nothing configured, listmodels (a metadata-only tool
    that doesn't need a provider) must remain callable. This is the
    "soft-landing" promise — the server is more useful as a partial
    install than as a hard failure."""

    monkeypatch.setattr("shutil.which", lambda _name: None)

    import server

    server.configure_providers()  # Must not raise

    # listmodels is registered and constructable
    assert "listmodels" in server.TOOLS
    tool = server.make_tool("listmodels")
    assert tool is not None

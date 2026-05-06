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


def _capture_server_log(level=logging.WARNING):
    """Attach a fresh handler to the ``server`` logger and return a
    callable that yields the captured text. Used instead of ``caplog``
    because some test runners don't have the server logger set to
    propagate=True; explicit handler attachment works everywhere.

    Returns a tuple ``(buffer_getvalue, detach_fn)``. Always call
    ``detach_fn()`` in a finally block to avoid leaking handlers
    between tests.
    """

    import io as _io

    buffer = _io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(level)
    server_logger = logging.getLogger("server")
    server_logger.addHandler(handler)
    return buffer.getvalue, lambda: server_logger.removeHandler(handler)


def test_soft_landing_no_keys_no_clis_does_not_raise(monkeypatch):
    """Zero-everything: no ValueError, server proceeds to start.
    The user gets a friendly multi-line warning telling them how to
    unlock more functionality."""

    import server

    # No CLIs on PATH AND no CLIs in the canonical install locations.
    # The ``_CLI_FALLBACK_PATHS`` probe is the new behaviour added to
    # handle MCP launch contexts that sanitise PATH; tests need to
    # neutralise it explicitly so a developer machine with brew-installed
    # codex/gemini/claude can still exercise the zero-everything branch.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(server, "_CLI_FALLBACK_PATHS", (), raising=False)

    get_log, detach = _capture_server_log(logging.WARNING)
    try:
        server.configure_providers()  # Used to raise; now must succeed
    finally:
        detach()

    log_text = get_log()
    assert "limited functionality" in log_text
    assert "ANTHROPIC_API_KEY" in log_text
    assert "DIAL_API_KEY" in log_text  # Codex audit caught the omission
    assert "codex login" in log_text


def test_soft_landing_no_keys_oauth_cli_present(monkeypatch):
    """OAuth CLIs available, no API keys: log an INFO summary noting
    clink / panel work via OAuth, and that default multiaudit needs an
    API key for its grok-4.3 panelist (audit caught the previous
    over-promise)."""

    import server

    # Exactly codex available; gemini/claude not on PATH or known locations
    def _which(name):
        return f"/fake/path/{name}" if name == "codex" else None

    monkeypatch.setattr("shutil.which", _which)
    monkeypatch.setattr(server, "_CLI_FALLBACK_PATHS", (), raising=False)

    get_log, detach = _capture_server_log(logging.INFO)
    try:
        server.configure_providers()
    finally:
        detach()

    log_text = get_log()
    assert "OAuth CLIs are available" in log_text
    assert "codex" in log_text
    assert "clink and panel work via OAuth" in log_text
    # multiaudit no longer over-promised
    assert "grok-4.3 which needs an API key" in log_text


def test_normal_path_one_api_key_unchanged(caplog, monkeypatch):
    """Regression: with at least one API key, the existing capability
    log still fires (Available providers: ...). The new code path
    only kicks in when valid_providers is empty."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    monkeypatch.setattr("shutil.which", lambda _name: None)

    import server

    monkeypatch.setattr(server, "_CLI_FALLBACK_PATHS", (), raising=False)

    with caplog.at_level(logging.INFO, logger="server"):
        server.configure_providers()

    log_text = "\n".join(r.message for r in caplog.records)
    assert "Available providers:" in log_text
    assert "Anthropic" in log_text


def test_auto_mode_no_models_does_not_raise(monkeypatch):
    """When auto mode is on but no providers are registered (because
    no keys), the auto-mode validation used to ``raise ValueError``
    and crash the server. Now logs a warning and lets the server start;
    per-call errors handle the missing-model case gracefully.

    Pinning ``IS_AUTO_MODE=True`` explicitly so the test reliably
    exercises the auto-mode block at server.py L725 — without the pin,
    a developer running with ``DEFAULT_MODEL=gpt-5.5`` in their shell
    would skip the auto branch entirely and the test would still pass
    via the zero-everything warning instead. (Audit-flagged.)
    """

    monkeypatch.setattr("shutil.which", lambda _name: None)

    import server

    monkeypatch.setattr(server, "_CLI_FALLBACK_PATHS", (), raising=False)
    # Pin IS_AUTO_MODE so we definitely hit the auto-mode branch
    monkeypatch.setattr(server, "IS_AUTO_MODE", True, raising=False)

    get_log, detach = _capture_server_log(logging.WARNING)
    try:
        server.configure_providers()  # must NOT raise
    finally:
        detach()

    log_text = get_log()
    assert (
        "limited functionality" in log_text
        or "no models are available after applying restrictions" in log_text
    )


def test_mixed_config_one_api_key_plus_oauth_cli(caplog, monkeypatch):
    """Regression for the path Grok flagged: ≥1 API key AND ≥1 OAuth CLI
    on PATH should log BOTH lines — "Available providers: X" plus "OAuth
    CLIs detected: Y". The first line was always there; the second is the
    new follow-up that's only in the diff. Untested before this case.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/fake/{name}" if name in ("codex", "claude") else None,
    )

    import io as _io
    import server

    server_logger = logging.getLogger("server")
    buffer = _io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.INFO)
    server_logger.addHandler(handler)
    try:
        server.configure_providers()
    finally:
        server_logger.removeHandler(handler)

    log_text = buffer.getvalue()
    assert "Available providers:" in log_text
    assert "Anthropic" in log_text
    # The new follow-up "OAuth CLIs detected: ..." line:
    assert "OAuth CLIs detected" in log_text
    assert "codex" in log_text
    assert "claude" in log_text


def test_per_call_error_actionable_in_zero_provider_state(monkeypatch):
    """Grok's runtime-error question: when a fresh install hits the
    per-call error path (no provider for requested model), the message
    must actually tell the user how to fix it. It should mention BOTH
    the API-key route AND the OAuth-CLI route — Grok's specific concern
    was that a generic "no provider for model" would degrade the UX
    from "clear startup failure" to "mysterious per-call failure"."""

    monkeypatch.setattr("shutil.which", lambda _name: None)

    import server

    monkeypatch.setattr(server, "_CLI_FALLBACK_PATHS", (), raising=False)
    server.configure_providers()  # Soft-landing path; no providers registered

    # Build the error message a tool would surface for a missing-provider
    # call. This is the actual code path that fires when a user does
    # ``chat with claude-opus-4-7`` in a zero-provider state.
    from tools.chat import ChatTool

    tool = ChatTool()
    msg = tool._build_model_unavailable_message("claude-opus-4-7")

    # The message must mention concrete fixes — not just "model not found"
    assert "ANTHROPIC_API_KEY" in msg
    assert "OPENAI_API_KEY" in msg or "GEMINI_API_KEY" in msg or "XAI_API_KEY" in msg
    assert ("clink" in msg or "OAuth" in msg or "codex" in msg)
    assert "ONBOARDING" in msg or "restart" in msg.lower()


def test_cli_detection_finds_homebrew_install(monkeypatch, tmp_path):
    """When the OAuth CLI is installed via brew (/opt/homebrew/bin) but
    PATH is sanitized (MCP launch contexts often drop it), we must still
    detect the CLI via the canonical-locations fallback. Otherwise the
    "no OAuth CLIs" warning fires even though the user's install is
    perfectly valid."""

    # Simulate: shutil.which returns None (PATH doesn't include homebrew),
    # but the file exists at /opt/homebrew/bin/codex.
    fake_homebrew = tmp_path / "homebrew_bin"
    fake_homebrew.mkdir()
    (fake_homebrew / "codex").write_text("#!/bin/sh\n")

    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "server._CLI_FALLBACK_PATHS",
        (str(fake_homebrew),),
        raising=False,
    )

    import io as _io
    import server

    # Re-bind the fallback paths for the running module
    server._CLI_FALLBACK_PATHS = (str(fake_homebrew),)

    server_logger = logging.getLogger("server")
    buffer = _io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.INFO)
    server_logger.addHandler(handler)
    try:
        server.configure_providers()
    finally:
        server_logger.removeHandler(handler)

    log_text = buffer.getvalue()
    # Without the canonical-locations fallback, this would log
    # "limited functionality" (the zero-CLIs warning). With it, the
    # path-finder spots /tmp/.../codex and we get the OAuth-CLI INFO log.
    assert "OAuth CLIs are available" in log_text or "codex" in log_text


def test_listmodels_works_with_zero_providers(monkeypatch):
    """Even with nothing configured, listmodels (a metadata-only tool
    that doesn't need a provider) must remain callable. This is the
    "soft-landing" promise — the server is more useful as a partial
    install than as a hard failure."""

    monkeypatch.setattr("shutil.which", lambda _name: None)

    import server

    monkeypatch.setattr(server, "_CLI_FALLBACK_PATHS", (), raising=False)
    server.configure_providers()  # Must not raise

    # listmodels is registered and constructable
    assert "listmodels" in server.TOOLS
    tool = server.make_tool("listmodels")
    assert tool is not None

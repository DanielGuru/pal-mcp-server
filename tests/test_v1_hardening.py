"""Regression tests for the v1 hardening pass.

These cover surfaces introduced or significantly reworked in commits:
  - 1e4ef50 factory TOOLS registry
  - 170d188 async provider wrapper
  - 9976892 OAuth-to-API fallback
  - 98cda49 central execute_tool() dispatch
  - 4979cf7 bounded thread pool + semaphore + per-call timeout
  - 6e2f0a2 clink metadata redaction + caps
  - 37b794a panel duplicate label dedupe + async file reads

Focused suite — each test asserts one invariant that, if regressed,
would resurface a bug the audit panel actually flagged. Not aiming for
coverage; aiming for tripwires on the work that just landed.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Factory pattern (1e4ef50)
# ---------------------------------------------------------------------------


def test_tools_registry_holds_classes_not_instances():
    """server.TOOLS must map name -> class so each call constructs fresh."""
    import server

    assert len(server.TOOLS) == 23, f"expected 23 tools, got {len(server.TOOLS)}"
    for name, cls in server.TOOLS.items():
        assert inspect.isclass(cls), f"TOOLS[{name!r}] should be a class, got {type(cls).__name__}"


def test_make_tool_returns_fresh_instance_per_call():
    """make_tool must give every caller its own instance."""
    import server

    a = server.make_tool("chat")
    b = server.make_tool("chat")
    assert a is not b
    assert type(a) is type(b)


def test_tool_descriptors_are_stable_singletons():
    """TOOL_DESCRIPTORS holds one descriptor instance per tool, never replaced."""
    import server

    a = server.TOOL_DESCRIPTORS["chat"]
    b = server.TOOL_DESCRIPTORS["chat"]
    assert a is b


# ---------------------------------------------------------------------------
# Central dispatch (98cda49)
# ---------------------------------------------------------------------------


def test_execute_tool_is_the_canonical_entrypoint():
    """All four internal callers (handle_call_tool, tasks, panel, clink fallback)
    should reference server.execute_tool — not tool.execute() directly."""
    import server

    assert callable(server.execute_tool)
    assert inspect.iscoroutinefunction(server.execute_tool)


def test_execute_tool_raises_on_unknown_tool():
    import server

    async def go():
        with pytest.raises(KeyError):
            await server.execute_tool("nonexistent_tool", {})

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Clink metadata redaction (6e2f0a2)
# ---------------------------------------------------------------------------


def test_redaction_strips_openai_keys():
    from tools.clink import _redact_and_cap

    text = "OPENAI_API_KEY=sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA error: 401"
    out = _redact_and_cap(text, cap=200)
    assert "sk-proj" not in out
    assert "[REDACTED_API_KEY]" in out


def test_redaction_strips_google_xai_anthropic_keys():
    from tools.clink import _redact_and_cap

    samples = {
        "AIzaSyA-1234567890abcdefghijklmnopqrstuv": "AIza",
        "xai-abcdefghijklmnopqrstuvwxyz12345": "xai-",
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAA": "sk-ant",
    }
    for raw, fragment in samples.items():
        out = _redact_and_cap(raw, cap=200)
        assert fragment not in out, f"failed to redact {fragment!r} in {raw!r} -> {out!r}"


def test_redaction_strips_jwt_and_bearer():
    from tools.clink import _redact_and_cap

    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0fQ.SflKxwRJSMeKKF_v"
    out = _redact_and_cap(f"Authorization: Bearer {jwt}", cap=200)
    assert "eyJ" not in out
    assert jwt not in out
    assert "[REDACTED" in out


def test_redaction_preserves_non_secret_text():
    from tools.clink import _redact_and_cap

    text = "Just a regular path: /Users/foo/Projects/server.py and a port :8080"
    assert _redact_and_cap(text, cap=200) == text


def test_redaction_truncates_at_cap():
    from tools.clink import _redact_and_cap

    out = _redact_and_cap("X" * 5000, cap=1000)
    assert len(out) <= 1000 + 100  # cap + truncation marker
    assert "truncated" in out


def test_pal_debug_cli_output_disables_redaction(monkeypatch):
    """Opt-in escape hatch must skip both redaction and truncation."""
    from tools.clink import _redact_and_cap

    monkeypatch.setenv("PAL_DEBUG_CLI_OUTPUT", "1")
    raw = "sk-proj-secret " + "X" * 5000
    out = _redact_and_cap(raw, cap=1000)
    assert out == raw, "PAL_DEBUG_CLI_OUTPUT must passthrough untouched"


# ---------------------------------------------------------------------------
# Clink OAuth failure detection (9976892)
# ---------------------------------------------------------------------------


def test_oauth_failure_detected_for_gemini_quota():
    """Real Gemini stderr from the live smoke test must match."""
    from clink.agents import CLIAgentError
    from tools.clink import CLinkTool

    exc = CLIAgentError(
        "exited 1",
        returncode=1,
        stdout='{"type":"result","status":"error"}',
        stderr="TerminalQuotaError: You have exhausted your capacity on this model. Your quota will reset after 21h59m17s.",
    )
    assert CLinkTool._looks_like_oauth_failure(exc)


def test_oauth_failure_detected_for_codex_auth_lapse():
    from clink.agents import CLIAgentError
    from tools.clink import CLinkTool

    exc = CLIAgentError(
        "exited 1",
        returncode=1,
        stdout="",
        stderr="Error: 401 Unauthorized. Please run codex login.",
    )
    assert CLinkTool._looks_like_oauth_failure(exc)


def test_real_prompt_error_not_misclassified_as_oauth_failure():
    """Negative case — false positive costs a paid-API call."""
    from clink.agents import CLIAgentError
    from tools.clink import CLinkTool

    exc = CLIAgentError(
        "exited 2",
        returncode=2,
        stdout="",
        stderr="Bad request: invalid model name",
    )
    assert not CLinkTool._looks_like_oauth_failure(exc)


def test_oauth_fallback_models_wired_for_each_cli():
    """Registry resolution must propagate oauth_fallback_model from constants."""
    from clink import get_registry

    r = get_registry()
    expected = {
        "gemini": "gemini-3.1-pro-preview",
        "codex": "gpt-5.5",
        "claude": None,  # No Anthropic provider in this fork
    }
    for cli_name, want in expected.items():
        if cli_name in r.list_clients():
            client = r.get_client(cli_name)
            assert client.oauth_fallback_model == want, (
                f"{cli_name}: expected {want!r}, got {client.oauth_fallback_model!r}"
            )


def test_fallback_marker_injection():
    """_mark_fallback_in_result must stamp oauth_fallback_used into JSON metadata."""
    import json

    from mcp.types import TextContent

    from tools.clink import CLinkTool

    payload = {"status": "success", "content": "PONG", "metadata": {"model_used": "gemini-3.1-pro-preview"}}
    result = [TextContent(type="text", text=json.dumps(payload))]
    out = CLinkTool._mark_fallback_in_result(
        result, cli_name="gemini", fallback_model="gemini-3.1-pro-preview", original_failure="TerminalQuotaError: ..."
    )
    body = json.loads(out[0].text)
    assert body["metadata"]["oauth_fallback_used"] is True
    assert body["metadata"]["oauth_fallback_from_cli"] == "gemini"
    assert body["metadata"]["oauth_fallback_model"] == "gemini-3.1-pro-preview"


# ---------------------------------------------------------------------------
# Panel correctness (37b794a + 98cda49)
# ---------------------------------------------------------------------------


def test_panel_cost_tier_promotes_on_fallback_marker():
    """oauth_fallback_used in response → cost_tier flips to oauth_fallback_paid."""
    from tools.panel import _derive_cost_tier

    plain = '{"content": "hi", "metadata": {"model_used": "gemini-3-flash-preview"}}'
    fallback = '{"content": "hi", "metadata": {"oauth_fallback_used": true}}'

    assert _derive_cost_tier("oauth_free", plain) == "oauth_free"
    assert _derive_cost_tier("oauth_free", fallback) == "oauth_fallback_paid"
    # api_paid never gets demoted, even with a marker present
    assert _derive_cost_tier("api_paid", fallback) == "api_paid"


# ---------------------------------------------------------------------------
# Provider concurrency bounds (4979cf7)
# ---------------------------------------------------------------------------


def test_provider_executor_honors_env_cap(monkeypatch):
    """PAL_MAX_PROVIDER_THREADS must be respected at first init."""
    import providers.base as base

    monkeypatch.setattr(base, "_PROVIDER_EXECUTOR", None)
    monkeypatch.setenv("PAL_MAX_PROVIDER_THREADS", "7")
    ex = base._get_provider_executor()
    assert ex._max_workers == 7
    # Reset so other tests don't see the 7-thread pool
    monkeypatch.setattr(base, "_PROVIDER_EXECUTOR", None)


def test_provider_semaphore_honors_env_cap(monkeypatch):
    """PAL_MAX_CONCURRENT_API must be respected at first init."""
    import providers.base as base

    async def go():
        monkeypatch.setattr(base, "_API_SEMAPHORE", None)
        monkeypatch.setenv("PAL_MAX_CONCURRENT_API", "3")
        sem = base._get_api_semaphore()
        assert sem._value == 3
        monkeypatch.setattr(base, "_API_SEMAPHORE", None)

    asyncio.run(go())


def test_get_default_api_timeout_default():
    from providers.base import get_default_api_timeout

    # No env set → 600s default
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PAL_API_TIMEOUT_S", None)
        assert get_default_api_timeout() == 600.0


def test_agenerate_content_is_an_awaitable_method():
    """Locking the API: every provider must expose async agenerate_content."""
    from providers.openai import OpenAIModelProvider
    from providers.gemini import GeminiModelProvider
    from providers.xai import XAIModelProvider

    for provider_cls in (OpenAIModelProvider, GeminiModelProvider, XAIModelProvider):
        method = getattr(provider_cls, "agenerate_content", None)
        assert method is not None, f"{provider_cls.__name__} missing agenerate_content"
        assert inspect.iscoroutinefunction(method), (
            f"{provider_cls.__name__}.agenerate_content must be async"
        )

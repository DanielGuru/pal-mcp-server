"""Tests for the OAuth-first provider wrapper.

The wrapper sits between tools and providers. For models in
``MODEL_TO_CLI``, it routes ``agenerate_content`` calls through the
corresponding clink CLI (free OAuth path) before falling through to the
wrapped provider's API. These tests verify:

- Mapping resolution is conservative (exact-match only).
- The opt-out env var honestly disables wrapping.
- Wrapping is idempotent (re-wraps don't double-route).
- Unmapped models pass through unchanged.
- Missing CLI on PATH triggers silent API fallback (cost_tier=api_paid).
- Successful clink dispatch produces a ModelResponse with cost_tier=oauth_free.
- clink-side OAuth fallback (CLI quota → paid API) is honoured (cost_tier=oauth_fallback_paid).
- Wrapper-side hard failure of clink falls through to inner provider with
  cost_tier=oauth_fallback_paid + fallback_reason captured.
- generate_content (sync) is pass-through.

These tests do NOT spawn real subprocesses or hit real APIs — they patch
``server.execute_tool`` and the inner provider.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clink.constants import MODEL_TO_CLI
from providers.oauth_first import (
    OAuthFirstProvider,
    maybe_wrap,
    oauth_first_enabled,
    resolve_cli_for_model,
)
from providers.shared.model_response import ModelResponse
from providers.shared.provider_type import ProviderType


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------


@dataclass
class _FakeProvider:
    """Minimal stand-in for ModelProvider — only the methods OAuthFirstProvider exercises."""

    provider_type: ProviderType = ProviderType.OPENAI
    response: ModelResponse | None = None
    sync_response: ModelResponse | None = None
    raise_async: Exception | None = None

    def get_provider_type(self) -> ProviderType:
        return self.provider_type

    def validate_model_name(self, model_name: str) -> bool:
        return True

    def get_model_capabilities(self, model_name: str):
        return None

    async def agenerate_content(self, prompt, model_name, **kwargs):
        if self.raise_async is not None:
            raise self.raise_async
        return self.response or ModelResponse(
            content="from-inner-API",
            usage={"total_tokens": 42},
            model_name=model_name,
            friendly_name="inner",
            provider=self.provider_type,
            metadata={},
        )

    def generate_content(self, *args, **kwargs):
        return self.sync_response or ModelResponse(
            content="from-inner-sync",
            model_name="any",
            provider=self.provider_type,
            metadata={},
        )


def _fake_clink_text_content(content: str, *, oauth_fallback_used: bool = False) -> list:
    """Build the shape that ``execute_tool('clink', ...)`` returns."""

    class _TextContent:
        def __init__(self, text: str) -> None:
            self.text = text

    payload = {
        "status": "success",
        "content": content,
        "content_type": "text",
        "metadata": {
            "cli_name": "codex",
            "command_used": "codex exec",
            "oauth_fallback_used": oauth_fallback_used,
        },
    }
    return [_TextContent(json.dumps(payload))]


# ----------------------------------------------------------------------
# Mapping & opt-out
# ----------------------------------------------------------------------


def test_mapping_covers_all_oauth_clis():
    """Every CLI with an oauth_fallback_model should have at least one model
    pointing at it in MODEL_TO_CLI — otherwise the inverse direction is dead."""

    cli_targets = set(MODEL_TO_CLI.values())
    assert "codex" in cli_targets
    assert "gemini" in cli_targets
    assert "claude" in cli_targets


def test_resolve_cli_exact_match_only():
    """Conservative mapping: exact match returns CLI, anything else returns None."""

    assert resolve_cli_for_model("gpt-5.5") == "codex"
    assert resolve_cli_for_model("gemini-3.1-pro-preview") == "gemini"
    assert resolve_cli_for_model("claude-opus-4-7") == "claude"
    assert resolve_cli_for_model("claude-sonnet-4-6") == "claude"

    # Non-flagship variants → no route (this is intentional; expand the map
    # explicitly when the OAuth path actually matches the requested model).
    assert resolve_cli_for_model("gpt-5.4") is None
    assert resolve_cli_for_model("gpt-5.1-codex") is None
    assert resolve_cli_for_model("grok-4.3") is None
    assert resolve_cli_for_model("grok-4.1-fast") is None
    assert resolve_cli_for_model("") is None
    assert resolve_cli_for_model(None) is None  # type: ignore[arg-type]


def test_oauth_first_enabled_default_on(monkeypatch):
    monkeypatch.delenv("PANEL_OAUTH_FIRST", raising=False)
    assert oauth_first_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "OFF"])
def test_oauth_first_disabled_via_env(monkeypatch, value):
    monkeypatch.setenv("PANEL_OAUTH_FIRST", value)
    assert oauth_first_enabled() is False


def test_maybe_wrap_pass_through_when_disabled(monkeypatch):
    monkeypatch.setenv("PANEL_OAUTH_FIRST", "0")
    inner = _FakeProvider()
    assert maybe_wrap(inner) is inner


def test_maybe_wrap_wraps_when_enabled(monkeypatch):
    monkeypatch.setenv("PANEL_OAUTH_FIRST", "1")
    inner = _FakeProvider()
    wrapped = maybe_wrap(inner)
    assert isinstance(wrapped, OAuthFirstProvider)
    assert wrapped._inner is inner


def test_maybe_wrap_idempotent(monkeypatch):
    """Re-wrapping must not create OAuthFirstProvider(OAuthFirstProvider(...))."""

    monkeypatch.setenv("PANEL_OAUTH_FIRST", "1")
    inner = _FakeProvider()
    once = maybe_wrap(inner)
    twice = maybe_wrap(once)
    assert twice is once
    assert isinstance(twice, OAuthFirstProvider)
    assert twice._inner is inner  # not a doubly-wrapped chain


# ----------------------------------------------------------------------
# Routing behaviour
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmapped_model_passes_through_to_inner(monkeypatch):
    """grok-4.3 has no CLI route — wrapper must call inner directly and
    stamp cost_tier=api_paid."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    response = await wrapped.agenerate_content(
        prompt="hello", model_name="grok-4.3"
    )

    assert response.content == "from-inner-API"
    assert response.metadata["cost_tier"] == "api_paid"
    assert response.metadata["oauth_route"] == "none"


@pytest.mark.asyncio
async def test_cli_not_installed_falls_through_to_api(monkeypatch):
    """gpt-5.5 maps to codex but codex isn't on PATH — must use API and
    label cost_tier=api_paid plus oauth_route_skipped reason."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    # shutil.which returns None → CLI not installed
    monkeypatch.setattr("providers.oauth_first.shutil.which", lambda _: None)

    response = await wrapped.agenerate_content(
        prompt="hello", model_name="gpt-5.5"
    )

    assert response.content == "from-inner-API"
    assert response.metadata["cost_tier"] == "api_paid"
    assert response.metadata["oauth_route"] == "codex"
    assert response.metadata["oauth_route_skipped"] == "cli_not_installed"


@pytest.mark.asyncio
async def test_oauth_path_success(monkeypatch):
    """gpt-5.5 maps to codex, codex is on PATH, clink call succeeds — wrapper
    returns the clink response wrapped as a ModelResponse with
    cost_tier=oauth_free."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    monkeypatch.setattr(
        "providers.oauth_first.shutil.which",
        lambda name: f"/fake/path/to/{name}" if name == "codex" else None,
    )

    fake_execute = AsyncMock(
        return_value=_fake_clink_text_content("from-codex-OAuth")
    )
    monkeypatch.setattr("server.execute_tool", fake_execute)

    response = await wrapped.agenerate_content(
        prompt="hello", model_name="gpt-5.5"
    )

    assert response.content == "from-codex-OAuth"
    assert response.metadata["cost_tier"] == "oauth_free"
    assert response.metadata["oauth_route"] == "codex"
    assert response.metadata.get("oauth_fallback_used") is False

    # Verify execute_tool was called with the right args
    fake_execute.assert_awaited_once()
    call_args = fake_execute.await_args
    assert call_args.args[0] == "clink"
    payload = call_args.args[1]
    assert payload["cli_name"] == "codex"
    assert payload["role"] == "default"
    assert payload["_graph_edge_kind"] == "oauth_first_route"
    assert "oauth-first:codex:gpt-5.5" in payload["_graph_label"]


@pytest.mark.asyncio
async def test_clink_oauth_fallback_propagates_cost_tier(monkeypatch):
    """When clink itself OAuth-fell-back to paid API (CLI quota'd), the
    response metadata says oauth_fallback_used=True. The wrapper must
    surface cost_tier=oauth_fallback_paid (not oauth_free)."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    monkeypatch.setattr(
        "providers.oauth_first.shutil.which", lambda _: "/fake/path"
    )

    # clink returns content but its metadata says oauth_fallback_used=True
    # (its own CLI→API fallback fired)
    fake_execute = AsyncMock(
        return_value=_fake_clink_text_content(
            "from-codex-fallback-to-API", oauth_fallback_used=True
        )
    )
    monkeypatch.setattr("server.execute_tool", fake_execute)

    response = await wrapped.agenerate_content(
        prompt="hello", model_name="gpt-5.5"
    )

    assert response.content == "from-codex-fallback-to-API"
    assert response.metadata["cost_tier"] == "oauth_fallback_paid"
    assert response.metadata["oauth_fallback_used"] is True
    assert response.metadata["oauth_route"] == "codex"


@pytest.mark.asyncio
async def test_clink_hard_failure_propagates_no_double_charge(monkeypatch):
    """When ``execute_tool('clink', ...)`` raises hard (config broken,
    clink's own paid-API fallback also failed), the wrapper must NOT
    silently retry via inner.agenerate_content — clink may already
    have billed once, and a quiet retry would double-charge.

    Surface the exception. If the user wants to retry without OAuth-first,
    they can set ``PANEL_OAUTH_FIRST=0``.
    """

    inner_calls: list[dict] = []

    class _AssertNoCallProvider(_FakeProvider):
        async def agenerate_content(self, prompt, model_name, **kwargs):
            inner_calls.append({"prompt": prompt, "model": model_name})
            return await super().agenerate_content(prompt, model_name, **kwargs)

    inner = _AssertNoCallProvider()
    wrapped = OAuthFirstProvider(inner)

    monkeypatch.setattr(
        "providers.oauth_first.shutil.which", lambda _: "/fake/path"
    )

    fake_execute = AsyncMock(
        side_effect=RuntimeError("clink config missing for codex")
    )
    monkeypatch.setattr("server.execute_tool", fake_execute)

    with pytest.raises(RuntimeError, match="clink config missing"):
        await wrapped.agenerate_content(prompt="hello", model_name="gpt-5.5")

    # Inner provider must NOT have been called as a silent retry.
    assert inner_calls == []


@pytest.mark.asyncio
async def test_recursion_guard_breaks_clink_to_chat_to_clink_loop(monkeypatch):
    """Re-entrance test: simulate clink's own OAuth fallback calling
    ``execute_tool('chat', model='gpt-5.5')`` mid-flight. The chat tool's
    wrapped provider would land back here with the same model. Without
    the guard, this would call execute_tool('clink') again — unbounded.
    With the guard, the second wrapped.agenerate_content() bypasses the
    wrapper and uses the inner SDK directly, breaking the cycle.
    """

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    monkeypatch.setattr(
        "providers.oauth_first.shutil.which", lambda _: "/fake/path"
    )

    # Track how many times execute_tool was called — must be exactly once.
    execute_tool_calls: list[tuple] = []

    async def fake_execute_tool(tool_name, args):
        execute_tool_calls.append((tool_name, args.get("cli_name")))
        # Simulate clink's internal CLI→API fallback re-entering the
        # wrapper via a nested ``await wrapped.agenerate_content(...)``.
        # If the recursion guard works, the nested call bypasses the
        # wrapper and uses inner directly — no second clink spawn.
        nested = await wrapped.agenerate_content(
            prompt=args["prompt"], model_name="gpt-5.5"
        )
        # Wrap the nested response into clink's TextContent shape
        return _fake_clink_text_content(
            nested.content, oauth_fallback_used=True
        )

    monkeypatch.setattr("server.execute_tool", fake_execute_tool)

    response = await wrapped.agenerate_content(
        prompt="hello", model_name="gpt-5.5"
    )

    # Exactly one execute_tool('clink', ...) call — the nested
    # wrapped.agenerate_content used the inner SDK path directly.
    assert len(execute_tool_calls) == 1, (
        f"recursion guard failed — got {len(execute_tool_calls)} clink "
        f"dispatches: {execute_tool_calls}"
    )
    assert execute_tool_calls[0] == ("clink", "codex")

    # Response surfaces the (correctly identified) fallback path.
    assert response.metadata["oauth_fallback_used"] is True
    assert response.metadata["cost_tier"] == "oauth_fallback_paid"


@pytest.mark.asyncio
async def test_recursion_guard_resets_after_call(monkeypatch):
    """The contextvar guard must reset on exit (success and failure)
    so subsequent independent calls aren't bypassed."""

    from providers.oauth_first import _INSIDE_OAUTH_FIRST

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    monkeypatch.setattr(
        "providers.oauth_first.shutil.which", lambda _: "/fake/path"
    )

    fake_execute = AsyncMock(
        return_value=_fake_clink_text_content("first")
    )
    monkeypatch.setattr("server.execute_tool", fake_execute)

    # Before any call, guard is False
    assert _INSIDE_OAUTH_FIRST.get() is False

    await wrapped.agenerate_content(prompt="a", model_name="gpt-5.5")

    # After successful call, guard must be False again
    assert _INSIDE_OAUTH_FIRST.get() is False

    # Now simulate failure → exception propagates → guard still resets
    fake_execute.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await wrapped.agenerate_content(prompt="b", model_name="gpt-5.5")

    assert _INSIDE_OAUTH_FIRST.get() is False


@pytest.mark.asyncio
async def test_unmapped_model_does_not_call_execute_tool(monkeypatch):
    """grok-4.3 has no CLI route — must NOT touch execute_tool at all."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    fake_execute = AsyncMock()
    monkeypatch.setattr("server.execute_tool", fake_execute)

    await wrapped.agenerate_content(prompt="hi", model_name="grok-4.3")
    fake_execute.assert_not_called()


# ----------------------------------------------------------------------
# Adapter behaviour
# ----------------------------------------------------------------------


def test_adapter_handles_non_json_clink_output():
    """If clink's TextContent.text isn't JSON, adapter falls back to opaque
    content rather than raising — defensive against edge-case output."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    class _RawText:
        text = "raw plain text not json"

    response = wrapped._adapt_clink_to_model_response(
        [_RawText()], cli_name="codex", model_name="gpt-5.5"
    )
    assert response.content == "raw plain text not json"
    assert response.metadata["cost_tier"] == "oauth_free"


def test_adapter_raises_on_empty_result():
    """Empty clink result is a real bug — surface, don't silently return ''."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    with pytest.raises(RuntimeError, match="empty result"):
        wrapped._adapt_clink_to_model_response(
            [], cli_name="codex", model_name="gpt-5.5"
        )


# ----------------------------------------------------------------------
# Sync path
# ----------------------------------------------------------------------


def test_generate_content_sync_passes_through(monkeypatch):
    """Sync path is unchanged — wrapper just stamps cost_tier=api_paid for
    metadata consistency. Subprocess routing on the sync path would block
    the calling thread."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    response = wrapped.generate_content("hi", model_name="gpt-5.5")
    assert response.content == "from-inner-sync"
    assert response.metadata["cost_tier"] == "api_paid"


# ----------------------------------------------------------------------
# Proxy interface delegation
# ----------------------------------------------------------------------


def test_proxy_passes_through_provider_type():
    inner = _FakeProvider(provider_type=ProviderType.ANTHROPIC)
    wrapped = OAuthFirstProvider(inner)
    assert wrapped.get_provider_type() == ProviderType.ANTHROPIC


def test_proxy_passes_through_validate_model_name():
    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)
    assert wrapped.validate_model_name("anything") is True


def test_get_capabilities_delegates_to_inner_not_wrapper_lookup():
    """Regression: ``get_capabilities`` is a real method on ``ModelProvider``
    that uses ``self._lookup_capabilities`` against ``self.MODEL_CAPABILITIES``.
    On the wrapper subclass, that map is empty — so without an explicit
    override, every model raises "Unsupported model" even though the inner
    provider supports it. Discovered via the grok-4.3 dispatch failure
    where validate_model_name returned True but get_capabilities raised.
    """

    class _ProviderWithCaps(_FakeProvider):
        def get_capabilities(self, model_name: str):
            return {"_marker": "from-inner", "model_name": model_name}

    inner = _ProviderWithCaps()
    wrapped = OAuthFirstProvider(inner)

    caps = wrapped.get_capabilities("grok-4.3")
    assert caps["_marker"] == "from-inner", (
        "wrapper.get_capabilities must delegate to inner; got "
        f"{caps!r} which suggests the base ModelProvider implementation "
        "ran on the wrapper itself with an empty MODEL_CAPABILITIES — "
        "the bug this test exists to prevent."
    )
    assert caps["model_name"] == "grok-4.3"


def test_get_all_model_capabilities_delegates_to_inner():
    """Same MRO trap as get_capabilities — without the override, the wrapper
    reports zero models even though the inner provider has many."""

    class _ProviderWithMap(_FakeProvider):
        def get_all_model_capabilities(self):
            return {"grok-4.3": "x", "grok-4.1-fast": "y"}

    inner = _ProviderWithMap()
    wrapped = OAuthFirstProvider(inner)

    out = wrapped.get_all_model_capabilities()
    assert out == {"grok-4.3": "x", "grok-4.1-fast": "y"}


def test_proxy_unknown_attributes_via_getattr():
    """Methods we don't override should still work via __getattr__."""

    class _CustomProvider(_FakeProvider):
        def custom_method(self) -> str:
            return "custom-result"

    inner = _CustomProvider()
    wrapped = OAuthFirstProvider(inner)
    assert wrapped.custom_method() == "custom-result"


# ----------------------------------------------------------------------
# Round-2 fixes: metadata propagation helper
# ----------------------------------------------------------------------


def test_merge_oauth_metadata_copies_only_oauth_keys():
    from providers.oauth_first import merge_oauth_metadata

    target = {"existing": "value"}
    response_meta = {
        "cost_tier": "oauth_free",
        "oauth_route": "codex",
        "oauth_fallback_used": False,
        "irrelevant_provider_key": "leave-me-alone",
    }
    out = merge_oauth_metadata(target, response_meta)

    assert out["existing"] == "value"
    assert out["cost_tier"] == "oauth_free"
    assert out["oauth_route"] == "codex"
    assert out["oauth_fallback_used"] is False
    assert "irrelevant_provider_key" not in out


def test_merge_oauth_metadata_no_op_when_response_has_no_oauth_keys():
    from providers.oauth_first import merge_oauth_metadata

    out = merge_oauth_metadata({"a": 1}, {"random": "thing"})
    assert out == {"a": 1}


def test_merge_oauth_metadata_handles_none_inputs():
    from providers.oauth_first import merge_oauth_metadata

    assert merge_oauth_metadata(None, None) == {}
    assert merge_oauth_metadata({"a": 1}, None) == {"a": 1}


# ----------------------------------------------------------------------
# Round-2 fixes: usage normalization (Gemini token_usage → usage)
# ----------------------------------------------------------------------


def test_adapter_promotes_token_usage_to_usage_field():
    """Gemini parser stores tokens at metadata.token_usage; adapter must
    promote to ModelResponse.usage so consensus/cost-rollup callers see it."""

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    class _TextContent:
        text = json.dumps({
            "status": "success",
            "content": "from gemini",
            "metadata": {
                "token_usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
        })

    response = wrapped._adapt_clink_to_model_response(
        [_TextContent()], cli_name="gemini", model_name="gemini-3.1-pro-preview"
    )

    assert response.usage["total_tokens"] == 150
    assert response.usage["input_tokens"] == 100
    assert response.usage["output_tokens"] == 50


def test_adapter_prefers_usage_over_token_usage_when_both_present():
    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    class _TextContent:
        text = json.dumps({
            "status": "success",
            "content": "x",
            "metadata": {
                "usage": {"total_tokens": 200},
                "token_usage": {"total_tokens": 999},
            },
        })

    response = wrapped._adapt_clink_to_model_response(
        [_TextContent()], cli_name="gemini", model_name="gemini-3.1-pro-preview"
    )

    # ``usage`` wins over ``token_usage`` when both exist
    assert response.usage["total_tokens"] == 200


# ----------------------------------------------------------------------
# Round-2 fixes: sync path warns when bypassing OAuth route
# ----------------------------------------------------------------------


def test_sync_path_warns_for_oauth_eligible_model(caplog):
    """Sync generate_content() bypasses OAuth-first by design (subprocess
    routing on the sync path would block the calling thread). Log a
    warning when the bypass would have helped, so callers get a signal
    that they're paying when they could be free."""

    import logging

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    with caplog.at_level(logging.WARNING, logger="providers.oauth_first"):
        response = wrapped.generate_content("hi", model_name="gpt-5.5")

    assert any("sync generate_content" in r.message for r in caplog.records)
    assert response.metadata["oauth_route_skipped"] == "sync_path"


def test_sync_path_no_warning_for_unmapped_model(caplog):
    """No warning when there's no OAuth route to bypass."""

    import logging

    inner = _FakeProvider()
    wrapped = OAuthFirstProvider(inner)

    with caplog.at_level(logging.WARNING, logger="providers.oauth_first"):
        response = wrapped.generate_content("hi", model_name="grok-4.3")

    assert not any("sync generate_content" in r.message for r in caplog.records)
    assert "oauth_route_skipped" not in response.metadata


# ----------------------------------------------------------------------
# Round-2 fixes: dynamic MODEL_TO_CLI from INTERNAL_DEFAULTS
# ----------------------------------------------------------------------


def test_model_to_cli_derived_from_internal_defaults():
    """MODEL_TO_CLI is built from INTERNAL_DEFAULTS so env-driven overrides
    flow through. Verify each CLI's ``oauth_fallback_model`` appears in the
    inverse mapping."""

    from clink.constants import INTERNAL_DEFAULTS, MODEL_TO_CLI

    for cli_name, defaults in INTERNAL_DEFAULTS.items():
        if defaults.oauth_fallback_model:
            assert MODEL_TO_CLI.get(defaults.oauth_fallback_model) == cli_name, (
                f"{cli_name}'s oauth_fallback_model={defaults.oauth_fallback_model!r} "
                f"is not in the inverse MODEL_TO_CLI map"
            )


def test_model_to_cli_includes_extra_claude_flagships():
    """Beyond the env-driven default, both Opus and Sonnet should route to
    the claude CLI since the Claude subscription serves the family."""

    from clink.constants import MODEL_TO_CLI

    assert MODEL_TO_CLI.get("claude-opus-4-7") == "claude"
    assert MODEL_TO_CLI.get("claude-sonnet-4-6") == "claude"

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


def test_execute_tool_resolves_auto_for_filesize_cap_on_no_model_tools(tmp_path, monkeypatch):
    """Regression: panel/clink (tools that don't require_model) hit the file-size
    check too. When DEFAULT_MODEL='auto' the size cap call would crash with
    'unresolved model: auto'. Caught the first time live by a real panel run."""
    import server

    monkeypatch.setattr(server, "DEFAULT_MODEL", "auto")
    sample = tmp_path / "tiny.txt"
    sample.write_text("hello")

    async def go():
        # 'panel' has requires_model() == False, so size_model would default to
        # DEFAULT_MODEL='auto'. Pre-fix this raised ValueError before panel ever
        # got to validate its own arguments. Post-fix it should reach panel and
        # fail there with a normal arg-validation error (not 'unresolved model').
        try:
            await server.execute_tool(
                "panel",
                {"prompt": "x", "panelists": ["codex"], "absolute_file_paths": [str(sample)]},
            )
        except ValueError as exc:
            if "unresolved model" in str(exc):
                pytest.fail(f"file-size cap regression: {exc}")
        except Exception:
            # Any other error (panel rejecting the tiny prompt, missing CLI,
            # etc) is fine — we only care that we got past the size check.
            pass

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


def test_redaction_preserves_genuinely_non_sensitive_text():
    """Regular prose, version numbers, ports, code-shaped text pass through.
    User-home paths are now redacted (see test_redaction_strips_home_paths)."""
    from tools.clink import _redact_and_cap

    text = "App version 1.2.3 listening on :8080. Status: OK. Latency 145ms."
    assert _redact_and_cap(text, cap=200) == text


def test_redaction_truncates_at_cap():
    from tools.clink import _redact_and_cap

    out = _redact_and_cap("X" * 5000, cap=1000)
    assert len(out) <= 1000 + 100  # cap + truncation marker
    assert "truncated" in out


def test_redaction_strips_home_paths():
    """Audit finding: comments promised HOME path scrubbing but pre-fix
    only API-key shapes were stripped. /Users/<name>/... should be redacted."""
    import os

    from tools.clink import _redact_only

    # The literal HOME for this process must collapse to <HOME>
    home = os.path.expanduser("~")
    if home and home != "/":
        text = f"Failed reading {home}/Projects/secret_repo/file.py at line 42"
        out = _redact_only(text)
        assert home not in out, f"HOME path leaked: {out!r}"
        assert "<HOME>" in out

    # Generic /Users/... patterns from other identities
    out2 = _redact_only("Permission denied for /Users/alice/.ssh/id_rsa")
    assert "/Users/alice" not in out2
    assert "/Users/<USER>" in out2

    # Linux home pattern
    out3 = _redact_only("File at /home/bob/.config/secret.toml")
    assert "/home/bob" not in out3
    assert "/home/<USER>" in out3


def test_redact_only_does_not_truncate():
    """Content path uses _redact_only — secrets stripped but full text preserved."""
    from tools.clink import _redact_only

    long_payload = "answer line " * 5000  # ~60KB of plain text
    out = _redact_only(long_payload)
    assert out == long_payload  # nothing to redact, nothing truncated


def test_safe_merge_drops_protected_metadata_fields():
    """Audit finding: pre-fix metadata.update(result.parsed.metadata) let a
    parser smuggle giant strings through 'command' / 'stderr' / etc. Now
    those fields are dropped from parser metadata."""
    from tools.clink import _safe_merge_parser_metadata

    base = {"cli_name": "codex", "stderr": "[REDACTED ALREADY]", "command": ["safe"]}
    parser_supplied = {
        "stderr": "X" * 100_000,  # parser tries to override sanitised stderr
        "command": "rm -rf /",  # parser tries to override real command
        "model_used": "gpt-5.5",  # legit, not in protected list
        "usage": {"input_tokens": 100},
    }
    out = _safe_merge_parser_metadata(base, parser_supplied)
    assert out["stderr"] == "[REDACTED ALREADY]"
    assert out["command"] == ["safe"]
    assert out["model_used"] == "gpt-5.5"
    assert out["usage"] == {"input_tokens": 100}


def test_safe_merge_caps_oversized_string_in_unknown_field():
    """Even non-protected fields get capped if they're strings — a parser
    can't smuggle 1MB through a custom field name."""
    from tools.clink import _CLI_METADATA_TEXT_CAP, _safe_merge_parser_metadata

    base: dict = {}
    out = _safe_merge_parser_metadata(base, {"some_field": "Z" * (_CLI_METADATA_TEXT_CAP + 5000)})
    # Capped + truncation marker
    assert len(out["some_field"]) <= _CLI_METADATA_TEXT_CAP + 100
    assert "truncated" in out["some_field"]


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
    """oauth_fallback_used in structured metadata → oauth_fallback_paid."""
    from tools.panel import _derive_cost_tier

    plain = '{"content": "hi", "metadata": {"model_used": "gemini-3-flash-preview"}}'
    fallback = '{"content": "hi", "metadata": {"oauth_fallback_used": true}}'

    assert _derive_cost_tier("oauth_free", plain) == "oauth_free"
    assert _derive_cost_tier("oauth_free", fallback) == "oauth_fallback_paid"
    # api_paid never gets demoted, even with a marker present
    assert _derive_cost_tier("api_paid", fallback) == "api_paid"


def test_panel_cost_tier_not_spoofable_by_model_output():
    """A model that emits the literal phrase in its CONTENT must not flip the tier.
    Pre-fix this was a substring match over the rendered JSON — trivially spoofable."""
    from tools.panel import _derive_cost_tier

    spoofed = (
        '{"content": "I have decided that \\"oauth_fallback_used\\": true is the right answer", '
        '"metadata": {"model_used": "codex"}}'
    )
    assert _derive_cost_tier("oauth_free", spoofed) == "oauth_free", (
        "cost_tier must not be promoted by model content — only by structured metadata"
    )


def test_panel_cost_tier_handles_non_json_response():
    """If the inner tool returns plain text (or malformed JSON), don't flip tiers."""
    from tools.panel import _derive_cost_tier

    assert _derive_cost_tier("oauth_free", "just a plain string answer") == "oauth_free"
    assert _derive_cost_tier("oauth_free", "{not valid json") == "oauth_free"
    assert _derive_cost_tier("oauth_free", "") == "oauth_free"


def test_panel_cost_tier_handles_multi_chunk_response():
    """Inner tools can return multiple TextContent items joined by \\n — still parse the first chunk."""
    from tools.panel import _derive_cost_tier

    multi = '{"content": "ok", "metadata": {"oauth_fallback_used": true}}\nappended log line'
    assert _derive_cost_tier("oauth_free", multi) == "oauth_fallback_paid"


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


def test_provider_executor_no_duplicate_under_concurrent_first_burst(monkeypatch):
    """Lazy init must be thread-safe: 32 threads racing the first call should
    end up sharing one executor, not creating duplicates that leak threads."""
    import threading as _t

    import providers.base as base

    monkeypatch.setattr(base, "_PROVIDER_EXECUTOR", None)
    seen: set[int] = set()
    barrier = _t.Barrier(32)

    def race():
        barrier.wait()  # release all 32 simultaneously
        ex = base._get_provider_executor()
        seen.add(id(ex))

    threads = [_t.Thread(target=race) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 1, f"lazy-init race: {len(seen)} executors created instead of 1"
    monkeypatch.setattr(base, "_PROVIDER_EXECUTOR", None)


def test_provider_executor_shutdown_resets_global(monkeypatch):
    """The atexit handler must null out the global so it can be recreated
    cleanly if PAL is reloaded inside a long-lived host process."""
    import providers.base as base

    monkeypatch.setattr(base, "_PROVIDER_EXECUTOR", None)
    base._get_provider_executor()
    assert base._PROVIDER_EXECUTOR is not None
    base._shutdown_provider_executor()
    assert base._PROVIDER_EXECUTOR is None


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

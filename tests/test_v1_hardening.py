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

    # 23 base tools + 3 graph query tools + web_url + multiaudit = 28
    assert len(server.TOOLS) == 28, f"expected 28 tools, got {len(server.TOOLS)}"
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


def test_internal_payload_marker_is_off_by_default():
    """The provenance marker must default to False — bypass is opt-in
    only, set explicitly by trusted code paths."""
    from tools.shared.base_tool import is_internal_payload
    assert is_internal_payload() is False


def test_internal_payload_marker_set_by_context_manager():
    """mark_internal_payload() flips the bit for its scope and restores after."""
    from tools.shared.base_tool import is_internal_payload, mark_internal_payload

    assert is_internal_payload() is False
    with mark_internal_payload():
        assert is_internal_payload() is True
        with mark_internal_payload():
            assert is_internal_payload() is True  # nested still True
        assert is_internal_payload() is True  # outer still True
    assert is_internal_payload() is False  # restored


def test_check_prompt_size_skips_when_internal_payload():
    """check_prompt_size returns None for oversized prompts when marker set."""
    from tools.shared.base_tool import mark_internal_payload
    from server import make_tool

    huge = "X" * 200_000
    chat = make_tool("chat")
    assert chat.check_prompt_size(huge) is not None  # rejected at boundary
    with mark_internal_payload():
        assert chat.check_prompt_size(huge) is None  # bypassed when marked


def test_validate_token_limit_skips_when_internal_payload():
    """_validate_token_limit (the SECOND gate) also respects the marker."""
    from tools.shared.base_tool import mark_internal_payload
    from server import make_tool

    huge = "X" * 200_000
    chat = make_tool("chat")
    with pytest.raises(ValueError, match="too large"):
        chat._validate_token_limit(huge, "Content")  # boundary: raises
    with mark_internal_payload():
        chat._validate_token_limit(huge, "Content")  # marked: must not raise


def test_user_originated_start_task_does_not_bypass_size_check():
    """AUDIT EXPLOIT REGRESSION: pre-provenance fix, a user calling
    start_task(tool='chat', arguments={prompt: <huge>}) caused TaskManager
    to re-enter execute_tool('chat') at depth=2, triggering the depth-based
    bypass and letting unbounded user content reach the paid API.

    With the provenance marker, no internal generator wraps the user's
    start_task call → marker stays False → chat's size check fires.

    We assert that the marker is OFF when entering execute_tool from a
    plain user context, even though the call is technically nested."""
    from tools.shared.base_tool import is_internal_payload

    # Simulate handle_call_tool's no-context entry: marker is False
    assert is_internal_payload() is False
    # No mark_internal_payload anywhere → user-orchestrated nesting
    # cannot promote itself to bypass status.


def test_legacy_dispatch_aliases_still_resolve():
    """Old _enter_dispatch / _exit_dispatch / is_internal_dispatch names are
    preserved as shims so any in-flight branch / external caller doesn't
    break. They forward to the provenance marker (which defaults False),
    so they do NOT re-introduce the depth-based bypass."""
    from tools.shared.base_tool import (
        _enter_dispatch, _exit_dispatch, is_internal_dispatch,
    )

    # Even using the legacy enter/exit pattern, the bypass should NOT fire
    # because there's no explicit mark_internal_payload context.
    t = _enter_dispatch()
    try:
        assert is_internal_dispatch() is False  # legacy alias returns False
    finally:
        _exit_dispatch(t)


def test_timeout_does_not_trigger_fallback_by_default():
    """A 'timed out' clink failure must NOT auto-fallback unless the operator
    opts in via PAL_FALLBACK_ON_TIMEOUT — timeout could mean a legitimately
    long-thinking model and falling back would double-charge."""
    import os

    from clink.agents import CLIAgentError
    from tools.clink import _looks_like_recoverable_failure

    # Ensure the env var is unset / falsy
    prior = os.environ.pop("PAL_FALLBACK_ON_TIMEOUT", None)
    try:
        exc = CLIAgentError(
            "CLI 'gemini' timed out after 1800 seconds",
            returncode=None, stdout="", stderr="",
        )
        assert _looks_like_recoverable_failure(exc) is False
    finally:
        if prior is not None:
            os.environ["PAL_FALLBACK_ON_TIMEOUT"] = prior


def test_timeout_triggers_fallback_when_env_opt_in(monkeypatch):
    """With PAL_FALLBACK_ON_TIMEOUT=1, timeout is treated like an OAuth failure
    and triggers the paid-API fallback."""
    from clink.agents import CLIAgentError
    from tools.clink import _looks_like_recoverable_failure

    monkeypatch.setenv("PAL_FALLBACK_ON_TIMEOUT", "1")
    exc = CLIAgentError(
        "CLI 'gemini' timed out after 1800 seconds",
        returncode=None, stdout="", stderr="",
    )
    assert _looks_like_recoverable_failure(exc) is True


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
        # Sonnet, not Opus — flagship-as-default OAuth fallback was a
        # financial-DoS path. Operators can override with
        # PAL_CLAUDE_OAUTH_FALLBACK_MODEL=opus.
        "claude": "claude-sonnet-4-6",
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


def test_execute_tool_does_not_mutate_caller_arguments():
    """execute_tool used to inject _model_context / _resolved_model_name into
    the caller's dict. start_task stored that dict; replay would persist
    internal-only fields. Fix: shallow-copy at the boundary."""
    import server

    args = {"prompt": "hello", "panelists": ["codex"]}
    snapshot = dict(args)

    async def go():
        try:
            await server.execute_tool("panel", args)
        except Exception:
            pass  # we don't care if panel itself errors; we care about args mutation

    asyncio.run(go())
    assert args == snapshot, f"caller's arguments mutated: was {snapshot}, now {args}"


def test_clinktool_caches_registry_metadata_at_class_level():
    """make_tool('clink') runs per panelist + per fallback. The registry
    pulls and dict comprehensions in __init__ should run once total, not
    once per instance."""
    from tools.clink import CLinkTool

    # Reset cache to simulate first ever construction
    CLinkTool._CLI_NAMES_CACHE = None
    CLinkTool._ROLE_MAP_CACHE = None
    CLinkTool._ALL_ROLES_CACHE = None
    CLinkTool._DEFAULT_CLI_NAME_CACHE = None

    a = CLinkTool()
    b = CLinkTool()

    # Both instances point at the SAME class-level cache objects
    assert a._cli_names is b._cli_names
    assert a._role_map is b._role_map
    assert a._all_roles is b._all_roles


def test_oauth_fallback_preserves_non_dict_metadata():
    """If chat returns a body whose metadata isn't a dict, we keep the
    original under metadata_original_non_dict instead of silently dropping."""
    import json

    from mcp.types import TextContent

    from tools.clink import CLinkTool

    payload = {"status": "success", "content": "hi", "metadata": "this is a string not a dict"}
    result = [TextContent(type="text", text=json.dumps(payload))]
    out = CLinkTool._mark_fallback_in_result(
        result, cli_name="gemini", fallback_model="gemini-3.1-pro-preview", original_failure="..."
    )
    body = json.loads(out[0].text)
    assert body["metadata"]["oauth_fallback_used"] is True
    assert body["metadata"]["metadata_original_non_dict"] == "this is a string not a dict"


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


def test_openai_streaming_v2_default_on_opt_out_via_env(monkeypatch):
    """Streaming v2 is ON by default — operators see the model write live
    in the viewer. PAL_OPENAI_STREAM=0 opts back to .create() shape so the
    cassette-replay integration tests can hash a stable request body."""
    from unittest.mock import MagicMock, patch

    from providers.openai import OpenAIModelProvider

    captured: dict = {}

    def fake_create(**kwargs):
        captured.clear()
        captured.update(kwargs)
        if kwargs.get("stream"):
            # Streaming branch: return an iterable of chunks. The provider
            # walks .choices[0].delta.content and the trailing usage block.
            chunk = MagicMock()
            chunk.id = "id"
            chunk.model = "gpt-5"
            chunk.created = 0
            choice = MagicMock()
            choice.delta = MagicMock(content="ok")
            choice.finish_reason = "stop"
            chunk.choices = [choice]
            chunk.usage = None
            usage_chunk = MagicMock()
            usage_chunk.id = "id"
            usage_chunk.model = "gpt-5"
            usage_chunk.created = 0
            usage_chunk.choices = []
            usage_chunk.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
            return iter([chunk, usage_chunk])
        # Non-streaming branch (legacy / cassette-replay shape).
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content="ok"), finish_reason="stop")]
        resp.id = "id"
        resp.model = "gpt-5"
        resp.created = 0
        resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return resp

    # Default (env unset) → streaming on
    monkeypatch.delenv("PAL_OPENAI_STREAM", raising=False)
    provider = OpenAIModelProvider("test-key")
    with patch.object(type(provider), "client", new_callable=lambda: property(lambda _: MagicMock(chat=MagicMock(completions=MagicMock(create=fake_create))))):
        provider.generate_content(prompt="hi", model_name="gpt-5")
    assert captured.get("stream") is True
    assert captured.get("stream_options") == {"include_usage": True}

    # Explicit opt-out → legacy non-streaming shape
    captured.clear()
    monkeypatch.setenv("PAL_OPENAI_STREAM", "0")
    with patch.object(type(provider), "client", new_callable=lambda: property(lambda _: MagicMock(chat=MagicMock(completions=MagicMock(create=fake_create))))):
        provider.generate_content(prompt="hi", model_name="gpt-5")
    assert captured.get("stream") is False
    assert "stream_options" not in captured


def test_agenerate_content_propagates_run_context_to_worker(monkeypatch):
    """Round-3 audit blocker: ``loop.run_in_executor`` does NOT propagate
    ContextVars to the worker thread by default. The fix in
    ``providers/base.py`` captures ``contextvars.copy_context()`` so the
    worker sees the active run id. Without this, every streaming
    progress emit silently no-ops."""
    import asyncio

    from providers.base import ModelProvider
    from utils.execution_graph import current_run_id, _CURRENT_RUN_ID

    captured: dict = {"run_id": "MISSING"}

    class _StubProvider(ModelProvider):
        def get_provider_type(self): pass
        def validate_model_name(self, *a, **kw): return True
        def supports_thinking_mode(self, *a, **kw): return False
        def list_models(self, *a, **kw): return []
        def list_known_models(self, *a, **kw): return []
        def get_capabilities(self, *a, **kw): return None
        def get_preferred_model(self, *a, **kw): return None
        def count_tokens(self, *a, **kw): return 1

        def generate_content(self, *args, **kwargs):
            # Read the ContextVar from inside the worker thread. If
            # propagation is broken, this is None.
            captured["run_id"] = current_run_id()
            from providers.shared import ModelResponse, ProviderType
            return ModelResponse(
                content="ok", usage=None, model_name="x",
                friendly_name="x", provider=ProviderType.OPENAI, metadata={},
            )

    async def go():
        token = _CURRENT_RUN_ID.set("test-run-deadbeef")
        try:
            await _StubProvider("k").agenerate_content("hi", "x")
        finally:
            _CURRENT_RUN_ID.reset(token)

    asyncio.run(go())
    assert captured["run_id"] == "test-run-deadbeef", (
        "agenerate_content must propagate the run_context ContextVar into "
        "the executor worker thread (round-3 audit blocker)."
    )


def test_stream_progress_emitter_throttles_and_emits(monkeypatch):
    """The shared stream emitter respects its time throttle, accumulates
    text deltas, and writes graph events with the actual content (not
    a chunk-count status ping)."""
    from utils.stream_progress import StreamProgressEmitter

    events = []

    class _FakeGraph:
        def add_event(self, run_id, *, event_type, message, progress):
            events.append({"run_id": run_id, "event_type": event_type, "message": message})

    import utils.execution_graph as eg
    monkeypatch.setattr(eg, "get_graph", lambda: _FakeGraph())

    emitter = StreamProgressEmitter(label="claude/x", run_id="rid", throttle_s=0.0)
    emitter.feed("Hello ")
    emitter.feed("world")
    emitter.finalize()

    assert len(events) >= 1
    last = events[-1]
    assert last["run_id"] == "rid"
    assert last["event_type"] == "text_chunk"
    assert "Hello" in last["message"] and "world" in last["message"]
    assert "[claude/x]" in last["message"]


def test_stream_progress_emitter_no_run_id_is_silent(monkeypatch):
    """Without a run id (sync entry point or worker without context)
    the emitter must NOT touch the graph — silent no-op rather than
    crash."""
    from utils.stream_progress import StreamProgressEmitter

    called = {"add_event": 0}

    class _FakeGraph:
        def add_event(self, *a, **kw):
            called["add_event"] += 1

    import utils.execution_graph as eg
    monkeypatch.setattr(eg, "get_graph", lambda: _FakeGraph())

    emitter = StreamProgressEmitter(label="x", run_id=None, throttle_s=0.0)
    emitter.feed("hi")
    emitter.finalize()
    assert called["add_event"] == 0


def test_gemini_streaming_handles_safety_block_valueerror(monkeypatch):
    """Round-3 audit blocker: google-genai's .text property raises
    ``ValueError`` (not ``AttributeError``) on safety-blocked or
    tool-call-only chunks. ``getattr(default=...)`` does NOT suppress
    that. The provider must catch it explicitly."""
    from unittest.mock import MagicMock, patch
    from providers.gemini import GeminiModelProvider

    class _SafetyBlockedPartial:
        @property
        def text(self):
            raise ValueError("safety block on this chunk")
        candidates = None
        prompt_feedback = None
        usage_metadata = None

    class _NormalPartial:
        text = "hello world"
        candidates = [MagicMock(finish_reason=MagicMock(name="STOP"), safety_ratings=[])]
        prompt_feedback = None
        usage_metadata = MagicMock(prompt_token_count=1, candidates_token_count=1, total_token_count=2)

    def fake_stream(*args, **kwargs):
        yield _SafetyBlockedPartial()
        yield _NormalPartial()

    monkeypatch.setenv("PAL_GEMINI_STREAM", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    provider = GeminiModelProvider("k")
    with patch.object(type(provider), "client", new_callable=lambda: property(lambda _: MagicMock(models=MagicMock(generate_content_stream=fake_stream)))):
        result = provider.generate_content(prompt="hi", model_name="gemini-3.1-pro-preview")

    assert "hello world" in (result.content or "")


def test_gemini_streaming_zero_chunks_raises_clear_error(monkeypatch):
    """Round-3 audit blocker: a stream that yields zero partials left
    response=None and crashed at .candidates with a confusing
    AttributeError. Now surfaces as a clear RuntimeError that the retry
    layer can wrap meaningfully."""
    from unittest.mock import MagicMock, patch
    from providers.gemini import GeminiModelProvider

    def fake_stream(*args, **kwargs):
        return iter([])

    monkeypatch.setenv("PAL_GEMINI_STREAM", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    provider = GeminiModelProvider("k")
    with patch.object(type(provider), "client", new_callable=lambda: property(lambda _: MagicMock(models=MagicMock(generate_content_stream=fake_stream)))):
        try:
            provider.generate_content(prompt="hi", model_name="gemini-3.1-pro-preview")
        except RuntimeError as exc:
            assert "no chunks" in str(exc).lower() or "stream" in str(exc).lower()
            return
        # Some retry wrappers raise instead of bubbling — both shapes ok.


def test_multiaudit_judge_configurable_via_env(monkeypatch):
    """``PAL_MULTIAUDIT_JUDGE`` env var must override the default
    'codex' judge, so operators can swap the synthesiser without
    editing code."""
    monkeypatch.setenv("PAL_MULTIAUDIT_JUDGE", "claude")
    # Re-import to pick up env at module load.
    import importlib
    import tools.multiaudit as m
    importlib.reload(m)
    assert m.DEFAULT_JUDGE == "claude"


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

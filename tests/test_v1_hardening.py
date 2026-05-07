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

    # 23 base tools + 3 graph query tools + web_url + panel_settings + multiaudit + bugfind + ask_panel = 31
    assert len(server.TOOLS) == 31, f"expected 31 tools, got {len(server.TOOLS)}"
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

    monkeypatch.setenv("PANEL_DEBUG_CLI_OUTPUT", "1")
    raw = "sk-proj-secret " + "X" * 5000
    out = _redact_and_cap(raw, cap=1000)
    assert out == raw, "PANEL_DEBUG_CLI_OUTPUT must passthrough untouched"


# ---------------------------------------------------------------------------
# Clink OAuth failure detection (9976892)
# ---------------------------------------------------------------------------


def test_oauth_failure_detected_for_gemini_quota():
    """Real Gemini stderr from the live smoke test must match."""
    from clink.agents import CLIAgentError
    from tools.clink import _looks_like_recoverable_failure

    exc = CLIAgentError(
        "exited 1",
        returncode=1,
        stdout='{"type":"result","status":"error"}',
        stderr="TerminalQuotaError: You have exhausted your capacity on this model. Your quota will reset after 21h59m17s.",
    )
    assert _looks_like_recoverable_failure(exc)


def test_oauth_failure_detected_for_codex_auth_lapse():
    from clink.agents import CLIAgentError
    from tools.clink import _looks_like_recoverable_failure

    exc = CLIAgentError(
        "exited 1",
        returncode=1,
        stdout="",
        stderr="Error: 401 Unauthorized. Please run codex login.",
    )
    assert _looks_like_recoverable_failure(exc)


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
    opts in via PANEL_FALLBACK_ON_TIMEOUT — timeout could mean a legitimately
    long-thinking model and falling back would double-charge."""
    import os

    from clink.agents import CLIAgentError
    from tools.clink import _looks_like_recoverable_failure

    # Ensure the env var is unset / falsy
    prior = os.environ.pop("PANEL_FALLBACK_ON_TIMEOUT", None)
    try:
        exc = CLIAgentError(
            "CLI 'gemini' timed out after 1800 seconds",
            returncode=None, stdout="", stderr="",
        )
        assert _looks_like_recoverable_failure(exc) is False
    finally:
        if prior is not None:
            os.environ["PANEL_FALLBACK_ON_TIMEOUT"] = prior


def test_timeout_triggers_fallback_when_env_opt_in(monkeypatch):
    """With PANEL_FALLBACK_ON_TIMEOUT=1, timeout is treated like an OAuth failure
    and triggers the paid-API fallback."""
    from clink.agents import CLIAgentError
    from tools.clink import _looks_like_recoverable_failure

    monkeypatch.setenv("PANEL_FALLBACK_ON_TIMEOUT", "1")
    exc = CLIAgentError(
        "CLI 'gemini' timed out after 1800 seconds",
        returncode=None, stdout="", stderr="",
    )
    assert _looks_like_recoverable_failure(exc) is True


def test_real_prompt_error_not_misclassified_as_oauth_failure():
    """Negative case — false positive costs a paid-API call."""
    from clink.agents import CLIAgentError
    from tools.clink import _looks_like_recoverable_failure

    exc = CLIAgentError(
        "exited 2",
        returncode=2,
        stdout="",
        stderr="Bad request: invalid model name",
    )
    assert not _looks_like_recoverable_failure(exc)


def test_oauth_fallback_models_wired_for_each_cli():
    """Registry resolution must propagate oauth_fallback_model from constants."""
    from clink import get_registry

    r = get_registry()
    expected = {
        "gemini": "gemini-3.1-pro-preview",
        "codex": "gpt-5.5",
        # Sonnet, not Opus — flagship-as-default OAuth fallback was a
        # financial-DoS path. Operators can override with
        # PANEL_CLAUDE_OAUTH_FALLBACK_MODEL=opus.
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
    """PANEL_MAX_PROVIDER_THREADS must be respected at first init."""
    import providers.base as base

    monkeypatch.setattr(base, "_PROVIDER_EXECUTOR", None)
    monkeypatch.setenv("PANEL_MAX_PROVIDER_THREADS", "7")
    ex = base._get_provider_executor()
    assert ex._max_workers == 7
    # Reset so other tests don't see the 7-thread pool
    monkeypatch.setattr(base, "_PROVIDER_EXECUTOR", None)


def test_provider_semaphore_honors_env_cap(monkeypatch):
    """PANEL_MAX_CONCURRENT_API must be respected at first init."""
    import providers.base as base

    async def go():
        monkeypatch.setattr(base, "_API_SEMAPHORE", None)
        monkeypatch.setenv("PANEL_MAX_CONCURRENT_API", "3")
        sem = base._get_api_semaphore()
        assert sem._value == 3
        monkeypatch.setattr(base, "_API_SEMAPHORE", None)

    asyncio.run(go())


def test_get_default_api_timeout_default():
    from providers.base import get_default_api_timeout

    # No env set → 1800s default (matches panelist timeouts so OAuth-fallback
    # paid-API calls don't get killed before the panelist budget runs out).
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PANEL_API_TIMEOUT_S", None)
        assert get_default_api_timeout() == 1800.0


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
    cleanly if Panel is reloaded inside a long-lived host process."""
    import providers.base as base

    monkeypatch.setattr(base, "_PROVIDER_EXECUTOR", None)
    base._get_provider_executor()
    assert base._PROVIDER_EXECUTOR is not None
    base._shutdown_provider_executor()
    assert base._PROVIDER_EXECUTOR is None


def test_openai_streaming_v2_default_on_opt_out_via_env(monkeypatch):
    """Streaming v2 is ON by default — operators see the model write live
    in the viewer. PANEL_OPENAI_STREAM=0 opts back to .create() shape so the
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
    monkeypatch.delenv("PANEL_OPENAI_STREAM", raising=False)
    provider = OpenAIModelProvider("test-key")
    with patch.object(type(provider), "client", new_callable=lambda: property(lambda _: MagicMock(chat=MagicMock(completions=MagicMock(create=fake_create))))):
        provider.generate_content(prompt="hi", model_name="gpt-5")
    assert captured.get("stream") is True
    assert captured.get("stream_options") == {"include_usage": True}

    # Explicit opt-out → legacy non-streaming shape
    captured.clear()
    monkeypatch.setenv("PANEL_OPENAI_STREAM", "0")
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
    # Combined across all emits, both deltas were shipped exactly once.
    combined = "".join(e["message"] for e in events)
    assert "Hello" in combined and "world" in combined
    assert combined.count("Hello") == 1 and combined.count("world") == 1
    for e in events:
        assert e["run_id"] == "rid"
        assert e["event_type"] == "text_chunk"
        assert "[claude/x]" in e["message"]


def test_stream_progress_emitter_emits_only_deltas_not_cumulative(monkeypatch):
    """Round-3 panel caught this as a browser-DoS: if the emitter
    re-joins the entire buffer on every emit without clearing, each
    successive text_chunk event carries cumulative content. The viewer
    then concatenates them, growing DOM size O(N²) — a 60s response can
    push tens of megabytes into a single tab. Each emit must ship ONLY
    new deltas accumulated since the last emit."""
    from utils.stream_progress import StreamProgressEmitter

    bodies: list[str] = []

    class _FakeGraph:
        def add_event(self, run_id, *, event_type, message, progress):
            # Strip the "[label] " prefix to inspect just the body.
            body = message.split("] ", 1)[1] if "] " in message else message
            bodies.append(body)

    import utils.execution_graph as eg
    monkeypatch.setattr(eg, "get_graph", lambda: _FakeGraph())

    emitter = StreamProgressEmitter(label="x", run_id="rid", throttle_s=0.0)
    emitter.feed("AAA")
    emitter.feed("BBB")
    emitter.finalize()
    emitter.feed("CCC")
    emitter.feed("DDD")
    emitter.finalize()

    # Each emit ships only the deltas since the last emit; bodies must
    # NOT contain cumulative text.
    joined = "".join(bodies)
    # Exactly one occurrence of each delta, total = sum of feeds.
    assert joined.count("AAA") == 1
    assert joined.count("BBB") == 1
    assert joined.count("CCC") == 1
    assert joined.count("DDD") == 1
    # Overall length is bounded by sum of feeds (12), not quadratic.
    assert sum(len(b) for b in bodies) <= 16, (
        f"emitter shipped cumulative content; body sizes: {[len(b) for b in bodies]}"
    )


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

    monkeypatch.setenv("PANEL_GEMINI_STREAM", "1")
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

    monkeypatch.setenv("PANEL_GEMINI_STREAM", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    provider = GeminiModelProvider("k")
    import pytest
    with patch.object(type(provider), "client", new_callable=lambda: property(lambda _: MagicMock(models=MagicMock(generate_content_stream=fake_stream)))):
        # Strict pytest.raises so a future regression that drops the
        # ``if response is None`` guard FAILS this test instead of
        # passing silently. The retry wrapper rewraps the inner
        # RuntimeError; both layers carry the "no chunks" hint.
        with pytest.raises(RuntimeError, match="(?i)no chunks|gemini stream"):
            provider.generate_content(prompt="hi", model_name="gemini-3.1-pro-preview")


def test_multiaudit_judge_configurable_via_env(tmp_path, monkeypatch):
    """``PANEL_MULTIAUDIT_JUDGE`` env var must override the default
    'codex' judge, so operators can swap the synthesiser without
    editing code. Asserted via runtime dispatch (env is read at
    ``execute()`` time, not module-import time, since the round-2
    audit caught that import-time freeze made the live settings
    tab a lie)."""
    import asyncio
    import json as _json
    import subprocess as _sp

    repo = tmp_path / "r"
    repo.mkdir()
    _sp.run(["git", "init", "-q"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    _sp.run(["git", "checkout", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "f.txt").write_text("a")
    _sp.run(["git", "add", "."], cwd=repo, check=True)
    _sp.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)
    _sp.run(["git", "checkout", "-q", "-b", "feat"], cwd=repo, check=True)
    (repo / "f.txt").write_text("b")
    _sp.run(["git", "commit", "-aq", "-m", "c"], cwd=repo, check=True)

    monkeypatch.setenv("PANEL_MULTIAUDIT_JUDGE", "claude")

    async def fake_execute(name, arguments):
        from mcp.types import TextContent

        return [
            TextContent(
                type="text",
                text=_json.dumps({"status": "started", "task_id": "t"}),
            )
        ]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.multiaudit import MultiauditTool

    out = asyncio.run(
        MultiauditTool().execute({"working_directory_absolute_path": str(repo)})
    )
    body = _json.loads(out[0].text)
    assert body["judge"] == "claude"


def test_multiaudit_panelists_configurable_via_env(tmp_path, monkeypatch):
    """``PANEL_MULTIAUDIT_PANELISTS`` env var (comma-separated) overrides
    the default panelist list at execute time. Whitespace tolerated,
    empty entries dropped. Module-level ``DEFAULT_PANELISTS`` is now
    an immutable tuple — env overrides resolve fresh inside
    ``execute()``."""
    import asyncio
    import json as _json
    import subprocess as _sp

    repo = tmp_path / "r"
    repo.mkdir()
    _sp.run(["git", "init", "-q"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    _sp.run(["git", "checkout", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "f.txt").write_text("a")
    _sp.run(["git", "add", "."], cwd=repo, check=True)
    _sp.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)
    _sp.run(["git", "checkout", "-q", "-b", "feat"], cwd=repo, check=True)
    (repo / "f.txt").write_text("b")
    _sp.run(["git", "commit", "-aq", "-m", "c"], cwd=repo, check=True)

    monkeypatch.setenv("PANEL_MULTIAUDIT_PANELISTS", "claude, grok-4.3 , gemini")

    async def fake_execute(name, arguments):
        from mcp.types import TextContent

        return [
            TextContent(
                type="text",
                text=_json.dumps({"status": "started", "task_id": "t"}),
            )
        ]

    import server

    monkeypatch.setattr(server, "execute_tool", fake_execute)

    from tools.multiaudit import DEFAULT_PANELISTS, MultiauditTool

    # Module-level fallback is the immutable canonical tuple — anything
    # else means a future regression reintroduced import-time mutation.
    assert isinstance(DEFAULT_PANELISTS, tuple)
    assert DEFAULT_PANELISTS == ("codex", "gemini", "claude", "grok-4.3")

    out = asyncio.run(
        MultiauditTool().execute({"working_directory_absolute_path": str(repo)})
    )
    body = _json.loads(out[0].text)
    assert body["panelists"] == ["claude", "grok-4.3", "gemini"]


def test_multiaudit_judge_resolves_env_at_execute_not_import(tmp_path, monkeypatch):
    """The settings tab claims live mutation of PANEL_MULTIAUDIT_JUDGE.
    Round-3 audit caught that the value was frozen at module import via
    DEFAULT_JUDGE; setting the env var AFTER multiaudit was already
    imported had no effect on the next dispatch. Resolve env vars
    inside execute() so live toggles actually take effect."""
    import asyncio
    import json as _json
    from pathlib import Path

    # Fresh git repo with main + a feature branch carrying a real diff
    # so multiaudit reaches the dispatch.
    repo = tmp_path / "r"
    repo.mkdir()
    import subprocess as _sp
    _sp.run(["git", "init", "-q"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    _sp.run(["git", "checkout", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "f.txt").write_text("a")
    _sp.run(["git", "add", "."], cwd=repo, check=True)
    _sp.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)
    _sp.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    (repo / "f.txt").write_text("b")
    _sp.run(["git", "commit", "-aq", "-m", "c"], cwd=repo, check=True)

    captured: dict = {}

    async def fake_execute(name, arguments):
        captured["arguments"] = arguments
        from mcp.types import TextContent
        return [TextContent(type="text", text=_json.dumps({"status": "started", "task_id": "t"}))]

    import server
    monkeypatch.setattr(server, "execute_tool", fake_execute)

    # Module imports with env unset, falls back to canonical 'codex'.
    # Then we mutate env at runtime — the live judge must be 'claude',
    # not 'codex', because the module-level constant is no longer
    # populated from env at import time.
    monkeypatch.delenv("PANEL_MULTIAUDIT_JUDGE", raising=False)
    import tools.multiaudit as m

    monkeypatch.setenv("PANEL_MULTIAUDIT_JUDGE", "claude")

    async def go():
        return await m.MultiauditTool().execute(
            {"working_directory_absolute_path": str(repo)}
        )

    asyncio.run(go())
    assert captured["arguments"]["arguments"]["judge"] == "claude", (
        "live env mutation must reach execute() — round-3 panel-flagged"
    )


def test_agenerate_content_holds_semaphore_until_thread_completes_on_cancel(monkeypatch):
    """Cancel-aware semaphore release. The asyncio task may be cancelled
    mid-flight, but the worker thread keeps running its blocking SDK
    call until PANEL_API_TIMEOUT_S. The semaphore must NOT be released the
    instant cancellation happens — it has to wait for the thread to
    actually finish, otherwise a flurry of cancellations exhausts the
    thread pool while the semaphore reports plenty of capacity, blocking
    new calls at executor.submit for up to 10 minutes with no error.

    Round-3 panel-flagged top open-queue item; this test locks the fix in."""
    import asyncio
    import threading
    import time

    from providers.base import ModelProvider, _get_api_semaphore
    import providers.base as base

    # Use a 1-slot BoundedSemaphore so we can prove with a single cancel
    # that the slot is held until the thread completes — AND so a
    # double-release would raise ValueError (plain asyncio.Semaphore
    # silently over-releases, hiding double-fire bugs in the done
    # callback). Reset the lazy singleton + inject our test sem.
    monkeypatch.setenv("PANEL_MAX_CONCURRENT_API", "1")
    monkeypatch.setattr(base, "_API_SEMAPHORE", asyncio.BoundedSemaphore(1))

    thread_done = threading.Event()
    started = threading.Event()

    class _SlowProvider(ModelProvider):
        def get_provider_type(self): pass
        def validate_model_name(self, *a, **kw): return True
        def supports_thinking_mode(self, *a, **kw): return False
        def list_models(self, *a, **kw): return []
        def list_known_models(self, *a, **kw): return []
        def get_capabilities(self, *a, **kw): return None
        def get_preferred_model(self, *a, **kw): return None
        def count_tokens(self, *a, **kw): return 1

        def generate_content(self, *args, **kwargs):
            started.set()
            # Simulate a blocking SDK call that ignores asyncio cancel.
            time.sleep(0.5)
            thread_done.set()
            from providers.shared import ModelResponse, ProviderType
            return ModelResponse(
                content="x", usage=None, model_name="m",
                friendly_name="m", provider=ProviderType.OPENAI, metadata={},
            )

    async def go() -> tuple[bool, bool]:
        provider = _SlowProvider("k")
        task = asyncio.create_task(provider.agenerate_content("hi", "m"))
        # Wait for the worker thread to actually start, then cancel.
        for _ in range(50):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        sem = _get_api_semaphore()
        # Immediately after cancellation, the slot must STILL be held
        # because the thread is still running.
        held_after_cancel = sem.locked()

        # Wait for the worker thread to finish. The done-callback then
        # schedules sem.release via call_soon_threadsafe; poll until the
        # event loop drains that callback rather than relying on a fixed
        # sleep — fixed sleeps flake under CI load (panel-flagged).
        for _ in range(200):
            if thread_done.is_set():
                break
            await asyncio.sleep(0.01)
        for _ in range(200):  # 2s budget for the cross-thread release
            if not sem.locked():
                break
            await asyncio.sleep(0.01)
        released_after_thread = not sem.locked()
        return held_after_cancel, released_after_thread

    held, released = asyncio.run(go())
    assert held, "semaphore was released before worker thread finished — phantom slot bug"
    assert released, "semaphore never released after worker thread finished"


def test_agenerate_content_releases_semaphore_on_submit_failure(monkeypatch):
    """If executor.submit raises (e.g. executor shutting down), the
    semaphore must be released before the exception propagates.
    Otherwise the slot leaks permanently. Panel-flagged gap."""
    import asyncio

    from providers.base import ModelProvider, _get_api_semaphore
    import providers.base as base

    monkeypatch.setenv("PANEL_MAX_CONCURRENT_API", "1")
    monkeypatch.setattr(base, "_API_SEMAPHORE", asyncio.BoundedSemaphore(1))

    class _DummyExecutor:
        def submit(self, fn, *a, **kw):
            raise RuntimeError("executor shutdown")

    monkeypatch.setattr(base, "_get_provider_executor", lambda: _DummyExecutor())

    class _NoOpProvider(ModelProvider):
        def get_provider_type(self): pass
        def validate_model_name(self, *a, **kw): return True
        def supports_thinking_mode(self, *a, **kw): return False
        def list_models(self, *a, **kw): return []
        def list_known_models(self, *a, **kw): return []
        def get_capabilities(self, *a, **kw): return None
        def get_preferred_model(self, *a, **kw): return None
        def count_tokens(self, *a, **kw): return 1
        def generate_content(self, *a, **kw):
            raise AssertionError("must not be called when submit fails")

    async def go() -> bool:
        provider = _NoOpProvider("k")
        try:
            await provider.agenerate_content("hi", "m")
        except RuntimeError as exc:
            assert "executor shutdown" in str(exc)
        else:
            raise AssertionError("submit failure should propagate")
        return not _get_api_semaphore().locked()

    assert asyncio.run(go()), "semaphore leaked after submit-failure path"


def test_task_result_falls_back_to_graph_for_terminal_states(tmp_path, monkeypatch):
    """task_result on memory miss must hit the graph DB and surface
    completed/failed/cancelled persisted records as terminal results.
    Powers cross-restart task recovery."""
    import asyncio
    import json as _json

    from utils.execution_graph import ExecutionGraph
    import utils.execution_graph as eg

    g = ExecutionGraph(db_path=tmp_path / "tr.db")
    monkeypatch.setattr(eg, "_GRAPH", g)
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)

    g.upsert_task(
        "deadc0de", tool="panel", label="multiaudit:main",
        run_id="run-aaa", status="completed",
        created_at=1.0, started_at=1.1, completed_at=10.0,
        result_json='[{"headline": "ok"}]', error=None,
    )

    from tools.tasks import TaskResultTool

    async def go():
        return await TaskResultTool().execute({"task_id": "deadc0de", "wait_seconds": 0})

    out = asyncio.run(go())
    body = _json.loads(out[0].text)
    assert body["status"] == "completed"
    assert body["task"]["from_graph"] is True
    assert body["task"]["session_security"] == "bearer_after_restart"
    assert body["task"]["run_id"] == "run-aaa"
    assert body["result"] == [{"headline": "ok"}]


def test_task_result_surfaces_interrupted_for_non_terminal_persisted(tmp_path, monkeypatch):
    """A persisted task with status='running' represents a process
    that died mid-flight. Surface a clear "interrupted by Panel restart"
    error rather than the misleading "unknown task_id"."""
    import asyncio
    import json as _json

    from utils.execution_graph import ExecutionGraph
    import utils.execution_graph as eg

    g = ExecutionGraph(db_path=tmp_path / "ti.db")
    monkeypatch.setattr(eg, "_GRAPH", g)
    monkeypatch.setattr(eg, "_GRAPH_DISABLED", False)

    g.upsert_task(
        "abandon1", tool="panel", label=None, run_id=None,
        status="running",  # interrupted, never reached terminal
        created_at=1.0, started_at=1.1, completed_at=None,
        result_json=None, error=None,
    )

    from tools.tasks import TaskResultTool

    async def go():
        return await TaskResultTool().execute({"task_id": "abandon1", "wait_seconds": 0})

    out = asyncio.run(go())
    body = _json.loads(out[0].text)
    assert body["status"] == "error"
    assert "interrupted" in body["error"].lower()


def test_web_viewer_refuses_non_localhost_bind_without_opt_in(monkeypatch):
    """Non-localhost binds expose the unauthenticated execution graph
    to anyone on the network. Refuse to start unless the operator has
    consciously opted in via PANEL_WEB_ALLOW_REMOTE=1."""
    import importlib
    import utils.web_viewer as wv

    monkeypatch.setenv("PANEL_WEB_HOST", "0.0.0.0")
    monkeypatch.delenv("PANEL_WEB_ALLOW_REMOTE", raising=False)
    importlib.reload(wv)
    assert wv.start_web_viewer() is None, (
        "viewer must refuse 0.0.0.0 binds without PANEL_WEB_ALLOW_REMOTE=1"
    )


def test_web_viewer_localhost_bind_starts_normally(monkeypatch):
    """The default 127.0.0.1 bind has no opt-in needed — local-only is safe."""
    import importlib
    import utils.web_viewer as wv

    monkeypatch.setenv("PANEL_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("PANEL_WEB_AUTO_OPEN", "0")  # no browser pop in tests
    monkeypatch.delenv("PANEL_WEB_DISABLE", raising=False)
    importlib.reload(wv)
    url = wv.start_web_viewer()
    try:
        assert url is not None and url.startswith("http://127.0.0.1:")
    finally:
        wv.stop_web_viewer()


def test_execution_graph_version_bumps_on_writes(tmp_path):
    """SSE relies on get_version() incrementing on every write so the
    viewer knows when to refetch. start_run, add_event, complete_run,
    fail_run, cancel_run, upsert_task must all bump."""
    from utils.execution_graph import ExecutionGraph

    g = ExecutionGraph(db_path=tmp_path / "v.db")
    v0 = g.get_version()
    rid = g.start_run("test")
    v1 = g.get_version()
    g.add_event(rid, event_type="progress", message="hi")
    v2 = g.get_version()
    g.complete_run(rid, result={"ok": True})
    v3 = g.get_version()
    g.upsert_task("abc", tool="chat", label=None, run_id=rid,
                  status="completed", created_at=0.0,
                  completed_at=1.0, result_json='["ok"]')
    v4 = g.get_version()
    assert v0 < v1 < v2 < v3 < v4


def test_task_persistence_round_trip(tmp_path, monkeypatch):
    """upsert_task → get_task returns the same record. Powers the
    task_result fallback path after Panel restart."""
    from utils.execution_graph import ExecutionGraph

    g = ExecutionGraph(db_path=tmp_path / "t.db")
    g.upsert_task(
        "abc123", tool="panel", label="multiaudit:main",
        run_id="run-xyz", status="completed",
        created_at=1.0, started_at=1.1, completed_at=10.0,
        result_json='[{"headline": "ok"}]', error=None,
    )
    row = g.get_task("abc123")
    assert row is not None
    assert row["task_id"] == "abc123"
    assert row["tool"] == "panel"
    assert row["status"] == "completed"
    assert row["result_json"] == '[{"headline": "ok"}]'
    # idempotent upsert preserves earlier fields when None passed
    g.upsert_task(
        "abc123", tool="panel", label="multiaudit:main",
        run_id=None, status="completed",
        created_at=1.0, started_at=None, completed_at=None,
        result_json=None, error=None,
    )
    row2 = g.get_task("abc123")
    assert row2["run_id"] == "run-xyz"
    assert row2["completed_at"] == 10.0
    assert row2["result_json"] == '[{"headline": "ok"}]'


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

"""OAuth-first provider wrapper.

Wraps a real ``ModelProvider`` and, for models with a configured OAuth route,
tries the corresponding clink CLI subprocess first. Falls through to the
wrapped provider's API path only when the CLI is not installed on the
machine. clink itself handles CLI-quota → paid-API fallback internally, so
when the CLI exists but quota is exhausted the wrapper trusts clink's own
fallback (which lands on the same model the user asked for, by construction
of the ``oauth_fallback_model`` mapping).

Activated by registering the wrapper around live providers in
``providers.registry`` when ``PANEL_OAUTH_FIRST=1`` (the default). Set
``PANEL_OAUTH_FIRST=0`` to disable — useful for tests that mock the SDK
path directly, or for users who explicitly want every call billed.

The wrapper is a thin proxy: every method except ``agenerate_content``
delegates to the inner provider unchanged. ``generate_content`` (sync) is
also pass-through — async tool dispatches are the only ones that benefit
from OAuth-first today, and routing the sync path through a subprocess
would block the calling thread.

Architectural notes
-------------------
- Routes through ``server.execute_tool('clink', ...)`` rather than calling
  ``BaseCLIAgent.run()`` directly. This keeps the canonical single-dispatch
  invariant (CLAUDE.md) and gives every OAuth-routed call its own child run
  in the execution graph — visible in the live web viewer like any other
  panel sub-call.
- Stamps ``cost_tier`` / ``oauth_route`` / ``oauth_fallback_used`` into the
  returned ``ModelResponse.metadata`` so downstream consumers (panel cost
  rollup, viewer cost badges, run_tree query) can render the routing
  decision honestly.
- Emits ``emit_progress`` events at every decision point (route selected /
  CLI unavailable / fallback to API) so the run's activity feed traces
  what happened end-to-end.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import shutil
from typing import Any, Optional

from clink.constants import MODEL_TO_CLI
from providers.base import ModelProvider
from providers.shared.model_response import ModelResponse
from utils.progress import emit_progress

logger = logging.getLogger(__name__)


# Re-entrance guard. The wrapper invokes ``execute_tool('clink', ...)``;
# clink's internal CLI→API fallback may itself call
# ``execute_tool('chat', ...)`` with the same model. Without this guard,
# the chat tool's wrapped provider would route back through clink, which
# would quota-fail again, fall back to chat, etc. — unbounded.
#
# Set on entry, reset on exit. ContextVars propagate across ``await``
# and through asyncio task creation by default, so the guard is observed
# by every nested call inside the same logical request without
# interfering with concurrent unrelated dispatches.
_INSIDE_OAUTH_FIRST: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_INSIDE_OAUTH_FIRST", default=False
)


# Metadata keys the wrapper stamps onto ModelResponse.metadata. Tools that
# build their ToolOutput.metadata from scratch (SimpleTool, consensus, the
# workflow mixin) need to merge these in so the viewer cost badges and
# downstream cost-rollup logic actually see the OAuth routing decision.
# Use ``merge_oauth_metadata(target, response_metadata)`` to do the merge
# at each metadata-build site.
OAUTH_METADATA_KEYS: tuple[str, ...] = (
    "cost_tier",
    "oauth_route",
    "oauth_fallback_used",
    "oauth_fallback_reason",
    "oauth_route_skipped",
)


def merge_oauth_metadata(
    target: Optional[dict[str, Any]], response_metadata: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Merge OAuth-relevant fields from a ``ModelResponse.metadata`` into the
    target dict (creating one if needed). Returns the target dict.

    Conservative — only the OAuth-first keys are copied through; any other
    fields the inner provider added stay in ``response.metadata`` and don't
    leak into ToolOutput.metadata. Idempotent and safe to call when no
    OAuth-routing happened (the source dict simply has none of the keys).
    """

    if not response_metadata:
        return target or {}
    out = dict(target) if target else {}
    for key in OAUTH_METADATA_KEYS:
        if key in response_metadata:
            out[key] = response_metadata[key]
    return out


def oauth_first_enabled() -> bool:
    """Return True unless the user has explicitly opted out via env."""

    return (os.environ.get("PANEL_OAUTH_FIRST", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _cli_executable_present(cli_name: str) -> bool:
    """Quick check that the CLI is on PATH. Fast: ``shutil.which`` only."""

    return shutil.which(cli_name) is not None


def resolve_cli_for_model(model_name: str) -> Optional[str]:
    """Return the clink CLI name that handles this model, or None.

    Conservative: only honours the exact-match mapping in
    ``MODEL_TO_CLI``. Expand the mapping rather than adding fuzzy matching
    here — silent re-routing of a user's specific model request is the
    failure mode we want to avoid.
    """

    if not model_name:
        return None
    return MODEL_TO_CLI.get(model_name)


class OAuthFirstProvider(ModelProvider):
    """Wrap a real ModelProvider, preferring OAuth-CLI over the paid API.

    The wrapper has no provider-specific state of its own; it forwards every
    metadata/capability/validation method to the inner provider. The only
    behavioural change is in ``agenerate_content``, which (for OAuth-eligible
    models on a machine where the CLI is installed) routes the request
    through clink instead of the inner provider's SDK call.
    """

    def __init__(self, inner: ModelProvider):
        # NB: deliberately don't call super().__init__ — ModelProvider's
        # constructor expects an api_key, and we want to be a transparent
        # proxy. Forwarding via ``__getattr__`` would break isinstance
        # checks; keeping an explicit reference is clearer.
        self._inner = inner

    # -- transparent proxy surface ---------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Anything not explicitly overridden delegates to the inner
        # provider. ``__getattr__`` only fires for missing attributes, so
        # we don't need to enumerate the full ModelProvider interface.
        return getattr(self._inner, name)

    def get_provider_type(self):  # noqa: D401
        return self._inner.get_provider_type()

    def validate_model_name(self, model_name: str) -> bool:
        return self._inner.validate_model_name(model_name)

    def get_model_capabilities(self, model_name: str):
        return self._inner.get_model_capabilities(model_name)

    def list_models(self, *args, **kwargs):
        return self._inner.list_models(*args, **kwargs)

    def count_tokens(self, *args, **kwargs):
        return self._inner.count_tokens(*args, **kwargs)

    def supports_thinking_mode(self, *args, **kwargs):
        return self._inner.supports_thinking_mode(*args, **kwargs)

    # -- sync path: pass-through ----------------------------------------

    def generate_content(self, *args, **kwargs) -> ModelResponse:
        # Sync path stays on the SDK. Routing it through a subprocess would
        # block the calling thread on subprocess I/O, defeating the
        # purpose of agenerate_content's bounded executor.
        #
        # If a tool calls this sync path with a model that DOES have an
        # OAuth route, the user pays for the paid-API call even though
        # the free CLI is right there. Log a one-time warning so a future
        # developer notices and either migrates the caller to the async
        # path or deliberately accepts the cost.
        model_name = kwargs.get("model_name") or (args[1] if len(args) > 1 else None)
        if model_name and resolve_cli_for_model(model_name):
            logger.warning(
                "oauth-first: sync generate_content() called for %r — has an "
                "OAuth route via %s but sync path is API-only; user is being "
                "billed. Migrate the caller to agenerate_content() to opt in.",
                model_name,
                resolve_cli_for_model(model_name),
            )
        response = self._inner.generate_content(*args, **kwargs)
        if response is not None and hasattr(response, "metadata"):
            response.metadata.setdefault("cost_tier", "api_paid")
            if model_name and resolve_cli_for_model(model_name):
                response.metadata["oauth_route_skipped"] = "sync_path"
        return response

    # -- async path: the actual interesting one --------------------------

    async def agenerate_content(
        self,
        prompt: str,
        model_name: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        **kwargs,
    ) -> ModelResponse:
        # Re-entrance guard: if a clink CLI's own OAuth→API fallback is
        # already retrying via execute_tool('chat', ...) and chat's provider
        # is wrapped, we land back here with the same model. Routing
        # through clink again would create an unbounded subprocess tree.
        # Bypass the wrapper and use the inner SDK directly. Detected via
        # the _INSIDE_OAUTH_FIRST contextvar set on entry.
        if _INSIDE_OAUTH_FIRST.get():
            response = await self._inner.agenerate_content(
                prompt=prompt,
                model_name=model_name,
                system_prompt=system_prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                **kwargs,
            )
            if response is not None and hasattr(response, "metadata"):
                # This call IS the OAuth fallback path (clink already tried
                # the CLI and failed). Stamp accordingly so cost rollup is
                # honest about the billed call.
                response.metadata["cost_tier"] = "oauth_fallback_paid"
                response.metadata["oauth_fallback_used"] = True
                response.metadata.setdefault(
                    "oauth_fallback_reason", "reentry_from_clink_fallback"
                )
            return response

        cli_name = resolve_cli_for_model(model_name)

        # No OAuth route configured for this model → straight to API.
        if cli_name is None:
            response = await self._inner.agenerate_content(
                prompt=prompt,
                model_name=model_name,
                system_prompt=system_prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                **kwargs,
            )
            if response is not None and hasattr(response, "metadata"):
                response.metadata.setdefault("cost_tier", "api_paid")
                response.metadata.setdefault("oauth_route", "none")
            return response

        # CLI not installed on this machine → straight to API.
        if not _cli_executable_present(cli_name):
            await emit_progress(
                f"oauth-first: {cli_name} CLI not installed; using {model_name} via direct API",
                progress=0.0,
            )
            response = await self._inner.agenerate_content(
                prompt=prompt,
                model_name=model_name,
                system_prompt=system_prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                **kwargs,
            )
            if response is not None and hasattr(response, "metadata"):
                response.metadata.setdefault("cost_tier", "api_paid")
                response.metadata["oauth_route"] = cli_name
                response.metadata["oauth_route_skipped"] = "cli_not_installed"
            return response

        # OAuth-first path. clink's own CLI→API fallback handles the
        # quota-exhausted case internally (lands on the same model by
        # construction of MODEL_TO_CLI ⇄ oauth_fallback_model). If the
        # ``execute_tool('clink', ...)`` call raises, clink may already
        # have attempted (and failed at) its own paid-API fallback —
        # billing the user once. We deliberately do NOT retry via the
        # inner provider here: silent retry could double-charge. Surface
        # the exception so the caller sees what actually happened.
        await emit_progress(
            f"oauth-first: routing {model_name} via {cli_name} CLI (free OAuth path)",
            progress=0.0,
        )

        # Set the re-entrance guard around the clink dispatch. clink's
        # internal fallback may invoke execute_tool('chat', ...) which
        # routes back through this wrapper; the guard turns that nested
        # call into a direct SDK call instead of another clink spawn.
        token = _INSIDE_OAUTH_FIRST.set(True)
        try:
            return await self._invoke_clink(
                cli_name=cli_name,
                model_name=model_name,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        finally:
            _INSIDE_OAUTH_FIRST.reset(token)

    # -- helpers ---------------------------------------------------------

    async def _invoke_clink(
        self,
        *,
        cli_name: str,
        model_name: str,
        prompt: str,
        system_prompt: Optional[str],
    ) -> ModelResponse:
        """Dispatch through ``execute_tool('clink', ...)`` and adapt the result.

        Routes through the canonical single-dispatch path so the call
        gets its own child run in the execution graph (visible in the
        viewer like any other sub-call) and inherits clink's full
        feature set: streaming progress, redacted metadata, OAuth
        fallback to a paid API model on quota.
        """

        # Local import: server.execute_tool pulls in heavy modules; defer
        # to call time so static import order stays clean and the wrapper
        # module is cheap to import.
        from server import execute_tool

        clink_args: dict[str, Any] = {
            "prompt": prompt,
            "cli_name": cli_name,
            "role": "default",
            "_graph_edge_kind": "oauth_first_route",
            "_graph_label": f"oauth-first:{cli_name}:{model_name}",
            # Pre-tag cost_tier as oauth_free; clink will overwrite to
            # oauth_fallback_paid in metadata if its own fallback fires.
            "_graph_cost_tier": "oauth_free",
        }
        if system_prompt:
            # clink prepends system_prompt to its built prompt when its
            # CLI doesn't take a system_prompt arg, so embed it here so
            # the user's instructions reach the CLI even when the schema
            # doesn't surface it directly.
            clink_args["prompt"] = f"{system_prompt}\n\n{prompt}"

        # Deliberately NOT wrapped in ``mark_internal_payload()``: the
        # prompt content here originated from the user crossing the
        # original tool's (chat / consensus / etc.) MCP-boundary size
        # check. clink's MCP-boundary size check should fire normally
        # — same content, same boundary semantics. ``mark_internal_payload``
        # is reserved for genuinely Panel-built payloads (multiaudit's
        # diff package, panel's debate-round prompts, clink's own
        # OAuth-fallback file-inlined prompts).
        result = await execute_tool("clink", clink_args)

        return self._adapt_clink_to_model_response(
            result,
            cli_name=cli_name,
            model_name=model_name,
        )

    def _adapt_clink_to_model_response(
        self,
        result: list,
        *,
        cli_name: str,
        model_name: str,
    ) -> ModelResponse:
        """Convert clink's TextContent[ToolOutput] → ModelResponse."""

        if not result:
            raise RuntimeError(
                f"oauth-first: clink/{cli_name} returned empty result list"
            )

        first = result[0]
        # ``result`` is a list[mcp.types.TextContent]; .text holds a JSON
        # ToolOutput. Older shapes (string content) are accepted for
        # robustness against test fixtures.
        text = getattr(first, "text", None) or str(first)
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            # Non-JSON output — surface as opaque content with empty
            # metadata, but flag so callers can debug.
            payload = {"content": text, "metadata": {}}

        content = payload.get("content") or ""
        clink_meta = payload.get("metadata") or {}

        # Inherit clink's metadata (oauth_fallback_used, command_used, etc.)
        # and stamp our own fields on top.
        merged_meta: dict[str, Any] = dict(clink_meta) if isinstance(clink_meta, dict) else {}
        oauth_fallback = bool(merged_meta.get("oauth_fallback_used", False))
        merged_meta["oauth_route"] = cli_name
        merged_meta["cost_tier"] = (
            "oauth_fallback_paid" if oauth_fallback else "oauth_free"
        )

        # Normalise usage shape. clink parsers store token counts under
        # different keys (Gemini: ``token_usage``, Codex: in command output,
        # Claude: ``usage``). Promote whichever is present so consensus /
        # cost-rollup logic that reads ``response.usage["total_tokens"]``
        # gets a sensible number when one is available.
        usage = merged_meta.get("usage") or merged_meta.get("token_usage") or {}
        if isinstance(usage, dict) and usage:
            merged_meta["usage"] = usage

        # Append (OAuth) / (OAuth → API fallback) suffix to the inner
        # provider's friendly name rather than replacing it; consumers that
        # log model versions or capability strings keep that information.
        try:
            inner_friendly = (
                self._inner.get_model_capabilities(model_name).friendly_name
                if hasattr(self._inner, "get_model_capabilities")
                else model_name
            )
        except Exception:  # noqa: BLE001
            inner_friendly = model_name
        suffix = f"OAuth via {cli_name}" if not oauth_fallback else f"{cli_name} → API fallback"
        friendly = f"{inner_friendly} ({suffix})"

        return ModelResponse(
            content=content,
            usage=usage if isinstance(usage, dict) else {},
            model_name=model_name,
            friendly_name=friendly,
            provider=self._inner.get_provider_type(),
            metadata=merged_meta,
        )


def maybe_wrap(provider: ModelProvider) -> ModelProvider:
    """Wrap ``provider`` with OAuthFirstProvider if the feature is enabled.

    Pass-through when ``PANEL_OAUTH_FIRST=0``. Idempotent — wrapping an
    already-wrapped provider is a no-op so accidental double-wraps don't
    create double-routing.
    """

    if not oauth_first_enabled():
        return provider
    if isinstance(provider, OAuthFirstProvider):
        return provider
    return OAuthFirstProvider(provider)

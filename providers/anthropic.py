"""Anthropic Claude provider — first-party SDK integration.

Mirrors providers/gemini.py in shape (sync ``generate_content``, retry
loop, registry-backed capabilities) but adapts to the Anthropic SDK's
``client.messages.create()`` surface. Used by:

  - The chat / consensus / panel tools when a caller names a Claude model
    explicitly (e.g. ``model="claude-opus-4-8"``).
  - The clink ``claude`` agent's OAuth-to-API fallback (``oauth_fallback_model``
    in ``clink/constants.py``) — when ``claude`` CLI hits its quota the
    panel falls back to this provider against ``ANTHROPIC_API_KEY``.

The Anthropic API is NOT OpenAI-compatible. We do not subclass
``OpenAICompatibleProvider``; we use the official ``anthropic`` SDK
directly, the same way ``providers/gemini.py`` uses ``google-genai``.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import TYPE_CHECKING, Any, ClassVar, Optional

if TYPE_CHECKING:
    from tools.models import ToolModelCategory

# Soft import. Configure_providers() only registers AnthropicModelProvider
# when ANTHROPIC_API_KEY is set, but the *module* still loads on every Panel
# boot (server.py imports it inside configure_providers, which still
# executes module-level code). A missing `anthropic` SDK on a machine that
# only uses other providers would otherwise crash the entire MCP server.
# Raise the clear error at provider instantiation instead.
try:
    import anthropic  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — exercised only on misconfigured installs
    anthropic = None  # type: ignore[assignment]

from utils.env import get_env
from utils.image_utils import validate_image

from .base import ModelProvider
from .registries.anthropic import AnthropicModelRegistry
from .registry_provider_mixin import RegistryBackedProviderMixin
from .shared import ModelResponse, ProviderType

logger = logging.getLogger(__name__)


class AnthropicModelProvider(RegistryBackedProviderMixin, ModelProvider):
    """First-party Anthropic Claude integration via the official SDK.

    Capability metadata is loaded from ``conf/anthropic_models.json`` (see
    ``AnthropicModelRegistry``). The provider exposes thinking-mode budgets
    using the same percentage scheme as Gemini so callers can pass a unified
    ``thinking_mode`` arg across providers.
    """

    REGISTRY_CLASS = AnthropicModelRegistry
    MODEL_CAPABILITIES: ClassVar[dict] = {}
    FRIENDLY_NAME = "Claude"

    # Same percentage scheme as the Gemini provider — keeps the cross-provider
    # API identical so a tool can pass the same thinking_mode regardless of
    # which model it lands on.
    # Legacy budget percentages — kept for now to gate "is thinking on at all"
    # but the Anthropic API for Opus 4.8 / Sonnet 5 no longer accepts the
    # old `thinking.type=enabled, budget_tokens=N` schema. The provider now
    # sends `thinking.type=adaptive` plus `output_config.effort`, mapped from
    # Panel's thinking_mode via THINKING_EFFORT below.
    THINKING_BUDGETS = {
        "minimal": 0.005,
        "low": 0.08,
        "medium": 0.33,
        "high": 0.67,
        "max": 1.0,
    }

    # Panel thinking_mode → Anthropic output_config.effort. The new schema
    # accepts low/medium/high; "minimal" maps to low (effort=none disables
    # thinking entirely, but Panel's "minimal" is conceptually still on),
    # "max" maps to high (no "max" effort in the API).
    THINKING_EFFORT = {
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "max": "high",
    }

    # Required by the Anthropic API. Overridden per-call when the caller
    # passes max_output_tokens; otherwise we fall back to the model's
    # registered max_output_tokens or this default.
    DEFAULT_MAX_TOKENS = 4096

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        if anthropic is None:
            raise RuntimeError(
                "anthropic SDK not installed but ANTHROPIC_API_KEY is set. "
                "Install with `pip install anthropic` or remove the key."
            )
        self._ensure_registry()
        super().__init__(api_key, **kwargs)
        self._client: Optional[anthropic.Anthropic] = None
        # Lock the lazy `client` init so concurrent panel fan-out doesn't
        # construct duplicate Anthropic SDK clients (each opens its own
        # HTTP connection pool). Same anti-pattern providers/base.py
        # already fixes for the executor; audit-flagged here.
        self._client_lock = threading.Lock()
        self._timeout_override = self._resolve_http_timeout()
        self._invalidate_capability_cache()

    def _resolve_http_timeout(self) -> Optional[float]:
        """Honour the same custom-timeout env vars as gemini / openai.

        Returns the largest configured value across CUSTOM_*_TIMEOUT envs,
        or None when no override is set (the SDK's defaults apply).
        """
        timeouts: list[float] = []
        for env_var in (
            "CUSTOM_CONNECT_TIMEOUT",
            "CUSTOM_READ_TIMEOUT",
            "CUSTOM_WRITE_TIMEOUT",
            "CUSTOM_POOL_TIMEOUT",
        ):
            raw = get_env(env_var)
            if raw:
                try:
                    timeouts.append(float(raw))
                except (TypeError, ValueError):
                    logger.warning("Invalid %s value '%s'; ignoring.", env_var, raw)
        return max(timeouts) if timeouts else None

    # ------------------------------------------------------------------
    # Provider identity
    # ------------------------------------------------------------------

    def get_provider_type(self) -> ProviderType:
        return ProviderType.ANTHROPIC

    def _raise_unsupported_model(self, model_name: str) -> None:
        raise ValueError(f"Unsupported Anthropic model: {model_name}")

    # ------------------------------------------------------------------
    # Lazy client
    # ------------------------------------------------------------------

    @property
    def client(self) -> anthropic.Anthropic:
        # Double-checked lock: the unlocked first read short-circuits the
        # common (already-initialised) case; the lock guards the actual
        # construction so concurrent first calls don't build duplicates.
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    kwargs: dict[str, Any] = {"api_key": self.api_key}
                    if self._timeout_override is not None:
                        kwargs["timeout"] = self._timeout_override
                    self._client = anthropic.Anthropic(**kwargs)
        return self._client

    # ------------------------------------------------------------------
    # generate_content (sync; agenerate_content wrapper inherited from base)
    # ------------------------------------------------------------------

    def generate_content(
        self,
        prompt: str,
        model_name: str,
        system_prompt: Optional[str] = None,
        temperature: float = 1.0,
        max_output_tokens: Optional[int] = None,
        thinking_mode: str = "medium",
        images: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Send the prompt to Claude and return a ``ModelResponse``.

        The Anthropic API takes ``messages=[{role,content}]`` plus an optional
        ``system=`` param. Images attach as message content blocks (base64
        data URIs preferred).
        """
        self.validate_parameters(model_name, temperature)
        capabilities = self.get_capabilities(model_name)
        capability_map = self.get_all_model_capabilities()
        resolved = self._resolve_model_name(model_name)

        # Build the user message. Text is always present; images become
        # additional content blocks if the model supports them.
        content_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if images and capabilities.supports_images:
            for image_path in images:
                block = self._image_block(image_path)
                if block is not None:
                    content_blocks.append(block)
        elif images and not capabilities.supports_images:
            logger.warning(
                "Model %s does not support images, ignoring %d image(s)",
                resolved,
                len(images),
            )

        messages = [{"role": "user", "content": content_blocks}]

        # max_tokens is REQUIRED by the Anthropic API. Prefer the caller's
        # explicit value, then the registry's default, then the SDK default.
        model_cfg = capability_map.get(resolved)
        max_tokens = (
            max_output_tokens
            or (model_cfg.max_output_tokens if model_cfg else None)
            or self.DEFAULT_MAX_TOKENS
        )

        # We use ``client.messages.stream(...)`` for ALL Anthropic calls so
        # the registry-advertised max_output_tokens (e.g. 65k for Opus, 32k
        # for Sonnet) is actually usable. Non-streaming + extended thinking
        # would otherwise error at ~21,333 with `stream=True is required`,
        # silently capping output for any large codegen request. Streaming
        # also lets us pump per-chunk progress events to the live viewer
        # (see ``_attempt`` below).
        request_kwargs: dict[str, Any] = {
            "model": resolved,
            "max_tokens": int(max_tokens),
            "messages": messages,
        }
        if system_prompt:
            request_kwargs["system"] = system_prompt

        # Anthropic's current API (Opus 4.8 / Sonnet 5) replaced the old
        # `thinking.type=enabled, budget_tokens=N` schema with adaptive
        # thinking + output_config.effort. The old schema returns 400 with
        # 'Use "thinking.type.adaptive" and "output_config.effort" to
        # control thinking behavior'.
        #
        # We send `thinking.type=adaptive` and let Anthropic decide the
        # actual budget; Panel's thinking_mode (minimal/low/medium/high/max)
        # maps to output_config.effort via THINKING_EFFORT.
        #
        # Temperature is still mutually exclusive with thinking on
        # Anthropic — attach exactly one. Audit-flagged: the previous code
        # attached both unconditionally and every thinking-eligible call
        # returned 400.
        thinking_enabled = (
            capabilities.supports_extended_thinking
            and thinking_mode in self.THINKING_EFFORT
            and model_cfg is not None
            and model_cfg.max_thinking_tokens > 0
        )
        effective_thinking = thinking_mode if thinking_enabled else None

        if thinking_enabled:
            request_kwargs["thinking"] = {"type": "adaptive"}
            request_kwargs["output_config"] = {"effort": self.THINKING_EFFORT[thinking_mode]}
        elif capabilities.supports_temperature:
            # Only pass temperature when thinking is OFF — Anthropic
            # forbids the combination.
            request_kwargs["temperature"] = temperature

        attempt_counter = {"value": 0}

        def _attempt() -> ModelResponse:
            attempt_counter["value"] += 1
            # Streaming path: accumulate text deltas and ask the SDK for the
            # final assembled message (carries usage + stop_reason). The
            # shared StreamProgressEmitter pushes the actual accumulating
            # content to the execution graph so the viewer renders the
            # model writing in real time, not just chunk counters. Run_id
            # is captured eagerly via the ContextVar (now propagated into
            # the worker thread by providers/base.py).
            from utils.stream_progress import make_emitter
            emitter = make_emitter(label=f"claude/{resolved}")
            with self.client.messages.stream(**request_kwargs) as stream:
                for piece in stream.text_stream:
                    if piece:
                        emitter.feed(piece)
                emitter.finalize()
                response = stream.get_final_message()
            text = self._extract_text(response)
            usage = self._extract_usage(response)
            return ModelResponse(
                content=text,
                usage=usage,
                model_name=resolved,
                friendly_name=self.FRIENDLY_NAME,
                provider=ProviderType.ANTHROPIC,
                metadata={
                    "thinking_mode": effective_thinking,
                    "stop_reason": getattr(response, "stop_reason", None),
                    "anthropic_id": getattr(response, "id", None),
                    "max_tokens_requested": int(max_tokens),
                    "streamed": True,
                },
            )

        try:
            return self._run_with_retries(
                operation=_attempt,
                max_attempts=4,
                delays=[1, 3, 5, 8],
                log_prefix=f"Anthropic API ({resolved})",
            )
        except Exception as exc:
            attempts = max(attempt_counter["value"], 1)
            raise RuntimeError(
                f"Anthropic API error for model {resolved} after {attempts} "
                f"attempt{'s' if attempts > 1 else ''}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Helpers — error classification, response shaping, image encoding
    # ------------------------------------------------------------------

    def _is_error_retryable(self, error: Exception) -> bool:
        """Decide whether to retry. Uses anthropic SDK exception types when
        available; falls back to substring sniffing for raw network errors."""
        # SDK exception classes — preferred path.
        retryable_classes = (
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        )
        if isinstance(error, retryable_classes):
            return True
        # Some SDK errors carry a status code we can read.
        status = getattr(error, "status_code", None)
        if status in {408, 425, 429, 500, 502, 503, 504}:
            return True
        # Substring fallback for wrapped/unknown errors.
        s = str(error).lower()
        for kw in ("timeout", "connection", "temporary", "unavailable", "retry", "ssl"):
            if kw in s:
                return True
        return False

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Pull text from the message's content block list.

        Anthropic returns ``response.content`` as a list of blocks. Text
        blocks have ``type='text'`` and a ``text`` attr; thinking blocks
        have ``type='thinking'`` and we deliberately exclude them from the
        user-facing content (they're metadata, not the answer)."""
        content = getattr(response, "content", None)
        if not content:
            return ""
        chunks: list[str] = []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
        return "".join(chunks)

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int]:
        usage_obj = getattr(response, "usage", None)
        if usage_obj is None:
            return {}
        out: dict[str, int] = {}
        in_tokens = getattr(usage_obj, "input_tokens", None)
        out_tokens = getattr(usage_obj, "output_tokens", None)
        if in_tokens is not None:
            out["input_tokens"] = int(in_tokens)
        if out_tokens is not None:
            out["output_tokens"] = int(out_tokens)
        if "input_tokens" in out and "output_tokens" in out:
            out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
        return out

    @staticmethod
    def _image_block(image_path: str) -> Optional[dict[str, Any]]:
        """Encode an image into Anthropic's content block shape.

        Anthropic uses base64 data with an explicit media_type. We always
        re-encode the validated bytes from `validate_image()` rather than
        passing through the caller's data: URL — audit-flagged: the
        previous code used a raw `split(",", 1)` on the data URL which
        bypassed validation (size cap, mime sniffing, payload integrity).
        """
        try:
            image_bytes, mime_type = validate_image(image_path)
            b64 = base64.b64encode(image_bytes).decode("ascii")
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": b64},
            }
        except ValueError as exc:
            logger.warning("Anthropic image rejected: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("Anthropic image processing failed for %s: %s", image_path, exc)
            return None

    # ------------------------------------------------------------------
    # Auto-mode preference matrix
    # ------------------------------------------------------------------

    def get_preferred_model(
        self, category: "ToolModelCategory", allowed_models: list[str]
    ) -> Optional[str]:
        from tools.models import ToolModelCategory

        if not allowed_models:
            return None

        def first(candidates: list[str]) -> Optional[str]:
            for c in candidates:
                if c in allowed_models:
                    return c
            return None

        if category == ToolModelCategory.EXTENDED_REASONING:
            picked = first(["claude-opus-4-8", "claude-sonnet-5"])
            return picked or allowed_models[0]
        if category == ToolModelCategory.FAST_RESPONSE:
            # Same invariant as the openai provider's FAST_RESPONSE: never
            # serve the flagship in this tier unless nothing else exists.
            picked = first(["claude-haiku-4-5-20251001", "claude-sonnet-5"])
            if picked:
                return picked
            non_flagship = [m for m in allowed_models if m != "claude-opus-4-8"]
            if non_flagship:
                return non_flagship[0]
            logger.warning(
                "Anthropic FAST_RESPONSE: allowlist contains only the flagship; "
                "returning claude-opus-4-8 reluctantly."
            )
            return allowed_models[0]
        # BALANCED / default
        picked = first(["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5-20251001"])
        return picked or allowed_models[0]


# Load registry data at import time so dependent code can reuse it.
AnthropicModelProvider._ensure_registry()

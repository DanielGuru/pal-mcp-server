"""Anthropic Claude provider — first-party SDK integration.

Mirrors providers/gemini.py in shape (sync ``generate_content``, retry
loop, registry-backed capabilities) but adapts to the Anthropic SDK's
``client.messages.create()`` surface. Used by:

  - The chat / consensus / panel tools when a caller names a Claude model
    explicitly (e.g. ``model="claude-opus-4-7"``).
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
from typing import TYPE_CHECKING, Any, ClassVar, Optional

if TYPE_CHECKING:
    from tools.models import ToolModelCategory

import anthropic

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
    THINKING_BUDGETS = {
        "minimal": 0.005,
        "low": 0.08,
        "medium": 0.33,
        "high": 0.67,
        "max": 1.0,
    }

    # Required by the Anthropic API. Overridden per-call when the caller
    # passes max_output_tokens; otherwise we fall back to the model's
    # registered max_output_tokens or this default.
    DEFAULT_MAX_TOKENS = 4096

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        self._ensure_registry()
        super().__init__(api_key, **kwargs)
        self._client: Optional[anthropic.Anthropic] = None
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

        request_kwargs: dict[str, Any] = {
            "model": resolved,
            "max_tokens": int(max_tokens),
            "messages": messages,
        }
        if system_prompt:
            request_kwargs["system"] = system_prompt
        # The API accepts temperature 0–1; we don't pass when the model
        # forbids it (none currently in the trimmed Anthropic registry, but
        # keep symmetric with other providers).
        if capabilities.supports_temperature:
            request_kwargs["temperature"] = temperature

        # Thinking budget: only attach when the model supports extended
        # thinking and the caller asked for a non-trivial mode.
        effective_thinking = thinking_mode if capabilities.supports_extended_thinking else None
        if (
            capabilities.supports_extended_thinking
            and thinking_mode in self.THINKING_BUDGETS
            and model_cfg is not None
            and model_cfg.max_thinking_tokens > 0
        ):
            budget = int(model_cfg.max_thinking_tokens * self.THINKING_BUDGETS[thinking_mode])
            # Anthropic requires the budget below max_tokens; clamp.
            budget = max(1024, min(budget, request_kwargs["max_tokens"] - 1))
            request_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

        attempt_counter = {"value": 0}

        def _attempt() -> ModelResponse:
            attempt_counter["value"] += 1
            response = self.client.messages.create(**request_kwargs)
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

        Anthropic uses base64 data with an explicit media_type. Both file
        paths and data: URLs are accepted; bad images are skipped with a
        warning so a single bad image doesn't blow up the whole call.
        """
        try:
            image_bytes, mime_type = validate_image(image_path)
            if image_path.startswith("data:"):
                _, data = image_path.split(",", 1)
                b64 = data
            else:
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
            picked = first(["claude-opus-4-7", "claude-sonnet-4-6"])
            return picked or allowed_models[0]
        if category == ToolModelCategory.FAST_RESPONSE:
            # Same invariant as the openai provider's FAST_RESPONSE: never
            # serve the flagship in this tier unless nothing else exists.
            picked = first(["claude-haiku-4-5-20251001", "claude-sonnet-4-6"])
            if picked:
                return picked
            non_flagship = [m for m in allowed_models if m != "claude-opus-4-7"]
            if non_flagship:
                return non_flagship[0]
            logger.warning(
                "Anthropic FAST_RESPONSE: allowlist contains only the flagship; "
                "returning claude-opus-4-7 reluctantly."
            )
            return allowed_models[0]
        # BALANCED / default
        picked = first(["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"])
        return picked or allowed_models[0]


# Load registry data at import time so dependent code can reuse it.
AnthropicModelProvider._ensure_registry()

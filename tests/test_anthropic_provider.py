"""Direct unit coverage for providers/anthropic.py.

Audit-flagged regressions tested here:
  - temperature MUST NOT be sent when extended thinking is enabled
    (Anthropic API rejects the combination).
  - thinking-budget clamp must satisfy `1024 <= budget < max_tokens`;
    when the constraint is unsatisfiable (max_tokens<=1024) thinking
    is disabled entirely.
  - max_tokens passes through unchanged — the registry-advertised cap
    (e.g. 65k Opus) is usable because the provider streams responses.
  - image data: URLs must NOT bypass validate_image()'s decoded bytes.
  - Lazy client construction is lock-protected so concurrent panel
    fan-out doesn't build duplicate Anthropic SDK clients.
  - clink claude OAuth fallback resolves to a real Anthropic SKU.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from providers.anthropic import AnthropicModelProvider
from providers.shared import ProviderType


class AnthropicRequestShapeTest(unittest.TestCase):
    """Exercise generate_content's request kwargs without hitting the API.

    We patch the SDK client and inspect what request_kwargs we built.
    """

    def _provider(self) -> AnthropicModelProvider:
        return AnthropicModelProvider("test-key")

    def _fake_response(self, text: str = "ok"):
        resp = MagicMock()
        resp.content = [MagicMock(type="text", text=text)]
        usage = MagicMock(input_tokens=10, output_tokens=20)
        resp.usage = usage
        resp.stop_reason = "end_turn"
        resp.id = "msg_test_123"
        resp.model = "claude-opus-4-7"
        return resp

    def _capture_call(self, **gen_kwargs) -> dict:
        """Run generate_content with patched SDK; return the kwargs the
        provider passed to messages.stream()."""
        provider = self._provider()
        captured: dict = {}

        def fake_stream(**kwargs):
            captured.update(kwargs)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.text_stream = iter(["ok"])
            ctx.get_final_message = MagicMock(return_value=self._fake_response())
            return ctx

        with patch.object(provider, "_client", None):
            with patch("providers.anthropic.anthropic.Anthropic") as mock_ctor:
                mock_client = MagicMock()
                mock_client.messages.stream.side_effect = fake_stream
                mock_ctor.return_value = mock_client
                provider.generate_content(prompt="hi", model_name="opus", **gen_kwargs)
        return captured

    def test_temperature_dropped_when_thinking_enabled(self):
        """Audit-flagged blocker: Anthropic API rejects requests that
        combine `temperature` with `thinking`. We must send only one."""
        kwargs = self._capture_call(thinking_mode="medium", temperature=0.7)
        self.assertIn("thinking", kwargs)
        self.assertNotIn(
            "temperature",
            kwargs,
            "temperature must NOT be sent when thinking is enabled",
        )

    def test_temperature_passed_when_thinking_disabled(self):
        """When thinking_mode resolves to disabled (low/no-budget model
        or max_tokens guard), temperature comes through normally."""
        # haiku has supports_extended_thinking=false in the registry
        captured = self._capture_call_for_model(
            "haiku", temperature=0.5, thinking_mode="medium"
        )
        self.assertNotIn("thinking", captured)
        self.assertEqual(captured.get("temperature"), 0.5)

    def _capture_call_for_model(self, model_name: str, **gen_kwargs) -> dict:
        provider = self._provider()
        captured: dict = {}

        def fake_stream(**kw):
            captured.update(kw)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.text_stream = iter(["ok"])
            ctx.get_final_message = MagicMock(return_value=self._fake_response())
            return ctx

        with patch.object(provider, "_client", None), patch(
            "providers.anthropic.anthropic.Anthropic"
        ) as mock_ctor:
            mock_client = MagicMock()
            mock_client.messages.stream.side_effect = fake_stream
            mock_ctor.return_value = mock_client
            provider.generate_content(prompt="hi", model_name=model_name, **gen_kwargs)
        return captured

    def test_max_tokens_passes_through_with_streaming(self):
        """The registry advertises 65k for opus / 32k for sonnet. Previously
        non-streaming + thinking forced a 21k cap, silently truncating large
        codegen output. With messages.stream() the cap is gone — verify the
        registry's full max_output_tokens reaches the SDK unchanged."""
        kwargs = self._capture_call(thinking_mode="medium")
        # opus registry max_output_tokens is 65536
        self.assertGreater(
            kwargs["max_tokens"],
            21000,
            "with streaming the 21k non-streaming cap should be lifted",
        )

    def test_thinking_disabled_when_max_tokens_too_small(self):
        """Audit-flagged blocker: when caller passes max_output_tokens
        <= 1024 the budget invariant `1024 <= budget < max_tokens` is
        unsatisfiable. Disable thinking instead of sending an invalid
        request."""
        kwargs = self._capture_call(max_output_tokens=512, thinking_mode="medium")
        self.assertNotIn("thinking", kwargs)
        self.assertEqual(kwargs["max_tokens"], 512)

    def test_thinking_budget_below_max_tokens(self):
        kwargs = self._capture_call(thinking_mode="medium")
        if "thinking" in kwargs:
            budget = kwargs["thinking"]["budget_tokens"]
            self.assertGreaterEqual(budget, 1024)
            self.assertLess(budget, kwargs["max_tokens"])


class AnthropicImageEncodingTest(unittest.TestCase):
    """The image data-URL fast path must NOT bypass validate_image's
    decoded bytes — audit caught raw split() of caller-supplied URLs."""

    def test_data_url_uses_validated_bytes_not_raw_split(self):
        from providers import anthropic as anthropic_mod

        with patch.object(anthropic_mod, "validate_image") as mock_validate:
            mock_validate.return_value = (b"\x89PNG\r\n\x1a\n", "image/png")
            block = AnthropicModelProvider._image_block("data:image/png;base64,POISONED")
            self.assertIsNotNone(block)
            self.assertEqual(block["source"]["media_type"], "image/png")
            # The encoded data must come from validate_image's BYTES
            # (b"\x89PNG..."), NOT the caller-supplied "POISONED" tail.
            import base64
            expected = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
            self.assertEqual(block["source"]["data"], expected)
            self.assertNotEqual(block["source"]["data"], "POISONED")


class AnthropicLazyClientLockTest(unittest.TestCase):
    """Concurrent first-access on .client must build exactly ONE SDK
    client. Without the double-checked lock, panel fan-out could spawn
    multiple connection pools."""

    def test_concurrent_first_access_constructs_single_client(self):
        provider = AnthropicModelProvider("test-key")
        construct_count = {"value": 0}

        def fake_ctor(*args, **kwargs):
            construct_count["value"] += 1
            return MagicMock()

        # Patch the constructor and unleash N threads at the .client property.
        with patch("providers.anthropic.anthropic.Anthropic", side_effect=fake_ctor):
            barrier = threading.Barrier(8)

            def race():
                barrier.wait()
                _ = provider.client

            threads = [threading.Thread(target=race) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(
            construct_count["value"],
            1,
            "double-checked lock failed — multiple SDK clients constructed",
        )


class AnthropicRetryClassificationTest(unittest.TestCase):
    """SDK exception types should be classified as retryable correctly."""

    def test_known_retryable_sdk_exceptions(self):
        import anthropic as ant_sdk

        provider = AnthropicModelProvider("test-key")
        # Simulate exceptions WITHOUT actually constructing them (some
        # SDK exception ctors require real httpx requests). The classifier
        # uses isinstance, so create dummy instances.
        # APIConnectionError, APITimeoutError, RateLimitError, InternalServerError
        for cls in (
            ant_sdk.APIConnectionError,
            ant_sdk.APITimeoutError,
        ):
            try:
                exc = cls.__new__(cls)
                self.assertTrue(provider._is_error_retryable(exc), f"{cls.__name__} should retry")
            except Exception:
                # If the SDK ever changes the class hierarchy, surface
                # rather than silently pass.
                raise

    def test_status_code_retryable(self):
        provider = AnthropicModelProvider("test-key")
        exc = type("FakeApiErr", (Exception,), {"status_code": 503})("server unavailable")
        self.assertTrue(provider._is_error_retryable(exc))

    def test_substring_fallback(self):
        provider = AnthropicModelProvider("test-key")
        self.assertTrue(provider._is_error_retryable(Exception("connection timeout")))
        self.assertTrue(provider._is_error_retryable(Exception("temporary network issue")))
        self.assertFalse(provider._is_error_retryable(Exception("invalid_api_key")))


class ClinkClaudeFallbackTest(unittest.TestCase):
    """The clink claude agent must fall back to a real Anthropic SKU on
    quota exhaustion — and that SKU must NOT be the most expensive
    flagship by default (audit-flagged financial-DoS path)."""

    def test_fallback_resolves_to_anthropic_sku(self):
        from clink.constants import INTERNAL_DEFAULTS

        target = INTERNAL_DEFAULTS["claude"].oauth_fallback_model
        self.assertIsNotNone(target)
        provider = AnthropicModelProvider("test-key")
        caps = provider.get_capabilities(target)
        self.assertEqual(caps.provider, ProviderType.ANTHROPIC)
        self.assertEqual(caps.model_name, target)

    def test_fallback_default_is_not_flagship(self):
        """Default must be a cheaper SKU. Operators can override with
        PAL_CLAUDE_OAUTH_FALLBACK_MODEL=opus to opt back into Opus."""
        import os
        from importlib import reload

        # Ensure we test with no override env var set.
        prior = os.environ.pop("PAL_CLAUDE_OAUTH_FALLBACK_MODEL", None)
        try:
            from clink import constants as clink_constants
            reload(clink_constants)
            target = clink_constants.INTERNAL_DEFAULTS["claude"].oauth_fallback_model
            self.assertNotEqual(
                target,
                "claude-opus-4-7",
                "default OAuth fallback should NOT be the flagship — pick Sonnet or smaller",
            )
        finally:
            if prior is not None:
                os.environ["PAL_CLAUDE_OAUTH_FALLBACK_MODEL"] = prior


class AnthropicRestrictionWiringTest(unittest.TestCase):
    """ANTHROPIC_ALLOWED_MODELS must gate the new paid provider — audit
    flagged the env var as silently a no-op."""

    def test_restriction_service_knows_anthropic(self):
        from utils.model_restrictions import ModelRestrictionService

        self.assertIn(
            ProviderType.ANTHROPIC,
            ModelRestrictionService.ENV_VARS,
            "ANTHROPIC must be in the restriction service ENV_VARS map",
        )
        self.assertEqual(
            ModelRestrictionService.ENV_VARS[ProviderType.ANTHROPIC],
            "ANTHROPIC_ALLOWED_MODELS",
        )

    def test_anthropic_allowlist_enforced(self):
        import os
        from utils.model_restrictions import ModelRestrictionService

        prior = os.environ.pop("ANTHROPIC_ALLOWED_MODELS", None)
        os.environ["ANTHROPIC_ALLOWED_MODELS"] = "claude-sonnet-4-6"
        try:
            svc = ModelRestrictionService()
            self.assertTrue(svc.is_allowed(ProviderType.ANTHROPIC, "claude-sonnet-4-6"))
            self.assertFalse(svc.is_allowed(ProviderType.ANTHROPIC, "claude-opus-4-7"))
        finally:
            os.environ.pop("ANTHROPIC_ALLOWED_MODELS", None)
            if prior is not None:
                os.environ["ANTHROPIC_ALLOWED_MODELS"] = prior


if __name__ == "__main__":
    unittest.main()

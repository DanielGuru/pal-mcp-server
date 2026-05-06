"""Backfill coverage for behaviour the panel-audited test deletion exposed.

After the model registry was trimmed to current flagships, the upstream test
suite was deleted en masse because every test referenced removed model names
(grok-4, o3, o4-mini, gemini-2.5-flash) or used sync `Mock` objects that the
new async provider wrapper cannot await. The panel that audited that deletion
named four classes of regression worth keeping coverage on:

  1. Current-registry smoke — the six flagships still resolve and validate.
  2. Restriction service — alias/canonical resolution under
     ``*_ALLOWED_MODELS`` is symmetric (target permits alias, alias permits
     canonical, unrelated models denied).
  3. Auto-mode preference matrix — the in-fork preference lists in each
     provider point at models that actually exist in the trimmed registry.
  4. Async wrapper contract — ``agenerate_content`` *delegates* to sync
     ``generate_content`` in a thread; it does not implement cross-provider
     fallback. (Audit: gemini panelist demanded fallback testing; codex and
     the judge rejected this as overreach — the wrapper does not implement
     it. Test the actual contract instead.)
"""

from __future__ import annotations

import asyncio
import importlib
import os
import unittest
from unittest.mock import MagicMock, patch

from providers.anthropic import AnthropicModelProvider
from providers.gemini import GeminiModelProvider
from providers.openai import OpenAIModelProvider
from providers.registry import ModelProviderRegistry
from providers.shared import ProviderType
from providers.xai import XAIModelProvider
from tools.models import ToolModelCategory
from utils.model_restrictions import ModelRestrictionService


CURRENT_FLAGSHIPS = {
    "openai": ["gpt-5.5", "gpt-5.4", "gpt-5.1-codex"],
    "gemini": ["gemini-3.1-pro-preview"],
    "xai": ["grok-4.3", "grok-4-1-fast-reasoning"],
    "anthropic": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
}


class CurrentRegistrySmokeTest(unittest.TestCase):
    """Each flagship in CLAUDE.md must resolve via its provider."""

    def test_openai_flagships_resolve_canonical(self):
        provider = OpenAIModelProvider("test-key")
        for canonical in CURRENT_FLAGSHIPS["openai"]:
            caps = provider.get_capabilities(canonical)
            self.assertEqual(caps.model_name, canonical)

    def test_gemini_flagships_resolve_canonical(self):
        provider = GeminiModelProvider("test-key")
        for canonical in CURRENT_FLAGSHIPS["gemini"]:
            caps = provider.get_capabilities(canonical)
            self.assertEqual(caps.model_name, canonical)

    def test_xai_flagships_resolve_canonical(self):
        provider = XAIModelProvider("test-key")
        for canonical in CURRENT_FLAGSHIPS["xai"]:
            caps = provider.get_capabilities(canonical)
            self.assertEqual(caps.model_name, canonical)

    def test_anthropic_flagships_resolve_canonical(self):
        provider = AnthropicModelProvider("test-key")
        for canonical in CURRENT_FLAGSHIPS["anthropic"]:
            caps = provider.get_capabilities(canonical)
            self.assertEqual(caps.model_name, canonical)

    def test_anthropic_aliases_resolve(self):
        """opus / sonnet / haiku must resolve to their canonical SKUs."""
        provider = AnthropicModelProvider("test-key")
        self.assertEqual(provider.get_capabilities("opus").model_name, "claude-opus-4-7")
        self.assertEqual(provider.get_capabilities("sonnet").model_name, "claude-sonnet-4-6")
        self.assertEqual(provider.get_capabilities("haiku").model_name, "claude-haiku-4-5-20251001")

    def test_anthropic_fast_response_picks_haiku_not_flagship(self):
        """Mirror the OpenAI invariant: FAST_RESPONSE never returns the
        flagship when a cheaper SKU is available."""
        provider = AnthropicModelProvider("test-key")
        all_models = provider.list_models(respect_restrictions=False)
        picked = provider.get_preferred_model(
            ToolModelCategory.FAST_RESPONSE, all_models
        )
        self.assertNotEqual(picked, "claude-opus-4-7")
        self.assertEqual(picked, "claude-haiku-4-5-20251001")

    def test_anthropic_extended_reasoning_picks_opus(self):
        provider = AnthropicModelProvider("test-key")
        all_models = provider.list_models(respect_restrictions=False)
        picked = provider.get_preferred_model(
            ToolModelCategory.EXTENDED_REASONING, all_models
        )
        self.assertEqual(picked, "claude-opus-4-7")

    def test_clink_claude_oauth_fallback_resolves(self):
        """The clink ``claude`` agent's oauth_fallback_model must be a real
        model in the Anthropic registry — otherwise quota fallbacks crash."""
        from clink.constants import INTERNAL_DEFAULTS

        fallback = INTERNAL_DEFAULTS["claude"].oauth_fallback_model
        self.assertIsNotNone(fallback, "claude clink agent must have an OAuth fallback now that Anthropic provider exists")
        provider = AnthropicModelProvider("test-key")
        caps = provider.get_capabilities(fallback)
        self.assertEqual(caps.model_name, fallback)

    def test_xai_fallback_constant_exists_in_registry(self):
        """Regression: PRIMARY_MODEL and FALLBACK_MODEL must both be canonical
        entries in conf/xai_models.json. The audit caught the fork shipping
        FALLBACK_MODEL='grok-4' which had no registry entry."""
        provider = XAIModelProvider("test-key")
        for attr in ("PRIMARY_MODEL", "FALLBACK_MODEL"):
            value = getattr(provider, attr)
            caps = provider.get_capabilities(value)
            self.assertEqual(
                caps.model_name,
                value,
                f"XAIModelProvider.{attr}={value!r} must resolve in the registry",
            )


class RestrictionServiceAliasResolutionTest(unittest.TestCase):
    """The restriction service must resolve aliases ↔ targets symmetrically.

    Without this, OPENAI_ALLOWED_MODELS=gpt-5.5 would deny `gpt5` even though
    `gpt5` is documented to alias `gpt-5.5`. This is the regression the
    deleted test_alias_target_restrictions.py covered.
    """

    def setUp(self) -> None:
        # Make sure nothing leaks from prior tests / process env.
        for key in (
            "OPENAI_ALLOWED_MODELS",
            "GOOGLE_ALLOWED_MODELS",
            "XAI_ALLOWED_MODELS",
        ):
            os.environ.pop(key, None)
        ModelProviderRegistry.clear_cache()

    def tearDown(self) -> None:
        for key in (
            "OPENAI_ALLOWED_MODELS",
            "GOOGLE_ALLOWED_MODELS",
            "XAI_ALLOWED_MODELS",
        ):
            os.environ.pop(key, None)
        ModelProviderRegistry.clear_cache()

    def _service_with(self, **env: str) -> ModelRestrictionService:
        for k, v in env.items():
            os.environ[k] = v
        return ModelRestrictionService()

    def test_canonical_in_allowlist_permits_alias(self):
        svc = self._service_with(XAI_ALLOWED_MODELS="grok-4.3")
        # `grok` is a known alias for grok-4.3 in conf/xai_models.json.
        self.assertTrue(
            svc.is_allowed(ProviderType.XAI, "grok-4.3", original_name="grok")
        )

    def test_alias_in_allowlist_permits_canonical(self):
        # `pro` aliases gemini-3.1-pro-preview.
        svc = self._service_with(GOOGLE_ALLOWED_MODELS="pro")
        self.assertTrue(
            svc.is_allowed(
                ProviderType.GOOGLE,
                "gemini-3.1-pro-preview",
                original_name="gemini-3.1-pro-preview",
            )
        )

    def test_unrelated_current_model_denied(self):
        svc = self._service_with(OPENAI_ALLOWED_MODELS="gpt-5.4")
        self.assertFalse(svc.is_allowed(ProviderType.OPENAI, "gpt-5.5"))

    def test_no_restrictions_means_allow_all(self):
        svc = ModelRestrictionService()  # no env set
        self.assertTrue(svc.is_allowed(ProviderType.XAI, "grok-4.3"))
        self.assertTrue(svc.is_allowed(ProviderType.OPENAI, "gpt-5.5"))


class AutoModePreferenceMatrixTest(unittest.TestCase):
    """get_preferred_fallback_model must return a model that actually exists
    in the trimmed registry, for every (provider, category) combination.

    This catches drift where preference lists fall behind the registry.
    """

    @patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "test-openai",
            "GEMINI_API_KEY": "",
            "XAI_API_KEY": "",
        },
        clear=False,
    )
    def test_only_openai_returns_canonical_openai_model(self):
        ModelProviderRegistry.reset_for_testing()
        ModelProviderRegistry.register_provider(ProviderType.OPENAI, OpenAIModelProvider)
        for category in ToolModelCategory:
            with self.subTest(category=category):
                picked = ModelProviderRegistry.get_preferred_fallback_model(category)
                provider = OpenAIModelProvider("test")
                caps = provider.get_capabilities(picked)
                self.assertEqual(caps.provider, ProviderType.OPENAI)

    @patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "",
            "GEMINI_API_KEY": "",
            "XAI_API_KEY": "test-xai",
        },
        clear=False,
    )
    def test_only_xai_returns_canonical_xai_model(self):
        ModelProviderRegistry.reset_for_testing()
        ModelProviderRegistry.register_provider(ProviderType.XAI, XAIModelProvider)
        for category in ToolModelCategory:
            with self.subTest(category=category):
                picked = ModelProviderRegistry.get_preferred_fallback_model(category)
                provider = XAIModelProvider("test")
                caps = provider.get_capabilities(picked)
                self.assertEqual(caps.provider, ProviderType.XAI)

    @patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "",
            "GEMINI_API_KEY": "test-gemini",
            "XAI_API_KEY": "",
        },
        clear=False,
    )
    def test_only_gemini_returns_canonical_gemini_model(self):
        ModelProviderRegistry.reset_for_testing()
        ModelProviderRegistry.register_provider(ProviderType.GOOGLE, GeminiModelProvider)
        for category in ToolModelCategory:
            with self.subTest(category=category):
                picked = ModelProviderRegistry.get_preferred_fallback_model(category)
                provider = GeminiModelProvider("test")
                caps = provider.get_capabilities(picked)
                self.assertEqual(caps.provider, ProviderType.GOOGLE)

    @patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "test-openai", "GEMINI_API_KEY": "test-gemini"},
        clear=False,
    )
    def test_extended_reasoning_prefers_openai_flagship(self):
        """gpt-5.5 (flagship) must beat gpt-5.4 in EXTENDED_REASONING — this
        was the panel's specific drift finding at providers/openai.py:119."""
        ModelProviderRegistry.reset_for_testing()
        ModelProviderRegistry.register_provider(ProviderType.OPENAI, OpenAIModelProvider)
        ModelProviderRegistry.register_provider(ProviderType.GOOGLE, GeminiModelProvider)
        provider = OpenAIModelProvider("test")
        all_openai = provider.list_models(respect_restrictions=False)
        picked = provider.get_preferred_model(
            ToolModelCategory.EXTENDED_REASONING, all_openai
        )
        # 5.5 is the current flagship. If 5.4 sneaks back in front of 5.5,
        # the panel-flagged drift is back.
        if "gpt-5.5" in all_openai:
            self.assertEqual(picked, "gpt-5.5")


class AsyncWrapperDelegationTest(unittest.TestCase):
    """``agenerate_content`` must delegate to sync ``generate_content`` in a
    bounded thread — nothing more. The audit explicitly rejected
    cross-provider-fallback testing because the wrapper does not implement
    that behaviour. Test the actual contract instead.
    """

    def test_agenerate_delegates_to_sync_generate(self):
        from providers.base import ModelProvider

        # Build the absolute minimum concrete provider — only the methods
        # `agenerate_content` itself needs.
        class _StubProvider(ModelProvider):
            def __init__(self):
                self.calls: list[tuple[str, str]] = []

            FRIENDLY_NAME = "stub"

            def get_provider_type(self):  # pragma: no cover - identity
                return ProviderType.OPENAI

            def validate_model_name(self, model_name):
                return True

            def list_models(self, **_kw):
                return ["stub"]

            def get_capabilities(self, model_name):
                raise NotImplementedError

            def count_tokens(self, text, model_name):
                return len(text)

            def supports_thinking_mode(self, model_name):
                return False

            def generate_content(
                self,
                prompt,
                model_name,
                system_prompt=None,
                temperature=0.3,
                max_output_tokens=None,
                **kwargs,
            ):
                self.calls.append((prompt, model_name))
                # Returns a stub object the caller treats as ModelResponse.
                response = MagicMock()
                response.content = "stub-output"
                return response

        provider = _StubProvider()

        async def _run():
            return await provider.agenerate_content(
                prompt="hello", model_name="stub-model"
            )

        result = asyncio.run(_run())
        self.assertEqual(result.content, "stub-output")
        self.assertEqual(provider.calls, [("hello", "stub-model")])


if __name__ == "__main__":
    unittest.main()

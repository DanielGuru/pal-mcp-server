"""Base interfaces and common behaviour for model providers."""

import asyncio
import atexit
import contextvars
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from tools.models import ToolModelCategory

from .shared import ModelCapabilities, ModelResponse, ProviderType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Concurrency & resource bounds for direct-API calls.
#
# Why this exists: agenerate_content runs the sync provider SDK on a worker
# thread via asyncio.to_thread. asyncio cannot cancel a running thread —
# stdlib limitation. So if a request hangs (network glitch, provider stall)
# the thread keeps running until the SDK call returns or the process exits.
# Pre-bound, repeated cancels could exhaust the default executor and burn
# arbitrary money on stuck calls.
#
# Three layers of defence, from outermost to innermost:
#   1. _API_SEMAPHORE caps concurrent paid API calls. Defaults to 16; tune
#      via PANEL_MAX_CONCURRENT_API. Acquired *before* the to_thread dispatch.
#   2. _PROVIDER_EXECUTOR caps the worker-thread pool. Even if the
#      semaphore were lifted, threads can't grow past this. Defaults to 32;
#      tune via PANEL_MAX_PROVIDER_THREADS.
#   3. PANEL_API_TIMEOUT_S (default 1800) is forwarded to SDK clients so the
#      thread always self-terminates within bound — see openai_compatible.py.
#      Default matches the panel/multiaudit/bugfind panelist timeout so an
#      OAuth-fallback path (CLI quota exhausted → paid Anthropic API) doesn't
#      get killed by the SDK timeout while the panelist budget still has room.
#
# Lazy-init on first use so unit tests that don't import asyncio still work.
# ---------------------------------------------------------------------------

_PROVIDER_EXECUTOR: Optional[ThreadPoolExecutor] = None
_API_SEMAPHORE: Optional[asyncio.Semaphore] = None
# Locks guard the lazy-init double-check below. Without them, two concurrent
# first-burst callers race the `if X is None` check and both construct fresh
# executors / semaphores. Duplicate executors leak threads past
# PANEL_MAX_PROVIDER_THREADS; duplicate semaphores temporarily defeat the
# global API cap. Audit panel finding (Grok flagged, judge endorsed as the
# top first-burst production bug from commit 4979cf7).
_PROVIDER_EXECUTOR_LOCK = threading.Lock()
_API_SEMAPHORE_LOCK = threading.Lock()


def _get_provider_executor() -> ThreadPoolExecutor:
    global _PROVIDER_EXECUTOR
    # Double-checked locking: the fast-path is a single read for warm calls;
    # only the cold path takes the lock and re-checks under it.
    if _PROVIDER_EXECUTOR is None:
        with _PROVIDER_EXECUTOR_LOCK:
            if _PROVIDER_EXECUTOR is None:
                max_workers = int(os.environ.get("PANEL_MAX_PROVIDER_THREADS", "32"))
                _PROVIDER_EXECUTOR = ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="panel-provider",
                )
                logger.info("Provider thread pool initialised: max_workers=%s", max_workers)
    return _PROVIDER_EXECUTOR


def _get_api_semaphore() -> asyncio.Semaphore:
    global _API_SEMAPHORE
    # DO NOT REMOVE THE LOCK. Concurrent first-burst callers will otherwise
    # both pass the `if X is None` check and create duplicate semaphores;
    # whichever assigns last wins, the other is orphaned, briefly defeating
    # the global API cap. threading.Lock here is correct: it's held for
    # microseconds (constructor + assignment), only contended on the very
    # first call, and the fast-path is a single read so warm calls never
    # touch it. asyncio.Lock is wrong because this getter is called from
    # both async and sync paths (worker threads in the bounded executor).
    # See commit 015e462 for the audit-panel finding this prevents.
    if _API_SEMAPHORE is None:
        with _API_SEMAPHORE_LOCK:
            if _API_SEMAPHORE is None:
                cap = int(os.environ.get("PANEL_MAX_CONCURRENT_API", "16"))
                _API_SEMAPHORE = asyncio.Semaphore(cap)
                logger.info("Provider API semaphore initialised: cap=%s", cap)
    return _API_SEMAPHORE


def get_default_api_timeout() -> float:
    """Per-call SDK timeout, in seconds. Bounds the worker-thread lifetime."""
    return float(os.environ.get("PANEL_API_TIMEOUT_S", "1800"))


def _shutdown_provider_executor() -> None:
    """Tear down the worker pool at process exit so daemon threads don't leak
    across MCP load/unload cycles. atexit-registered."""
    global _PROVIDER_EXECUTOR
    with _PROVIDER_EXECUTOR_LOCK:
        if _PROVIDER_EXECUTOR is not None:
            try:
                _PROVIDER_EXECUTOR.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 — atexit must not raise
                pass
            _PROVIDER_EXECUTOR = None


atexit.register(_shutdown_provider_executor)


class ModelProvider(ABC):
    """Abstract base class for all model backends in the MCP server.

    Role
        Defines the interface every provider must implement so the registry,
        restriction service, and tools have a uniform surface for listing
        models, resolving aliases, and executing requests.

    Responsibilities
        * expose static capability metadata for each supported model via
          :class:`ModelCapabilities`
        * accept user prompts, forward them to the underlying SDK, and wrap
          responses in :class:`ModelResponse`
        * report tokenizer counts for budgeting and validation logic
        * advertise provider identity (``ProviderType``) so restriction
          policies can map environment configuration onto providers
        * validate whether a model name or alias is recognised by the provider

    Shared helpers like temperature validation, alias resolution, and
    restriction-aware ``list_models`` live here so concrete subclasses only
    need to supply their catalogue and wire up SDK-specific behaviour.
    """

    # All concrete providers must define their supported models
    MODEL_CAPABILITIES: dict[str, Any] = {}

    def __init__(self, api_key: str, **kwargs):
        """Initialize the provider with API key and optional configuration."""
        self.api_key = api_key
        self.config = kwargs
        self._sorted_capabilities_cache: Optional[list[tuple[str, ModelCapabilities]]] = None

    # ------------------------------------------------------------------
    # Provider identity & capability surface
    # ------------------------------------------------------------------
    @abstractmethod
    def get_provider_type(self) -> ProviderType:
        """Return the concrete provider identity."""

    def get_capabilities(self, model_name: str) -> ModelCapabilities:
        """Resolve capability metadata for a model name.

        This centralises the alias resolution → lookup → restriction check
        pipeline so providers only override the pieces they genuinely need to
        customise. Subclasses usually only override ``_lookup_capabilities`` to
        integrate a registry or dynamic source, or ``_finalise_capabilities`` to
        tweak the returned object.

        Args:
            model_name: Canonical model name or its alias
        """

        resolved_model_name = self._resolve_model_name(model_name)
        capabilities = self._lookup_capabilities(resolved_model_name, model_name)

        if capabilities is None:
            self._raise_unsupported_model(model_name)

        self._ensure_model_allowed(capabilities, resolved_model_name, model_name)
        return self._finalise_capabilities(capabilities, resolved_model_name, model_name)

    def get_all_model_capabilities(self) -> dict[str, ModelCapabilities]:
        """Return statically declared capabilities when available."""

        model_map = getattr(self, "MODEL_CAPABILITIES", None)
        if isinstance(model_map, dict) and model_map:
            return {k: v for k, v in model_map.items() if isinstance(v, ModelCapabilities)}
        return {}

    def get_capabilities_by_rank(self) -> list[tuple[str, ModelCapabilities]]:
        """Return model capabilities sorted by effective capability rank."""

        if self._sorted_capabilities_cache is not None:
            return list(self._sorted_capabilities_cache)

        model_configs = self.get_all_model_capabilities()
        if not model_configs:
            self._sorted_capabilities_cache = []
            return []

        items = list(model_configs.items())
        items.sort(key=lambda item: (-item[1].get_effective_capability_rank(), item[0]))
        self._sorted_capabilities_cache = items
        return list(items)

    def _invalidate_capability_cache(self) -> None:
        """Clear cached sorted capability data (call after dynamic updates)."""

        self._sorted_capabilities_cache = None

    def list_models(
        self,
        *,
        respect_restrictions: bool = True,
        include_aliases: bool = True,
        lowercase: bool = False,
        unique: bool = False,
    ) -> list[str]:
        """Return formatted model names supported by this provider."""

        model_configs = self.get_all_model_capabilities()
        if not model_configs:
            return []

        restriction_service = None
        if respect_restrictions:
            from utils.model_restrictions import get_restriction_service

            restriction_service = get_restriction_service()

        if restriction_service:
            allowed_configs = {}
            for model_name, config in model_configs.items():
                if restriction_service.is_allowed(self.get_provider_type(), model_name):
                    allowed_configs[model_name] = config
            model_configs = allowed_configs

        if not model_configs:
            return []

        return ModelCapabilities.collect_model_names(
            model_configs,
            include_aliases=include_aliases,
            lowercase=lowercase,
            unique=unique,
        )

    # ------------------------------------------------------------------
    # Request execution
    # ------------------------------------------------------------------
    @abstractmethod
    def generate_content(
        self,
        prompt: str,
        model_name: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_output_tokens: Optional[int] = None,
        **kwargs,
    ) -> ModelResponse:
        """Generate content using the model.

        This is the core method that all providers must implement to generate responses
        from their models. Providers should handle model-specific capabilities and
        constraints appropriately.

        Args:
            prompt: The main user prompt/query to send to the model
            model_name: Canonical model name or its alias that the provider supports
            system_prompt: Optional system instructions to prepend to the prompt for
                          establishing context, behavior, or role
            temperature: Controls randomness in generation (0.0=deterministic, 1.0=creative),
                        default 0.3. Some models may not support temperature control
            max_output_tokens: Optional maximum number of tokens to generate in the response.
                              If not specified, uses the model's default limit
            **kwargs: Additional provider-specific parameters that vary by implementation
                     (e.g., thinking_mode for Gemini, top_p for OpenAI, images for vision models)

        Returns:
            ModelResponse: Standardized response object containing:
                - content: The generated text response
                - usage: Token usage statistics (input/output/total)
                - model_name: The model that was actually used
                - friendly_name: Human-readable provider/model identifier
                - provider: The ProviderType enum value
                - metadata: Provider-specific metadata (finish_reason, safety info, etc.)

        Raises:
            ValueError: If the model is not supported, parameters are invalid,
                       or the model is restricted by policy
            RuntimeError: If the API call fails after retries
        """

    async def agenerate_content(
        self,
        prompt: str,
        model_name: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_output_tokens: Optional[int] = None,
        **kwargs,
    ) -> ModelResponse:
        """Async wrapper around generate_content.

        The provider SDKs we depend on (OpenAI, Gemini, xAI) expose synchronous
        `.create()` methods that block for the entire request — often 30-90s
        for reasoning models. Calling them directly from an `async def` stalls
        the asyncio event loop and serialises every other coroutine in flight.
        That breaks panel parallelism: paid-API panelists block their peers'
        setup work until they return.

        We dispatch the existing sync stack to a worker thread via
        ``loop.run_in_executor`` (using Panel's bounded executor — see
        ``_PROVIDER_EXECUTOR``). Each call captures the caller's
        ``contextvars`` and runs the worker via ``ctx.run(...)`` so the
        active ``run_id`` ContextVar reaches provider code; without that,
        provider-side streaming progress emits silently no-op. The event
        loop stays free; concurrent panelists actually run concurrently.
        The inner ``time.sleep`` in ``_run_with_retries`` is isolated by
        the same mechanism — no asyncio refactor needed deeper in the
        stack.

        Streaming v2 (per-token deltas pumped to the live viewer via the
        execution graph) is implemented for Anthropic / OpenAI / xAI /
        Gemini direct-API providers in ``providers/anthropic.py``,
        ``providers/openai_compatible.py``, and ``providers/gemini.py``.
        The OpenAI Responses endpoint (gpt-5.1-codex / o3-pro) still
        uses ``.create()``; that's tracked on the open queue.
        """
        sem = _get_api_semaphore()
        executor = _get_provider_executor()
        loop = asyncio.get_running_loop()

        # Round-3 audit blocker: ``loop.run_in_executor`` does NOT
        # propagate ContextVars to the worker thread. Streaming-progress
        # emitters in the providers read ``current_run_id()`` and got
        # None on every call — the whole feature was silent dead code.
        # Fix: capture the caller's context here (where the run_context
        # ContextVar IS set) and run the worker inside it. Now provider
        # threads see the active run id and emit graph events correctly.
        ctx = contextvars.copy_context()

        def _call_in_context() -> ModelResponse:
            return ctx.run(
                self.generate_content,
                prompt,
                model_name,
                system_prompt,
                temperature,
                max_output_tokens,
                **kwargs,
            )

        # Cancel-aware semaphore release. The naive `async with sem:` form
        # releases the slot when the asyncio task is cancelled, but Python
        # cannot cancel a running thread — the worker is still in the
        # SDK's blocking .create()/.stream() call until PANEL_API_TIMEOUT_S
        # forces the SDK to bail. Result: 16 simultaneous cancellations
        # would free 16 semaphore slots while leaving 16 threads occupied
        # (default pool 32). New agenerate_content calls would acquire a
        # slot and then BLOCK at executor.submit waiting for a thread —
        # for up to 10 minutes — with no error visible to the caller.
        #
        # Fix: hold the semaphore until the WORKER THREAD finishes
        # (success / exception / SDK-timeout), not until the asyncio
        # task is cancelled. The semaphore now reflects real provider
        # concurrency, never a phantom-released slot for a stuck thread.
        await sem.acquire()
        try:
            cf_future = executor.submit(_call_in_context)
        except BaseException:
            sem.release()
            raise

        # Schedule the release for when the underlying concurrent.future
        # actually resolves. ``call_soon_threadsafe`` is required because
        # this callback fires on the worker thread, not the event loop.
        def _release_when_thread_done(_cf: "concurrent.futures.Future") -> None:
            loop.call_soon_threadsafe(sem.release)

        cf_future.add_done_callback(_release_when_thread_done)

        # Wrap so we can `await` and propagate cancellation cleanly. If
        # the asyncio task is cancelled, the awaitable raises
        # CancelledError, but the underlying thread keeps running until
        # SDK timeout — the done-callback above releases the semaphore
        # then, not now.
        try:
            return await asyncio.wrap_future(cf_future, loop=loop)
        except asyncio.CancelledError:
            logger.debug(
                "agenerate_content cancelled mid-flight for %s/%s; semaphore "
                "release deferred until worker thread finishes",
                self.get_provider_type(), model_name,
            )
            raise

    def count_tokens(self, text: str, model_name: str) -> int:
        """Estimate token usage for a piece of text."""

        resolved_model = self._resolve_model_name(model_name)

        if not text:
            return 0

        estimated = max(1, len(text) // 4)
        logger.debug("Estimating %s tokens for model %s via character heuristic", estimated, resolved_model)
        return estimated

    def close(self) -> None:
        """Clean up any resources held by the provider."""

        return

    # ------------------------------------------------------------------
    # Retry helpers
    # ------------------------------------------------------------------
    def _is_error_retryable(self, error: Exception) -> bool:
        """Return True when an error warrants another attempt.

        Subclasses with structured provider errors should override this hook.
        The default implementation only retries obvious transient failures such
        as timeouts or 5xx responses detected via string inspection.
        """

        error_str = str(error).lower()

        if "429" in error_str or "rate limit" in error_str:
            return False

        retryable_indicators = [
            "timeout",
            "connection",
            "temporary",
            "unavailable",
            "retry",
            "reset",
            "refused",
            "broken pipe",
            "tls",
            "handshake",
            "network",
            "500",
            "502",
            "503",
            "504",
        ]

        return any(indicator in error_str for indicator in retryable_indicators)

    def _run_with_retries(
        self,
        operation: Callable[[], Any],
        *,
        max_attempts: int,
        delays: Optional[list[float]] = None,
        log_prefix: str = "",
    ):
        """Execute ``operation`` with retry semantics.

        Args:
            operation: Callable returning the provider result.
            max_attempts: Maximum number of attempts (>=1).
            delays: Optional list of sleep durations between attempts.
            log_prefix: Optional identifier for log clarity.

        Returns:
            Whatever ``operation`` returns.

        Raises:
            The last exception when all retries fail or the error is not retryable.
        """

        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        attempts = max_attempts
        delays = delays or []
        last_exc: Optional[Exception] = None

        for attempt_index in range(attempts):
            try:
                return operation()
            except Exception as exc:  # noqa: BLE001 - bubble exact provider errors
                last_exc = exc
                attempt_number = attempt_index + 1

                # Decide whether to retry based on subclass hook
                retryable = self._is_error_retryable(exc)
                if not retryable or attempt_number >= attempts:
                    raise

                delay_idx = min(attempt_index, len(delays) - 1) if delays else -1
                delay = delays[delay_idx] if delay_idx >= 0 else 0.0

                if delay > 0:
                    logger.warning(
                        "%s retryable error (attempt %s/%s): %s. Retrying in %ss...",
                        log_prefix or self.__class__.__name__,
                        attempt_number,
                        attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "%s retryable error (attempt %s/%s): %s. Retrying...",
                        log_prefix or self.__class__.__name__,
                        attempt_number,
                        attempts,
                        exc,
                    )

        # Should never reach here because loop either returns or raises
        raise last_exc if last_exc else RuntimeError("Retry loop exited without result")

    # ------------------------------------------------------------------
    # Validation hooks
    # ------------------------------------------------------------------
    def validate_model_name(self, model_name: str) -> bool:
        """
        Return ``True`` when the model resolves to an allowed capability.

        Args:
            model_name: Canonical model name or its alias
        """

        try:
            self.get_capabilities(model_name)
        except ValueError:
            return False
        return True

    def validate_parameters(self, model_name: str, temperature: float, **kwargs) -> None:
        """
        Validate model parameters against capabilities.

        Args:
            model_name: Canonical model name or its alias
        """

        capabilities = self.get_capabilities(model_name)

        if not capabilities.temperature_constraint.validate(temperature):
            constraint_desc = capabilities.temperature_constraint.get_description()
            raise ValueError(f"Temperature {temperature} is invalid for model {model_name}. {constraint_desc}")

    # ------------------------------------------------------------------
    # Preference / registry hooks
    # ------------------------------------------------------------------
    def get_preferred_model(self, category: "ToolModelCategory", allowed_models: list[str]) -> Optional[str]:
        """Get the preferred model from this provider for a given category."""

        return None

    def get_model_registry(self) -> Optional[dict[str, Any]]:
        """Return the model registry backing this provider, if any."""

        return None

    # ------------------------------------------------------------------
    # Capability lookup pipeline
    # ------------------------------------------------------------------
    def _lookup_capabilities(
        self,
        canonical_name: str,
        requested_name: Optional[str] = None,
    ) -> Optional[ModelCapabilities]:
        """Return ``ModelCapabilities`` for the canonical model name."""

        return self.get_all_model_capabilities().get(canonical_name)

    def _ensure_model_allowed(
        self,
        capabilities: ModelCapabilities,
        canonical_name: str,
        requested_name: str,
    ) -> None:
        """Raise ``ValueError`` if the model violates restriction policy."""

        try:
            from utils.model_restrictions import get_restriction_service
        except Exception:  # pragma: no cover - only triggered if service import breaks
            return

        restriction_service = get_restriction_service()
        if not restriction_service:
            return

        if restriction_service.is_allowed(self.get_provider_type(), canonical_name, requested_name):
            return

        raise ValueError(
            f"{self.get_provider_type().value} model '{canonical_name}' is not allowed by restriction policy."
        )

    def _finalise_capabilities(
        self,
        capabilities: ModelCapabilities,
        canonical_name: str,
        requested_name: str,
    ) -> ModelCapabilities:
        """Allow subclasses to adjust capability metadata before returning."""

        return capabilities

    def _raise_unsupported_model(self, model_name: str) -> None:
        """Raise the canonical unsupported-model error."""

        raise ValueError(f"Unsupported model '{model_name}' for provider {self.get_provider_type().value}.")

    def _resolve_model_name(self, model_name: str) -> str:
        """Resolve model shorthand to full name.

        This implementation uses the hook methods to support different
        model configuration sources.

        Args:
            model_name: Canonical model name or its alias

        Returns:
            Resolved model name
        """
        # Get model configurations from the hook method
        model_configs = self.get_all_model_capabilities()

        # First check if it's already a base model name (case-sensitive exact match)
        if model_name in model_configs:
            return model_name

        # Check case-insensitively for both base models and aliases
        model_name_lower = model_name.lower()

        # Check base model names case-insensitively
        for base_model in model_configs:
            if base_model.lower() == model_name_lower:
                return base_model

        # Check aliases from the model configurations
        alias_map = ModelCapabilities.collect_aliases(model_configs)
        for base_model, aliases in alias_map.items():
            if any(alias.lower() == model_name_lower for alias in aliases):
                return base_model

        # If not found, return as-is
        return model_name

"""Internal defaults and constants for clink."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_STREAM_LIMIT = 10 * 1024 * 1024  # 10MB per stream

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILTIN_PROMPTS_DIR = PROJECT_ROOT / "systemprompts" / "clink"
CONFIG_DIR = PROJECT_ROOT / "conf" / "cli_clients"
USER_CONFIG_DIR = Path.home() / ".panel" / "cli_clients"


@dataclass(frozen=True)
class CLIInternalDefaults:
    """Internal defaults applied to a CLI client during registry load."""

    parser: str
    additional_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    default_role_prompt: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    runner: str | None = None
    # When the OAuth-backed CLI fails for a recoverable reason (quota
    # exhaustion, auth lapse, etc.), the clink tool transparently retries the
    # same prompt against this paid-API model via the chat tool. We always
    # try the OAuth path first, so when the CLI's quota replenishes the next
    # call goes back to the free path automatically.
    oauth_fallback_model: str | None = None


INTERNAL_DEFAULTS: dict[str, CLIInternalDefaults] = {
    "gemini": CLIInternalDefaults(
        parser="gemini_stream_jsonl",
        additional_args=["-o", "stream-json"],
        default_role_prompt="systemprompts/clink/default.txt",
        runner="gemini",
        oauth_fallback_model="gemini-3.1-pro-preview",
    ),
    "codex": CLIInternalDefaults(
        parser="codex_jsonl",
        additional_args=["exec"],
        default_role_prompt="systemprompts/clink/default.txt",
        runner="codex",
        oauth_fallback_model="gpt-5.5",
    ),
    "claude": CLIInternalDefaults(
        parser="claude_json",
        additional_args=["--print", "--output-format", "json"],
        default_role_prompt="systemprompts/clink/default.txt",
        runner="claude",
        # When the user's Claude CLI subscription hits its quota, fall
        # back to the paid Anthropic API. Requires ANTHROPIC_API_KEY.
        # Defaults to Sonnet (not Opus) so an unattended quota crossover
        # doesn't silently start spending at flagship rates — the panel
        # explicitly flagged Opus-as-default as a financial-DoS path. Set
        # PANEL_CLAUDE_OAUTH_FALLBACK_MODEL to override (e.g. "opus" if the
        # operator has explicitly opted into paid-Opus fallback).
        oauth_fallback_model=os.environ.get(
            "PANEL_CLAUDE_OAUTH_FALLBACK_MODEL", "claude-sonnet-4-6"
        ),
    ),
}


def _build_model_to_cli() -> dict[str, str]:
    """Inverse of ``oauth_fallback_model``: which CLI handles which model.

    Derived from ``INTERNAL_DEFAULTS`` so env-driven overrides like
    ``PANEL_CLAUDE_OAUTH_FALLBACK_MODEL`` automatically flow through to
    the OAuth-first routing layer — no manual sync between two tables.

    Augmented with a few extra flagships per CLI that the OAuth path can
    reasonably serve; the ``oauth_fallback_model`` field on each CLI is
    only ONE model, but each CLI subscription typically serves a small
    family (claude → opus + sonnet; codex → gpt-5.5; gemini → 3.1 Pro
    preview). Add more entries here as the registry grows; new flagships
    should be opt-in (added to this map) rather than fuzzy-matched, since
    silent re-routing of a user's specific model request is the failure
    mode this layer must avoid.
    """

    # Start with the canonical inverse mapping.
    mapping: dict[str, str] = {
        defaults.oauth_fallback_model: cli_name
        for cli_name, defaults in INTERNAL_DEFAULTS.items()
        if defaults.oauth_fallback_model
    }

    # Hand-curated extras: models the same CLI can reasonably serve.
    # For Claude: opus + sonnet share the claude CLI / Anthropic
    # subscription, so both should route. Whichever is the configured
    # ``oauth_fallback_model`` wins over the static entry if there's
    # a collision (env override always takes precedence).
    extras = {
        "claude-opus-4-7": "claude",
        "claude-sonnet-4-6": "claude",
    }
    for model, cli in extras.items():
        mapping.setdefault(model, cli)

    return mapping


MODEL_TO_CLI: dict[str, str] = _build_model_to_cli()

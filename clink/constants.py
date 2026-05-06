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
USER_CONFIG_DIR = Path.home() / ".pal" / "cli_clients"


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
        # PAL_CLAUDE_OAUTH_FALLBACK_MODEL to override (e.g. "opus" if the
        # operator has explicitly opted into paid-Opus fallback).
        oauth_fallback_model=os.environ.get(
            "PAL_CLAUDE_OAUTH_FALLBACK_MODEL", "claude-sonnet-4-6"
        ),
    ),
}

"""clink tool - bridge Panel MCP requests to external AI CLIs."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import TextContent
from pydantic import BaseModel, Field

from clink import get_registry
from clink.agents import AgentOutput, CLIAgentError, create_agent
from clink.models import ResolvedCLIClient, ResolvedCLIRole
from config import TEMPERATURE_BALANCED
from tools.models import ToolModelCategory, ToolOutput
from tools.shared.base_models import COMMON_FIELD_DESCRIPTIONS
from tools.shared.exceptions import ToolExecutionError
from tools.simple.base import SchemaBuilder, SimpleTool
from utils.progress import emit_progress

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 20_000
SUMMARY_PATTERN = re.compile(r"<SUMMARY>(.*?)</SUMMARY>", re.IGNORECASE | re.DOTALL)

# Caps on how much CLI stdout/stderr/raw_output_file we surface to the MCP
# caller. Pre-cap, the success path forwarded the entire stderr stream and
# the entire raw JSON the CLI wrote to its output file — both can include
# absolute paths under ~/, env-derived strings, prompt fragments, and (in
# error cases) full auth-failure dumps with token-shaped strings. Set high
# enough to keep useful debug detail but bounded enough that a misbehaving
# CLI can't fill MCP transport with megabytes of internal state. Override
# via env for one-off forensics without redeploying.
_CLI_METADATA_TEXT_CAP = int(os.environ.get("PANEL_CLINK_METADATA_CAP", "2048"))
_CLI_RAW_OUTPUT_CAP = int(os.environ.get("PANEL_CLINK_RAW_OUTPUT_CAP", "8192"))

# Pattern -> redaction-token. Matched against stderr/stdout/raw_output_file
# AND CLI content before forwarding to MCP. Conservative: real provider errors
# usually only include API-key strings if they were echoed back by an angry
# SDK. We strip anything shaped like a known token format.
#
#   - sk-... and sk-ant-...  : OpenAI / Anthropic API keys
#   - AIza...                : Google API keys
#   - xai-...                : xAI keys
#   - eyJhbG... (JWT-shaped) : OAuth bearer tokens
#   - Bearer <hex/jwt>       : Authorization headers echoed in errors
#   - HOME-rooted paths      : /Users/<name>/... (mac), /home/<name>/... (linux),
#                              C:\Users\<name>\... (windows)
#                              The user's actual home is detected at module load
#                              and rewritten to <HOME>; the more general patterns
#                              catch other users' paths the CLI might surface.
# Redaction helpers moved to utils/redaction.py so bugfind/multiaudit can
# reuse them on locally-collected content (log tails, attached files)
# before sending to multi-provider panel prompts. Codex audit-flagged.
from utils.redaction import redact_secrets as _redact_only  # noqa: E402
from utils.redaction import redact_and_cap as _redact_and_cap  # noqa: E402


# Metadata field names a CLI parser must NEVER override on its own — these are
# the safety-critical ones we sanitised on the way in. If a parser supplies any
# of them via result.parsed.metadata they get silently dropped (the parser is
# untrusted: a malicious or buggy CLI could put 50MB of stderr into a "command"
# field expecting it to ride through). Audit panel finding from codex.
_PROTECTED_METADATA_FIELDS: frozenset[str] = frozenset({
    "stdout",
    "stderr",
    "command",
    "raw_output_file",
    "cli_name",
    "return_code",
    "duration_seconds",
    "parser",
    "role",
})


def _safe_merge_parser_metadata(
    base: dict[str, Any],
    parser_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Merge parser-supplied metadata into the sanitised base, dropping any
    keys the parser is not allowed to override and capping any string values
    that survive (so a parser can't smuggle a 10MB JSON dump through a
    custom field name)."""
    if not parser_metadata:
        return base
    for key, value in parser_metadata.items():
        if key in _PROTECTED_METADATA_FIELDS:
            logger.debug("clink: parser tried to override protected metadata field %r — dropped", key)
            continue
        if isinstance(value, str):
            base[key] = _redact_and_cap(value, cap=_CLI_METADATA_TEXT_CAP)
        else:
            base[key] = value
    return base

# Substrings (case-insensitive) in a CLI's stdout/stderr that mark a recoverable
# OAuth-side failure: the caller's free quota / login is the problem, not the
# request itself. When matched AND the CLI has a configured oauth_fallback_model,
# clink transparently retries the same prompt against the paid API.
#
# Real-world signals seen so far:
#   - Gemini CLI: "TerminalQuotaError", "QUOTA_EXHAUSTED", "exhausted your capacity",
#                 "quota will reset"
#   - Codex CLI:  "401 Unauthorized", "Please run codex login", "not authenticated"
# Add new patterns conservatively — false positives cost a paid-API call.
OAUTH_FAILURE_PATTERNS: tuple[str, ...] = (
    "terminalquotaerror",
    "quota_exhausted",
    "exhausted your capacity",
    "quota will reset",
    "rate_limit_exceeded",
    "401 unauthorized",
    "not authenticated",
    "please run codex login",
    "please run gemini login",
    "invalid_grant",
    "unauthenticated",
)

# Opt-in: when PANEL_FALLBACK_ON_TIMEOUT=1, a clink subprocess that hangs past
# its timeout (vs. failing with a quota signal) ALSO triggers the OAuth-to-API
# fallback. Off by default because timeout is ambiguous — could be a stuck
# CLI (where you'd want to fall back) or a legitimately long-running request
# the model is just thinking through (where falling back would double-charge).
# Set this when running unattended panels where any answer beats no answer.
_TIMEOUT_FALLBACK_PATTERNS: tuple[str, ...] = (
    "timed out",
    "timeout",
)


def _looks_like_recoverable_failure(
    exc: CLIAgentError, *, client: "ResolvedCLIClient | None" = None
) -> bool:
    """OAuth signals always trigger fallback. Timeouts trigger only when
    PANEL_FALLBACK_ON_TIMEOUT is set.

    Pattern matching merges:
      - Per-CLI patterns from ``CLIInternalDefaults.oauth_failure_patterns``
        (set at clink/constants.py — vendor-specific quota signals).
      - Global ``OAUTH_FAILURE_PATTERNS`` (broad catches that apply to any
        OAuth-style auth/quota issue).

    Per-CLI lets us add a signature unique to one vendor without false-
    firing on the others (panel finding: globals were one-size-fits-all
    and accumulating risked unrelated CLIs matching unrelated patterns)."""
    haystack = " ".join(filter(None, [str(exc), exc.stdout or "", exc.stderr or ""])).lower()
    patterns = list(OAUTH_FAILURE_PATTERNS)
    if client is not None:
        try:
            from clink.constants import INTERNAL_DEFAULTS
            extras = INTERNAL_DEFAULTS.get(client.name.lower())
            if extras and extras.oauth_failure_patterns:
                patterns.extend(extras.oauth_failure_patterns)
        except Exception:  # noqa: BLE001 — never fail the recoverable check
            pass
    if any(p in haystack for p in patterns):
        return True
    if os.environ.get("PANEL_FALLBACK_ON_TIMEOUT", "").strip().lower() in ("1", "true", "yes", "on"):
        if any(p in haystack for p in _TIMEOUT_FALLBACK_PATTERNS):
            return True
    return False


class CLinkRequest(BaseModel):
    """Request model for clink tool."""

    prompt: str = Field(..., description="Prompt forwarded to the target CLI.")
    cli_name: str | None = Field(
        default=None,
        description="Configured CLI client name to invoke. Defaults to the first configured CLI if omitted.",
    )
    role: str | None = Field(
        default=None,
        description="Optional role preset defined in the CLI configuration (defaults to 'default').",
    )
    absolute_file_paths: list[str] = Field(
        default_factory=list,
        description=COMMON_FIELD_DESCRIPTIONS["absolute_file_paths"],
    )
    images: list[str] = Field(
        default_factory=list,
        description=COMMON_FIELD_DESCRIPTIONS["images"],
    )
    continuation_id: str | None = Field(
        default=None,
        description=COMMON_FIELD_DESCRIPTIONS["continuation_id"],
    )


class CLinkTool(SimpleTool):
    """Bridge MCP requests to configured CLI agents.

    Schema metadata is cached at construction time and execution relies on the shared
    SimpleTool hooks for conversation memory. Prompt preparation is customised so we
    pass instructions and file references suitable for another CLI agent.
    """

    # Class-level cache for registry-derived metadata. make_tool('clink') runs
    # per panelist + per OAuth fallback, so __init__ fires N+ times per panel
    # call. The registry itself is memoized but the dict comprehensions below
    # are wasted work for static config. Audit panel finding (Codex + Grok).
    _CLI_NAMES_CACHE: list[str] | None = None
    _ROLE_MAP_CACHE: dict[str, list[str]] | None = None
    _ALL_ROLES_CACHE: list[str] | None = None
    _DEFAULT_CLI_NAME_CACHE: str | None = None

    @classmethod
    def _ensure_registry_cache(cls) -> None:
        if cls._CLI_NAMES_CACHE is not None:
            return
        registry = get_registry()
        names = registry.list_clients()
        cls._CLI_NAMES_CACHE = names
        cls._ROLE_MAP_CACHE = {name: registry.list_roles(name) for name in names}
        cls._ALL_ROLES_CACHE = sorted({role for roles in cls._ROLE_MAP_CACHE.values() for role in roles})
        if "gemini" in names:
            cls._DEFAULT_CLI_NAME_CACHE = "gemini"
        else:
            cls._DEFAULT_CLI_NAME_CACHE = names[0] if names else None

    def __init__(self) -> None:
        # Populate class-level cache once; per-instance attrs just point at it.
        type(self)._ensure_registry_cache()
        self._registry = get_registry()  # registry itself is memoized; cheap
        self._cli_names = type(self)._CLI_NAMES_CACHE
        self._role_map = type(self)._ROLE_MAP_CACHE
        self._all_roles = type(self)._ALL_ROLES_CACHE
        self._default_cli_name = type(self)._DEFAULT_CLI_NAME_CACHE
        self._active_system_prompt: str = ""
        super().__init__()

    def get_name(self) -> str:
        return "clink"

    def get_description(self) -> str:
        return (
            "Link a request to an external AI CLI (Gemini CLI, Qwen CLI, etc.) through Panel MCP to reuse "
            "their capabilities inside existing workflows."
        )

    def get_annotations(self) -> dict[str, Any]:
        return {"readOnlyHint": True}

    def requires_model(self) -> bool:
        return False

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.BALANCED

    def get_default_temperature(self) -> float:
        return TEMPERATURE_BALANCED

    def get_system_prompt(self) -> str:
        return self._active_system_prompt or ""

    def get_request_model(self):
        return CLinkRequest

    def get_input_schema(self) -> dict[str, Any]:
        # Surface configured CLI names and roles directly in the schema so MCP clients
        # (and downstream agents) can discover available options without consulting
        # a separate registry call.
        role_descriptions = []
        for name in self._cli_names:
            roles = ", ".join(sorted(self._role_map.get(name, ["default"]))) or "default"
            role_descriptions.append(f"{name}: {roles}")

        if role_descriptions:
            cli_available = ", ".join(self._cli_names) if self._cli_names else "(none configured)"
            default_text = (
                f" Default: {self._default_cli_name}." if self._default_cli_name and len(self._cli_names) <= 1 else ""
            )
            cli_description = (
                "Configured CLI client name (from conf/cli_clients). Available: " + cli_available + default_text
            )
            role_description = (
                "Optional role preset defined for the selected CLI (defaults to 'default'). Roles per CLI: "
                + "; ".join(role_descriptions)
            )
        else:
            cli_description = "Configured CLI client name (from conf/cli_clients)."
            role_description = "Optional role preset defined for the selected CLI (defaults to 'default')."

        properties = {
            "prompt": {
                "type": "string",
                "description": "User request forwarded to the CLI (conversation context is pre-applied).",
            },
            "cli_name": {
                "type": "string",
                "enum": self._cli_names,
                "description": cli_description,
            },
            "role": {
                "type": "string",
                "enum": self._all_roles or ["default"],
                "description": role_description,
            },
            "absolute_file_paths": SchemaBuilder.SIMPLE_FIELD_SCHEMAS["absolute_file_paths"],
            "images": SchemaBuilder.COMMON_FIELD_SCHEMAS["images"],
            "continuation_id": SchemaBuilder.COMMON_FIELD_SCHEMAS["continuation_id"],
        }

        schema = {
            "type": "object",
            "properties": properties,
            "required": ["prompt"],
            "additionalProperties": False,
        }

        if len(self._cli_names) > 1:
            schema["required"].append("cli_name")

        return schema

    def get_tool_fields(self) -> dict[str, dict[str, Any]]:
        """Unused by clink because we override the schema end-to-end."""
        return {}

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        self._current_arguments = arguments
        request = self.get_request_model()(**arguments)

        path_error = self._validate_file_paths(request)
        if path_error:
            self._raise_tool_error(path_error)

        selected_cli = request.cli_name or self._default_cli_name
        if not selected_cli:
            self._raise_tool_error("No CLI clients are configured for clink.")

        try:
            client_config = self._registry.get_client(selected_cli)
        except KeyError as exc:
            self._raise_tool_error(str(exc))

        try:
            role_config = client_config.get_role(request.role)
        except KeyError as exc:
            self._raise_tool_error(str(exc))

        absolute_file_paths = self.get_request_files(request)
        images = self.get_request_images(request)
        continuation_id = self.get_request_continuation_id(request)

        self._model_context = arguments.get("_model_context")

        # Off-loop file read so concurrent panel fan-out doesn't pause on
        # filesystem latency (small win individually, real with N panelists).
        system_prompt_text = await asyncio.to_thread(
            role_config.prompt_path.read_text, encoding="utf-8"
        )
        include_system_prompt = not self._use_external_system_prompt(client_config)

        try:
            prompt_text = await self._prepare_prompt_for_role(
                request,
                role_config,
                system_prompt=system_prompt_text,
                include_system_prompt=include_system_prompt,
            )
        except Exception as exc:
            logger.exception("Failed to prepare clink prompt")
            self._raise_tool_error(f"Failed to prepare prompt: {exc}")

        agent = create_agent(client_config)
        try:
            result = await agent.run(
                role=role_config,
                prompt=prompt_text,
                system_prompt=system_prompt_text if system_prompt_text.strip() else None,
                files=absolute_file_paths,
                images=images,
            )
        except CLIAgentError as exc:
            # OAuth-to-API fallback: when the CLI fails for a recoverable reason
            # (quota exhausted, auth lapse) AND we have a configured paid-API
            # fallback model, retry the same prompt via chat. We re-attempt the
            # OAuth path on every subsequent call, so the moment quota replenishes
            # we're back to the free path with no manual intervention.
            fallback = await self._try_oauth_fallback(
                exc=exc,
                client_config=client_config,
                prompt_text=prompt_text,
                images=images,
            )
            if fallback is not None:
                return fallback

            metadata = self._build_error_metadata(client_config, exc)
            self._raise_tool_error(
                f"CLI '{client_config.name}' execution failed: {exc}",
                metadata=metadata,
            )

        metadata = self._build_success_metadata(client_config, role_config, result)
        metadata = self._prune_metadata(metadata, client_config, reason="normal")

        # Redact secrets/HOME paths from the actual CLI answer too, not just
        # metadata. A CLI that prints a key in its normal output (codex CLI
        # echoing env, a debug-mode session, etc.) would otherwise forward
        # them verbatim. Don't truncate — _apply_output_limit handles size.
        content, metadata = self._apply_output_limit(
            client_config,
            _redact_only(result.parsed.content),
            metadata,
        )

        model_info = {
            "provider": client_config.name,
            "model_name": result.parsed.metadata.get("model_used"),
        }

        if continuation_id:
            try:
                self._record_assistant_turn(continuation_id, content, request, model_info)
            except Exception:
                logger.debug("Failed to record assistant turn for continuation %s", continuation_id, exc_info=True)

        continuation_offer = self._create_continuation_offer(request, model_info)
        if continuation_offer:
            tool_output = self._create_continuation_offer_response(
                content,
                continuation_offer,
                request,
                model_info,
            )
            tool_output.metadata = self._merge_metadata(tool_output.metadata, metadata)
        else:
            tool_output = ToolOutput(
                status="success",
                content=content,
                content_type="text",
                metadata=metadata,
            )

        return [TextContent(type="text", text=tool_output.model_dump_json())]

    async def prepare_prompt(self, request) -> str:
        client_config = self._registry.get_client(request.cli_name)
        role_config = client_config.get_role(request.role)
        system_prompt_text = await asyncio.to_thread(
            role_config.prompt_path.read_text, encoding="utf-8"
        )
        include_system_prompt = not self._use_external_system_prompt(client_config)
        return await self._prepare_prompt_for_role(
            request,
            role_config,
            system_prompt=system_prompt_text,
            include_system_prompt=include_system_prompt,
        )

    async def _prepare_prompt_for_role(
        self,
        request: CLinkRequest,
        role: ResolvedCLIRole,
        *,
        system_prompt: str,
        include_system_prompt: bool,
    ) -> str:
        """Load the role prompt and assemble the final user message."""
        self._active_system_prompt = system_prompt
        try:
            user_content = self.handle_prompt_file_with_fallback(request).strip()
            # Pass the CLI name so the framing matches the spawned CLI.
            cli_name = (request.cli_name or "").strip() if request else ""
            guidance = self._agent_capabilities_guidance(cli_name)
            file_section = self._format_file_references(self.get_request_files(request))

            sections: list[str] = []
            active_prompt = self.get_system_prompt().strip()
            if include_system_prompt and active_prompt:
                sections.append(active_prompt)
            sections.append(guidance)
            sections.append("=== USER REQUEST ===\n" + user_content)
            if file_section:
                sections.append("=== FILE REFERENCES ===\n" + file_section)
            sections.append("Provide your response below using your own CLI tools as needed:")
            return "\n\n".join(sections)
        finally:
            self._active_system_prompt = ""

    def _use_external_system_prompt(self, client: ResolvedCLIClient) -> bool:
        runner_name = (client.runner or client.name).lower()
        return runner_name == "claude"

    def _build_success_metadata(
        self,
        client: ResolvedCLIClient,
        role: ResolvedCLIRole,
        result: AgentOutput,
    ) -> dict[str, Any]:
        """Capture execution metadata for successful CLI calls."""
        metadata: dict[str, Any] = {
            "cli_name": client.name,
            "role": role.name,
            "command": result.sanitized_command,
            "duration_seconds": round(result.duration_seconds, 3),
            "parser": result.parser_name,
            "return_code": result.returncode,
        }
        # Safe merge: parser-supplied metadata is untrusted. It cannot
        # override the safety-critical fields we just built (would let a
        # CLI smuggle 50MB stderr through a "command" override) and any
        # string values it contributes are capped + secret-redacted.
        metadata = _safe_merge_parser_metadata(metadata, result.parsed.metadata)

        if result.stderr.strip():
            metadata.setdefault(
                "stderr",
                _redact_and_cap(result.stderr.strip(), cap=_CLI_METADATA_TEXT_CAP),
            )
        if result.output_file_content and "raw" not in metadata:
            metadata["raw_output_file"] = _redact_and_cap(
                result.output_file_content,
                cap=_CLI_RAW_OUTPUT_CAP,
            )
        return metadata

    def _merge_metadata(self, base: dict[str, Any] | None, extra: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base or {})
        merged.update(extra)
        return merged

    def _apply_output_limit(
        self,
        client: ResolvedCLIClient,
        content: str,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if len(content) <= MAX_RESPONSE_CHARS:
            return content, metadata

        summary = self._extract_summary(content)
        if summary:
            summary_text = summary
            if len(summary_text) > MAX_RESPONSE_CHARS:
                logger.debug(
                    "Clink summary from %s exceeded %d chars; truncating summary to fit.",
                    client.name,
                    MAX_RESPONSE_CHARS,
                )
                summary_text = summary_text[:MAX_RESPONSE_CHARS]
            summary_metadata = self._prune_metadata(metadata, client, reason="summary")
            summary_metadata.update(
                {
                    "output_summarized": True,
                    "output_original_length": len(content),
                    "output_summary_length": len(summary_text),
                    "output_limit": MAX_RESPONSE_CHARS,
                }
            )
            logger.info(
                "Clink compressed %s output via <SUMMARY>: original=%d chars, summary=%d chars",
                client.name,
                len(content),
                len(summary_text),
            )
            return summary_text, summary_metadata

        truncated_metadata = self._prune_metadata(metadata, client, reason="truncated")
        truncated_metadata.update(
            {
                "output_truncated": True,
                "output_original_length": len(content),
                "output_limit": MAX_RESPONSE_CHARS,
            }
        )

        excerpt_limit = min(4000, MAX_RESPONSE_CHARS // 2)
        excerpt = content[:excerpt_limit]
        truncated_metadata["output_excerpt_length"] = len(excerpt)

        logger.warning(
            "Clink truncated %s output: original=%d chars exceeds limit=%d; excerpt_length=%d",
            client.name,
            len(content),
            MAX_RESPONSE_CHARS,
            len(excerpt),
        )

        message = (
            f"CLI '{client.name}' produced {len(content)} characters, exceeding the configured clink limit "
            f"({MAX_RESPONSE_CHARS} characters). The full output was suppressed to stay within MCP response caps. "
            "Please narrow the request (review fewer files, summarize results) or run the CLI directly for the full log.\n\n"
            f"--- Begin excerpt ({len(excerpt)} of {len(content)} chars) ---\n{excerpt}\n--- End excerpt ---"
        )

        return message, truncated_metadata

    def _extract_summary(self, content: str) -> str | None:
        match = SUMMARY_PATTERN.search(content)
        if not match:
            return None
        summary = match.group(1).strip()
        return summary or None

    def _prune_metadata(
        self,
        metadata: dict[str, Any],
        client: ResolvedCLIClient,
        *,
        reason: str,
    ) -> dict[str, Any]:
        cleaned = dict(metadata)
        events = cleaned.pop("events", None)
        if events is not None:
            cleaned[f"events_removed_for_{reason}"] = True
            logger.debug(
                "Clink dropped %s events metadata for %s response (%s)",
                client.name,
                reason,
                type(events).__name__,
            )
        return cleaned

    def _build_error_metadata(self, client: ResolvedCLIClient, exc: CLIAgentError) -> dict[str, Any]:
        """Assemble metadata for failed CLI calls.

        Both stdout and stderr go through _redact_and_cap because failure
        outputs are exactly where SDK clients are most likely to echo back
        partial credentials, full request bodies, or auth-token shapes.
        """
        metadata: dict[str, Any] = {
            "cli_name": client.name,
            "return_code": exc.returncode,
        }
        if exc.stdout:
            metadata["stdout"] = _redact_and_cap(exc.stdout.strip(), cap=_CLI_METADATA_TEXT_CAP)
        if exc.stderr:
            metadata["stderr"] = _redact_and_cap(exc.stderr.strip(), cap=_CLI_METADATA_TEXT_CAP)
        return metadata

    @staticmethod
    def _looks_like_oauth_failure(exc: CLIAgentError) -> bool:
        """Best-effort check that a CLI failure originated from OAuth side, not the prompt."""
        haystack = " ".join(filter(None, [str(exc), exc.stdout or "", exc.stderr or ""])).lower()
        return any(pattern in haystack for pattern in OAUTH_FAILURE_PATTERNS)

    async def _try_oauth_fallback(
        self,
        *,
        exc: CLIAgentError,
        client_config: ResolvedCLIClient,
        prompt_text: str,
        images: list[str],
    ) -> list[TextContent] | None:
        """Retry a recoverable CLI failure against the configured paid-API model.

        Returns the chat tool's TextContent response on success, with
        ``oauth_fallback_used`` markers injected into the response metadata so
        callers (panel, the user) know the call was billed. Returns None when:
          - no fallback is configured for this CLI, or
          - the failure isn't an OAuth-side failure, or
          - the fallback model has no configured provider.

        Raises ToolExecutionError when the fallback itself fails — the
        original behaviour of silently returning None hid broken fallback
        config (stale API key, etc.) behind the original CLI quota error
        forever (panel audit finding).

        File handling: clink already inlined absolute_file_paths into
        ``prompt_text`` during prompt prep. We deliberately pass an empty
        files list to chat to avoid double-inclusion (was a real bug in the
        first F1 cut).
        """
        fallback_model = client_config.oauth_fallback_model
        if not fallback_model or not _looks_like_recoverable_failure(
            exc, client=client_config
        ):
            return None

        from providers.registry import ModelProviderRegistry
        if ModelProviderRegistry.get_provider_for_model(fallback_model) is None:
            logger.info(
                "clink %s: OAuth failure detected but fallback model %r has no configured provider; "
                "returning original error.",
                client_config.name,
                fallback_model,
            )
            return None

        await emit_progress(
            f"clink/{client_config.name}: OAuth path failed ({type(exc).__name__}); "
            f"falling back to paid API model {fallback_model}",
            progress=0.0,
        )
        logger.warning(
            "clink %s OAuth path failed (%s); falling back to %s via chat tool",
            client_config.name,
            exc,
            fallback_model,
        )

        from server import execute_tool

        chat_args: dict[str, Any] = {
            "prompt": prompt_text,
            "model": fallback_model,
            # Empty — files are already in prompt_text. Passing them again
            # would double-include their content in the chat request.
            "absolute_file_paths": [],
            "images": images,
            "working_directory_absolute_path": str(client_config.working_dir or Path.cwd() or "/tmp"),
            # Tell the graph layer this is a fallback edge (not normal spawn)
            # and pre-tag the cost_tier as paid even before the call returns.
            "_graph_edge_kind": "fallback",
            "_graph_cost_tier": "oauth_fallback_paid",
            "_graph_label": f"oauth-fallback:{client_config.name}",
        }
        try:
            # The fallback prompt_text already has the user's files inlined
            # (clink builds it that way). Mark internal so the chat tool's
            # MCP-transport size check bypasses — the content is Panel-built,
            # not raw user input crossing the boundary.
            from tools.shared.base_tool import mark_internal_payload
            with mark_internal_payload():
                result = await execute_tool("chat", chat_args)
        except Exception as fallback_exc:  # noqa: BLE001
            logger.exception(
                "clink %s OAuth fallback to %s also failed",
                client_config.name,
                fallback_model,
            )
            await emit_progress(
                f"clink/{client_config.name}: fallback to {fallback_model} also failed ({fallback_exc})",
                progress=1.0,
            )
            self._raise_tool_error(
                f"CLI '{client_config.name}' failed AND OAuth fallback to {fallback_model} also failed: "
                f"original={type(exc).__name__}: {exc}; fallback={type(fallback_exc).__name__}: {fallback_exc}",
                metadata={
                    "cli_name": client_config.name,
                    "oauth_fallback_attempted": True,
                    "oauth_fallback_model": fallback_model,
                    "fallback_failure": f"{type(fallback_exc).__name__}: {fallback_exc}",
                },
            )

        # Inject fallback markers so the panel cost_tier and any caller can
        # see this run was billed despite the user asking for a free CLI.
        result = self._mark_fallback_in_result(
            result,
            cli_name=client_config.name,
            fallback_model=fallback_model,
            original_failure=f"{type(exc).__name__}: {exc}",
        )

        await emit_progress(
            f"clink/{client_config.name}: ✓ recovered via {fallback_model}",
            progress=1.0,
        )
        return result

    @staticmethod
    def _mark_fallback_in_result(
        result: list[TextContent],
        *,
        cli_name: str,
        fallback_model: str,
        original_failure: str,
    ) -> list[TextContent]:
        """Stamp oauth_fallback_used markers onto a chat tool's TextContent payload.

        We rewrap the JSON body so the panel and downstream callers can
        determine the real cost tier. Best-effort — if the body isn't JSON
        we leave it alone rather than corrupt the response.
        """
        import json
        if not result:
            return result
        first = result[0]
        text = getattr(first, "text", None)
        if not text:
            return result
        try:
            payload = json.loads(text)
        except Exception:  # noqa: BLE001
            return result
        if isinstance(payload, dict):
            existing = payload.get("metadata")
            # If metadata exists but isn't a dict, preserve the original under
            # metadata_original_non_dict instead of silently dropping it. The
            # audit panel flagged the silent-drop as "real but narrow data loss"
            # — losing upstream metadata when chat returns an unexpected schema.
            metadata: dict[str, Any] = {}
            if isinstance(existing, dict):
                metadata = dict(existing)
            elif existing is not None:
                metadata["metadata_original_non_dict"] = existing
            metadata["oauth_fallback_used"] = True
            metadata["oauth_fallback_from_cli"] = cli_name
            metadata["oauth_fallback_model"] = fallback_model
            metadata["oauth_fallback_original_failure"] = original_failure
            payload["metadata"] = metadata
            new_text = json.dumps(payload)
            return [TextContent(type="text", text=new_text)] + list(result[1:])
        return result

    def _raise_tool_error(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        error_output = ToolOutput(status="error", content=message, content_type="text", metadata=metadata)
        raise ToolExecutionError(error_output.model_dump_json())

    def _agent_capabilities_guidance(self, cli_name: str = "") -> str:
        # cli_name should be set by the caller to ``client.name`` so the
        # prompt framing matches the actual CLI we spawned. Older versions
        # hardcoded "Gemini CLI agent" for every CLI — codex and claude
        # got Gemini-flavoured framing (panel-flagged: a soft prompt-
        # mismatch that nudged non-Gemini CLIs toward Gemini-style
        # behaviour). Default to a generic phrasing if no CLI name is
        # supplied so the message is never literally false.
        agent_label = f"the {cli_name} CLI agent" if cli_name else "this CLI agent"
        return (
            f"You are operating through {agent_label}. You have access to your full suite of "
            "CLI capabilities—including launching web searches, reading files, and using any other "
            "available tools. Gather current information yourself and deliver the final answer without "
            "asking the Panel MCP host to perform searches or file reads."
        )

    def _format_file_references(self, files: list[str]) -> str:
        if not files:
            return ""

        references: list[str] = []
        for file_path in files:
            try:
                path = Path(file_path)
                stat = path.stat()
                modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                size = stat.st_size
                references.append(f"- {file_path} (last modified {modified}, {size} bytes)")
            except OSError:
                references.append(f"- {file_path} (unavailable)")
        return "\n".join(references)

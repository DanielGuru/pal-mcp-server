"""Parser for Claude CLI streaming JSONL output (`claude -p --output-format stream-json --verbose`).

Claude stream-json emits one JSON object per line during execution. Shape (truncated):

  {"type":"system","subtype":"init","cwd":"...","session_id":"...","model":"...","tools":[...]}
  {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"..."}],...}}
  {"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"..."}}],...}}
  {"type":"user","message":{"role":"user","content":[{"type":"tool_result",...}]}}
  {"type":"result","subtype":"success","duration_ms":...,"result":"<final text>",...}

This parser aggregates assistant text content into the final response and
exposes ``describe_event`` so the streaming runner emits live progress events
("claude: starting (model)…", "claude: tool_use Read → path", "claude:
drafting response…"). Without this, claude with single-shot ``--output-format
json`` produced ZERO progress events for the panel adapter — multi-minute
runs looked like a 300s hang to the user.
"""

from __future__ import annotations

import json
from typing import Any

from .base import BaseParser, ParsedCLIResponse, ParserError


class ClaudeStreamJSONLParser(BaseParser):
    """Parse stdout produced by ``claude --output-format stream-json --verbose``."""

    name = "claude_stream_jsonl"

    # ----- progress hook ----------------------------------------------------

    def describe_event(self, raw_line: str) -> str | None:
        line = raw_line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        event_type = event.get("type")
        if not isinstance(event_type, str):
            return None

        if event_type == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                model = event.get("model")
                return f"claude: starting ({model})…" if model else "claude: starting…"
            return None

        if event_type == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    name = block.get("name") or "tool"
                    inp = block.get("input") or {}
                    target = (
                        inp.get("path")
                        or inp.get("file_path")
                        or inp.get("pattern")
                        or inp.get("command")
                        or inp.get("query")
                    )
                    if target:
                        short = str(target)
                        if len(short) > 80:
                            short = short[:77] + "…"
                        return f"claude: {name} → {short}"
                    return f"claude: calling {name}…"
                if btype == "thinking":
                    return "claude: thinking…"
                if btype == "text":
                    return "claude: drafting response…"
            return None

        if event_type == "user":
            # Tool results coming back into the conversation. Useful as a
            # progress beat ("a tool call just returned") without spamming.
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    name = block.get("tool_use_name") or "tool"
                    return f"claude: {name} done"
            return None

        if event_type == "result":
            subtype = event.get("subtype")
            usage = event.get("usage") or {}
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            if inp is not None and out is not None:
                return f"claude: complete (in: {inp:,}, out: {out:,} tok)"
            duration_ms = event.get("duration_ms")
            if isinstance(duration_ms, (int, float)):
                return f"claude: complete ({duration_ms / 1000:.1f}s, {subtype or 'ok'})"
            return f"claude: complete ({subtype or 'ok'})"

        if event_type == "error":
            msg = event.get("message")
            return f"claude error: {msg}" if isinstance(msg, str) else "claude error"

        return None

    # ----- final aggregation ------------------------------------------------

    def parse(self, stdout: str, stderr: str) -> ParsedCLIResponse:
        if not stdout.strip():
            raise ParserError(
                "Claude CLI returned empty stdout while stream-json output was expected"
            )

        events: list[dict[str, Any]] = []
        text_chunks: list[str] = []
        result_payload: dict[str, Any] | None = None
        errors: list[str] = []

        for raw in stdout.splitlines():
            raw = raw.strip()
            if not raw or not raw.startswith("{"):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            events.append(event)

            etype = event.get("type")
            if etype == "assistant":
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            text_chunks.append(text)
            elif etype == "result":
                result_payload = event
            elif etype == "error":
                msg = event.get("message")
                if isinstance(msg, str):
                    errors.append(msg)

        # Prefer the canonical `result.result` field when present (it's the
        # finalised assistant message; chunks may be intermediate). Fall back
        # to joined text blocks for resilience.
        content = ""
        if result_payload is not None:
            result_field = result_payload.get("result")
            if isinstance(result_field, str) and result_field.strip():
                content = result_field.strip()
        if not content:
            content = "".join(text_chunks).strip()

        if not content:
            if errors:
                content = "\n".join(errors)
            else:
                stderr_text = stderr.strip()
                if stderr_text:
                    return ParsedCLIResponse(
                        content=(
                            "Claude CLI returned no textual result. Raw stderr was "
                            "preserved for troubleshooting."
                        ),
                        metadata={"events": events, "stderr": stderr_text},
                    )
                raise ParserError(
                    "Claude stream-json output did not include any assistant text or result"
                )

        metadata: dict[str, Any] = {"events": events}
        if errors:
            metadata["errors"] = errors
        if result_payload is not None:
            for key in (
                "subtype",
                "duration_ms",
                "duration_api_ms",
                "session_id",
                "uuid",
                "model_used",
            ):
                if key in result_payload:
                    metadata[key] = result_payload[key]
            usage = result_payload.get("usage")
            if isinstance(usage, dict):
                metadata["usage"] = usage
            model_usage = result_payload.get("modelUsage")
            if isinstance(model_usage, dict) and model_usage:
                metadata["model_usage"] = model_usage
                metadata.setdefault("model_used", next(iter(model_usage.keys())))
            permission_denials = result_payload.get("permission_denials")
            if isinstance(permission_denials, list) and permission_denials:
                metadata["permission_denials"] = permission_denials
            metadata["is_error"] = bool(result_payload.get("is_error"))
        if stderr and stderr.strip():
            metadata["stderr"] = stderr.strip()

        return ParsedCLIResponse(content=content, metadata=metadata)

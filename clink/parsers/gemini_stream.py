"""Parser for Gemini CLI streaming JSONL output (`gemini -o stream-json`).

Gemini stream-json emits one JSON object per line during execution:

  {"type":"init","model":"...","session_id":"..."}
  {"type":"message","role":"user","content":"..."}
  {"type":"message","role":"assistant","content":"...","delta":true}
  {"type":"tool_call","name":"read_file","arguments":{...}}
  {"type":"tool_result","name":"read_file",...}
  {"type":"result","status":"success","stats":{...}}

This parser aggregates assistant message deltas into the final content and
exposes a describe_event hook so the streaming runner can surface live
progress (file reads, tool calls, model output) to the host UI.
"""

from __future__ import annotations

import json
from typing import Any

from .base import BaseParser, ParsedCLIResponse, ParserError


class GeminiStreamJSONLParser(BaseParser):
    """Parse stdout produced by `gemini -o stream-json`."""

    name = "gemini_stream_jsonl"

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

        if event_type == "init":
            model = event.get("model")
            return f"gemini: starting ({model})…" if model else "gemini: starting…"

        if event_type == "message":
            role = event.get("role")
            if role == "user":
                # Echo of input — not interesting for progress
                return None
            if role == "assistant":
                if event.get("delta"):
                    return None  # too noisy; per-token deltas
                return "gemini: drafting response…"
            return None

        if event_type == "tool_call":
            name = event.get("name") or event.get("tool") or "tool"
            args = event.get("arguments") or {}
            # Common interesting args: path, pattern, command
            target = args.get("path") or args.get("file_path") or args.get("pattern") or args.get("command")
            if target:
                short = str(target)
                if len(short) > 80:
                    short = short[:77] + "…"
                return f"gemini: {name} → {short}"
            return f"gemini: calling {name}…"

        if event_type == "tool_result":
            name = event.get("name") or "tool"
            return f"gemini: {name} done"

        if event_type == "thinking":
            return "gemini: thinking…"

        if event_type == "result":
            stats = event.get("stats") or {}
            total = stats.get("total_tokens")
            inp = stats.get("input_tokens")
            out = stats.get("output_tokens")
            if inp is not None and out is not None:
                return f"gemini: complete (in: {inp:,}, out: {out:,} tok)"
            if total is not None:
                return f"gemini: complete ({total:,} tok)"
            return "gemini: complete"

        if event_type == "error":
            msg = event.get("message")
            return f"gemini error: {msg}" if isinstance(msg, str) else "gemini error"

        return None

    # ----- final aggregation ------------------------------------------------

    def parse(self, stdout: str, stderr: str) -> ParsedCLIResponse:
        if not stdout.strip():
            raise ParserError("Gemini CLI returned empty stdout while stream-json output was expected")

        events: list[dict[str, Any]] = []
        assistant_chunks: list[str] = []
        final_assistant: str | None = None
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
            if etype == "message" and event.get("role") == "assistant":
                content = event.get("content")
                if not isinstance(content, str):
                    continue
                if event.get("delta"):
                    # Streaming token chunks — concatenate
                    assistant_chunks.append(content)
                else:
                    # Non-delta message — treat as a complete assistant turn,
                    # supersedes accumulated deltas if it appears later.
                    final_assistant = content
            elif etype == "result":
                result_payload = event
            elif etype == "error":
                msg = event.get("message")
                if isinstance(msg, str):
                    errors.append(msg)

        # Prefer a complete (non-delta) assistant message; fall back to joined deltas.
        if final_assistant is not None and final_assistant.strip():
            content = final_assistant.strip()
        else:
            content = "".join(assistant_chunks).strip()

        if not content:
            if errors:
                content = "\n".join(errors)
            else:
                raise ParserError("Gemini stream-json output did not include an assistant message")

        metadata: dict[str, Any] = {"events": events}
        if errors:
            metadata["errors"] = errors
        if result_payload is not None:
            stats = result_payload.get("stats")
            if isinstance(stats, dict):
                metadata["stats"] = stats
            status = result_payload.get("status")
            if isinstance(status, str):
                metadata["result_status"] = status
        if stderr and stderr.strip():
            metadata["stderr"] = stderr.strip()

        return ParsedCLIResponse(content=content, metadata=metadata)

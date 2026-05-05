"""Parser for Codex CLI JSONL output."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseParser, ParsedCLIResponse, ParserError


class CodexJSONLParser(BaseParser):
    """Parse stdout emitted by `codex exec --json`."""

    name = "codex_jsonl"

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

        # turn lifecycle
        if event_type == "turn.started":
            return "codex: starting…"
        if event_type == "turn.completed":
            usage = event.get("usage") or {}
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            if inp is not None and out is not None:
                return f"codex: turn complete (in: {inp:,}, out: {out:,} tok)"
            return "codex: turn complete"

        # items (the unit of activity)
        if event_type == "item.started":
            item = event.get("item") or {}
            kind = item.get("type")
            if kind == "agent_message":
                return "codex: drafting response…"
            if kind == "reasoning":
                return "codex: thinking…"
            if kind == "tool_call":
                tool = item.get("name") or item.get("tool") or "tool"
                return f"codex: calling {tool}…"
            if kind == "command_execution":
                cmd = item.get("command") or ""
                if isinstance(cmd, list):
                    cmd = " ".join(cmd)
                if isinstance(cmd, str) and cmd:
                    short = cmd if len(cmd) <= 80 else cmd[:77] + "…"
                    return f"codex: $ {short}"
                return "codex: running command…"
            if kind == "file_read":
                path = item.get("path") or item.get("file") or ""
                return f"codex: reading {path}" if path else "codex: reading file"
            if kind:
                return f"codex: {kind}…"
            return None

        if event_type == "item.completed":
            item = event.get("item") or {}
            kind = item.get("type")
            if kind == "tool_call":
                return "codex: tool returned"
            if kind == "command_execution":
                rc = item.get("returncode")
                return f"codex: command done (rc={rc})" if rc is not None else "codex: command done"
            return None

        if event_type == "error":
            msg = event.get("message")
            return f"codex error: {msg}" if isinstance(msg, str) else "codex error"

        return None

    def parse(self, stdout: str, stderr: str) -> ParsedCLIResponse:
        lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        events: list[dict[str, Any]] = []
        agent_messages: list[str] = []
        errors: list[str] = []
        usage: dict[str, Any] | None = None

        for line in lines:
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            events.append(event)
            event_type = event.get("type")
            if event_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        agent_messages.append(text.strip())
            elif event_type == "error":
                message = event.get("message")
                if isinstance(message, str) and message.strip():
                    errors.append(message.strip())
            elif event_type == "turn.completed":
                turn_usage = event.get("usage")
                if isinstance(turn_usage, dict):
                    usage = turn_usage

        if not agent_messages and errors:
            agent_messages.extend(errors)

        if not agent_messages:
            raise ParserError("Codex CLI JSONL output did not include an agent_message item")

        content = "\n\n".join(agent_messages).strip()
        metadata: dict[str, Any] = {"events": events}
        if errors:
            metadata["errors"] = errors
        if usage:
            metadata["usage"] = usage
        if stderr and stderr.strip():
            metadata["stderr"] = stderr.strip()

        return ParsedCLIResponse(content=content, metadata=metadata)

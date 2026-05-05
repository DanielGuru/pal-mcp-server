"""Parser interfaces for clink runner outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedCLIResponse:
    """Result of parsing CLI stdout/stderr."""

    content: str
    metadata: dict[str, Any]


class ParserError(RuntimeError):
    """Raised when CLI output cannot be parsed into a structured response."""


class BaseParser:
    """Base interface for CLI output parsers."""

    name: str = "base"

    def parse(self, stdout: str, stderr: str) -> ParsedCLIResponse:
        raise NotImplementedError("Parsers must implement parse()")

    def describe_event(self, raw_line: str) -> str | None:
        """Optional hook: convert one raw stdout line into a progress message.

        Called per stdout line during streaming execution. Return a short human
        status string (e.g. "thinking…", "calling tool: shell") to surface as
        an MCP progress notification, or None to suppress this line.

        Default: no progress events.
        """
        _ = raw_line
        return None

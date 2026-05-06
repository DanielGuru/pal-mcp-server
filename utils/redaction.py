"""Shared secret-redaction helpers.

Originally lived in ``tools/clink.py`` for redacting CLI subprocess
output before forwarding to MCP. Promoted to ``utils/redaction.py``
when ``bugfind`` started auto-attaching local log tails to multi-
provider panel prompts (codex audit caught the gap: log lines
containing API keys / Bearer tokens / JWTs would be sent verbatim
to OpenAI / Anthropic / Gemini / xAI).

Any code path that takes locally-collected text and ships it to an
external model provider should run it through ``redact_secrets``
first. The patterns are conservative — they catch known token
shapes (``sk-``, ``sk-ant-``, ``AIza``, ``xai-``, JWTs, ``Bearer``
headers) plus user-home paths. False positives are acceptable;
false negatives leak credentials.

Opt-out via ``PANEL_DEBUG_CLI_OUTPUT=1`` (passes text through
unchanged) — only intended for local debugging.
"""

from __future__ import annotations

import os
import re

# The user's home directory at module-load time. Redacted to ``<HOME>``
# so absolute paths in error messages don't leak the username (still
# common to see ``/Users/alice/Projects/secret-customer-X/...`` in a
# stack trace).
_HOME = os.path.expanduser("~")


def _build_redaction_patterns() -> tuple[tuple[re.Pattern[str], str], ...]:
    """Compile the redaction pattern set once at module load.

    Order matters: the literal-HOME pattern is registered before the
    generic ``/Users/...`` fallback so the operator's own home gets a
    distinct marker and other users' paths get the generic ``<USER>``.
    """

    patterns: list[tuple[re.Pattern[str], str]] = [
        # API key shapes — most providers include the prefix in the key.
        (re.compile(r"sk-(?:ant-)?[A-Za-z0-9_\-]{20,}"), "[REDACTED_API_KEY]"),
        (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[REDACTED_API_KEY]"),
        (re.compile(r"xai-[A-Za-z0-9_\-]{20,}"), "[REDACTED_API_KEY]"),
        # JWT-shaped tokens.
        (
            re.compile(
                r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
            ),
            "[REDACTED_JWT]",
        ),
        # Bearer headers echoed in errors / log lines.
        (re.compile(r"(?i)Bearer\s+[A-Za-z0-9_\-\.=]{16,}"), "Bearer [REDACTED]"),
    ]
    if _HOME and _HOME not in ("/", ""):
        patterns.append((re.compile(re.escape(_HOME)), "<HOME>"))
    # Generic user-home paths for content referencing other identities.
    patterns.append((re.compile(r"/Users/[^/\s'\"]+"), "/Users/<USER>"))
    patterns.append((re.compile(r"/home/[^/\s'\"]+"), "/home/<USER>"))
    patterns.append(
        (
            re.compile(r"C:\\Users\\[^\\\s'\"]+", re.IGNORECASE),
            r"C:\\Users\\<USER>",
        )
    )
    return tuple(patterns)


_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    _build_redaction_patterns()
)


def redact_secrets(text: str) -> str:
    """Strip secret/path patterns from ``text`` without truncating.

    Use this on any locally-collected content (log lines, file contents,
    subprocess stdout/stderr) before it crosses into a panel prompt or
    MCP metadata where a third party might see it. The function is
    intentionally cheap — pattern matching only, no parsing — so it's
    safe to call on every line of a streaming log.

    Returns the input unchanged if ``PANEL_DEBUG_CLI_OUTPUT`` is set,
    a local-development escape hatch.
    """

    if not text or os.environ.get("PANEL_DEBUG_CLI_OUTPUT"):
        return text
    redacted = text
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_and_cap(text: str, *, cap: int) -> str:
    """Bound + redact a string for safe inclusion in MCP metadata.

    Mirrors ``redact_secrets`` but also truncates at ``cap`` chars (with
    a clear marker). Use for clink stdout/stderr/raw_output_file fields
    where a malicious or runaway CLI could otherwise smuggle 50MB
    through a metadata channel.
    """

    if not text:
        return text
    if os.environ.get("PANEL_DEBUG_CLI_OUTPUT"):
        return text
    redacted = redact_secrets(text)
    if len(redacted) > cap:
        return redacted[:cap] + (
            f"\n[…truncated {len(redacted) - cap} chars by metadata cap]"
        )
    return redacted

"""MCP progress notification helper for PAL.

When an MCP client sends a tool call with a `progressToken` in the request
metadata, the server may emit `notifications/progress` while the call is in
flight. PAL uses this to surface live status from long-running tools (clink
subprocesses, streaming provider calls, multi-model orchestration).

This helper is a no-op when there is no active request context or the client
did not request progress, so it is safe to sprinkle anywhere.

A contextvar-scoped sink override is also exposed so background runners (the
TaskManager in `tools/tasks.py`) can capture progress events into an in-memory
buffer instead of trying to forward over MCP — the originating request has
already returned by the time a background task is producing events.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("pal.progress")

# Awaitable callback that consumes a single progress event.
ProgressSink = Callable[[str, float, Optional[float]], Awaitable[None]]

# When set on the current asyncio context, emit_progress routes here instead
# of going to the MCP transport. Background tasks install a sink that appends
# to a TaskRecord so callers can poll for live progress.
_progress_sink: ContextVar[Optional[ProgressSink]] = ContextVar(
    "pal_progress_sink", default=None
)


def set_progress_sink(callback: ProgressSink) -> Token:
    """Install a contextvar-scoped progress sink and return a reset token."""
    return _progress_sink.set(callback)


def reset_progress_sink(token: Token) -> None:
    """Restore a previously installed sink (or clear it)."""
    _progress_sink.reset(token)


async def emit_progress(
    message: str,
    *,
    progress: float,
    total: Optional[float] = None,
) -> bool:
    """Send an MCP progress notification on the current request, if any.

    Routing precedence:
      1. If a sink is installed via set_progress_sink, deliver there. (Used by
         background TaskManager to capture events from tools whose originating
         MCP request has already returned.)
      2. Otherwise look up the active MCP request and send a notification
         only if the client supplied a progressToken.
      3. Otherwise no-op.

    Args:
        message: Short human-readable status (e.g. "codex: reading 12 files").
        progress: Monotonically increasing scalar.
        total: Optional upper bound for `progress`.

    Returns:
        True if delivered (to a sink or via MCP), False if skipped.
    """
    sink = _progress_sink.get()
    if sink is not None:
        try:
            await sink(message, progress, total)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Progress sink raised: %s", exc)
            return False

    try:
        from mcp.server.lowlevel.server import request_ctx  # type: ignore
        ctx = request_ctx.get()
    except (ImportError, LookupError):
        return False

    meta = getattr(ctx, "meta", None)
    if meta is None:
        return False

    token = getattr(meta, "progressToken", None)
    if token is None:
        return False

    try:
        await ctx.session.send_progress_notification(
            progress_token=token,
            progress=progress,
            total=total,
            message=message,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to send progress notification: %s", exc)
        return False

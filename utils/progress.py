"""MCP progress notification helper for PAL.

When an MCP client sends a tool call with a `progressToken` in the request
metadata, the server may emit `notifications/progress` while the call is in
flight. PAL uses this to surface live status from long-running tools (clink
subprocesses, streaming provider calls, multi-model orchestration).

This helper is a no-op when there is no active request context or the client
did not request progress, so it is safe to sprinkle anywhere.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("pal.progress")


async def emit_progress(
    message: str,
    *,
    progress: float,
    total: Optional[float] = None,
) -> bool:
    """Send an MCP progress notification on the current request, if any.

    Args:
        message: Short human-readable status (e.g. "codex: reading 12 files").
        progress: Monotonically increasing scalar. If `total` is None, treat as
            an opaque step counter; otherwise interpret as a fraction.
        total: Optional upper bound for `progress`.

    Returns:
        True if a notification was sent, False if skipped (no request context,
        no progressToken from client, or transport error).
    """
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
    except Exception as exc:
        logger.debug("Failed to send progress notification: %s", exc)
        return False

"""ContextVar-based access to the MCP host session for tools that want to
sample the host LLM (Claude Code) via mcp ``sampling/createMessage``.

Why a contextvar
----------------
- During a normal MCP tool call, ``mcp.server.lowlevel.server.request_ctx``
  is set and ``request_ctx.get().session`` works. Easy.
- For background-task tool calls (start_task → _run → execute_tool), the
  task runs in a different asyncio context spawned by TaskManager. The
  request context from the original MCP call is gone. TaskManager already
  captures the session at start_task time (``record.session``); this
  module exposes that capture to nested code via a contextvar so panel
  can find it without explicit threading through every signature.

Two writers:
  - ``server.execute_tool`` sets the var from request_ctx for live calls
  - ``TaskManager._run`` sets it from the captured ``record.session`` for
    background calls
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


_HOST_SESSION_VAR: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "pal_host_session", default=None
)


def get_host_session() -> Optional[Any]:
    """Return the active MCP ServerSession or None.

    None means: no session is reachable in this context. Callers that need
    sampling should fail closed with a clear "host sampling unavailable"
    message instead of silently degrading."""
    return _HOST_SESSION_VAR.get()


def set_host_session(session: Optional[Any]) -> contextvars.Token:
    """Bind a session into the current context. Returns a token the caller
    must pass to ``reset_host_session`` to undo the bind."""
    return _HOST_SESSION_VAR.set(session)


def reset_host_session(token: contextvars.Token) -> None:
    try:
        _HOST_SESSION_VAR.reset(token)
    except (ValueError, LookupError):
        # Token from a different context — ignore (the caller's cleanup
        # was best-effort anyway).
        pass


def capture_from_request_ctx() -> Optional[Any]:
    """Best-effort grab of the active session from the MCP request context.
    Returns None outside an MCP request (e.g. unit tests, background tasks
    without a propagated session)."""
    try:
        from mcp.server.lowlevel.server import request_ctx  # type: ignore
        ctx = request_ctx.get()
    except Exception:  # noqa: BLE001
        return None
    if ctx is None:
        return None
    return getattr(ctx, "session", None)


def host_supports_sampling(session: Any) -> bool:
    """Best-effort check that the connected MCP client advertised the
    sampling capability. Falls back to True if we can't introspect — the
    actual create_message call will raise cleanly if the host doesn't
    support it, and we want to attempt rather than refuse based on a
    missed handshake field."""
    if session is None:
        return False
    try:
        from mcp.types import ClientCapabilities, SamplingCapability
        cap = ClientCapabilities(sampling=SamplingCapability())
        return bool(session.check_client_capability(cap))
    except Exception as exc:  # noqa: BLE001
        logger.debug("host_supports_sampling: introspection failed (%s); will attempt anyway", exc)
        return True

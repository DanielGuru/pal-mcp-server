"""Shared helpers for parsing ``start_task`` dispatch responses.

``multiaudit`` and ``bugfind`` both call ``execute_tool('start_task', ...)``
and need to distinguish:
  - successful dispatch (``{"status": "started", "task_id": "..."}``)
  - structured refusal (``{"status": "error", "error": "..."}``) — does
    NOT raise, returns a normal JSON payload.

Without proper parsing, a tool that only catches exceptions will report
``{"status": "started", "task_id": null}`` to the user during a refusal
— operational lie at exactly the moment the user is chasing a bug.
Audit-flagged in the bugfind review and again in the multiaudit review
when this helper still lived in ``tools.bugfind`` (cross-tool private
import). Now in ``tools/shared/`` so neither tool depends on the other.
"""

from __future__ import annotations

import json
from typing import Any


def extract_start_status(start_result: list[Any]) -> tuple[str | None, str | None]:
    """Parse start_task's response. Returns ``(status, error_message)``.

    ``start_task`` returns ``{"status": "started", "task_id": "..."}`` on
    success and ``{"status": "error", "error": "..."}`` on refusal
    (admission control, unknown wrapped tool, etc.) WITHOUT raising.
    Callers must check the parsed status — ``None`` means the response
    shape was unparsable, which is itself a contract violation.
    """

    if not start_result:
        return None, "empty start_task response"
    text = getattr(start_result[0], "text", None)
    if not text:
        return None, "start_task response had no text"
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None, "start_task response was not JSON"
    if not isinstance(body, dict):
        return None, "start_task response was not a dict"

    # Direct shape (start_task returns this verbatim)
    if "status" in body:
        return str(body["status"]), str(body.get("error") or "") or None

    # Wrapped-ToolOutput shape — content holds the JSON we want
    content = body.get("content")
    if isinstance(content, str):
        try:
            inner = json.loads(content)
            if isinstance(inner, dict) and "status" in inner:
                return str(inner["status"]), str(inner.get("error") or "") or None
        except (json.JSONDecodeError, ValueError):
            pass

    return None, "start_task response had no status field"


def extract_task_id(start_result: list[Any]) -> str | None:
    """Pull task_id out of a successful start_task response. Returns None
    if not present (caller should already have checked status via
    :func:`extract_start_status` and rejected non-started responses)."""

    if not start_result:
        return None
    text = getattr(start_result[0], "text", None)
    if not text:
        return None
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(body, dict):
        # Direct shape (start_task returns this verbatim)
        if isinstance(body.get("task_id"), str):
            return body["task_id"]
        # Wrapped-ToolOutput shape
        content = body.get("content")
        if isinstance(content, str):
            try:
                inner = json.loads(content)
                if isinstance(inner, dict) and isinstance(inner.get("task_id"), str):
                    return inner["task_id"]
            except (json.JSONDecodeError, ValueError):
                pass
    return None

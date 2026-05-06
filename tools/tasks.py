"""Async background-task pattern for PAL.

Long-running PAL tools (audits, multi-model consensus, large code reviews) are
synchronous from Claude Code's perspective: while they run, the conversation
turn is blocked. The four tools in this module wrap any other PAL tool and
return immediately with a `task_id`, letting Claude continue interacting while
work happens in the background.

Public tools registered with the server:
  start_task  — fire any other PAL tool, return task_id instantly
  task_status — peek at status + recent progress events (instant)
  task_result — fetch finished result; optionally wait briefly for completion
  cancel_task — stop a running task

Tasks live in memory of the PAL process; if PAL restarts, in-flight tasks are
lost. Completed records are retained for a bounded TTL **and** a bounded count
so callers can fetch results late without unbounded memory growth.

Hardenings on the v1 scaffold (informed by Codex's audit):
  - Bounded concurrent tasks (admission control)
  - Periodic GC + count-bounded completed records
  - 'cancelling' state — cancel_task awaits teardown briefly before reporting
  - Strict argument parsing — malformed payloads return structured errors
  - Push notifications on completion via session.send_notification (so callers
    don't need to poll; the host UI is told the task finished)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp.types import (
    LoggingMessageNotification,
    LoggingMessageNotificationParams,
    ServerNotification,
    TextContent,
)

from tools.models import ToolModelCategory
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool

logger = logging.getLogger("pal.tasks")

# How long to retain completed/failed records before garbage collecting.
COMPLETED_TTL_S = 60 * 60  # 1 hour
# Max concurrent running tasks (admission control).
MAX_CONCURRENT_TASKS = 16
# Max retained completed records (FIFO eviction beyond this).
MAX_COMPLETED_RECORDS = 64
# Periodic GC frequency.
GC_INTERVAL_S = 30.0
# Max progress events held per task (rolling buffer; oldest dropped).
PROGRESS_BUFFER_SIZE = 200
# Cap on task_result wait_seconds — never block longer than this even if asked.
MAX_WAIT_SECONDS = 600
# Bound on cancel_task awaiting teardown before responding.
CANCEL_TEARDOWN_GRACE_S = 8.0


# ---------------------------------------------------------------------------
# Argument parsing helpers — explicit "missing vs invalid" handling
# ---------------------------------------------------------------------------


class _BadArg(ValueError):
    """Raised internally when an argument is invalid; callers convert to JSON."""


def _require_str(arguments: dict, key: str) -> str:
    if key not in arguments:
        raise _BadArg(f"missing required field {key!r}")
    val = arguments[key]
    if not isinstance(val, str) or not val:
        raise _BadArg(f"{key!r} must be a non-empty string")
    return val


def _optional_int(arguments: dict, key: str, default: int, *, lo: int = 0, hi: int) -> int:
    raw = arguments.get(key, default)
    if isinstance(raw, bool):  # bool is a subclass of int — reject explicitly
        raise _BadArg(f"{key!r} must be an integer, got bool")
    if not isinstance(raw, int):
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            raise _BadArg(f"{key!r} must be an integer") from None
    if raw < lo or raw > hi:
        raise _BadArg(f"{key!r} must be between {lo} and {hi}")
    return raw


def _optional_float(arguments: dict, key: str, default: float, *, lo: float = 0.0, hi: float) -> float:
    raw = arguments.get(key, default)
    if isinstance(raw, bool):
        raise _BadArg(f"{key!r} must be numeric, got bool")
    if not isinstance(raw, (int, float)):
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            raise _BadArg(f"{key!r} must be numeric") from None
    raw = float(raw)
    if raw != raw or raw == float("inf") or raw == float("-inf"):
        raise _BadArg(f"{key!r} must be finite")
    if raw < lo:
        raw = lo
    if raw > hi:
        raw = hi
    return raw


# ---------------------------------------------------------------------------
# Records and manager
# ---------------------------------------------------------------------------


@dataclass
class TaskRecord:
    task_id: str
    tool_name: str
    arguments: dict
    label: str  # short user-supplied or auto-derived identifier for logs/UI
    status: str  # pending | running | completed | failed | cancelling | cancelled
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result_text: Optional[list[str]] = None  # serialized list[TextContent].text
    error: Optional[str] = None
    # Structured form of `error` when the exception was ToolExecutionError —
    # whose message body is JSON-serialised ToolOutput. Lets automation read
    # status/content/metadata fields without re-parsing the string. Audit
    # finding (codex): pre-fix the only error surface was a stringified
    # JSON-inside-a-traceback-line, hostile to programmatic consumers.
    error_payload: Optional[dict[str, Any]] = None
    progress_events: deque = field(default_factory=lambda: deque(maxlen=PROGRESS_BUFFER_SIZE))
    asyncio_task: Optional[asyncio.Task] = field(default=None, repr=False)
    completion_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Captured at start_task time so we can push a completion notification
    # without an active request_ctx. May be None if start_task was called
    # outside an MCP request (shouldn't normally happen).
    session: Any = field(default=None, repr=False)

    def to_summary(self, *, include_progress: int = 0) -> dict[str, Any]:
        elapsed = None
        if self.started_at is not None:
            ref = self.completed_at if self.completed_at is not None else time.time()
            elapsed = round(ref - self.started_at, 2)
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "tool": self.tool_name,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": elapsed,
        }
        if self.error is not None:
            out["error"] = self.error
        if include_progress:
            recent = list(self.progress_events)[-include_progress:]
            out["progress_events"] = recent
            out["progress_event_count"] = len(self.progress_events)
        return out

    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


class TaskManager:
    """Process-wide registry of background PAL tasks.

    Single-asyncio-thread access only — asyncio runs cooperatively in one
    thread and dict ops are atomic, so no lock is needed.
    """

    _instance: Optional["TaskManager"] = None

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._gc_task: Optional[asyncio.Task] = None

    @classmethod
    def get(cls) -> "TaskManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # -- admission helpers -------------------------------------------------

    def _active_count(self) -> int:
        return sum(1 for r in self._tasks.values() if not r.is_terminal())

    def _ensure_gc_loop(self) -> None:
        if self._gc_task is None or self._gc_task.done():
            self._gc_task = asyncio.create_task(self._gc_loop(), name="pal-task-gc")

    async def _gc_loop(self) -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(GC_INTERVAL_S)
                except asyncio.CancelledError:
                    return
                try:
                    self._gc()
                except Exception:  # noqa: BLE001
                    logger.exception("Task GC iteration raised; continuing")
        except Exception:  # noqa: BLE001
            logger.exception("Task GC loop crashed")

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        tool_name: str,
        arguments: dict,
        label: str,
        *,
        session: Any = None,
    ) -> tuple[Optional[TaskRecord], Optional[str]]:
        """Start a task. Returns (record, error). On admission failure, error is set."""
        active = self._active_count()
        if active >= MAX_CONCURRENT_TASKS:
            return None, (
                f"too_many_active_tasks: {active}/{MAX_CONCURRENT_TASKS} running. "
                "Wait for tasks to finish or cancel one."
            )

        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(
            task_id=task_id,
            tool_name=tool_name,
            arguments=arguments,
            label=label,
            status="pending",
            created_at=time.time(),
            session=session,
        )
        self._tasks[task_id] = record
        record.asyncio_task = asyncio.create_task(
            self._run(record),
            name=f"pal-task-{task_id}",
        )
        self._ensure_gc_loop()
        self._gc()  # opportunistic eviction
        return record, None

    @staticmethod
    def _session_match(record: TaskRecord, session: Any) -> bool:
        """A record is visible to a session if (a) it has no session bound
        (started outside an MCP request, e.g. tests) or (b) the session is the
        same Python object as the one captured at start_task time. Identity
        comparison is used because MCP doesn't expose a stable session id."""
        if record.session is None:
            return True
        if session is None:
            return False
        return record.session is session

    def get_record(
        self,
        task_id: str,
        *,
        session: Any = None,
        require_session: bool = False,
    ) -> Optional[TaskRecord]:
        record = self._tasks.get(task_id)
        if record is None:
            return None
        if require_session and not self._session_match(record, session):
            return None
        return record

    def list_records(self, *, session: Any = None) -> list[TaskRecord]:
        if session is None:
            return list(self._tasks.values())
        return [r for r in self._tasks.values() if self._session_match(r, session)]

    async def cancel(
        self,
        task_id: str,
        *,
        await_teardown: bool = True,
        session: Any = None,
        require_session: bool = False,
    ) -> dict[str, Any]:
        """Cancel a task. Optionally awaits teardown to confirm the subprocess died.

        Returns a dict describing the outcome:
            {ok: True/False, reason: str, status: <task status>}
        """
        record = self._tasks.get(task_id)
        if record is None:
            return {"ok": False, "reason": "unknown_task_id"}
        if require_session and not self._session_match(record, session):
            return {"ok": False, "reason": "not_owner"}
        if record.is_terminal():
            return {"ok": False, "reason": "already_terminal", "status": record.status}
        if record.status not in ("pending", "running", "cancelling"):
            return {"ok": False, "reason": f"unexpected_status:{record.status}"}

        # Mark cancelling immediately so concurrent status calls reflect it.
        record.status = "cancelling"
        record.progress_events.append(
            {"ts": time.time(), "msg": "cancel requested", "progress": 0.0}
        )

        if record.asyncio_task is not None and not record.asyncio_task.done():
            record.asyncio_task.cancel()

        if not await_teardown:
            return {"ok": True, "reason": "cancel_dispatched", "status": record.status}

        try:
            await asyncio.wait_for(
                record.completion_event.wait(),
                timeout=CANCEL_TEARDOWN_GRACE_S,
            )
            return {"ok": True, "reason": "cancelled", "status": record.status}
        except asyncio.TimeoutError:
            return {
                "ok": True,
                "reason": "cancel_in_progress",
                "status": record.status,
                "hint": "Subprocess teardown still running; poll task_status to confirm.",
            }

    async def _run(self, record: TaskRecord) -> None:
        from utils.progress import reset_progress_sink, set_progress_sink

        async def sink(msg: str, progress: float, total: Optional[float]) -> None:
            record.progress_events.append(
                {"ts": time.time(), "msg": msg, "progress": progress}
            )

        token = set_progress_sink(sink)
        record.status = "running"
        record.started_at = time.time()
        record.progress_events.append(
            {"ts": time.time(), "msg": f"task started: {record.tool_name}", "progress": 0.0}
        )

        try:
            # Route through the central dispatcher — model resolution + file
            # size validation MUST run for background-task launches too,
            # otherwise start_task is a quiet way to bypass MCP-boundary
            # checks (the audit's #1 finding).
            from server import execute_tool

            try:
                result = await execute_tool(record.tool_name, record.arguments)
            except KeyError:
                raise ValueError(f"Unknown tool: {record.tool_name!r}")
            record.result_text = [
                getattr(item, "text", str(item)) for item in (result or [])
            ]
            record.status = "completed"
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.error = "task cancelled by request"
            raise
        except Exception as exc:  # noqa: BLE001
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            # Best-effort structured payload for automation. ToolExecutionError
            # carries a JSON-serialised ToolOutput in its message; parse it so
            # callers don't have to peel the string back open.
            try:
                from tools.shared.exceptions import ToolExecutionError as _TEE
                import json as _json
                if isinstance(exc, _TEE):
                    parsed = _json.loads(str(exc))
                    if isinstance(parsed, dict):
                        record.error_payload = parsed
            except Exception:  # noqa: BLE001 — payload is best-effort
                pass
            logger.exception("Background task %s failed", record.task_id)
        finally:
            reset_progress_sink(token)
            record.completed_at = time.time()
            record.progress_events.append(
                {
                    "ts": record.completed_at,
                    "msg": f"task {record.status}",
                    "progress": 999.0,
                }
            )
            record.completion_event.set()
            # Free the cached arguments to help bound memory; caller has the
            # task summary already, no need to keep a copy of the input.
            record.arguments = {}
            # Best-effort push notification so the host UI sees the finish
            # without waiting for a polling task_result call.
            await self._push_completion_notification(record)

    async def _push_completion_notification(self, record: TaskRecord) -> None:
        if record.session is None:
            return
        try:
            elapsed = (
                round(record.completed_at - record.started_at, 2)
                if (record.completed_at and record.started_at)
                else None
            )
            payload = {
                "event": "pal.task.finished",
                "task_id": record.task_id,
                "tool": record.tool_name,
                "label": record.label,
                "status": record.status,
                "elapsed_seconds": elapsed,
                "error": record.error,
            }
            level = "info" if record.status == "completed" else (
                "warning" if record.status == "cancelled" else "error"
            )
            notification = ServerNotification(
                LoggingMessageNotification(
                    method="notifications/message",
                    params=LoggingMessageNotificationParams(
                        level=level,
                        logger="pal.tasks",
                        data=payload,
                    ),
                )
            )
            await record.session.send_notification(notification)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to push completion notification: %s", exc)

    # -- gc ----------------------------------------------------------------

    def _gc(self) -> None:
        """Evict completed records: by TTL first, then by count cap (FIFO)."""
        now = time.time()
        # 1. TTL eviction
        cutoff = now - COMPLETED_TTL_S
        stale = [
            tid
            for tid, rec in self._tasks.items()
            if rec.completed_at is not None and rec.completed_at < cutoff
        ]
        for tid in stale:
            self._tasks.pop(tid, None)

        # 2. Count cap on completed records (oldest first)
        completed = [
            (rec.completed_at or 0.0, tid)
            for tid, rec in self._tasks.items()
            if rec.is_terminal()
        ]
        completed.sort()
        excess = len(completed) - MAX_COMPLETED_RECORDS
        for _, tid in completed[: max(0, excess)]:
            self._tasks.pop(tid, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


def _capture_session() -> Any:
    """Return the live MCP session for this request, or None if outside one."""
    try:
        from mcp.server.lowlevel.server import request_ctx  # type: ignore
        return request_ctx.get().session
    except (ImportError, LookupError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Tool: start_task
# ---------------------------------------------------------------------------


_META_TOOLS = frozenset({"start_task", "task_status", "task_result", "cancel_task"})


class StartTaskTool(BaseTool):
    """Fire any other PAL tool in the background, return a task_id immediately."""

    def get_name(self) -> str:
        return "start_task"

    def get_description(self) -> str:
        return (
            "Run any other PAL tool in the background and return immediately with a "
            "task_id. Use for long calls (audits, consensus across models, code "
            "reviews) so the conversation is not blocked. The host receives a "
            "notifications/message push when the task finishes; you can also poll "
            "task_status / task_result to fetch progress and the final response."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "description": "Name of the PAL tool to execute (e.g. 'clink', 'chat', 'consensus', 'codereview').",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments object passed verbatim to the wrapped tool.",
                    "additionalProperties": True,
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for this task (shown in status output).",
                },
            },
            "required": ["tool", "arguments"],
            "additionalProperties": False,
        }

    def get_annotations(self) -> Optional[dict[str, Any]]:
        return {"readOnlyHint": False, "openWorldHint": True}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.FAST_RESPONSE

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            tool_name = _require_str(arguments, "tool")
        except _BadArg as exc:
            return _json_response({"status": "error", "error": str(exc)})

        if "arguments" not in arguments:
            return _json_response({"status": "error", "error": "missing 'arguments' field"})
        forward_args = arguments["arguments"]
        if not isinstance(forward_args, dict):
            return _json_response(
                {"status": "error", "error": "'arguments' must be an object (got %s)" % type(forward_args).__name__}
            )

        if tool_name in _META_TOOLS:
            return _json_response(
                {
                    "status": "error",
                    "error": f"refusing to wrap meta-tool {tool_name!r} — would create a recursion hazard",
                }
            )

        from server import TOOLS

        if tool_name not in TOOLS:
            return _json_response(
                {
                    "status": "error",
                    "error": f"unknown tool {tool_name!r}",
                    "known": sorted(TOOLS.keys()),
                }
            )

        label = arguments.get("label") or tool_name
        if not isinstance(label, str):
            return _json_response({"status": "error", "error": "'label' must be a string"})

        record, err = TaskManager.get().start(
            tool_name, forward_args, label, session=_capture_session()
        )
        if err is not None:
            return _json_response({"status": "error", "error": err})

        return _json_response(
            {
                "status": "started",
                "task_id": record.task_id,
                "tool": record.tool_name,
                "label": record.label,
                "hint": (
                    "Conversation is unblocked. The host will receive a "
                    "notifications/message push when this task finishes; you can "
                    "also poll task_status or call task_result(task_id, wait_seconds=N)."
                ),
            }
        )


# ---------------------------------------------------------------------------
# Tool: task_status
# ---------------------------------------------------------------------------


class TaskStatusTool(BaseTool):
    """Peek at a background task's status and recent progress events."""

    def get_name(self) -> str:
        return "task_status"

    def get_description(self) -> str:
        return (
            "Return the current status of a background task started via start_task, "
            "including recent progress events. Instant — does not wait. Pass "
            "task_id='all' to list every known task."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task id from start_task, or 'all' to list every task.",
                },
                "events": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": PROGRESS_BUFFER_SIZE,
                    "description": "How many recent progress events to include (default 10).",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        }

    def get_annotations(self) -> Optional[dict[str, Any]]:
        return {"readOnlyHint": True}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.FAST_RESPONSE

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            task_id = _require_str(arguments, "task_id")
            events = _optional_int(arguments, "events", 10, lo=0, hi=PROGRESS_BUFFER_SIZE)
        except _BadArg as exc:
            return _json_response({"status": "error", "error": str(exc)})

        manager = TaskManager.get()
        session = _capture_session()
        if task_id == "all":
            return _json_response(
                {
                    "status": "ok",
                    "tasks": [
                        r.to_summary(include_progress=events)
                        for r in manager.list_records(session=session)
                    ],
                }
            )
        record = manager.get_record(task_id, session=session, require_session=True)
        if record is None:
            return _json_response({"status": "error", "error": f"unknown task_id {task_id!r}"})
        return _json_response({"status": "ok", "task": record.to_summary(include_progress=events)})


# ---------------------------------------------------------------------------
# Tool: task_result
# ---------------------------------------------------------------------------


class TaskResultTool(BaseTool):
    """Fetch the result of a background task; optionally wait briefly."""

    def get_name(self) -> str:
        return "task_result"

    def get_description(self) -> str:
        return (
            "Fetch the final response from a background task started via start_task. "
            "If the task has not finished and wait_seconds > 0, blocks up to that "
            "many seconds for it to complete. Returns the wrapped tool's output verbatim."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task id from start_task.",
                },
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": MAX_WAIT_SECONDS,
                    "description": (
                        "If the task has not finished, wait up to this many seconds "
                        "for completion. 0 = no wait (return current state immediately). "
                        f"Capped at {MAX_WAIT_SECONDS}s."
                    ),
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        }

    def get_annotations(self) -> Optional[dict[str, Any]]:
        return {"readOnlyHint": True}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.FAST_RESPONSE

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            task_id = _require_str(arguments, "task_id")
            wait_seconds = _optional_float(
                arguments, "wait_seconds", 0.0, lo=0.0, hi=MAX_WAIT_SECONDS
            )
        except _BadArg as exc:
            return _json_response({"status": "error", "error": str(exc)})

        record = TaskManager.get().get_record(task_id, session=_capture_session(), require_session=True)
        if record is None:
            return _json_response({"status": "error", "error": f"unknown task_id {task_id!r}"})

        if not record.is_terminal() and wait_seconds > 0:
            try:
                await asyncio.wait_for(record.completion_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass

        if not record.is_terminal():
            return _json_response(
                {
                    "status": "still_running",
                    "task": record.to_summary(include_progress=10),
                    "hint": "Call again with a longer wait_seconds, or use task_status to peek.",
                }
            )

        payload: dict[str, Any] = {
            "status": record.status,
            "task": record.to_summary(include_progress=0),
        }
        if record.status == "completed":
            payload["result"] = record.result_text
        if record.error is not None:
            payload["error"] = record.error
        if record.error_payload is not None:
            payload["error_payload"] = record.error_payload
        return _json_response(payload)


# ---------------------------------------------------------------------------
# Tool: cancel_task
# ---------------------------------------------------------------------------


class CancelTaskTool(BaseTool):
    """Stop a running background task; await teardown briefly before responding."""

    def get_name(self) -> str:
        return "cancel_task"

    def get_description(self) -> str:
        return (
            "Cancel a running background task started via start_task. The task is "
            f"transitioned to 'cancelling' and we await teardown for up to "
            f"{CANCEL_TEARDOWN_GRACE_S}s. The response indicates whether teardown "
            "completed within that window."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task id from start_task.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        }

    def get_annotations(self) -> Optional[dict[str, Any]]:
        return {"readOnlyHint": False}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.FAST_RESPONSE

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            task_id = _require_str(arguments, "task_id")
        except _BadArg as exc:
            return _json_response({"status": "error", "error": str(exc)})

        session = _capture_session()
        outcome = await TaskManager.get().cancel(
            task_id, session=session, require_session=True
        )
        record = TaskManager.get().get_record(task_id, session=session, require_session=True)
        return _json_response(
            {
                **outcome,
                "task": record.to_summary(include_progress=5) if record else None,
            }
        )

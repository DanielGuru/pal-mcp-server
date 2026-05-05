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
lost. Completed records are retained for a bounded TTL so callers can fetch
results late without races.
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

from mcp.types import TextContent

from tools.models import ToolModelCategory
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool

logger = logging.getLogger("pal.tasks")

# How long to retain completed/failed records before garbage-collecting them.
COMPLETED_TTL_S = 60 * 60  # 1 hour
# Max progress events held per task (rolling buffer; oldest dropped).
PROGRESS_BUFFER_SIZE = 200
# Cap on task_result wait_seconds — never block longer than this even if asked.
MAX_WAIT_SECONDS = 600


# ---------------------------------------------------------------------------
# Records and manager
# ---------------------------------------------------------------------------


@dataclass
class TaskRecord:
    task_id: str
    tool_name: str
    arguments: dict
    label: str  # short user-supplied or auto-derived identifier for logs/UI
    status: str  # pending | running | completed | failed | cancelled
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result_text: Optional[list[str]] = None  # serialized list[TextContent].text
    error: Optional[str] = None
    progress_events: deque = field(default_factory=lambda: deque(maxlen=PROGRESS_BUFFER_SIZE))
    asyncio_task: Optional[asyncio.Task] = field(default=None, repr=False)
    completion_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def to_summary(self, *, include_progress: int = 0) -> dict[str, Any]:
        """Summary safe to send back to the client."""
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


class TaskManager:
    """Process-wide registry of background PAL tasks.

    Single-asyncio-thread access only — no locking around the dict because
    asyncio runs cooperatively in one thread and dict ops are atomic.
    """

    _instance: Optional["TaskManager"] = None

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}

    @classmethod
    def get(cls) -> "TaskManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def start(self, tool_name: str, arguments: dict, label: str) -> TaskRecord:
        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(
            task_id=task_id,
            tool_name=tool_name,
            arguments=arguments,
            label=label,
            status="pending",
            created_at=time.time(),
        )
        self._tasks[task_id] = record
        record.asyncio_task = asyncio.create_task(
            self._run(record),
            name=f"pal-task-{task_id}",
        )
        # Best-effort GC of stale completed records.
        self._gc()
        return record

    def get_record(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def list_records(self) -> list[TaskRecord]:
        return list(self._tasks.values())

    async def cancel(self, task_id: str) -> bool:
        record = self._tasks.get(task_id)
        if record is None:
            return False
        if record.status not in ("pending", "running"):
            return False
        if record.asyncio_task is not None and not record.asyncio_task.done():
            record.asyncio_task.cancel()
            # Don't await the cancel — let the runner finalize state in its
            # own finally block. The caller can poll task_status.
        return True

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
            # Local import to avoid circular dep with server.py
            from server import TOOLS

            tool = TOOLS.get(record.tool_name)
            if tool is None:
                raise ValueError(f"Unknown tool: {record.tool_name!r}")

            # The wrapped tool's execute returns list[TextContent]
            result = await tool.execute(record.arguments)
            record.result_text = [
                getattr(item, "text", str(item)) for item in (result or [])
            ]
            record.status = "completed"
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.error = "task cancelled by request"
            # Re-raise so the asyncio.Task itself is marked cancelled.
            raise
        except Exception as exc:  # noqa: BLE001
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
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

    def _gc(self) -> None:
        """Remove completed records older than COMPLETED_TTL_S."""
        cutoff = time.time() - COMPLETED_TTL_S
        stale = [
            tid
            for tid, rec in self._tasks.items()
            if rec.completed_at is not None and rec.completed_at < cutoff
        ]
        for tid in stale:
            self._tasks.pop(tid, None)


# ---------------------------------------------------------------------------
# Helper to render JSON output as a single TextContent
# ---------------------------------------------------------------------------


def _json_response(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


# ---------------------------------------------------------------------------
# Tool: start_task
# ---------------------------------------------------------------------------


class StartTaskTool(BaseTool):
    """Fire any other PAL tool in the background, return a task_id immediately."""

    def get_name(self) -> str:
        return "start_task"

    def get_description(self) -> str:
        return (
            "Run any other PAL tool in the background and return immediately with a "
            "task_id. Use for long calls (audits, consensus across models, code "
            "reviews) so the conversation is not blocked. Poll task_status / "
            "task_result to fetch progress and the final response."
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
        tool_name = arguments.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return _json_response({"status": "error", "error": "missing 'tool'"})
        forward_args = arguments.get("arguments") or {}
        if not isinstance(forward_args, dict):
            return _json_response({"status": "error", "error": "'arguments' must be an object"})
        if tool_name in {"start_task", "task_status", "task_result", "cancel_task"}:
            return _json_response(
                {
                    "status": "error",
                    "error": f"refusing to wrap meta-tool {tool_name!r} — would create a recursion hazard",
                }
            )

        # Validate the wrapped tool exists before scheduling.
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
        record = TaskManager.get().start(tool_name, forward_args, label)
        return _json_response(
            {
                "status": "started",
                "task_id": record.task_id,
                "tool": record.tool_name,
                "label": record.label,
                "hint": (
                    "Call task_status or task_result with this task_id. "
                    "Conversation is unblocked while it runs."
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
        task_id = arguments.get("task_id")
        events = int(arguments.get("events") or 10)
        if not isinstance(task_id, str) or not task_id:
            return _json_response({"status": "error", "error": "missing 'task_id'"})

        manager = TaskManager.get()
        if task_id == "all":
            return _json_response(
                {
                    "status": "ok",
                    "tasks": [r.to_summary(include_progress=events) for r in manager.list_records()],
                }
            )
        record = manager.get_record(task_id)
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
        task_id = arguments.get("task_id")
        wait_seconds = float(arguments.get("wait_seconds") or 0)
        wait_seconds = max(0.0, min(wait_seconds, MAX_WAIT_SECONDS))
        if not isinstance(task_id, str) or not task_id:
            return _json_response({"status": "error", "error": "missing 'task_id'"})

        record = TaskManager.get().get_record(task_id)
        if record is None:
            return _json_response({"status": "error", "error": f"unknown task_id {task_id!r}"})

        if record.status in ("pending", "running") and wait_seconds > 0:
            try:
                await asyncio.wait_for(record.completion_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass

        if record.status in ("pending", "running"):
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
        return _json_response(payload)


# ---------------------------------------------------------------------------
# Tool: cancel_task
# ---------------------------------------------------------------------------


class CancelTaskTool(BaseTool):
    """Stop a running background task."""

    def get_name(self) -> str:
        return "cancel_task"

    def get_description(self) -> str:
        return "Cancel a running background task started via start_task. No-op if already finished."

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
        task_id = arguments.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return _json_response({"status": "error", "error": "missing 'task_id'"})
        ok = await TaskManager.get().cancel(task_id)
        record = TaskManager.get().get_record(task_id)
        return _json_response(
            {
                "status": "cancelled" if ok else "noop",
                "task": record.to_summary(include_progress=5) if record else None,
            }
        )

"""Async background-task pattern for Panel.

Long-running Panel tools (audits, multi-model consensus, large code reviews) are
synchronous from Claude Code's perspective: while they run, the conversation
turn is blocked. The four tools in this module wrap any other Panel tool and
return immediately with a `task_id`, letting Claude continue interacting while
work happens in the background.

Public tools registered with the server:
  start_task  — fire any other Panel tool, return task_id instantly
  task_status — peek at status + recent progress events (instant)
  task_result — fetch finished result; optionally wait briefly for completion
  cancel_task — stop a running task

Tasks live in memory of the Panel process; if Panel restarts, in-flight tasks are
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
import os
import re
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

logger = logging.getLogger("panel.tasks")


def _persist_task(record: "TaskRecord") -> None:
    """Best-effort write of task lifecycle to the execution graph DB.
    Survives Panel restart so task_result(task_id) keeps working for
    completed tasks. In-flight tasks die with the process — we don't
    pretend otherwise — but final outputs persist.

    Swallows everything: persistence failure must NOT break the live
    task path. The graph itself is observability, not load-bearing."""
    try:
        from utils.execution_graph import get_graph
        graph = get_graph()
        if graph is None:
            return
        result_json = (
            json.dumps(record.result_text)
            if record.status == "completed" and record.result_text is not None
            else None
        )
        # Bound stored payloads so a single huge tool output (e.g. a
        # 200KB panel transcript) doesn't bloat the tasks table.
        # _SNAPSHOT_CAP mirrors the cap on runs.result_json. Round-3
        # panel-flagged: previously unbounded.
        if result_json is not None:
            try:
                from utils.execution_graph import _SNAPSHOT_CAP
            except ImportError:  # pragma: no cover
                _SNAPSHOT_CAP = 16384
            if len(result_json) > _SNAPSHOT_CAP:
                result_json = result_json[:_SNAPSHOT_CAP] + "…[truncated]"
        # Best-effort linkage to the graph run that backed this task.
        # We don't carry the run_id through execute_tool's contextvar
        # boundary directly (that would mean threading a hook through
        # generic dispatch code). Instead, after the task starts, we
        # query the graph for the most recent root run whose tool name
        # matches the task and whose start_ts is >= the task's start.
        # For a single-task-running-one-tool setup this is correct
        # essentially always; if it ever mislinks, the result_json is
        # the authoritative payload anyway.
        run_id: Optional[str] = None
        if record.started_at is not None:
            try:
                rows = graph.list_runs(limit=20, tool_name=record.tool_name)
                for r in rows:
                    if r.get("parent_run_id"):
                        continue  # only roots
                    started = r.get("started_at") or 0.0
                    if started >= record.started_at - 1.0:
                        run_id = r.get("run_id")
                        break
            except Exception:  # noqa: BLE001
                pass
        graph.upsert_task(
            record.task_id,
            tool=record.tool_name,
            label=record.label,
            run_id=run_id,
            status=record.status,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            result_json=result_json,
            error=record.error,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("task persistence failed: %s", exc)

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
# Cap on task_result wait_seconds. Hard ceiling on how long any single
# task_result call can block the host LLM's turn. 30s default keeps the
# conversation channel responsive to user input even mid-audit (the host can't
# receive new messages while a tool call is in-flight). Override via
# PANEL_TASK_WAIT_CAP_S only when running headless / non-interactive.
import os as _os
try:
    MAX_WAIT_SECONDS = max(1.0, float(_os.environ.get("PANEL_TASK_WAIT_CAP_S", "30")))
except (TypeError, ValueError):
    MAX_WAIT_SECONDS = 30.0
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
    """Process-wide registry of background Panel tasks.

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
            self._gc_task = asyncio.create_task(self._gc_loop(), name="panel-task-gc")

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
        _persist_task(record)  # status=pending on creation
        record.asyncio_task = asyncio.create_task(
            self._run(record),
            name=f"panel-task-{task_id}",
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
        from utils.host_session import reset_host_session, set_host_session
        from utils.progress import reset_progress_sink, set_progress_sink

        async def sink(msg: str, progress: float, total: Optional[float]) -> None:
            record.progress_events.append(
                {"ts": time.time(), "msg": msg, "progress": progress}
            )

        token = set_progress_sink(sink)
        # Propagate the MCP session captured at start_task time so any nested
        # tool that needs sampling (panel's 'host' agent) can find it via the
        # contextvar — request_ctx isn't valid in this background asyncio task.
        host_token = set_host_session(record.session) if record.session is not None else None
        record.status = "running"
        record.started_at = time.time()
        record.progress_events.append(
            {"ts": time.time(), "msg": f"task started: {record.tool_name}", "progress": 0.0}
        )
        _persist_task(record)

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
            if host_token is not None:
                reset_host_session(host_token)
            record.completed_at = time.time()
            record.progress_events.append(
                {
                    "ts": record.completed_at,
                    "msg": f"task {record.status}",
                    "progress": 999.0,
                }
            )
            record.completion_event.set()
            _persist_task(record)  # final lifecycle write — survives restart
            # Free the cached arguments to help bound memory; caller has the
            # task summary already, no need to keep a copy of the input.
            record.arguments = {}
            # Drop a completion marker into ~/.panel/inbox/ so a Claude Code
            # ``Stop`` / ``UserPromptSubmit`` hook (installed by
            # ``panel-install-hooks``) can wake the model with a
            # ``<system-reminder>`` instead of forcing the agent to poll
            # ``task_result``. Best-effort: never fails the task path.
            self._write_inbox_marker(record)
            # Best-effort push notification so the host UI sees the finish
            # without waiting for a polling task_result call. Note: Claude
            # Code currently strips ``notifications/message`` from the
            # model's context — this is host telemetry, not a wake-up.
            # Push notifications to the agent flow through the inbox above.
            await self._push_completion_notification(record)

    def _write_inbox_marker(self, record: TaskRecord) -> None:
        """Atomic write of a completion marker to ``~/.panel/inbox/``. The
        Claude Code drain hook (installed via ``panel-install-hooks``) reads
        these and injects a system reminder when the agent next runs.

        Two enriching pieces beyond the bare lifecycle metadata:

        - ``run_id``: the panel/ask_panel/multiaudit/bugfind run id, pulled
          from the result JSON. Lets the system-reminder point the model
          at ``run_tree(run_id, mode='transcript')`` for the full debate.
        - ``transcript_digest``: a compact summary of the panel verdict
          (judge headline + each panelist's final 1-line take + the
          combined recommended-actions list). Inlined into the wake-up
          system-reminder so the model lands already knowing what was
          said, without a follow-up tool call."""
        try:
            from utils.task_inbox import write_completion_marker

            elapsed = (
                round(record.completed_at - record.started_at, 2)
                if (record.completed_at and record.started_at)
                else None
            )
            run_id, digest = _extract_run_id_and_digest(
                record.tool_name, record.result_text
            )
            write_completion_marker(
                task_id=record.task_id,
                tool=record.tool_name,
                label=record.label,
                status=record.status,
                created_at=record.created_at,
                completed_at=record.completed_at,
                elapsed_seconds=elapsed,
                run_id=run_id,
                error=record.error,
                transcript_digest=digest,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never fail
            logger.debug("inbox marker write failed for %s: %s", record.task_id, exc)

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
                "event": "panel.task.finished",
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
                        logger="panel.tasks",
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
# Inbox-marker enrichment: extract run_id + a compact transcript digest from a
# panel-family tool's result so the asyncRewake wake-up can carry the actual
# panel takeaway instead of just "task X finished".
# ---------------------------------------------------------------------------


_DIGEST_MAX_PANELISTS = 8
_DIGEST_MAX_ACTIONS = 20
# Per-field char caps. Panelist text is UNTRUSTED model output and lands
# inside a system-reminder we inject into the next turn. Without bounds an
# adversarial / runaway panelist could push tens of KB into our context, or
# craft <system-reminder> tags / secret-shaped strings to manipulate the
# model. Sanitisation still applies even with the bumped budget.
#
# The user explicitly asked for the full per-round, per-panelist transcript
# inline in the wake-up — same content as the live web viewer page —
# rather than a one-line summary that forces a follow-up `run_tree`
# call. Budget sized for codex/claude doing deep file investigation on
# big PRs, where individual round-1 responses routinely hit 10-15 KB:
#   per-panelist body 16 KB × 4 panelists × 3 rounds = 192 KB
#   judge body 16 KB
#   total cap 200 KB (with headroom for headers, recommended actions)
# Override via PANEL_DIGEST_TOTAL_CAP / PANEL_DIGEST_PANELIST_BODY_CAP /
# PANEL_DIGEST_JUDGE_BODY_CAP for operators who want a tighter context
# cost. Source-side cap (SUMMARY_RESPONSE_EXCERPT_CHARS in panel.py) is
# bumped in lockstep so the digest cap isn't downstream of a smaller
# excerpt.
_DIGEST_HEADLINE_CAP = 800
_DIGEST_PANELIST_LINE_CAP = 300   # one-line structured headline (above body)
_DIGEST_PANELIST_BODY_CAP = int(
    os.environ.get("PANEL_DIGEST_PANELIST_BODY_CAP", "16000")
)
_DIGEST_JUDGE_BODY_CAP = int(
    os.environ.get("PANEL_DIGEST_JUDGE_BODY_CAP", "16000")
)
_DIGEST_ACTION_CAP = 800
_DIGEST_TOTAL_CAP = int(
    os.environ.get("PANEL_DIGEST_TOTAL_CAP", "200000")
)


def _sanitise_untrusted(text: str, *, cap: int) -> str:
    """Apply to ANY string that originated from a panelist's response
    before inlining it into the wake-up system-reminder.

    Defenses:
      - Length cap (cap chars).
      - Strip control chars except \\n (would break the reminder layout
        or smuggle terminal escape sequences).
      - Neutralise ``<system-reminder>`` tags so a panelist can't inject a
        nested reminder block that the host would parse as authoritative.
      - Redact secret shapes via utils.redaction so a panelist who
        echoed an API key / Bearer header / JWT in their summary can't
        leak it through us.
    """
    if not isinstance(text, str):
        return ""
    if not text:
        return ""
    # 1. Cap first so all subsequent work runs on bounded data.
    if len(text) > cap:
        text = text[: cap - 1] + "…"
    # 2. Strip control chars (keep \n, \t).
    text = "".join(
        ch for ch in text if ch == "\n" or ch == "\t" or ch.isprintable()
    )
    # 3. Neutralise system-reminder tags. Both opening and closing forms,
    # case-insensitive. Panelists don't need to emit these in summaries
    # ever — replacing with a visible escape preserves their intent
    # without giving us a nested reminder.
    text = re.sub(
        r"</?system-reminder>",
        lambda m: m.group(0).replace("<", "‹").replace(">", "›"),
        text,
        flags=re.IGNORECASE,
    )
    # 4. Redact secret shapes — best-effort, uses Panel's existing
    # redaction so the rules stay consistent with clink stdout / log
    # tail handling.
    try:
        from utils.redaction import redact_secrets

        text = redact_secrets(text)
    except Exception:  # noqa: BLE001 — never fail the task path on this
        pass
    return text.strip()


def _cap_total(text: str, cap: int = _DIGEST_TOTAL_CAP) -> str:
    """Hard ceiling on the whole formatted digest. If we exceed, truncate
    with a visible marker so the model knows it was cut."""
    if len(text) <= cap:
        return text
    return text[: cap - len("\n\n[…digest truncated]")] + "\n\n[…digest truncated]"


def _extract_run_id_and_digest(
    tool_name: str, result_text: Optional[list[str]]
) -> tuple[Optional[str], Optional[str]]:
    """Pull ``(run_id, digest)`` from a panel-family tool's result. Returns
    ``(None, None)`` for non-panel tools or unparseable payloads."""
    if not result_text:
        return None, None
    try:
        payload = json.loads(result_text[0])
    except (ValueError, IndexError, TypeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None

    # panel / ask_panel / multiaudit / bugfind all surface this field.
    run_id = payload.get("panel_run_id") or payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        run_id = None

    digest = _format_completion_digest(payload)
    return run_id, digest


def _render_panelist_block(p: Any, *, round_num: Optional[int]) -> Optional[str]:
    """Render one panelist's contribution as a markdown block: header line
    with metadata + structured headline, then the full prose body.

    Used by both the per-round path (iterating debate_history) and the
    legacy single-round path (panelists list). round_num is included in
    the header when present so the wake-up reads as a chronological
    transcript matching the live web viewer page.
    """
    if not isinstance(p, dict):
        return None
    label = _sanitise_untrusted(
        str(p.get("label") or p.get("agent") or "?"),
        cap=80,
    ) or "?"
    cost = _sanitise_untrusted(str(p.get("cost_tier") or "?"), cap=40) or "?"
    duration = p.get("duration_s")
    duration_str = (
        f"{duration:.0f}s" if isinstance(duration, (int, float)) else "?"
    )
    round_prefix = f"round {round_num} · " if round_num else ""

    if not p.get("ok"):
        err = _sanitise_untrusted(str(p.get("error") or "failed"), cap=160)
        return f"### {round_prefix}{label} [{cost}, {duration_str}] ✗\n{err}"

    summary = p.get("summary") or {}
    verdict = _sanitise_untrusted(str(summary.get("verdict") or "?"), cap=40) or "?"
    severity = _sanitise_untrusted(str(summary.get("severity") or "?"), cap=40) or "?"
    ph_raw = (summary.get("headline") or "").strip()
    ph = _sanitise_untrusted(ph_raw, cap=_DIGEST_PANELIST_LINE_CAP)
    header = f"### {round_prefix}{label} [{verdict}/{severity}, {duration_str}, {cost}]"
    if ph:
        header += f"\n**{ph}**"
    body_raw = (p.get("response_excerpt") or "").strip()
    if body_raw:
        body = _sanitise_untrusted(body_raw, cap=_DIGEST_PANELIST_BODY_CAP)
        if body:
            return f"{header}\n\n{body}"
    return header


def _format_completion_digest(payload: dict[str, Any]) -> Optional[str]:
    """Render the FINAL panel takeaway as a compact text block. We deliberately
    show ONLY the synthesised result — judge headline, each panelist's final
    structured summary (verdict / severity / one-line headline), and the
    deduped recommended-actions list. Round-by-round debate history is left
    in the graph; the model can fetch it via run_tree if it wants to dig in.

    Every text field that originated from a panelist's response (headline,
    panelist headlines, recommended actions) is run through
    ``_sanitise_untrusted`` before inlining: per-field cap, control-char
    strip, ``<system-reminder>`` tag neutralisation, secret-shape redaction.
    Without that, an adversarial or runaway panelist could push secrets
    or nested reminder tags into our wake-up context.
    """
    lines: list[str] = []

    headline_raw = (payload.get("headline") or "").strip()
    headline = _sanitise_untrusted(headline_raw, cap=_DIGEST_HEADLINE_CAP)
    if headline:
        lines.append(f"**Verdict:** {headline}")

    # Prefer the per-round debate_history when present (debate_rounds>=1).
    # Otherwise fall back to the canonical final-round panelists list. The
    # debate_history rendering matches what the user sees on the live web
    # viewer page: each round, each panelist, full body.
    debate_history = payload.get("debate_history")
    panelists = payload.get("panelists") or []

    if isinstance(debate_history, list) and debate_history:
        all_blocks: list[str] = []
        for entry in debate_history:
            if not isinstance(entry, dict):
                continue
            round_num = entry.get("round")
            round_panelists = entry.get("panelists") or []
            if not isinstance(round_panelists, list):
                continue
            for p in round_panelists[:_DIGEST_MAX_PANELISTS]:
                block = _render_panelist_block(p, round_num=round_num)
                if block:
                    all_blocks.append(block)
        if all_blocks:
            lines.append("\n**Panel transcript:**")
            lines.append("\n\n".join(all_blocks))
    elif isinstance(panelists, list) and panelists:
        blocks: list[str] = []
        for p in panelists[:_DIGEST_MAX_PANELISTS]:
            block = _render_panelist_block(p, round_num=None)
            if block:
                blocks.append(block)
        if blocks:
            lines.append("\n**Panelists:**")
            lines.append("\n\n".join(blocks))

    judge = payload.get("judge")
    if isinstance(judge, dict):
        if judge.get("ok"):
            judge_label = _sanitise_untrusted(
                str(judge.get("agent") or "?"), cap=40
            ) or "?"
            jh = _sanitise_untrusted(
                (judge.get("headline") or "").strip(),
                cap=_DIGEST_HEADLINE_CAP,
            )
            # Body — the judge's full synthesis prose. The viewer renders
            # this verbatim under the `judge:<agent>` heading; mirror that
            # shape here so the wake-up matches the page the user sees.
            body_raw = (judge.get("response_excerpt") or judge.get("response") or "").strip()
            body = _sanitise_untrusted(body_raw, cap=_DIGEST_JUDGE_BODY_CAP)
            if body:
                lines.append(f"\n### judge:{judge_label}\n\n{body}")
            elif jh and jh != headline:
                # No body available — fall back to the headline so the
                # user at least sees the judge's verdict line.
                lines.append(f"\n**Judge ({judge_label}):** {jh}")
        else:
            err = _sanitise_untrusted(
                str(judge.get("error") or "failed"), cap=160
            )
            lines.append(f"\n**Judge:** ✗ {err}")

    actions = _collect_recommended_actions(panelists)
    if actions:
        lines.append("\n**Recommended actions (combined):**")
        for a in actions[:_DIGEST_MAX_ACTIONS]:
            sanitised = _sanitise_untrusted(a, cap=_DIGEST_ACTION_CAP)
            if sanitised:
                lines.append(f"- {sanitised}")

    if not lines:
        return None
    return _cap_total("\n".join(lines))


def _collect_recommended_actions(panelists: Any) -> list[str]:
    """Flatten + dedupe the per-panelist ``recommended_actions`` lists.
    Each entry in the structured summary is a multi-line string of bullets;
    we split on newlines, strip prefixes, and dedupe while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    if not isinstance(panelists, list):
        return out
    for p in panelists:
        if not isinstance(p, dict):
            continue
        summary = p.get("summary") or {}
        for raw in summary.get("recommended_actions") or []:
            if not isinstance(raw, str):
                continue
            for line in raw.splitlines():
                cleaned = line.strip()
                if cleaned.startswith(("-", "*", "•")):
                    cleaned = cleaned[1:].lstrip()
                if not cleaned:
                    continue
                if cleaned in seen:
                    continue
                seen.add(cleaned)
                out.append(cleaned)
    return out


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

# Tools that are ALREADY async by design — their execute() body dispatches
# a long-running panel via start_task internally and returns immediately
# with a task_id pointing at the inner panel run. Wrapping them in another
# start_task creates a confusing 0s "completed" hook because the OUTER
# wrapper genuinely completes in 0s (the inner tool body returns fast),
# while the actual debate runs under a different (inner) task_id that the
# caller never knows to poll. Reject loudly and tell the caller the right
# shape, since terse-prompted LLMs default to wrapping defensively.
#
# ask_panel is NOT in this list — it's a synchronous fan-out (PanelTool.execute
# awaits the gather of panelists) that callers SHOULD wrap in start_task.
_SELF_ASYNC_TOOLS = frozenset({"multiaudit", "bugfind"})


class StartTaskTool(BaseTool):
    """Fire any other Panel tool in the background, return a task_id immediately."""

    def get_name(self) -> str:
        return "start_task"

    def get_description(self) -> str:
        return (
            "Run any other Panel tool in the background and return immediately with a "
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
                    "description": "Name of the Panel tool to execute (e.g. 'clink', 'chat', 'consensus', 'codereview').",
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

        if tool_name in _SELF_ASYNC_TOOLS:
            return _json_response(
                {
                    "status": "error",
                    "error": (
                        f"{tool_name!r} is already async by design — call it "
                        "directly, do NOT wrap in start_task. The tool returns "
                        "immediately with a task_id pointing at the inner panel "
                        "run; poll that task_id (not start_task's). Wrapping "
                        "creates a 0s 'completed' hook that masks the real run."
                    ),
                    "correct_invocation": {
                        "tool": tool_name,
                        "arguments": forward_args,
                    },
                    "self_async_tools": sorted(_SELF_ASYNC_TOOLS),
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
            "task_id='all' to list every known task. "
            "**After calling this, ALWAYS surface the meaningful content to "
            "the user as plain text in your reply** — status, elapsed seconds, "
            "and the most recent 2-3 progress event labels. The tool result is "
            "JSON for your consumption; the user sees nothing unless you write "
            "it. Bad: call task_status, get JSON back, end turn silent. Good: "
            "call task_status, then say 'panel running 4m23s, codex done, "
            "claude streaming' or similar. End the turn with that text — "
            "don't loop into another silent poll."
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
            "many seconds for it to complete. Returns the wrapped tool's output verbatim. "
            f"NOTE: wait_seconds is capped at {int(MAX_WAIT_SECONDS)}s. Long blocks freeze the "
            "user out of the conversation; prefer short polls or wait for the push-completion "
            "notification Panel emits when the task finishes. "
            "**After calling this, ALWAYS surface the verdict to the user as plain "
            "text in your reply.** The result is JSON; the user sees nothing unless "
            "you write the verdict / headline / panelist takes / recommended actions "
            "out as readable prose. For panel-family results, prefer "
            "`run_tree(run_id, mode='transcript')` — cleaner output, drops the "
            "tool-call chatter and gives you just the panelists' final answers + "
            "judge synthesis. After fetching, end your turn with the surfaced "
            "verdict text — do NOT call more tools to 'process' it."
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
            # Memory miss — fall back to the graph DB for tasks that
            # completed before a Panel restart. In-flight tasks die with
            # the process and aren't recoverable; only terminal records
            # survive (we won't lie about a task being still running).
            try:
                from utils.execution_graph import get_graph
                graph = get_graph()
                if graph is not None:
                    persisted = graph.get_task(task_id)
                    if persisted is not None:
                        status = persisted.get("status")
                        if status in ("completed", "failed", "cancelled"):
                            payload: dict[str, Any] = {
                                "status": status,
                                "task": {
                                    "task_id": task_id,
                                    "tool": persisted.get("tool"),
                                    "label": persisted.get("label"),
                                    # Preserve link to the execution
                                    # graph run so callers can drill
                                    # into run_tree(run_id) post-restart
                                    # to recover the panel sub-tree.
                                    "run_id": persisted.get("run_id"),
                                    "status": status,
                                    "created_at": persisted.get("created_at"),
                                    "started_at": persisted.get("started_at"),
                                    "completed_at": persisted.get("completed_at"),
                                    # Round-3 audit security note: the
                                    # in-memory task path enforces
                                    # session ownership via
                                    # `require_session=True`. The graph
                                    # fallback CANNOT — the original
                                    # session object died with the
                                    # process. A task_id is therefore
                                    # effectively a bearer secret after
                                    # restart. Panel is local-only by
                                    # default (viewer bound to 127.0.0.1
                                    # without PANEL_WEB_ALLOW_REMOTE) so
                                    # the practical exposure is the
                                    # local user; we surface the
                                    # restart-recovered marker on the
                                    # response so callers can detect
                                    # they crossed that boundary.
                                    "from_graph": True,
                                    "session_security": "bearer_after_restart",
                                },
                            }
                            if status == "completed" and persisted.get("result_json"):
                                try:
                                    payload["result"] = json.loads(persisted["result_json"])
                                except (TypeError, ValueError):
                                    payload["result"] = persisted["result_json"]
                            if persisted.get("error"):
                                payload["error"] = persisted["error"]
                            return _json_response(payload)
                        # Persisted but not terminal → process died mid-run.
                        return _json_response({
                            "status": "error",
                            "error": (
                                f"task {task_id!r} was interrupted by Panel restart "
                                f"(last state: {status}). In-flight tasks aren't recoverable."
                            ),
                        })
            except Exception:  # noqa: BLE001
                pass
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

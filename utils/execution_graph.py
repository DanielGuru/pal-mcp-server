"""SQLite-backed execution graph — durable record of every PAL tool dispatch.

Why this exists
---------------
Pre-graph, every multi-agent run was ephemeral: panel results lived in
TaskManager._tasks (in-memory), conversation history in conversation_memory
(in-memory). PAL restart wiped everything. There was no way to:

  - resume a panel after process death
  - replay a prior debate to compare model behaviour over time
  - audit who-paid-for-what across a session
  - attribute cost back to the user-facing call that triggered N panelist
    sub-calls and an OAuth fallback

The audit panel converged on this as the single highest-leverage next-level
move (codex's pick, grok flipped to it in round 2, gemini conceded fully).
The judge: "build utils/execution_graph.py backed by SQLite as the next
major feature. Start append-only and minimal: runs, events, edges, messages,
artifacts, continuations."

Design choices
--------------
- **SQLite, not external DB.** Zero ops, lives in $HOME, fits a single-user
  developer tool. WAL mode enables concurrent reads while a writer is
  active. Path tunable via PAL_GRAPH_DB; default ~/.pal/execution_graph.db.

- **Append-only events; mutable runs.** Events are an immutable timeline
  (start, progress, complete, error). Runs carry summary fields that update
  in place (status, completed_at, result_json). Cheap to query, easy to
  reconstruct.

- **Sync writes guarded by a single lock.** SQLite is fast enough that
  microsecond writes don't justify a writer thread or async queue for v1.
  A threading.Lock serializes WAL writes from concurrent panel fan-out.

- **contextvars-tracked parent.** Run lineage (panel→panelist→clink→fallback)
  is captured automatically via a ContextVar, not threaded through every
  signature. execute_tool sets the current_run; nested execute_tool calls
  pick it up as their parent.

- **Optional, never load-bearing.** If the DB is unavailable (read-only FS,
  bad path, locked) PAL keeps working — graph emissions are logged and
  swallowed. The graph is observability + replay, never a hard dependency.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    """Where to put the execution graph by default.

    Per-repo isolation: drops in `<cwd>/.pal/execution_graph.db` so each
    project Claude Code opens gets its own memory of PAL runs. The web
    viewer attached to that PAL instance then shows only that repo's
    debate history — no cross-contamination from other projects running
    PAL at the same time.

    Override semantics:
      - `PAL_GRAPH_DB=<absolute path>` — pin to a specific file (e.g.
        the legacy `~/.pal/execution_graph.db` for users who liked the
        global view).
      - `PAL_GRAPH_DB=""`              — disable the graph entirely.
    """
    override = os.environ.get("PAL_GRAPH_DB")
    if override is not None:
        # Empty string means "disabled" — handled by get_graph(); for path
        # resolution just pass through whatever the user set.
        return Path(override)
    return Path.cwd() / ".pal" / "execution_graph.db"


# ---------------------------------------------------------------------------
# Context: the currently executing run id, used to derive parent_run_id for
# any nested execute_tool dispatch. Set by ExecutionGraph.start_run() via the
# `with run_context(...)` helper.
# ---------------------------------------------------------------------------

_CURRENT_RUN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "pal_current_run_id", default=None
)


def current_run_id() -> Optional[str]:
    """Run id active in this async context, if any."""
    return _CURRENT_RUN_ID.get()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    tool_name TEXT NOT NULL,
    label TEXT,
    status TEXT NOT NULL,           -- pending | running | completed | failed | cancelled
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    args_json TEXT,                 -- redacted snapshot of arguments
    result_json TEXT,               -- redacted snapshot of result
    error TEXT,                     -- short string form
    error_payload_json TEXT,        -- structured payload when available
    cost_tier TEXT,                 -- oauth_free | oauth_fallback_paid | api_paid
    model_used TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    event_type TEXT NOT NULL,       -- start | progress | complete | error | edge
    message TEXT,
    progress REAL,
    payload_json TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);

CREATE TABLE IF NOT EXISTS edges (
    parent_run_id TEXT NOT NULL,
    child_run_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- spawn | fallback | judge | debate
    ts REAL NOT NULL,
    PRIMARY KEY (parent_run_id, child_run_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_parent ON edges(parent_run_id);
"""

# Cap stored arg/result snapshots so a 50MB attachment doesn't bloat the DB.
_SNAPSHOT_CAP = int(os.environ.get("PAL_GRAPH_SNAPSHOT_CAP", "16384"))


def _redact_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """Strip internal-only fields and bound long string values before storing."""
    if not isinstance(args, dict):
        return {"_value": str(args)[:_SNAPSHOT_CAP]}
    out: dict[str, Any] = {}
    for key, value in args.items():
        # Internal context objects don't serialise cleanly and aren't worth replaying.
        if key.startswith("_"):
            continue
        if isinstance(value, str) and len(value) > _SNAPSHOT_CAP:
            out[key] = value[:_SNAPSHOT_CAP] + f"…[+{len(value) - _SNAPSHOT_CAP} chars]"
        else:
            out[key] = value
    return out


def _serialise(payload: Any) -> Optional[str]:
    """JSON-dump anything; return None if it can't be represented."""
    if payload is None:
        return None
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        try:
            text = json.dumps({"_unserialisable": str(payload)[:_SNAPSHOT_CAP]})
        except Exception:  # noqa: BLE001
            return None
    if len(text) > _SNAPSHOT_CAP:
        text = text[:_SNAPSHOT_CAP] + "..."
    return text


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


class ExecutionGraph:
    """Append-only execution log + mutable run summaries, backed by SQLite."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False: panel fan-out runs through this from
        # multiple threads (the bounded provider executor). The lock above
        # serialises actual writes; SQLite WAL mode handles concurrent reads.
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we control transactions manually if needed
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        # busy_timeout: when two PAL instances launched from the same repo
        # contend on a write, wait up to 5s for the lock instead of failing
        # immediately and dropping the best-effort graph write. Audit panel
        # finding (Codex narrowed Gemini's 'no WAL' to this real risk).
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(SCHEMA_SQL)
        logger.info("ExecutionGraph opened at %s", self.db_path)

    # -- writes -------------------------------------------------------------

    def start_run(
        self,
        tool_name: str,
        *,
        label: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        args: Optional[dict[str, Any]] = None,
        edge_kind: str = "spawn",
    ) -> str:
        """Insert a new run row in `running` state and return its id.

        When ``parent_run_id`` is given, an edge of ``edge_kind`` is auto-
        created. Use ``edge_kind='fallback'`` for clink→chat OAuth fallback,
        ``'judge'`` for panel judge, ``'debate'`` for adversarial round
        spawns. Default 'spawn' covers normal nesting.
        """
        run_id = uuid.uuid4().hex
        now = time.time()
        args_json = _serialise(_redact_arguments(args or {}))
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, parent_run_id, tool_name, label, status, created_at, started_at, args_json) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
                (run_id, parent_run_id, tool_name, label, now, now, args_json),
            )
            self._conn.execute(
                "INSERT INTO events (run_id, ts, event_type, message) VALUES (?, ?, 'start', ?)",
                (run_id, now, f"start: {tool_name}"),
            )
            if parent_run_id:
                self._conn.execute(
                    "INSERT OR IGNORE INTO edges (parent_run_id, child_run_id, kind, ts) VALUES (?, ?, ?, ?)",
                    (parent_run_id, run_id, edge_kind, now),
                )
        return run_id

    def complete_run(
        self,
        run_id: str,
        *,
        result: Any = None,
        cost_tier: Optional[str] = None,
        model_used: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status='completed', completed_at=?, result_json=?, cost_tier=?, model_used=? "
                "WHERE run_id=?",
                (now, _serialise(result), cost_tier, model_used, run_id),
            )
            self._conn.execute(
                "INSERT INTO events (run_id, ts, event_type, message) VALUES (?, ?, 'complete', ?)",
                (run_id, now, "complete"),
            )

    def fail_run(
        self,
        run_id: str,
        *,
        error: str,
        error_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status='failed', completed_at=?, error=?, error_payload_json=? WHERE run_id=?",
                (now, error[:_SNAPSHOT_CAP], _serialise(error_payload), run_id),
            )
            self._conn.execute(
                "INSERT INTO events (run_id, ts, event_type, message) VALUES (?, ?, 'error', ?)",
                (run_id, now, error[:512]),
            )

    def cancel_run(self, run_id: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status='cancelled', completed_at=? WHERE run_id=?",
                (now, run_id),
            )

    def add_event(
        self,
        run_id: str,
        *,
        event_type: str,
        message: str,
        progress: float = 0.0,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (run_id, ts, event_type, message, progress, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, now, event_type, message[:1024], progress, _serialise(payload)),
            )

    def add_edge(self, parent_run_id: str, child_run_id: str, *, kind: str = "spawn") -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO edges (parent_run_id, child_run_id, kind, ts) VALUES (?, ?, ?, ?)",
                (parent_run_id, child_run_id, kind, now),
            )

    # -- reads --------------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cur, row)

    def get_run_events(self, run_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY ts ASC, event_id ASC",
            (run_id,),
        )
        return [self._row_to_dict(cur, row) for row in cur.fetchall()]

    def list_runs(
        self,
        *,
        limit: int = 50,
        status: Optional[str] = None,
        tool_name: Optional[str] = None,
        since_ts: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if tool_name:
            clauses.append("tool_name = ?")
            params.append(tool_name)
        if since_ts is not None:
            clauses.append("created_at >= ?")
            params.append(since_ts)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        cur = self._conn.execute(
            f"SELECT * FROM runs{where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [self._row_to_dict(cur, row) for row in cur.fetchall()]

    def get_run_tree(self, root_run_id: str) -> Optional[dict[str, Any]]:
        """Return the run + recursive children + edges + events. The full
        replay surface — feeds the get_run / replay MCP tools."""
        root = self.get_run(root_run_id)
        if root is None:
            return None
        cur = self._conn.execute("SELECT * FROM edges WHERE parent_run_id=?", (root_run_id,))
        edges = [self._row_to_dict(cur, row) for row in cur.fetchall()]
        children: list[dict[str, Any]] = []
        for edge in edges:
            child_tree = self.get_run_tree(edge["child_run_id"])
            if child_tree is not None:
                child_tree["edge_kind"] = edge["kind"]
                children.append(child_tree)
        root["events"] = self.get_run_events(root_run_id)
        root["children"] = children
        return root

    @staticmethod
    def _row_to_dict(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
        return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Singleton + safe accessor
# ---------------------------------------------------------------------------

_GRAPH: Optional[ExecutionGraph] = None
_GRAPH_LOCK = threading.Lock()
_GRAPH_DISABLED = False


def get_graph() -> Optional[ExecutionGraph]:
    """Return the process-wide ExecutionGraph instance, or None if disabled.

    Disabled when:
      - PAL_GRAPH_DB="" (explicit opt-out)
      - The first init attempt failed (file-system / permission). We don't
        retry — the graph is observability, never load-bearing.
    """
    global _GRAPH, _GRAPH_DISABLED
    if _GRAPH_DISABLED:
        return None
    if _GRAPH is not None:
        return _GRAPH
    with _GRAPH_LOCK:
        if _GRAPH is not None:
            return _GRAPH
        if os.environ.get("PAL_GRAPH_DB") == "":
            _GRAPH_DISABLED = True
            logger.info("ExecutionGraph disabled (PAL_GRAPH_DB='')")
            return None
        try:
            _GRAPH = ExecutionGraph()
            return _GRAPH
        except Exception as exc:  # noqa: BLE001
            _GRAPH_DISABLED = True
            logger.warning("ExecutionGraph init failed (%s); graph disabled for this process", exc)
            return None


@contextmanager
def run_context(
    tool_name: str,
    *,
    label: Optional[str] = None,
    args: Optional[dict[str, Any]] = None,
    edge_kind: str = "spawn",
) -> Iterator[Optional[str]]:
    """Open a run, set it as the current contextvar, close on exit.

    Yields the run_id (or None if the graph is disabled). Parent is auto-
    derived from the contextvar so nested execute_tool dispatches form a
    tree without explicit threading.

    Best-effort throughout: any error during graph writes is swallowed so
    the caller's actual work is never broken by observability problems.
    """
    graph = get_graph()
    if graph is None:
        yield None
        return

    parent = current_run_id()
    try:
        run_id = graph.start_run(
            tool_name,
            label=label,
            parent_run_id=parent,
            args=args,
            edge_kind=edge_kind,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph.start_run failed for %s: %s", tool_name, exc)
        yield None
        return

    token = _CURRENT_RUN_ID.set(run_id)
    error_was_set = False
    try:
        yield run_id
    except Exception as exc:
        error_was_set = True
        try:
            graph.fail_run(run_id, error=f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        _CURRENT_RUN_ID.reset(token)
        if not error_was_set:
            # complete_run was probably called by the caller with structured
            # result/cost_tier; if not, mark as complete with no detail.
            graph_run = None
            try:
                graph_run = graph.get_run(run_id)
            except Exception:  # noqa: BLE001
                pass
            if graph_run and graph_run.get("status") == "running":
                try:
                    graph.complete_run(run_id)
                except Exception:  # noqa: BLE001
                    pass

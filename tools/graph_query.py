"""MCP tools to query the SQLite-backed execution graph.

Three tools: list_runs, get_run, run_tree. They read from the same
ExecutionGraph singleton populated by server.execute_tool. Survives Panel
restart — the original use case the audit panel called out: "restart-safe
panels, replay, auditing, cost attribution."

All three are read-only and free (no model calls). Safe to invoke during a
panel run; SQLite WAL mode handles concurrent readers.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from mcp.types import TextContent

from tools.models import ToolModelCategory, ToolOutput
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool

logger = logging.getLogger(__name__)


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"status": "error", "error": message}, indent=2))]


def _json_response(payload: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


def _check_graph() -> tuple[Any, Optional[list[TextContent]]]:
    """Common graph-availability check returning (graph_or_None, error_response_or_None)."""
    from utils.execution_graph import get_graph

    graph = get_graph()
    if graph is None:
        return None, _err("Execution graph is disabled (PANEL_GRAPH_DB='') or unavailable.")
    return graph, None


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


class ListRunsTool(BaseTool):
    """List recent runs from the execution graph, optionally filtered."""

    def get_name(self) -> str:
        return "list_runs"

    def get_description(self) -> str:
        return (
            "List recent tool dispatches recorded in the execution graph. "
            "Filter by status (running/completed/failed/cancelled) or tool name. "
            "Returns most-recent-first. Use get_run / run_tree to drill into one."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Max rows to return (default 20).",
                },
                "status": {
                    "type": "string",
                    "enum": ["running", "completed", "failed", "cancelled"],
                    "description": "Filter by status.",
                },
                "tool_name": {
                    "type": "string",
                    "description": "Filter by tool name (e.g. 'panel', 'clink').",
                },
            },
            "additionalProperties": False,
        }

    def get_annotations(self) -> dict[str, Any]:
        return {"readOnlyHint": True}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.BALANCED

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        graph, err = _check_graph()
        if err is not None:
            return err

        limit = int(arguments.get("limit") or 20)
        status = arguments.get("status")
        tool_name = arguments.get("tool_name")
        rows = graph.list_runs(limit=limit, status=status, tool_name=tool_name)
        return _json_response({"status": "ok", "count": len(rows), "runs": rows})


# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------


class GetRunTool(BaseTool):
    """Fetch a single run with its full event timeline."""

    def get_name(self) -> str:
        return "get_run"

    def get_description(self) -> str:
        return (
            "Fetch one run by id with its full event timeline (start / progress / "
            "complete / error). Use list_runs first to find ids; use run_tree for "
            "the recursive descendants of a panel/parent run."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run id (32-char hex from list_runs / start_run output).",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        }

    def get_annotations(self) -> dict[str, Any]:
        return {"readOnlyHint": True}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.BALANCED

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        graph, err = _check_graph()
        if err is not None:
            return err

        run_id = arguments.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return _err("'run_id' must be a non-empty string")

        run = graph.get_run(run_id)
        if run is None:
            return _err(f"No run found with id {run_id!r}")
        run["events"] = graph.get_run_events(run_id)
        return _json_response({"status": "ok", "run": run})


# ---------------------------------------------------------------------------
# run_tree
# ---------------------------------------------------------------------------


class RunTreeTool(BaseTool):
    """Fetch a run + every descendant (panel→panelist→clink→fallback)."""

    def get_name(self) -> str:
        return "run_tree"

    def get_description(self) -> str:
        return (
            "Fetch a run and recursively all its descendants (children, edges, "
            "events). The full replay surface for a panel: every panelist "
            "sub-call, any OAuth fallback, the judge run, and per-leaf "
            "cost_tier — all from one query, even after Panel restart. "
            "**For panel/multiaudit/bugfind results, use mode='transcript' "
            "to get JUST the panelist verdicts + judge synthesis as clean "
            "text — same view the user sees on the live web viewer page. "
            "Always prefer this over scraping task_status's progress event "
            "log when you want to read the panel's findings.** "
            "**After calling this, surface the verdicts to the user as "
            "plain readable text in your reply.** The user sees nothing "
            "unless you write the headline / per-panelist takes / "
            "recommended actions out as prose. End your turn with that "
            "surfaced text — do not chain more tool calls to 'process' it."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Root run id. Get from list_runs or any prior MCP response that surfaced it (multiaudit/bugfind/panel responses include run ids in web_viewer_url).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["full", "transcript"],
                    "description": (
                        "'full' (default): return the full tree JSON with "
                        "events, children, and cost rollup. 'transcript': "
                        "return ONLY the panelist_answer + judge_synthesis "
                        "events as a chronologically-ordered text block — "
                        "same view the user sees on the live web viewer "
                        "page. Use this after a panel/multiaudit/bugfind "
                        "completes to read the verdicts directly without "
                        "spawning a subagent to scrape task_status."
                    ),
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        }

    def get_annotations(self) -> dict[str, Any]:
        return {"readOnlyHint": True}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.BALANCED

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        graph, err = _check_graph()
        if err is not None:
            return err

        run_id = arguments.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return _err("'run_id' must be a non-empty string")

        tree = graph.get_run_tree(run_id)
        if tree is None:
            return _err(f"No run found with id {run_id!r}")

        mode = (arguments.get("mode") or "full").lower()
        if mode == "transcript":
            # Return ONLY the panelist verdicts + judge synthesis as
            # chronological text. Same view the user sees on the live
            # web viewer. Bypasses the noise of progress events,
            # tool_use logs, and the recursive tree structure — just
            # the actual model outputs an agent or human cares about.
            transcript = _render_panel_transcript(tree)
            return _json_response(
                {
                    "status": "ok",
                    "mode": "transcript",
                    "run_id": run_id,
                    "transcript": transcript,
                    "cost_tier_rollup": _cost_tier_rollup(tree),
                }
            )

        # Roll up cost-tier counts so callers can scan spend without
        # walking the tree themselves.
        rollup = _cost_tier_rollup(tree)
        return _json_response({"status": "ok", "tree": tree, "cost_tier_rollup": rollup})


def _render_panel_transcript(tree: dict[str, Any]) -> str:
    """Walk the run tree, collect panelist_answer + judge_synthesis events,
    sort chronologically, return as a single text block.

    This is the canonical "read the panel results like the user does"
    view — every agent that finishes a panel/multiaudit/bugfind dispatch
    should call run_tree with mode='transcript' instead of scraping
    task_status's full progress-event stream (which contains tool_use
    chatter, file reads, command echoes, etc. that drown the verdicts).
    """

    events: list[dict[str, Any]] = []

    def _walk(node: dict[str, Any]) -> None:
        for ev in node.get("events") or []:
            etype = ev.get("event_type")
            if etype in ("panelist_answer", "judge_synthesis"):
                events.append(ev)
        for child in node.get("children") or []:
            _walk(child)

    _walk(tree)
    events.sort(key=lambda e: e.get("ts") or 0)

    if not events:
        return (
            "(no panelist_answer or judge_synthesis events found in this "
            "run tree — either the run is still in progress or it wasn't "
            "a panel-style dispatch. Use mode='full' for the raw tree.)"
        )

    blocks: list[str] = []
    for ev in events:
        msg = (ev.get("message") or "").rstrip()
        if not msg:
            continue
        blocks.append(msg)
    return "\n\n---\n\n".join(blocks)


def _cost_tier_rollup(tree: dict[str, Any]) -> dict[str, int]:
    """Walk the tree, count occurrences of each cost_tier on the leaves +
    intermediate nodes. Cheap aggregate spend signal."""
    counts: dict[str, int] = {}
    stack: list[dict[str, Any]] = [tree]
    while stack:
        node = stack.pop()
        tier = node.get("cost_tier") or "untagged"
        counts[tier] = counts.get(tier, 0) + 1
        for child in node.get("children") or []:
            stack.append(child)
    return counts

"""Panel orchestration — fan out one prompt to N models concurrently with optional judge.

This is the headline feature for multi-model orchestration. Where `consensus`
runs models sequentially with shared state, `panel`:

  - Fires every panelist in parallel (asyncio.gather)
  - Routes each panelist through the cheapest path automatically:
      * 'codex' / 'gemini'           -> clink (OAuth, free)
      * any other string             -> chat tool (paid API; Grok lives here)
  - Optionally calls a judge model afterwards with all panelist outputs and
    the original prompt to synthesize divergence/agreement
  - Returns structured JSON: per-panelist response + duration + cost-tier flag,
    plus the judge synthesis if requested

Designed to be wrapped in `start_task` for long audits so the conversation is
not blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from mcp.types import TextContent

from tools.models import ToolModelCategory
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool
from utils.progress import emit_progress

logger = logging.getLogger("pal.panel")

# Names that route through clink (subprocess CLI, OAuth, free)
DEFAULT_TIMEOUT_S = 600
MAX_PANELISTS = 8

# Cap on per-panelist response text included in the judge prompt. Keeps the
# judge's context bounded even if one panelist produces a wall of output.
JUDGE_PER_PANELIST_CHAR_CAP = 8000


def _is_clink_agent(name: str) -> bool:
    """Decide whether `name` should route through clink.

    Derived from clink's runtime registry rather than a hard-coded set, so
    adding a new clink CLI in conf/cli_clients/ makes panel route to it
    automatically.
    """
    try:
        from clink.registry import get_registry
        return name.lower() in {n.lower() for n in get_registry().list_clients()}
    except Exception:  # noqa: BLE001
        # Conservative fallback if the registry isn't importable.
        return name.lower() in {"codex", "gemini", "claude"}


def _fresh_tool(tool_name: str):
    """Return a freshly instantiated tool of the same class as the registered one.

    Critical for parallel safety: PAL's tool classes (CLinkTool, ChatTool) keep
    per-call state on `self` during execute() (`_current_arguments`,
    `_current_model_name`, `_model_context`). Sharing a singleton across
    concurrent panelists corrupts that state. Fresh instances are cheap and
    eliminate the race.
    """
    from server import TOOLS
    base = TOOLS.get(tool_name)
    if base is None:
        raise RuntimeError(f"tool {tool_name!r} not registered")
    return type(base)()


def _normalize_panelist(entry: Any) -> dict[str, Any]:
    """Accept either a string or an object spec; return a normalized dict."""
    if isinstance(entry, str):
        return {"agent": entry}
    if isinstance(entry, dict):
        return dict(entry)
    raise ValueError(f"Each panelist must be a string or object, got {type(entry).__name__}")


async def _run_panelist(
    panelist: dict[str, Any],
    *,
    prompt: str,
    files: list[str],
    images: list[str],
    timeout: float,
) -> dict[str, Any]:
    """Run a single panelist. Returns a structured per-panelist outcome."""
    agent = panelist.get("agent")
    if not isinstance(agent, str) or not agent:
        return {
            "agent": str(agent),
            "ok": False,
            "error": "panelist 'agent' must be a non-empty string",
        }

    role = panelist.get("role") or "default"
    label = panelist.get("label") or agent
    is_clink = _is_clink_agent(agent)
    started = time.monotonic()

    await emit_progress(f"panel/{label}: dispatching", progress=0.0)

    try:
        if is_clink:
            tool = _fresh_tool("clink")  # per-call instance: parallel-safe
            args = {
                "prompt": prompt,
                "cli_name": agent,
                "role": role,
                "absolute_file_paths": files,
                "images": images,
            }
            cost_tier = "oauth_free"
        else:
            tool = _fresh_tool("chat")  # per-call instance: parallel-safe
            args = {
                "prompt": prompt,
                "model": agent,
                "absolute_file_paths": files,
                "images": images,
                "working_directory_absolute_path": panelist.get("working_directory_absolute_path") or "/tmp",
            }
            cost_tier = "api_paid"

        result = await asyncio.wait_for(tool.execute(args), timeout=timeout)
        duration = round(time.monotonic() - started, 2)
        # tool.execute returns list[TextContent]; concatenate text
        text_parts = [getattr(item, "text", str(item)) for item in (result or [])]
        await emit_progress(f"panel/{label}: ✓ done ({duration}s)", progress=1.0)
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": True,
            "cost_tier": cost_tier,
            "duration_s": duration,
            "response": "\n".join(text_parts),
        }
    except asyncio.TimeoutError:
        duration = round(time.monotonic() - started, 2)
        await emit_progress(f"panel/{label}: ✗ timed out", progress=1.0)
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": False,
            "duration_s": duration,
            "error": f"timed out after {timeout}s",
        }
    except Exception as exc:  # noqa: BLE001
        duration = round(time.monotonic() - started, 2)
        await emit_progress(f"panel/{label}: ✗ error", progress=1.0)
        return {
            "agent": agent,
            "label": label,
            "role": role,
            "ok": False,
            "duration_s": duration,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _truncate(text: str, *, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = text[: cap - 80]
    return head + f"\n…[panel: truncated {len(text) - cap + 80:,} chars]"


def _build_judge_prompt(original_prompt: str, panelist_results: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    sections.append("You are synthesizing a panel of AI models that each independently answered a question.")
    sections.append("Identify points of agreement, points of divergence, the strongest argument from each, "
                    "and your overall recommendation. Be terse and concrete.")
    sections.append("\n=== ORIGINAL QUESTION ===\n" + original_prompt.strip())
    for r in panelist_results:
        if r.get("ok"):
            response = (r.get("response") or "").strip()
            response = _truncate(response, cap=JUDGE_PER_PANELIST_CHAR_CAP)
            sections.append(
                f"\n=== PANELIST: {r['agent']} (role={r.get('role')}, {r.get('duration_s')}s) ===\n{response}"
            )
        else:
            sections.append(f"\n=== PANELIST: {r['agent']} — FAILED: {r.get('error')} ===")
    sections.append("\n=== YOUR SYNTHESIS ===\n")
    return "\n".join(sections)


def _panel_status(panelists_ok: int, panelists_total: int) -> str:
    if panelists_ok == 0:
        return "failed"
    if panelists_ok < panelists_total:
        return "partial"
    return "completed"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class PanelTool(BaseTool):
    """Fan out one prompt to multiple AI models concurrently, optionally judged."""

    def get_name(self) -> str:
        return "panel"

    def get_description(self) -> str:
        return (
            "Fan out the same prompt to multiple AI models in parallel and return "
            "structured per-model responses. Each panelist named 'codex' or 'gemini' "
            "is routed through clink (OAuth, free). Other names go through chat as "
            "paid API model strings (e.g. 'grok-4.3', 'gpt-5.5'). Optionally specify "
            "a 'judge' (any agent name) which receives all panelist outputs and "
            "synthesizes agreement / divergence / recommendation. Use start_task to "
            "wrap this for long audits so the conversation isn't blocked."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The question or task to put to every panelist verbatim.",
                },
                "panelists": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PANELISTS,
                    "description": (
                        "List of agents to consult in parallel. Each entry is either a string "
                        "(e.g. 'codex', 'gemini', 'grok-4.3', 'gpt-5.5') OR an object with "
                        "{agent, role?, label?}. Total max %d panelists." % MAX_PANELISTS
                    ),
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "agent": {"type": "string"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["default", "codereviewer", "planner"],
                                    },
                                    "label": {"type": "string"},
                                },
                                "required": ["agent"],
                                "additionalProperties": True,
                            },
                        ],
                    },
                },
                "judge": {
                    "type": "string",
                    "description": (
                        "Optional agent name to synthesize the panel. If set, after all "
                        "panelists complete the judge receives the original prompt + all "
                        "panelist outputs and produces a synthesis. Use 'codex' or 'gemini' "
                        "for free OAuth synthesis; any other name uses paid API."
                    ),
                },
                "absolute_file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to share with every panelist.",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional image paths shared with every panelist.",
                },
                "panelist_timeout_s": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 1800,
                    "description": "Per-panelist timeout in seconds (default 600).",
                },
            },
            "required": ["prompt", "panelists"],
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
        return ToolModelCategory.EXTENDED_REASONING

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        # ----- argument parsing -----
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _err("'prompt' must be a non-empty string")

        raw_panelists = arguments.get("panelists")
        if not isinstance(raw_panelists, list) or not raw_panelists:
            return _err("'panelists' must be a non-empty list")
        if len(raw_panelists) > MAX_PANELISTS:
            return _err(f"too many panelists ({len(raw_panelists)}); max is {MAX_PANELISTS}")

        try:
            panelists = [_normalize_panelist(p) for p in raw_panelists]
        except ValueError as exc:
            return _err(str(exc))

        files = arguments.get("absolute_file_paths") or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            return _err("'absolute_file_paths' must be a list of strings")
        images = arguments.get("images") or []
        if not isinstance(images, list) or not all(isinstance(i, str) for i in images):
            return _err("'images' must be a list of strings")

        timeout = arguments.get("panelist_timeout_s")
        if timeout is None:
            timeout = float(DEFAULT_TIMEOUT_S)
        else:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                return _err("'panelist_timeout_s' must be numeric")
            if timeout < 5 or timeout > 1800:
                return _err("'panelist_timeout_s' must be between 5 and 1800")

        judge = arguments.get("judge")
        if judge is not None and (not isinstance(judge, str) or not judge.strip()):
            return _err("'judge' must be a non-empty string when provided")

        # ----- fan out (streaming via as_completed) -----
        await emit_progress(
            f"panel: dispatching to {len(panelists)} panelists in parallel",
            progress=0.0,
        )
        started = time.monotonic()
        panelist_tasks = [
            asyncio.create_task(
                _run_panelist(p, prompt=prompt, files=files, images=images, timeout=timeout),
                name=f"panelist:{p.get('label') or p.get('agent')}",
            )
            for p in panelists
        ]
        panelist_results: list[dict[str, Any]] = []
        finished = 0
        try:
            for fut in asyncio.as_completed(panelist_tasks):
                outcome = await fut
                panelist_results.append(outcome)
                finished += 1
                tag = "✓" if outcome.get("ok") else "✗"
                await emit_progress(
                    f"panel: {tag} {outcome.get('label')} ({finished}/{len(panelists)})",
                    progress=float(finished),
                    total=float(len(panelists) + (1 if judge else 0)),
                )
        except asyncio.CancelledError:
            for t in panelist_tasks:
                if not t.done():
                    t.cancel()
            raise
        panel_duration = round(time.monotonic() - started, 2)

        ok_count = sum(1 for r in panelist_results if r.get("ok"))
        panel_status = _panel_status(ok_count, len(panelist_results))

        await emit_progress(
            f"panel: {ok_count}/{len(panelist_results)} succeeded ({panel_status}) in {panel_duration}s",
            progress=float(len(panelists)),
            total=float(len(panelists) + (1 if judge else 0)),
        )

        # ----- optional judge synthesis -----
        judge_result: Optional[dict[str, Any]] = None
        if judge:
            if ok_count == 0:
                # Nothing useful to synthesize — skip the judge.
                judge_result = {
                    "agent": judge,
                    "ok": False,
                    "error": "skipped: 0 panelists produced output",
                }
            else:
                judge_panelist = {"agent": judge, "label": f"judge:{judge}", "role": "default"}
                judge_prompt = _build_judge_prompt(prompt, panelist_results)
                judge_started = time.monotonic()
                await emit_progress(
                    f"panel: invoking judge ({judge})",
                    progress=float(len(panelists)),
                    total=float(len(panelists) + 1),
                )
                judge_outcome = await _run_panelist(
                    judge_panelist,
                    prompt=judge_prompt,
                    files=[],  # judge sees the panelist outputs, not the original files
                    images=[],
                    timeout=timeout,
                )
                judge_outcome["duration_s"] = round(time.monotonic() - judge_started, 2)
                judge_result = judge_outcome

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "status": panel_status,
                        "panel_duration_s": panel_duration,
                        "panelists_ok": ok_count,
                        "panelists_total": len(panelist_results),
                        "panelists": panelist_results,
                        "judge": judge_result,
                    },
                    indent=2,
                    default=str,
                ),
            )
        ]


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"status": "error", "error": message}, indent=2))]

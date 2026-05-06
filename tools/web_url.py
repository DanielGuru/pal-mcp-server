"""web_url MCP tool — return the live execution-graph viewer URL.

Panel boots a tiny HTTP server alongside the MCP stdio loop (see
utils/web_viewer.py). This tool exposes the URL so Claude Code can hand
it to the user on demand: ``"open the panel viewer"`` → tool call →
``http://127.0.0.1:8765/``.

Free, instant, read-only. Returns a structured payload either way (live
URL or a "disabled" status) so callers don't need to special-case None.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from tools.models import ToolModelCategory
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool


class WebUrlTool(BaseTool):
    def get_name(self) -> str:
        return "web_url"

    def get_description(self) -> str:
        return (
            "Return the URL of the local Panel execution-graph viewer. Use this "
            "to give the user a live view of an in-flight panel/audit run. The "
            "viewer is a single web page that auto-refreshes — they can watch "
            "panelists complete, see the debate tree, drill into individual "
            "sub-runs, and read per-leaf cost_tier without polling task_status "
            "by hand."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

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
        from utils.web_viewer import get_server_url

        url = get_server_url()
        if url is None:
            payload = {
                "status": "disabled",
                "message": (
                    "Web viewer is not running. It's disabled when "
                    "PANEL_WEB_DISABLE is set, or it failed to bind a port at "
                    "startup. Check logs/mcp_server.log for the reason."
                ),
            }
        else:
            payload = {
                "status": "ok",
                "url": url,
                "hint": (
                    "Open this URL in a browser. The page auto-refreshes the "
                    "run list every 2s and the selected run-tree every 1.5s — "
                    "watch panel runs unfold without polling task_status."
                ),
            }
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

"""bugfind MCP tool — magic-phrase trigger for a bug-shaped panel investigation.

Sister tool to ``multiaudit``. Same orchestration shape (panel fan-out +
adversarial debate + judge synthesis), different rubric and different
context capture: instead of packaging a ``git diff``, ``bugfind`` packages
the user's bug description plus auto-attached debugging context (recent
commits, recent error log tail, optionally explicit file attachments).

The user-facing flow we want
----------------------------
1. User sees a bug, describes it: "the viewer header shows 'grok-4.3'
   instead of 'panel' for multiaudit runs".
2. User says "bugfind it" (or "find this bug", "panel debug this", ...).
3. Claude Code calls this tool with the bug description.
4. Tool grabs context: recent commits (what's been changing), the tail
   of ``logs/mcp_server.log`` filtered to ERROR/Traceback/Failed, and
   any explicitly-attached files. Builds a structured investigation
   prompt with the bug rubric, fires ``start_task('panel', ...)``.
5. Returns task_id + live viewer URL + summary.
6. User opens the viewer and watches the panel argue toward a fix.
7. Final headline + judge synthesis: a unified diff proposal the user
   can review/apply.

Why a dedicated tool, not "just call panel"
-------------------------------------------
- Bug descriptions without context are useless; tool gathers context
  automatically (recent commits, error logs, referenced files).
- The bug-investigation rubric is opinionated (REPRO / ROOT CAUSE /
  MINIMAL FIX / REGRESSION TEST / BLAST RADIUS / WHAT YOU MISSED).
- The judge is asked to synthesise a unified diff so the output is
  actionable — not just "here's what's wrong" but "here's the fix to
  apply".
- Composes with all existing infrastructure: panel, start_task, OAuth
  fallback, redaction, web viewer, execution graph.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from tools.models import ToolModelCategory
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool

logger = logging.getLogger(__name__)


# Same default panel as multiaudit: four frontier model families. Override
# via env / per-call args. Keep ``host`` opt-in (Claude Code doesn't
# advertise sampling capability today; including it polluted every audit
# with a "host failed" row).
#
# DELIBERATELY IMMUTABLE: the import-time fallback is the hardcoded
# 4-model list; env-driven overrides are read fresh inside ``execute()``.
# An earlier version mutated this at import time, which created a stale-
# defaults bug after live env clearing (panel-flagged in the bugfind
# audit). Don't reintroduce module-level mutation.
DEFAULT_PANELISTS = ("codex", "gemini", "claude", "grok-4.3")
DEFAULT_DEBATE_ROUNDS = 1
DEFAULT_PANELIST_TIMEOUT_S = 300

# Caps to keep context windows sane. The bug description plus all
# auto-attached context goes into one prompt; cap each piece so a
# verbose log file or a giant attached file can't blow the budget.
_LOG_TAIL_CHAR_CAP = 8_000  # ~ last 200 lines of error-only log
_FILE_CHAR_CAP = 30_000     # per attached file
# Total cap of 200KB — well under every default panelist's context
# window (Gemini 1M, Claude / GPT-5.5 200K, Grok-4.3 128K) so the cap
# is a "no single bugfind eats the whole budget" guardrail, not a
# functional ceiling. Was 80KB; raised after audit feedback that
# users attaching 4 × 30KB files were silently losing trailing
# attachments to the tail-truncate.
_TOTAL_CONTEXT_CHAR_CAP = 200_000
_RECENT_COMMITS_COUNT = 8
_LOG_FILE_CANDIDATES = ("logs/mcp_server.log", "logs/mcp_activity.log")


class BugfindTool(BaseTool):
    """Package a bug description + auto-collected context as a multi-model
    investigation panel."""

    def get_name(self) -> str:
        return "bugfind"

    def get_description(self) -> str:
        return (
            "Trigger a multi-model investigation of a bug. Reads the user's "
            "bug description and auto-attaches context (recent commits, "
            "error log tail, any explicitly attached files), then fires an "
            "adversarial debate panel (codex + gemini + claude + grok-4.3 "
            "by default, 1 debate round, codex as judge) with a structured "
            "rubric: REPRO / ROOT CAUSE / MINIMAL FIX / REGRESSION TEST / "
            "BLAST RADIUS / WHAT YOU MISSED. Returns the task_id + live web "
            "viewer URL + a summary line. The judge synthesises a final fix "
            "proposal the user can review and apply. "
            "Use this when the user says 'bugfind', 'bugfind it', 'find "
            "this bug', 'use panel to find the bug', 'what's breaking', "
            "'panel debug this', 'diagnose with all models', or any similar "
            "phrasing meaning 'fan this bug out to multiple models for an "
            "opinionated diagnosis + fix proposal'."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bug_description": {
                    "type": "string",
                    "description": (
                        "What's broken. The more specific the better — "
                        "describe symptoms, what was expected, what actually "
                        "happens, any reproduction steps you've tried. File "
                        "paths and line numbers in the description help the "
                        "panel zero in. This is the only required field."
                    ),
                },
                "attached_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Absolute paths of files the panel should read "
                        "verbatim. Use this when the bug references specific "
                        "code that the panel needs to see. Each file is "
                        "capped at ~30KB; truncated files get a marker. "
                        "**SECURITY: file contents are sent verbatim to the "
                        "configured panelist APIs (OpenAI / Anthropic / "
                        "Gemini / xAI / OAuth CLIs) as part of the panel "
                        "prompt. Don't attach files containing secrets — "
                        "no redaction is applied to attached_files content. "
                        "Attach source code, config templates, error "
                        "screenshots' source — not `.env`, credentials, or "
                        "shell history.**"
                    ),
                },
                "panelists": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Override the default panelist list. Each entry is "
                        "an agent name as panel.py expects (clink CLI name "
                        "like 'codex'/'gemini', or a paid model id like "
                        "'grok-4.3' / 'gpt-5.5')."
                    ),
                },
                "judge": {
                    "type": "string",
                    "description": (
                        "Agent that synthesises the final fix proposal. "
                        "Defaults to PANEL_BUGFIND_JUDGE env var, else "
                        "'codex'. Use any panelist name or any valid model id."
                    ),
                },
                "debate_rounds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                    "description": (
                        "Adversarial debate rounds after the initial parallel "
                        "fan-out. Default 1. Set 0 for fan-out + judge only, "
                        "2-3 for deeper pressure-testing (slower)."
                    ),
                },
                "panelist_timeout_s": {
                    "type": "number",
                    "minimum": 30,
                    "maximum": 1800,
                    "description": "Per-panelist timeout (default 300s).",
                },
                "working_directory_absolute_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the repo to investigate. Defaults "
                        "to the server's CWD; override when running Panel "
                        "from a different working directory."
                    ),
                },
                "skip_log_tail": {
                    "type": "boolean",
                    "description": (
                        "Skip auto-attaching the error log tail. Default "
                        "false. Useful when the bug isn't reflected in logs "
                        "(UI bugs, doc bugs, etc.) and you want to keep the "
                        "context window tight."
                    ),
                },
            },
            "required": ["bug_description"],
            "additionalProperties": False,
        }

    def get_annotations(self) -> dict[str, Any]:
        return {"readOnlyHint": False, "openWorldHint": True}

    def get_system_prompt(self) -> str:
        return ""

    def get_request_model(self):
        return ToolRequest

    def requires_model(self) -> bool:
        return False

    def get_model_category(self) -> ToolModelCategory:
        return ToolModelCategory.EXTENDED_REASONING

    async def prepare_prompt(self, request: ToolRequest) -> str:
        return ""

    def format_response(self, response: str, request: ToolRequest, model_info: dict = None) -> str:
        return response

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        bug_description = (arguments.get("bug_description") or "").strip()
        if not bug_description:
            return _err(
                "bug_description is required and must be non-empty. Describe "
                "what's broken, what you expected, what actually happens, "
                "and any reproduction steps you've tried."
            )

        cwd = arguments.get("working_directory_absolute_path") or str(Path.cwd())
        cwd_path = Path(cwd)
        if not cwd_path.exists():
            return _err(
                f"working_directory_absolute_path does not exist: {cwd}. "
                "Pass an absolute path to the repo you want investigated."
            )
        if not cwd_path.is_dir():
            # An existing regular file would pass exists() but crash
            # subprocess.run(..., cwd=cwd) with NotADirectoryError later.
            # Fail fast with a clear message instead. (Audit-flagged.)
            return _err(
                f"working_directory_absolute_path is not a directory: {cwd}. "
                "Pass the absolute path to a repository directory, not a file."
            )

        # Live env defaults — same pattern as multiaudit (round-3 panel
        # finding: the settings tab mutates env at runtime, so freezing at
        # import time made live toggles a lie). DEFAULT_PANELISTS is now
        # the immutable hardcoded tuple — when env is cleared at runtime,
        # we fall back to the canonical list, not a stale mutated copy.
        env_judge = (os.environ.get("PANEL_BUGFIND_JUDGE") or "").strip()
        env_panelists = (os.environ.get("PANEL_BUGFIND_PANELISTS") or "").strip()
        live_default_judge = env_judge or "codex"
        live_default_panelists = (
            [p.strip() for p in env_panelists.split(",") if p.strip()]
            if env_panelists
            else list(DEFAULT_PANELISTS)
        )

        panelists = arguments.get("panelists") or live_default_panelists
        judge = arguments.get("judge") or live_default_judge
        debate_rounds = arguments.get("debate_rounds")
        if debate_rounds is None:
            debate_rounds = DEFAULT_DEBATE_ROUNDS
        timeout_s = arguments.get("panelist_timeout_s") or DEFAULT_PANELIST_TIMEOUT_S
        skip_log_tail = bool(arguments.get("skip_log_tail", False))
        attached_files: list[str] = arguments.get("attached_files") or []

        # ------ collect context ------
        recent_commits = _git_safe(
            cwd, ["log", f"-n{_RECENT_COMMITS_COUNT}", "--pretty=format:%h %s"]
        )

        log_tail = "" if skip_log_tail else _read_log_tail(cwd_path, _LOG_TAIL_CHAR_CAP)

        attached_blobs: list[tuple[str, str, bool]] = []  # (path, content, truncated)
        for f in attached_files:
            if not isinstance(f, str) or not f.strip():
                continue
            blob, truncated = _read_file_capped(f, _FILE_CHAR_CAP)
            if blob is not None:
                attached_blobs.append((f, blob, truncated))

        # ------ build the investigation prompt ------
        prompt = _build_investigation_prompt(
            bug_description=bug_description,
            recent_commits=recent_commits,
            log_tail=log_tail,
            attached_blobs=attached_blobs,
        )

        # Hard total cap — if the user attached huge files plus the log
        # tail plus a long bug description, truncate the WHOLE thing
        # before dispatch rather than blowing the panelist context window.
        prompt, total_truncated = _cap_total(prompt, _TOTAL_CONTEXT_CHAR_CAP)

        # ------ dispatch via start_task ------
        from server import execute_tool
        from tools.shared.base_tool import mark_internal_payload

        # Short label for the picker / logs — first ~60 chars of bug desc
        short_label = bug_description.splitlines()[0][:60].strip()
        if len(short_label) < len(bug_description.splitlines()[0]):
            short_label += "…"

        panel_args = {
            "tool": "panel",
            "label": f"bugfind:{short_label}",
            "arguments": {
                "prompt": prompt,
                "panelists": panelists,
                "judge": judge,
                "debate_rounds": int(debate_rounds),
                "panelist_timeout_s": float(timeout_s),
            },
        }
        try:
            with mark_internal_payload():
                start_result = await execute_tool("start_task", panel_args)
        except Exception as exc:  # noqa: BLE001
            return _err(f"start_task dispatch failed: {type(exc).__name__}: {exc}")

        # start_task returns structured error payloads (e.g. too many active
        # tasks, unknown wrapped tool) WITHOUT raising. Without this guard,
        # bugfind would happily report "started" with task_id=null and tell
        # the user to poll a task that doesn't exist — operational lie at
        # exactly the moment the user is chasing a bug. (Audit-flagged.)
        start_status, start_error = _extract_start_status(start_result)
        if start_status != "started":
            return _err(
                f"start_task refused dispatch: {start_error or 'unknown error'} "
                f"(status={start_status!r}). The panel was NOT started."
            )
        task_id = _extract_task_id(start_result)
        if not task_id:
            return _err(
                "start_task returned status=started but no task_id was found "
                "in the response. The panel may or may not have started; "
                "this is a server-side contract violation."
            )

        # Web viewer deep-link to this bugfind run, same logic as multiaudit
        try:
            from utils.web_viewer import get_server_url
            from utils.execution_graph import current_run_id
            web_url = get_server_url()
            if web_url:
                rid = current_run_id()
                if rid:
                    sep = "&" if "?" in web_url else "?"
                    web_url = f"{web_url}{sep}run={rid}"
        except Exception:  # noqa: BLE001
            web_url = None

        summary = (
            f"Bugfind dispatched — {len(panelists)} panelists "
            f"({', '.join(panelists)}), {debate_rounds} debate "
            f"round{'s' if debate_rounds != 1 else ''}, judge={judge}, "
            f"~{len(prompt)} chars of context"
            f"{' (truncated)' if total_truncated else ''}."
        )

        payload: dict[str, Any] = {
            "status": "started",
            "summary": summary,
            "task_id": task_id,
            "panelists": panelists,
            "judge": judge,
            "debate_rounds": int(debate_rounds),
            "context": {
                "bug_description_chars": len(bug_description),
                "recent_commits_attached": bool(recent_commits.strip()),
                "log_tail_attached": bool(log_tail.strip()),
                "attached_files": [b[0] for b in attached_blobs],
                "attached_files_truncated": [b[0] for b in attached_blobs if b[2]],
                "total_truncated": total_truncated,
            },
            "next_steps": [
                "Open the web viewer URL below to watch the debate live.",
                "Poll task_status(task_id) for high-level progress.",
                "When complete, call run_tree(run_id, mode='transcript') to read the panelist verdicts + judge fix proposal as clean text — same view as the live viewer page. Pull the run_id from web_viewer_url's ?run=<id> query param.",
                "Or: task_result(task_id, wait_seconds=N) for the synthesized final output.",
            ],
        }
        if web_url:
            payload["web_viewer_url"] = web_url
        else:
            payload["web_viewer_url"] = None
            payload["web_viewer_note"] = (
                "Web viewer not running. Set PANEL_WEB_DISABLE='' (default) and restart "
                "Panel to get a live URL, or use task_status / run_tree to follow progress."
            )

        return [TextContent(type="text", text=json.dumps(payload, indent=2))]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"status": "error", "error": msg}))]


def _git_safe(cwd: str, argv: list[str]) -> str:
    """Best-effort git invocation. Returns empty string on failure rather
    than raising — git context is nice-to-have for bugfind, not critical."""
    if not (Path(cwd) / ".git").exists():
        return ""
    try:
        out = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _read_log_tail(cwd: Path, char_cap: int) -> str:
    """Return the last N chars of the most recent error/traceback context
    from the log file. Filters to ERROR / Traceback / Failed lines and a
    handful of trailing lines for stack-trace continuation. Empty string
    if no logs are present."""
    for candidate in _LOG_FILE_CANDIDATES:
        path = cwd / candidate
        if path.exists() and path.is_file():
            try:
                # Read last 500KB only — enough context for recent errors
                # without slurping a multi-GB log file.
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 500_000))
                    raw = f.read().decode("utf-8", errors="ignore")
            except OSError:
                continue

            # Filter to error-relevant lines plus a trailing context window
            lines = raw.splitlines()
            keep_indices: set[int] = set()
            for i, line in enumerate(lines):
                if re.search(r"\b(ERROR|Traceback|Failed|Exception)\b", line):
                    # Capture this line + 8 following (for stack traces)
                    for j in range(i, min(len(lines), i + 9)):
                        keep_indices.add(j)
            if not keep_indices:
                return ""
            kept = "\n".join(lines[i] for i in sorted(keep_indices))
            if len(kept) > char_cap:
                kept = kept[-char_cap:]
                kept = "[...older error lines truncated...]\n" + kept
            return kept

    return ""


def _read_file_capped(path: str, char_cap: int) -> tuple[str | None, bool]:
    """Read a file, return (content, truncated) or (None, False) on
    failure. Skips binary / unreadable files cleanly."""
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None, False
        # Refuse files > 5MB as a sanity check
        if p.stat().st_size > 5 * 1024 * 1024:
            return f"[file {path} too large to attach: {p.stat().st_size} bytes]", True
        text = p.read_text(encoding="utf-8", errors="ignore")
        if len(text) > char_cap:
            return text[:char_cap] + f"\n\n[…truncated {len(text) - char_cap} chars]", True
        return text, False
    except OSError:
        return None, False


def _cap_total(prompt: str, char_cap: int) -> tuple[str, bool]:
    if len(prompt) <= char_cap:
        return prompt, False
    truncated = prompt[:char_cap]
    truncated += f"\n\n[…total prompt truncated to {char_cap} chars by bugfind cap]"
    return truncated, True


def _build_investigation_prompt(
    *,
    bug_description: str,
    recent_commits: str,
    log_tail: str,
    attached_blobs: list[tuple[str, str, bool]],
) -> str:
    commits_section = (
        f"\n\n=== RECENT COMMITS (what's been changing) ===\n{recent_commits.strip()}"
        if recent_commits.strip()
        else ""
    )
    log_section = (
        f"\n\n=== ERROR LOG TAIL (filtered to ERROR / Traceback / Failed / Exception) ===\n{log_tail}"
        if log_tail.strip()
        else ""
    )
    files_section = ""
    if attached_blobs:
        chunks = []
        for path, content, truncated in attached_blobs:
            marker = " [truncated]" if truncated else ""
            chunks.append(f"--- {path}{marker} ---\n{content}")
        files_section = "\n\n=== ATTACHED FILES ===\n" + "\n\n".join(chunks)

    return f"""You are a panelist in an adversarial multi-model bug investigation. \
Be opinionated. Defend your position. Only change your mind when convinced.

=== BUG DESCRIPTION ===
{bug_description}\
{commits_section}\
{log_section}\
{files_section}

=== YOUR JOB ===
Output structure required, in this order:

1. **REPRO** — exact steps to reproduce. What does the user have to do to \
see this bug? Expected behaviour vs actual. If the bug description doesn't \
give you enough to reproduce reliably, say so and propose what's missing.

2. **ROOT CAUSE** — the file:line where the bug originates, with reasoning. \
Be specific. "Probably somewhere in the auth flow" is not a root cause; \
"`tools/foo.py:142`'s early return drops the X header before Y validates" \
is. If you don't have enough code to identify the root cause, say so and \
list the files you'd need to read.

3. **MINIMAL FIX** — the smallest change that would fix the bug. Provide \
a code snippet (not just prose). Format as a unified diff if you can. \
If multiple fixes are plausible, pick one and defend it.

4. **REGRESSION TEST** — the test that would have caught this bug. Be \
precise: which file, which test name, which assertions. Don't say "add a \
test"; say "in `tests/test_foo.py` add `test_x_drops_y_header_on_z`, \
asserting that ...". If a test framework convention applies, use it.

5. **BLAST RADIUS** — what else this bug could be affecting that the user \
hasn't noticed yet. Adjacent code paths that share the same root cause. \
"(none)" if you're confident the bug is isolated.

6. **WHAT YOU MISSED** — speculate about what the original implementer \
likely didn't consider. Was there an edge case they assumed away? A spec \
they didn't read? A failure mode they couldn't see in their dev environment? \
This section is the most valuable for preventing the next bug like this — \
do not skip it.

If a section legitimately has nothing to say, write "(none)" — do not pad. \
Do not flag style/preference unless it's directly relevant to the bug.

You have to commit to a position. The judge will synthesize the panel into \
a single fix proposal at the end; if you're hand-wavy, your perspective will \
get out-voted. Be specific, cite file:line, write actual code.

Begin your investigation. This is round 1; in round 2 you'll see the other \
panelists' takes and must engage directly — what did they get wrong, where \
did they convince you, what's your revised position?
"""


def _extract_start_status(
    start_result: list[TextContent],
) -> tuple[str | None, str | None]:
    """Parse start_task's response. Returns (status, error_message).

    start_task returns ``{"status": "started", "task_id": "..."}`` on
    success and ``{"status": "error", "error": "..."}`` on refusal
    (admission control, unknown wrapped tool, etc.) WITHOUT raising.
    Bugfind needs to distinguish these so it doesn't lie about
    successful dispatch. (Audit-flagged.)
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


def _extract_task_id(start_result: list[TextContent]) -> str | None:
    """start_task returns a JSON-encoded ToolOutput with task_id inside."""
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
        if isinstance(body.get("task_id"), str):
            return body["task_id"]
        content = body.get("content")
        if isinstance(content, str):
            try:
                inner = json.loads(content)
                if isinstance(inner, dict) and isinstance(inner.get("task_id"), str):
                    return inner["task_id"]
            except (json.JSONDecodeError, ValueError):
                pass
    return None

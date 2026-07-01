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


# Same default panel as multiaudit: codex + gemini + sonnet 5 + opus 4.8.
# Both Anthropic slots are OAuth-first via the user's Claude subscription
# (routed through clink by ``providers/oauth_first.py``: sonnet → clink
# 'claude', opus → clink 'claude_opus' with --model opus), with paid-API
# fallback on quota landing on the same model. Override
# via env / per-call args. Keep ``host`` opt-in (Claude Code doesn't
# advertise sampling capability today; including it polluted every audit
# with a "host failed" row).
#
# DELIBERATELY IMMUTABLE: the import-time fallback is the hardcoded
# 4-model list; env-driven overrides are read fresh inside ``execute()``.
# An earlier version mutated this at import time, which created a stale-
# defaults bug after live env clearing (panel-flagged in the bugfind
# audit). Don't reintroduce module-level mutation.
# Anthropic slots use the dict-form panelist spec so debate-round peer
# headers say ``=== PEER PANELIST: sonnet ===`` / ``opus ===`` rather
# than two near-identical model-id rows. See the matching block in
# tools/multiaudit.py for the rationale; behaviour is identical here.
DEFAULT_PANELISTS: tuple[Any, ...] = (
    "codex",
    "gemini",
    {"agent": "claude-sonnet-5", "label": "sonnet"},
    {"agent": "claude-opus-4-8", "label": "opus"},
)
DEFAULT_DEBATE_ROUNDS = 1
DEFAULT_PANELIST_TIMEOUT_S = 1800  # see multiaudit comment — claude needs room
# on deep rubrics; 600s prevents the slow-but-thorough panelist from getting
# truncated mid-investigation.

# Caps to keep context windows sane, but generous per the "don't be cheap"
# directive — real bug investigations need full files and full log tails,
# not chopped excerpts. Earlier defaults were clipping later attachments
# silently. All env-overridable for operators who want it tighter.
_LOG_TAIL_CHAR_CAP = int(os.environ.get("PANEL_BUGFIND_LOG_TAIL_CAP", "60000"))
# Per attached file. 200 KB easily holds a 5-10k-line source file; super-
# large generated files would still get clipped, which is fine — those
# usually aren't what's interesting in a bug investigation.
_FILE_CHAR_CAP = int(os.environ.get("PANEL_BUGFIND_FILE_CAP", "200000"))
# Total ceiling on the entire prompt: bug description + attached files +
# log tail + commits. Under every default panelist's context window
# (Gemini 1M, Claude / GPT-5.5 200K, Grok-4.3 128K), so the cap is a
# "no single bugfind eats the whole budget" guardrail. 1 MB lets you
# attach ~5 large files without losing the tail to truncation.
_TOTAL_CONTEXT_CHAR_CAP = int(os.environ.get("PANEL_BUGFIND_TOTAL_CAP", "1000000"))
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
            "adversarial debate panel (codex + gemini + claude-sonnet-5 + claude-opus-4-8 "
            "by default, 1 debate round, codex as judge) with a structured "
            "rubric: REPRO / ROOT CAUSE / MINIMAL FIX / REGRESSION TEST / "
            "BLAST RADIUS / WHAT YOU MISSED. Returns the task_id + live web "
            "viewer URL + a summary line. The judge synthesises a final fix "
            "proposal the user can review and apply. "
            "**Already async — call DIRECTLY, do NOT wrap in start_task.** "
            "bugfind dispatches the panel via start_task internally and "
            "returns immediately with a task_id pointing at that inner run. "
            "Wrapping it in another start_task causes a confusing 0s 'completed' "
            "hook (the outer wrapper genuinely finishes in ~90ms, while the real "
            "investigation runs under an inner task_id you'd never see). The "
            "server rejects the double-wrap with a clear error to enforce this. "
            "**FIRE AND FORGET after dispatch.** Hand the user the web viewer "
            "URL from the response, tell them the investigation is running, "
            "and END YOUR TURN. Do NOT silently poll task_status / task_result "
            "in a loop — the Stop hook delivers the synthesised fix proposal "
            "via system-reminder automatically when the panel finishes (with "
            "the digest inline). Polling shows the user nothing while it runs "
            "and wastes turns. Only check status if the user explicitly asks. "
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
                        "capped at ~200KB per file; truncated files get a marker. "
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
                        "'claude-sonnet-5' / 'gpt-5.5')."
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
                    "maximum": 3600,
                    "description": "Per-panelist timeout (default 1800s).",
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
            repo_root=cwd,
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
        from tools.shared.task_dispatch import extract_start_status

        start_status, start_error = extract_start_status(start_result)
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
            f"({', '.join(_panelist_display(panelists))}), {debate_rounds} debate "
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


def _panelist_display(panelists: list[Any]) -> list[str]:
    """Render mixed string / dict-form panelist specs as human-readable
    names for summary lines (see ``tools/multiaudit.py`` for the matching
    helper; behaviour is identical)."""

    out: list[str] = []
    for entry in panelists:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            out.append(str(entry.get("label") or entry.get("agent") or entry))
        else:
            out.append(str(entry))
    return out


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
    from the log file. Filters to ERROR / Traceback / Failed / Exception
    lines and a handful of trailing lines for stack-trace continuation.
    Empty string if no logs are present.

    **Output is redacted via** :func:`utils.redaction.redact_secrets`
    before return because the caller inlines this verbatim into a
    panel prompt that ships to OpenAI / Anthropic / Gemini / xAI. An
    ERROR line that echoed an API key, Bearer header, or JWT would
    otherwise leak to all 4 providers' request logs. Codex audit-flagged.
    """

    from utils.redaction import redact_secrets

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
            # Redact AFTER truncation/filtering — strips API keys / JWTs /
            # Bearer headers / HOME paths from the final string before it
            # leaves this process for a multi-provider panel prompt.
            return redact_secrets(kept)

    return ""


def _read_file_capped(path: str, char_cap: int) -> tuple[str | None, bool]:
    """Read a file, return (content, truncated) or (None, False) on
    failure. Skips binary / unreadable files cleanly.

    Content is redacted via :func:`utils.redaction.redact_secrets` before
    return — the schema warns users that ``attached_files`` contents
    are sent verbatim to panelist APIs, but a defence-in-depth
    redaction pass strips obvious API keys / JWTs / Bearer tokens
    in case the user accidentally attached a file containing them.
    """
    from utils.redaction import redact_secrets

    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None, False
        # Refuse files > 5MB as a sanity check
        if p.stat().st_size > 5 * 1024 * 1024:
            return f"[file {path} too large to attach: {p.stat().st_size} bytes]", True
        text = p.read_text(encoding="utf-8", errors="ignore")
        truncated = len(text) > char_cap
        if truncated:
            text = text[:char_cap] + f"\n\n[…truncated {len(text) - char_cap} chars]"
        return redact_secrets(text), truncated
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
    repo_root: str,
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

    return f"""You are a senior engineer who's about to be paged for this bug \
in production. The user can't reproduce it on demand — your job is to find \
the root cause from the evidence below and propose the smallest fix that \
won't make things worse. Adversarial, opinionated, specific.

REPO ROOT: `{repo_root}`
If you're a CLI agent (codex / gemini / claude), open any file in that repo \
directly with your read tool when the inline evidence isn't enough. If you're \
an API model without file tools (grok), reason from the inline content and \
mark unverifiable claims as MED/LOW confidence.

=== BUG DESCRIPTION ===
{bug_description}\
{commits_section}\
{log_section}\
{files_section}

=== HARD RULES ===
- No "this might be" / "it could be" / "perhaps" / "consider". Commit to a \
position. If you're uncertain, tag it LOW confidence — don't hedge in prose.
- Do not invent code that isn't shown. If your hypothesis depends on code \
you can't see, say so explicitly under `EVIDENCE GAPS` and name the files \
you'd need.
- Do not pad. If a section has nothing real to say, write `(none)`.
- Tag every hypothesis and finding with **confidence** (HIGH = the evidence \
shows it; MED = consistent with evidence + strong reasoning; LOW = pattern \
match suspicion).

=== OUTPUT STRUCTURE ===

1. **REPRO** — exact steps to reproduce. What sequence of inputs, timing, \
or environment triggers it? Expected vs actual. If the description doesn't \
give you a deterministic repro, say so and propose what would make it \
deterministic (a specific input, env var, race window, log line to grep).

2. **ROOT CAUSE** — `[HIGH|MED|LOW]` `file:line` — one sentence stating the \
defect, then 2-4 sentences of reasoning that ties evidence (log lines, \
commit messages, attached file contents) to the code. Bad: "probably somewhere \
in the auth flow". Good: "`tools/foo.py:142`'s early return drops the X-Auth \
header before `validate_request` reads it; the log tail's `KeyError: 'x-auth'` \
on line 89 of the trace is consistent". If multiple plausible root causes, \
list each with its own confidence and pick the one you'd debug first.

3. **MINIMAL FIX** — the smallest change that fixes the root cause WITHOUT \
introducing regressions. Show the actual code as a unified diff:
   ```diff
   --- a/path/to/file.py
   +++ b/path/to/file.py
   @@ -line,N +line,M @@
   -broken line
   +fixed line
   ```
   Or ≤10 lines of corrected code if the diff format isn't natural. Not \
"refactor this" — show the change. State explicitly what the fix does NOT \
address (related issues that need separate fixes).

4. **REGRESSION TEST** — the test that would have caught this. Be precise: \
file path, test name, the assertion. Match the repo's existing test style — \
look at `tests/` if available. Bad: "add a test". Good: "in \
`tests/test_foo.py`, add `def test_drops_xauth_on_early_return():` asserting \
that `validate_request` raises `MissingHeaderError` when the early-return \
path runs". If the repo has no test infrastructure, say so and propose a \
manual repro instead.

5. **BLAST RADIUS** — what else has this same root cause that the user \
hasn't reported yet? Adjacent code paths, other callers of the broken \
function, similar patterns elsewhere in the codebase. `(none)` if you're \
confident it's isolated and explain why.

6. **CONCURRENCY / TIMING / DATA-LOSS angle** — production bugs are often \
race conditions, retry double-fires, half-deployed state, or stale cache. \
Address each that applies, `(none)` for those that don't:
   - Could this be a race? (shared state, ordering assumption, async/await)
   - Could this be a retry making things worse? (idempotency)
   - Could this only show up under partial deployment? (old/new client mix)
   - Could the proposed fix lose data if it lands and then gets rolled back?

7. **WHAT YOU MISSED — what the original implementer likely didn't consider** \
— this section is the most valuable for preventing the next bug like this. \
An edge case they assumed away? A spec they didn't read? A failure mode \
invisible in dev? Be specific, not generic ("they didn't think about \
concurrency" is generic; "they assumed the request always carries `X-Auth` \
because their dev environment's middleware injects it" is specific).

8. **EVIDENCE GAPS** — what couldn't you verify from the description, \
commits, log tail, and attached files? List each gap and what you'd need \
to close it. Forces honesty: if your `HIGH` confidence root cause actually \
rests on assumptions, downgrade it.

The judge will synthesise the panel into a single fix proposal. If you're \
hand-wavy, your perspective gets out-voted by panelists who showed actual \
code. Cite `file:line` for every claim, write actual diffs, commit to a \
confidence level.

This is round 1. In round 2 you'll see the other panelists' takes and must \
engage directly: for each peer, name their single strongest finding and \
either **CONCEDE** (one line on what they saw that you missed) or \
**COUNTER** (specific reason their hypothesis is wrong — cite evidence). \
Vague "I mostly agree" is not acceptable in round 2.
"""


# Both ``_extract_start_status`` and ``_extract_task_id`` previously
# lived here; moved to ``tools/shared/task_dispatch.py`` so multiaudit
# and bugfind don't cross-couple on private helpers. Audit-flagged
# (the previous extraction was incomplete — left _extract_task_id
# duplicated in both tools).
from tools.shared.task_dispatch import extract_task_id as _extract_task_id  # noqa: E402

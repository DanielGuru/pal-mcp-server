"""multiaudit MCP tool — magic-phrase trigger for a PR-shaped audit.

The user-facing flow we want
----------------------------
1. Claude Code finishes a PR-shaped change.
2. User says "OK multiaudit it now" (or "audit this", "panel this PR", etc.).
3. Claude Code calls this tool.
4. Tool reads `git diff` for the current branch (or uncommitted changes if
   none vs main), grabs recent commit messages for context, builds a
   structured audit prompt with an explicit rubric, fires
   `start_task('panel', ...)` with the requested debate setup, and returns
   the task_id + the live web-viewer URL + a one-line summary.
5. User opens the URL and watches the debate.
6. Claude Code can poll task_status / run_tree to surface intermediate
   findings before the panel finishes.
7. Final headline + judge synthesis come back when the panel completes.

Why a dedicated tool, not "just call panel"
-------------------------------------------
- Reading the diff + building the right prompt every time is mechanical;
  delegating it to Panel keeps Claude Code's workflow tight ("multiaudit"
  → one tool call → here's the URL).
- The audit rubric is opinionated and consistent across runs — bugs,
  security, perf, design, missing tests. Lets the panel actually compare
  apples to apples between runs.
- Passing the diff through Panel means the audit is captured in the
  execution graph against a real run_id the user can replay later.
- Composes cleanly with all the existing infrastructure: panel,
  start_task, OAuth fallback, redaction, the web viewer.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.types import TextContent

from tools.models import ToolModelCategory
from tools.shared.base_models import ToolRequest
from tools.shared.base_tool import BaseTool

logger = logging.getLogger(__name__)


# Default 4-way debate covering the four current frontier model families
# at zero / minimal cost:
#   - codex   : OpenAI flagship via clink OAuth (free).
#   - gemini  : Google flagship via clink OAuth (free, falls back to paid
#               on quota).
#   - claude  : Anthropic Opus via clink OAuth (free with the user's
#               Claude subscription).
#   - grok-4.3: xAI flagship via paid API (no OAuth path on xAI).
#
# 'host' is intentionally NOT in the defaults: Claude Code (the typical
# host) does not advertise the MCP sampling capability today, so 'host'
# always fails immediately. Including it in the defaults polluted every
# multiaudit with a "host failed" row. Operators who run Panel under a
# host that DOES support sampling (or want to invite the host explicitly)
# can pass panelists=["host", "codex", ...] manually.
# DELIBERATELY IMMUTABLE: the import-time fallback is the hardcoded
# 4-model tuple; env-driven overrides are read fresh inside ``execute()``.
# An earlier version mutated this at import time, which created a stale-
# defaults bug after live env clearing (panel-flagged in the multiaudit
# audit, mirroring the bugfind fix). Don't reintroduce module-level
# mutation.
DEFAULT_PANELISTS = ("codex", "gemini", "claude", "grok-4.3")
DEFAULT_DEBATE_ROUNDS = 1
DEFAULT_PANELIST_TIMEOUT_S = 300

# Cap the diff payload we forward to panelists so a 50KB diff doesn't blow
# the context window. We surface a clear marker when this fires so the
# panel knows it's only reasoning about a subset.
_DIFF_CHAR_CAP = 60_000
# Recent commits we include for "what's the user trying to accomplish"
# context — short messages, not full diffs.
_RECENT_COMMITS_COUNT = 8


class MultiauditTool(BaseTool):
    """Package the current branch's changes as a multi-model audit panel."""

    def get_name(self) -> str:
        return "multiaudit"

    def get_description(self) -> str:
        return (
            "Trigger a multi-model audit of the current branch's changes. "
            "Reads the git diff (vs main, or uncommitted if no diff vs main), "
            "packages it with intent context from recent commits, and fires "
            "an adversarial debate panel (codex + gemini + claude + grok-4.3 by default, "
            "1 debate round, codex as judge). Returns the task_id + live web "
            "viewer URL + a summary line. The user opens the URL to watch the "
            "debate; you poll task_status / run_tree for intermediate findings. "
            "Use this when the user says 'multiaudit', 'audit this', 'audit "
            "this PR', 'panel this branch', 'review with all models', or any "
            "similar phrasing meaning 'fan this work out to multiple models for "
            "an opinionated review before I commit/push'."
        )

    def get_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "extra_context": {
                    "type": "string",
                    "description": (
                        "Optional extra context for the panel: what the change "
                        "is intended to do, what specifically to scrutinise, any "
                        "prior reviewer concerns. Will be appended to the audit "
                        "prompt verbatim."
                    ),
                },
                "base_branch": {
                    "type": "string",
                    "description": (
                        "Branch to diff against (default: 'main'). Use 'HEAD' "
                        "to audit only uncommitted changes."
                    ),
                },
                "panelists": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Override the default panelist list. Each entry is an "
                        "agent name as panel.py expects (clink CLI name like "
                        "'codex'/'gemini', or a paid model id like 'grok-4.3' / "
                        "'gpt-5.5')."
                    ),
                },
                "judge": {
                    "type": "string",
                    "description": "Agent that synthesises the final headline. Defaults to PANEL_MULTIAUDIT_JUDGE env var, else 'codex'. Use any panelist name (e.g. 'claude', 'gemini', 'grok-4.3', 'codex') or any valid model id.",
                },
                "debate_rounds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                    "description": (
                        "Adversarial debate rounds after the initial parallel "
                        "fan-out. Default 1. Set 0 for fan-out + judge only, 2-3 "
                        "for deeper pressure-testing (slower)."
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
                        "Absolute path to the git repo to audit. Defaults to "
                        "the server's CWD; override when running Panel from a "
                        "different working directory than the project."
                    ),
                },
            },
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
        cwd = arguments.get("working_directory_absolute_path") or str(Path.cwd())
        cwd_path = Path(cwd)
        if not cwd_path.exists() or not (cwd_path / ".git").exists():
            return _err(
                f"Not a git repository: {cwd}. Pass working_directory_absolute_path "
                "to point at the repo you want audited."
            )

        base_branch = arguments.get("base_branch") or "main"
        # Reject refs that would be parsed as git options. shell=False blocks
        # true shell-injection (we use ["git", *argv]) but git itself treats
        # any argv element starting with `-` as an option flag — `--upload-pack`,
        # `--exec`, etc. — so a malicious base_branch like '--upload-pack=evil'
        # would still execute via the git command itself.
        if not isinstance(base_branch, str) or not base_branch:
            return _err("'base_branch' must be a non-empty string")
        if base_branch.startswith("-"):
            return _err(
                f"invalid base_branch {base_branch!r}: refs starting with '-' "
                "are interpreted as git options. Use a real branch / tag / SHA."
            )
        # Lightweight character whitelist — matches what git itself accepts
        # in a ref name plus a few path-style characters for SHAs and
        # remote-tracking refs (e.g. 'origin/main', 'v1.2.3', 'a1b2c3d').
        if not all(c.isalnum() or c in "/._-" for c in base_branch):
            return _err(
                f"invalid base_branch {base_branch!r}: must contain only "
                "alphanumerics and the characters '/', '.', '_', '-'."
            )

        extra_context = arguments.get("extra_context") or ""
        # Resolve env defaults at execute() time, not import time. The
        # settings tab mutates os.environ live; freezing these at module
        # load made the live-judge / live-panelists toggles a lie.
        # Round-3 panel-flagged.
        env_judge = (os.environ.get("PANEL_MULTIAUDIT_JUDGE") or "").strip()
        env_panelists = (os.environ.get("PANEL_MULTIAUDIT_PANELISTS") or "").strip()
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

        # ------ collect git context ------
        try:
            current_branch = _git(cwd, ["branch", "--show-current"]).strip() or "(detached)"
            diff_vs_base = _git(cwd, ["diff", f"{base_branch}...HEAD"], allow_empty=True)
            uncommitted = _git(cwd, ["diff", "HEAD"], allow_empty=True)
            staged = _git(cwd, ["diff", "--cached"], allow_empty=True)
            recent_commits = _git(
                cwd,
                ["log", f"-n{_RECENT_COMMITS_COUNT}", "--pretty=format:%h %s"],
                allow_empty=True,
            )
            files_changed = _git(
                cwd,
                ["diff", "--name-only", f"{base_branch}...HEAD"],
                allow_empty=True,
            ).strip()
        except _GitError as exc:
            return _err(f"git command failed: {exc}")

        diff_blob, diff_source = _pick_diff(diff_vs_base, uncommitted, staged)
        if not diff_blob.strip():
            return _err(
                f"No changes to audit. Tried '{base_branch}...HEAD', uncommitted, "
                "and staged — all empty. Make some changes first or pass a "
                "different base_branch."
            )
        diff_blob, truncated = _cap(diff_blob, _DIFF_CHAR_CAP)

        # ------ build the audit prompt ------
        prompt = _build_audit_prompt(
            current_branch=current_branch,
            base_branch=base_branch,
            diff_source=diff_source,
            diff_blob=diff_blob,
            diff_truncated=truncated,
            recent_commits=recent_commits,
            files_changed=files_changed,
            extra_context=extra_context,
        )

        # ------ dispatch the panel via start_task ------
        from server import execute_tool
        from tools.shared.base_tool import mark_internal_payload

        panel_args = {
            "tool": "panel",
            "label": f"multiaudit:{current_branch}",
            "arguments": {
                "prompt": prompt,
                "panelists": panelists,
                "judge": judge,
                "debate_rounds": int(debate_rounds),
                "panelist_timeout_s": float(timeout_s),
            },
        }
        try:
            # mark_internal_payload signals to size-check gates that this
            # prompt is Panel-generated (the diff package + audit rubric we
            # just built), not raw user input. ContextVar inheritance
            # propagates the marker through start_task → TaskManager._run →
            # panel → panelists, so all nested size checks bypass cleanly.
            with mark_internal_payload():
                start_result = await execute_tool("start_task", panel_args)
        except Exception as exc:  # noqa: BLE001
            return _err(f"start_task dispatch failed: {type(exc).__name__}: {exc}")

        # start_task returns structured error payloads (admission control
        # failure, unknown wrapped tool, etc.) WITHOUT raising. Without
        # this guard, multiaudit would happily report "started" with
        # task_id=null and tell the user to poll a task that doesn't
        # exist — operational lie at the worst possible moment.
        # Shared helper lives in tools/shared/ now to avoid the
        # cross-tool private-import coupling the panel called out.
        from tools.shared.task_dispatch import extract_start_status

        start_status, start_error = extract_start_status(start_result)
        if start_status != "started":
            return _err(
                f"start_task refused dispatch: {start_error or 'unknown error'} "
                f"(status={start_status!r}). The audit panel was NOT started."
            )
        task_id = _extract_task_id(start_result)
        if not task_id:
            return _err(
                "start_task returned status=started but no task_id was found. "
                "The panel may or may not have started; this is a server-side "
                "contract violation."
            )

        # ------ get the web viewer URL if available ------
        # Deep-link to this multiaudit's own run so the operator's auto-opened
        # tab lands on the run that was just dispatched, not whatever the
        # picker happened to auto-pick. current_run_id() returns the
        # multiaudit run we're inside (set by execute_tool via run_context).
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
            f"Multiaudit dispatched against {current_branch} ({diff_source}) — "
            f"{len(panelists)} panelists ({', '.join(panelists)}), "
            f"{debate_rounds} debate round{'s' if debate_rounds != 1 else ''}, "
            f"judge={judge}, ~{len(diff_blob)} chars of diff."
        )

        payload: dict[str, Any] = {
            "status": "started",
            "summary": summary,
            "task_id": task_id,
            "branch": current_branch,
            "base_branch": base_branch,
            "diff_source": diff_source,
            "diff_truncated": truncated,
            "panelists": panelists,
            "judge": judge,
            "debate_rounds": int(debate_rounds),
            "files_changed": files_changed.split("\n") if files_changed else [],
            "next_steps": [
                "Open the web viewer URL below to watch the debate live.",
                "Poll task_status(task_id) for high-level progress.",
                "When complete, call run_tree(run_id, mode='transcript') to read the panelist verdicts + judge synthesis as clean text — same view as the live viewer page. Pull the run_id from web_viewer_url's ?run=<id> query param.",
                "Or: task_result(task_id, wait_seconds=N) for the synthesized final headline.",
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


class _GitError(RuntimeError):
    pass


def _git(cwd: str, argv: list[str], *, allow_empty: bool = False) -> str:
    """Run a git command, return stdout. Raises _GitError on non-zero unless
    allow_empty (in which case empty output is returned)."""
    try:
        out = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        if allow_empty and exc.returncode == 1:
            # `git diff` returns 1 when there are differences in some modes;
            # for our usage that's fine — return whatever stdout we got.
            return exc.stdout or ""
        raise _GitError(f"git {' '.join(argv)}: {exc.stderr.strip() or exc}")
    except FileNotFoundError as exc:
        raise _GitError(f"git executable not found: {exc}")
    except subprocess.TimeoutExpired as exc:
        raise _GitError(f"git {' '.join(argv)}: timed out: {exc}")
    return out.stdout or ""


def _pick_diff(
    diff_vs_base: str, uncommitted: str, staged: str
) -> tuple[str, str]:
    """Pick the most useful diff payload to audit.

    Priority:
      1. Diff against base_branch (the PR-shaped view) if non-empty.
      2. Uncommitted changes (working tree + index) if any.
      3. Staged-only changes as a last resort.

    Returns (diff_blob, source_label) so the panel knows what it's seeing.
    """
    if diff_vs_base.strip():
        return diff_vs_base, "branch vs base"
    if uncommitted.strip():
        return uncommitted, "uncommitted changes"
    if staged.strip():
        return staged, "staged changes"
    return "", "(none)"


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    truncated = text[:limit]
    truncated += f"\n\n[…truncated {len(text) - limit} chars by multiaudit cap]"
    return truncated, True


def _build_audit_prompt(
    *,
    current_branch: str,
    base_branch: str,
    diff_source: str,
    diff_blob: str,
    diff_truncated: bool,
    recent_commits: str,
    files_changed: str,
    extra_context: str,
) -> str:
    truncation_note = (
        "\n\n(Diff was truncated; do not assume you've seen all changes — "
        "if your verdict depends on something outside the truncated window, "
        "say so explicitly.)"
        if diff_truncated
        else ""
    )
    extra_section = (
        f"\n\n=== EXTRA CONTEXT FROM AUTHOR ===\n{extra_context.strip()}"
        if extra_context.strip()
        else ""
    )
    files_section = (
        f"\n\n=== FILES CHANGED ===\n{files_changed.strip()}"
        if files_changed.strip()
        else ""
    )
    commits_section = (
        f"\n\n=== RECENT COMMITS (intent context) ===\n{recent_commits.strip()}"
        if recent_commits.strip()
        else ""
    )

    return f"""You are a panelist in an adversarial multi-model code-review audit. \
Be opinionated. Defend your position. Only change your mind when convinced.

The change you're reviewing is on branch `{current_branch}` (vs `{base_branch}`, \
diff source: {diff_source}). Output structure required:

1. **VERDICT** — one paragraph. Land/don't-land/needs-changes. Lead with that.
2. **BUGS** — concrete defects you'd want fixed before merging. Cite file:line. \
Do not flag style/preference as a bug.
3. **DESIGN CONCERNS** — load-bearing design decisions you'd push back on. \
Be specific about what to change and why.
4. **SECURITY** — anything that loosens a security boundary, leaks secrets, \
mishandles untrusted input, or removes a check.
5. **MISSING TESTS** — behaviour the diff introduces or modifies that has no \
test coverage. Be precise about what should be tested, not "add more tests".
6. **WHAT YOU'D ATTACK** — if you wanted to break this PR in production, where \
would you aim? One scenario per bullet. This is the most valuable section — \
do not skip it.

If a section legitimately has nothing to flag, write "(none)" — do not pad. \
Do not flag "consider adding a comment" or "rename this variable" type nits \
unless they materially affect correctness or maintainability.

You are reading the actual diff. Cite file:line for every claim. Do not \
hallucinate code that isn't shown — if you need more context, say so explicitly \
and reason about what's visible.{truncation_note}\
{extra_section}\
{files_section}\
{commits_section}

=== DIFF ===
{diff_blob}

=== END DIFF ===

Begin your audit. This is round 1; in round 2 you'll see the other panelists' \
takes and must engage directly — what did they get wrong, where did they convince \
you, what's your revised position?
"""


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
        # Direct shape (start_task returns this verbatim).
        if isinstance(body.get("task_id"), str):
            return body["task_id"]
        # Wrapped-ToolOutput shape — content field holds the JSON we want.
        content = body.get("content")
        if isinstance(content, str):
            try:
                inner = json.loads(content)
                if isinstance(inner, dict) and isinstance(inner.get("task_id"), str):
                    return inner["task_id"]
            except (json.JSONDecodeError, ValueError):
                pass
    return None


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"status": "error", "error": message}, indent=2))]

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
DEFAULT_PANELIST_TIMEOUT_S = 600  # 10 min. Claude does deep file investigation
# on audit prompts and consistently exceeds 300s on the multiaudit rubric;
# 600s gives him room to finish without trading off depth. Codex/gemini/grok
# typically finish in 30-90s and this only matters when the slowest panelist
# stretches.

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
            "Reads the git diff (vs main, then uncommitted, then staged, then "
            "the last commit if all of those are empty — so it works equally "
            "well before OR after you've committed), "
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
                        "Optional directive for the panel — treat this as the "
                        "specific question you want answered, not just background. "
                        "Use it to narrow scope (\"focus only on the auth changes "
                        "in middleware.py\"), exclude noise (\"ignore the 30-file "
                        "rename, it's mechanical\"), pose a specific question "
                        "(\"is the SQL-injection fix in db.py actually safe under "
                        "concurrent writes?\"), or share intent (\"this is a "
                        "hot-fix for the prod outage filed as INC-1247\"). When "
                        "set, panelists lead with this directive and filter the "
                        "standard rubric sections to what's relevant. Inlined "
                        "verbatim into the audit prompt."
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
            # Last-commit fallback: when branch == base_branch and the user
            # has already committed, the three primary diffs are all empty.
            # Audit HEAD~1..HEAD instead so the tool stays useful post-commit.
            # Tolerate the "no parent" edge case (initial commit) silently.
            try:
                last_commit = _git(
                    cwd, ["diff", "HEAD~1..HEAD"], allow_empty=True
                )
                last_commit_files = _git(
                    cwd, ["diff", "--name-only", "HEAD~1..HEAD"], allow_empty=True
                ).strip()
            except _GitError:
                last_commit = ""
                last_commit_files = ""
        except _GitError as exc:
            return _err(f"git command failed: {exc}")

        diff_blob, diff_source = _pick_diff(
            diff_vs_base, uncommitted, staged, last_commit
        )
        # When falling back to the last commit, surface its files in
        # files_changed too — otherwise the response payload reports an
        # empty list while the panel is reasoning about real changes.
        if diff_source == "last commit (HEAD)" and last_commit_files:
            files_changed = last_commit_files
        if not diff_blob.strip():
            return _err(
                f"No changes to audit. Tried '{base_branch}...HEAD', uncommitted, "
                "staged, and last commit (HEAD~1..HEAD) — all empty. Make some "
                "changes first or pass a different base_branch."
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
            repo_root=cwd,
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
    diff_vs_base: str,
    uncommitted: str,
    staged: str,
    last_commit: str = "",
) -> tuple[str, str]:
    """Pick the most useful diff payload to audit.

    Priority:
      1. Diff against base_branch (the PR-shaped view) if non-empty.
      2. Uncommitted changes (working tree + index) if any.
      3. Staged-only changes.
      4. Last commit (HEAD~1..HEAD) so multiaudit still works post-commit
         — this is the common case when you've already pushed and want the
         models to look at what just landed.

    Returns (diff_blob, source_label) so the panel knows what it's seeing.
    """
    if diff_vs_base.strip():
        return diff_vs_base, "branch vs base"
    if uncommitted.strip():
        return uncommitted, "uncommitted changes"
    if staged.strip():
        return staged, "staged changes"
    if last_commit.strip():
        return last_commit, "last commit (HEAD)"
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
    repo_root: str,
) -> str:
    truncation_note = (
        "\n\n(Diff was truncated by Panel's char cap; the FILES CHANGED list "
        "below is complete. Open the files you actually need from REPO ROOT "
        "via your CLI's file-read tool — do not assume you've seen everything "
        "in the inline diff window.)"
        if diff_truncated
        else ""
    )
    extra_section = (
        "\n\n=== AUTHOR DIRECTIVE — focus / ignore / specific question ===\n"
        f"{extra_context.strip()}\n"
        "(Treat the directive above as the primary question to answer. The "
        "standard rubric sections are still required, but filter each section "
        "to what's actually relevant to this directive — do not pad sections "
        "with material outside the directive's scope. If the directive narrows "
        "the audit, narrow your VERDICT and BUGS lists accordingly; broader "
        "concerns can go in OMISSIONS as 'directive-scoped: out of scope, but "
        "you should also know X'.)"
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

    return f"""You are a senior engineer doing the kind of code review you'd \
do for a teammate whose change you're going to be on call for. Adversarial, \
opinionated, specific. You're not here to be polite. You're here to find what \
breaks before it ships.

REPO ROOT: `{repo_root}`
The change is on branch `{current_branch}` (vs `{base_branch}`, source: \
{diff_source}).

=== HOW TO INVESTIGATE ===
- If you are a CLI agent (codex / gemini / claude) you have READ access to the \
repo root above. The inline diff is a starting point, not the boundary — open \
files you need with your CLI's read tool. For big PRs the diff is truncated; \
the FILES CHANGED list is complete.
- If you are an API model without file tools (grok, etc.), reason strictly \
from the inline diff + FILES CHANGED + RECENT COMMITS. Mark anything that \
would require reading a file you don't have as MED/LOW confidence — don't \
guess at line numbers.
- DO NOT run typecheckers (tsc, mypy, pyright), test runners (pytest, jest, \
vitest, go test), linters, build commands, or installers. Your job is to READ \
code, not execute it. CI runs those separately.
- DO NOT respond with `files_required_to_continue`, `files_needed`, \
`mandatory_instructions`, or any clarification-request JSON. This is \
fire-and-forget — there is no second turn where Panel feeds you more files. \
If you ask, you get nothing back and your audit slot is wasted. Read the \
files yourself (CLI agents) or reason from what's inline (API models).

=== HARD RULES ===
- No "consider X" / "you might want to" / "perhaps". Say what you mean. If \
something is wrong, say it's wrong and how to fix it.
- Do not restate the diff back. The reader already saw it. Findings only.
- Do not pad. If a section has nothing real to flag, write `(none)` and move on.
- No style nits, no "rename this variable", no "add a docstring". If your \
finding wouldn't make a teammate change the PR, drop it.
- Cite `file:line` for every concrete claim. Numbers must come from a file you \
actually opened — do not invent line numbers from a guess at what's around the \
diff window.
- Tag every concrete finding with **severity** (P0 = blocker, ship breaks; \
P1 = must-fix before merge; P2 = should-fix soon) and **confidence** (HIGH = \
I see the bug in the diff or the file I read; MED = strong reasoning, partial \
read; LOW = pattern-match suspicion, worth checking).

=== OUTPUT STRUCTURE ===

1. **VERDICT** — one short paragraph. `LAND` / `LAND WITH CHANGES` / \
`BLOCK`. Lead with that label. Add your confidence in the verdict (HIGH/MED/LOW) \
and the single biggest reason.

2. **BUGS** — actual defects, not opinions. For each:
   - `[P0|P1|P2 / HIGH|MED|LOW]` `file:line` — what's wrong (1 sentence)
   - **Fix:** a unified-diff-style snippet OR ≤5 lines of corrected code. \
Not "refactor this" — show the change.

3. **SECURITY** — anything that loosens a boundary, leaks a secret, mishandles \
untrusted input, weakens auth, broadens permissions, or strips a check. Same \
`[severity / confidence] file:line` + fix sketch shape as BUGS. Include supply-\
chain (new deps), injection surfaces, and any redaction/logging that could leak \
PII or tokens.

4. **CONCURRENCY / ROLLOUT / ROLLBACK** — production-shaped questions a static \
review misses. Address each that applies; write `(none)` for those that don't:
   - Race conditions / ordering assumptions / shared-state mutations
   - What happens half-deployed (old clients hitting new server, or vice versa)
   - Retry / idempotency behaviour on transient failure
   - Migration order if schema/contract changed
   - **Rollback story:** if this lands and breaks in prod, what's the undo? \
If "revert and redeploy" doesn't work (data written, schema changed, cache \
poisoned), say so loudly.

5. **DESIGN / ARCHITECTURE** — load-bearing decisions you'd push back on. Be \
specific about what to change and why. Include: wrong abstraction layer, \
leaking implementation through the API, premature generality, hidden coupling, \
violations of an invariant the rest of the codebase relies on.

6. **OMISSIONS — what the diff DIDN'T change but should have** — the most \
expensive bugs live here. The diff's negative space:
   - Callers/consumers of changed functions not updated
   - Public API / OpenAPI / schema / type definitions out of sync with code
   - New flag/config added but not wired through
   - New error path created but not handled upstream
   - Docs / CLAUDE.md / tests that codify the OLD behaviour
   - Rename or deletion that other files still reference

7. **MISSING TESTS** — behaviour the diff introduces or changes that has no \
test exercising it. Be precise: name the function and the failure mode, not \
"add coverage". Also flag tests that look like they cover something but only \
hit the happy path.

8. **WHAT YOU'D ATTACK** — if you wanted to break this PR in prod, where would \
you aim? Top 1-3 scenarios. One concrete attack per bullet — input shape, \
timing, scale, or environment that the author probably didn't think about.

9. **ASSUMPTIONS** — list every assumption you made that you couldn't verify \
from the diff alone (caller behaviour, framework guarantees, env config). \
Forces honesty: if half your verdict rests on assumptions, that's important \
signal.{truncation_note}\
{extra_section}\
{files_section}\
{commits_section}

=== DIFF ===
{diff_blob}

=== END DIFF ===

This is round 1. In round 2 you'll see the other panelists' takes and must \
engage directly: for each peer, name their single strongest finding and either \
**CONCEDE** (with one line on what they saw that you missed) or **COUNTER** \
(with a specific reason their finding is wrong, overstated, or out of scope). \
Vague "I mostly agree" is not acceptable in round 2.
"""


# ``_extract_task_id`` lives in ``tools/shared/task_dispatch.py`` (alongside
# ``extract_start_status``) so multiaudit and bugfind don't keep duplicate
# private helpers. Audit-flagged: the previous extraction was incomplete.
from tools.shared.task_dispatch import extract_task_id as _extract_task_id  # noqa: E402


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"status": "error", "error": message}, indent=2))]

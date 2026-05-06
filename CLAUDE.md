# CLAUDE.md — Panel MCP Server (custom fork)

Substantially rewritten fork of `BeehiveInnovations/pal-mcp-server` (formerly PAL MCP), now at `github.com/DanielGuru/panel-mcp-server`. Upstream stalled in December 2025; this fork ships fixes plus orchestration features (background tasks, parallel panels, adversarial debate, observable streaming, OAuth-to-API fallback, central validated dispatch, bounded provider concurrency) that don't exist upstream. **Don't assume parity with upstream PAL** — read this doc, not the upstream README.

If you are an AI agent working on this fork: this file is for you.

---

## What this is

A Model Context Protocol server that lets one AI agent (typically Claude Code) consult, orchestrate, and debate multiple other models. 29 MCP tools in six families:

1. **Direct provider tools** — `chat`, `consensus`, `codereview`, `debug`, `thinkdeep`, `precommit`, `planner`, etc. Hit OpenAI / Gemini / xAI APIs via paid keys. `tools/*.py`, `tools/simple/base.py`, `tools/workflow/`.
2. **Clink** — `clink` runs an external CLI (Codex CLI, Gemini CLI, Claude CLI) as a subprocess. Uses each CLI's own auth, so **OAuth (free)** when the CLI is logged in via subscription. `tools/clink.py` + `clink/`. Includes automatic OAuth-to-API fallback (see below).
3. **Async background tasks** — `start_task`, `task_status`, `task_result`, `cancel_task`. Wrap any other tool so the conversation isn't blocked. `tools/tasks.py`. Admission control, periodic GC, session ownership, push completion notifications.
4. **Panel orchestration** — `panel` fans one prompt to N models in parallel, optional judge synthesis, optional adversarial `debate_rounds`. Reserved panelist name `host` routes through MCP sampling so Claude Code is a peer in the debate. `tools/panel.py`.
5. **PR-shaped audit + bug-shaped investigation** — `multiaudit` reads `git diff`, packages it with intent context, and fires `start_task('panel', ...)` with `[codex, gemini, claude, grok-4.3]`. `bugfind` (sister tool, same shape) takes a `bug_description` plus auto-collected context (recent commits, error log tail, optionally attached files) and fires the same 4-way panel with a bug-investigation rubric (REPRO / ROOT CAUSE / MINIMAL FIX / REGRESSION TEST / BLAST RADIUS / WHAT YOU MISSED). `host` is intentionally excluded from defaults because Claude Code doesn't advertise MCP sampling capability today; pass `panelists=["host", ...]` explicitly when running under an MCP host that does. `tools/multiaudit.py` + `tools/bugfind.py`.
6. **Execution graph + web viewer** — `list_runs`, `get_run`, `run_tree`, `web_url`. Read-only access to the SQLite-backed durable record of every dispatch + a live HTTP viewer page that auto-opens in the browser on Panel boot. Survives Panel restart. `tools/graph_query.py` + `utils/execution_graph.py` + `utils/web_viewer.py`.

**Reading panel results — canonical path.** When a `panel`/`multiaudit`/`bugfind` task completes, call `run_tree(run_id, mode='transcript')` to get the panelist verdicts + judge synthesis as clean text — exactly what the user sees on the live web viewer page. The run_id comes from `web_viewer_url`'s `?run=<id>` query param in any panel-style response. **Do NOT scrape `task_status`'s full progress-event stream into a subagent** — that stream carries tool_use chatter, file reads, and command echoes that bury the actual verdicts (a 50KB JSON when only ~6KB is signal). The transcript mode filters to `panelist_answer` + `judge_synthesis` events, sorts chronologically, returns clean text. When the user asks "what did the panel say?" or "show me the verdicts", this is the one tool call that answers it.

---

## Magic-phrase recognition (for the AI agent reading this)

There are TWO magic-phrase tools — `multiaudit` for PR-shaped review, `bugfind` for bug-shaped investigation. Both fan one prompt to a 4-way panel and return a viewer URL. **Don't ask, don't propose alternatives — just dispatch the right one.**

### `multiaudit` — PR-shaped audit (BEFORE commit/push)

When the user says any of these, call `multiaudit`:

- "multiaudit" / "multiaudit it" / "OK multiaudit it now"
- "audit this" / "audit this PR" / "audit this branch"
- "panel this" / "panel this PR" / "panel this branch"
- "review with all" / "review with all models"
- "fan this out" / "fan it out for review"
- "what do the models think" (about a code change)
- "second opinion from everyone"

`multiaudit` reads the current branch's `git diff`, builds a structured audit prompt with the standard rubric (verdict / bugs / design / security / missing tests / what you'd attack), fires a 4-way panel via `start_task` (codex + gemini + claude + grok-4.3, 1 debate round, codex as judge), and returns a task_id + the live web viewer URL. Hand the user the URL immediately, then poll `task_status` / `run_tree` / `task_result` to surface progress and findings. When the user adds context ("multiaudit but focus on the auth changes"), pass it through as `extra_context`. The user wants the audit BEFORE you commit/push code — multiaudit is a gate, not a post-hoc check.

### `bugfind` — bug-shaped investigation

When the user says any of these, call `bugfind`:

- "bugfind" / "bugfind it" / "bugfind this"
- "find this bug" / "use panel to find the bug"
- "what's breaking" / "why is X broken"
- "panel debug this" / "diagnose with all models"
- "all four of you find the bug"

`bugfind` takes a `bug_description` (required — the user's full description of the symptom, what they expected, what actually happens), auto-attaches context (recent commits, the tail of `logs/mcp_server.log` filtered to ERROR/Traceback/Failed/Exception, and any files passed via `attached_files`), and fires the same 4-way panel — but with a DIFFERENT rubric: REPRO / ROOT CAUSE / MINIMAL FIX (with code snippet, ideally a unified diff) / REGRESSION TEST / BLAST RADIUS / WHAT YOU MISSED. The judge synthesises a single fix proposal the user can review and apply. When file paths or symbols appear in the bug description, call `bugfind` with `attached_files=[<absolute paths>]` so the panelists can read the actual code rather than guessing. Skip the log tail (`skip_log_tail=true`) for UI/doc bugs that aren't reflected in logs.

---

## Key architectural invariants

These are the load-bearing rules. Violate them and concurrency, security, or cost guarantees regress:

- **Single dispatch path.** `server.execute_tool(name, arguments)` is the only execution entrypoint. handle_call_tool, TaskManager._run, panel._run_panelist, and clink OAuth fallback all route through it. Never call `tool.execute()` directly from a new caller — file-size + model validation lives in execute_tool, and skipping it lets a 50MB file hit the paid API.
- **TOOLS are factories, not singletons.** `server.TOOLS: dict[str, type[BaseTool]]`. Construct via `make_tool(name)` for execution. Use `TOOL_DESCRIPTORS[name]` for read-only metadata only — never call `.execute()` on a descriptor. This eliminates per-call state corruption under concurrent panel fan-out.
- **Async provider calls bounded on three layers.** `agenerate_content` on `ModelProvider` runs the sync SDK on a thread, gated by:
  - `PANEL_MAX_CONCURRENT_API` semaphore (default 16) acquired *before* dispatch
  - `PANEL_MAX_PROVIDER_THREADS` ThreadPoolExecutor (default 32) caps thread leakage
  - `PANEL_API_TIMEOUT_S` (default 600) forwarded as SDK `timeout=` so threads always self-terminate
- **Sync `generate_content` stays as the canonical method.** Tests mock it directly. The async wrapper delegates; don't delete the sync version.
- **Lazy-init of the executor + semaphore is lock-protected.** `_get_provider_executor` and `_get_api_semaphore` use double-checked locking with `threading.Lock`. Don't strip the lock — concurrent first-burst calls would otherwise race the `if X is None` check and create duplicate executors (leaking threads past the cap) or duplicate semaphores (defeating the global API cap).
- **Execution graph is observability, never load-bearing.** Every `execute_tool` dispatch records a run; nested calls form a tree via the contextvars-tracked parent. Graph writes are best-effort and swallow on failure. Disabled via `PANEL_GRAPH_DB=""`. Internal-only graph hints (`_graph_edge_kind`, `_graph_cost_tier`, `_graph_label`) are popped from args at the dispatch boundary so tools never see them.
- **Execution graph is per-repo by default.** Default DB path is `<cwd>/.panel/execution_graph.db` so each project Claude Code opens has its own isolated debate history. `.panel/` is gitignored. Override with `PANEL_GRAPH_DB=<path>` for a shared/global view. The web viewer attached to a Panel instance only ever sees that instance's DB.
- **Size-check bypass is PROVENANCE-based, not depth-based.** Three size gates (`check_prompt_size`, `_validate_token_limit`, workflow `MCP_SIZE_CHECK`) skip when `is_internal_payload()` returns True. The marker is set ONLY by trusted code paths via the `mark_internal_payload()` context manager: multiaudit (its diff package), panel (round 2+ debate prompts + judge prompt), clink OAuth fallback (file-inlined prompt_text). User-supplied args going through start_task / panel / chat see marker=False and the size check fires normally. **Do not re-introduce the depth-based bypass** — that was exploitable: `start_task(tool='chat', arguments={prompt: <huge>})` made depth=2 and skipped the check on user content. Audit-flagged in v1.1.
- **Web viewer is lazy-started and per-session.** Does NOT boot at MCP server startup; the first `execute_tool` call lazy-starts it. Means: open Claude Code without using Panel → no tab. First Panel tool call → tab pops, viewer runs for the rest of the process. Process exit → viewer dies with it. Bound to `127.0.0.1` by default. Boot failures log and continue. Disabled via `PANEL_WEB_DISABLE`.
- **Live activity feed via emit_progress → graph.** `utils/progress.py` `emit_progress` writes every event into the execution graph against the current `run_context`. The viewer renders running runs' events as a prominent live activity feed (max-height scroll, accent border, colour-coded by event_type). Completed runs collapse the feed to a `<details>` summary. This is the streaming-v1 surface the user actually wanted.
- **`host` panelist is reserved for MCP sampling.** Routes through `ServerSession.create_message` to ask the connected MCP client to invoke its own LLM. Any clink CLI named `host` would be shadowed (the routing in `_is_clink_agent` returns False for `host`).
- **Host session is reachable via contextvar in nested calls.** `utils/host_session.py` exposes a ContextVar that `server.execute_tool` (live calls) and `TaskManager._run` (background calls) populate. Read with `get_host_session()`. The host panelist fails cleanly with a diagnostic if no session is reachable.
- **OAuth-first, always.** Clink calls the configured CLI via subprocess every time. If the CLI fails for a recoverable reason (TerminalQuotaError, 401, etc.), `_try_oauth_fallback` retries via `oauth_fallback_model`. Fallback failures are SURFACED, not swallowed. When quota replenishes, the next call uses the free path automatically — no state.
- **OAuth-first extends to direct-API tools.** `OAuthFirstProvider` (`providers/oauth_first.py`) wraps every registered provider when `PANEL_OAUTH_FIRST=1` (default). For models in `clink.constants.MODEL_TO_CLI` (gpt-5.5 → codex, gemini-3.1-pro-preview → gemini, claude-opus-4-7 / claude-sonnet-4-6 → claude), `agenerate_content` routes through `execute_tool('clink', ...)` first — same canonical dispatch path, same execution-graph child run, same redacted progress events. CLI not on PATH → silent fall-through to direct API with `cost_tier=api_paid`. clink raises hard (config broken / its own paid-API fallback also failed) → propagate the exception (no silent retry — clink may have already billed once; doubling-up risks two charges per call). Re-entrance guarded by the `_INSIDE_OAUTH_FIRST` ContextVar so clink's internal CLI→API fallback (which calls `execute_tool('chat', ...)` with the same flagship model) doesn't infinite-loop. Conservative exact-match mapping — non-flagship variants (`gpt-5.4`, `grok-*`, etc.) bypass the wrapper. Sync `generate_content` stays on the SDK to avoid blocking the calling thread on subprocess I/O (logs a warning when called for an OAuth-eligible model so the bypass doesn't silently bill).
- **Soft-landing on zero providers.** `configure_providers()` does NOT raise when no API keys + no OAuth CLIs are present. Used to be a hard `ValueError` that crashed startup; now logs a friendly capability summary (what's available, what's blocked, how to unlock more) and proceeds. `listmodels` / `version` / `web_url` / graph-query tools always work. Tools that need a provider (chat, consensus, codereview, etc.) surface per-call errors. Auto-mode-with-no-models is also softened from `raise` → `logger.warning`. The server is more useful as a partial install that tells the user what to fix than as a hard failure.
- **Clink metadata is redacted + capped.** `_redact_and_cap` strips API-key shapes (sk-/AIza/xai-/sk-ant-), JWTs, and Bearer headers from stdout/stderr/raw_output_file before forwarding to MCP. Truncates at `PANEL_CLINK_METADATA_CAP` / `PANEL_CLINK_RAW_OUTPUT_CAP`. Opt-out via `PANEL_DEBUG_CLI_OUTPUT=1` for local debugging only.

---

## How this fork differs from upstream

- **Trimmed model registry** to current flagships (gpt-5.5, gpt-5.4, gpt-5.1-codex, gemini-3.1-pro-preview, grok-4.3, grok-4.1-fast).
- **Safer clink defaults** — no `--dangerously-bypass-approvals-and-sandbox` (codex), no `--yolo` (gemini); replaced with `--skip-git-repo-check` and stream-json `-p` argv passing, neither of which relax security.
- **Streaming progress** for clink subprocesses (`utils/progress.py` + parser `describe_event` hook).
- **Async background-task pattern** with push completion notifications.
- **Panel + adversarial debate** as a first-class orchestration tool.
- **Factory-pattern TOOLS registry** (see invariants above).
- **Central validated dispatch** via `execute_tool()` — internal callers can no longer bypass MCP-boundary validation.
- **Async provider wrapper** with semaphore + bounded executor + per-call timeout.
- **OAuth-to-API fallback** (`tools/clink.py`). Mapping in `clink/constants.py`: gemini→gemini-3.1-pro-preview, codex→gpt-5.5. Panel reads `oauth_fallback_used` from response metadata so cost_tier honestly reports `oauth_fallback_paid` instead of mislabelling paid runs as free.
- **Clink metadata redaction.** `_redact_only` strips API-key shapes, JWTs, Bearer headers, and HOME paths from CLI stdout/stderr/raw_output_file AND the actual content. Capped via `PANEL_CLINK_METADATA_CAP` / `PANEL_CLINK_RAW_OUTPUT_CAP`. Parser-supplied metadata is filtered through `_safe_merge_parser_metadata` so a malicious or buggy CLI can't smuggle 50MB through a `command`-shaped field.
- **Panel cost_tier reads structured metadata, not substrings.** `_derive_cost_tier` JSON-parses the response and reads `metadata.oauth_fallback_used` directly — un-spoofable by a model emitting the literal phrase in its content.
- **Durable execution graph** (`utils/execution_graph.py`). SQLite + WAL, append-only events, tree-shaped runs/edges. Survives Panel restart. Powers the `list_runs` / `get_run` / `run_tree` query tools.
- **MCP handshake instructions** encode cost-routing (clink for free, chat for paid), async-routing (long calls go through start_task), and panel-routing (keyword cues for picking modes).

---

## Architecture map

```
server.py                  MCP entry point. TOOLS factory dict, TOOL_DESCRIPTORS
                           cache, make_tool(name), execute_tool(name, args) —
                           the canonical dispatch — handle_call_tool wrapper,
                           handshake instructions.
tools/
  shared/base_tool.py      BaseTool — get_name, get_input_schema, execute, etc.
  simple/base.py           SimpleTool — chat-style tools with provider integration.
                           Mutates self per call; instantiate fresh via make_tool().
  workflow/                Multi-step workflow tools (consensus, codereview, debug…).
  chat.py / clink.py / …   Concrete tool implementations.
  tasks.py                 TaskManager + 4 task tools (background pattern).
  panel.py                 Parallel fan-out + judge + adversarial debate.
                           Reads cost_tier from response metadata.
clink/
  agents/                  Per-CLI subprocess runners (Base/Gemini/Codex/Claude).
  parsers/                 Per-CLI output parsers + describe_event progress hooks.
  registry.py              Loads CLI configs from conf/cli_clients/*.json.
  constants.py             INTERNAL_DEFAULTS — per-CLI parser, args,
                           oauth_fallback_model.
providers/
  base.py                  ModelProvider — sync generate_content + async
                           agenerate_content wrapper, _run_with_retries,
                           bounded ThreadPoolExecutor + API semaphore + timeout.
  openai_compatible.py     Sync .create() calls (OpenAI/xAI/Azure shared base).
                           Forwards PANEL_API_TIMEOUT_S as SDK timeout=.
  openai.py / gemini.py /  Provider subclasses.
  xai.py / azure_openai.py
conf/
  *_models.json            Per-provider model registries (capabilities, aliases).
  cli_clients/*.json       Per-CLI clink configs (command, args, roles).
systemprompts/             System prompts (per-tool, per-clink-role).
utils/
  progress.py              MCP progress notifications + contextvar sink override.
tests/
  test_v1_hardening.py     Regression tripwires for factory / dispatch /
                           redaction / OAuth detection / panel cost / bounds.
  test_execution_graph.py  Storage layer + run_context contextvar parent
                           threading + query tools.
  test_web_viewer.py       HTTP endpoints + index page + web_url tool.
  test_multiaudit.py       Git context capture, panel dispatch, defaults.
  test_host_panelist.py    MCP sampling path — session, capability, error
                           modes, empty content, routing.
utils/
  execution_graph.py       SQLite-backed durable record of every dispatch.
                           Lazy-init singleton, best-effort writes, optional.
  web_viewer.py            stdlib HTTP server in a daemon thread; auto-open
                           browser on boot; serves the live viewer page.
  host_session.py          ContextVar holding the active MCP session so the
                           'host' panel agent can sample the host LLM.
tools/
  graph_query.py           list_runs / get_run / run_tree MCP tools.
  web_url.py               web_url MCP tool — return the live viewer URL.
  multiaudit.py            Magic-phrase PR audit. Reads git diff, fires
                           start_task('panel', ...) with default panelists
                           [codex, gemini, claude, grok-4.3]. host is
                           opt-in (Claude Code doesn't advertise sampling
                           capability today).
  bugfind.py               Magic-phrase bug investigation. Takes a
                           bug_description, auto-attaches recent commits
                           + error log tail + optional files, fires the
                           same 4-way panel with a bug rubric (REPRO /
                           ROOT CAUSE / MINIMAL FIX / REGRESSION TEST /
                           BLAST RADIUS / WHAT YOU MISSED).
```

---

## Local dev setup

Editable install — source edits propagate without cache games. **Don't use `uvx --from /local/path`** — uv caches built wheels and reuses them, leading to confusing "my edit didn't take effect" debugging.

```bash
uv tool install --editable ~/Projects/panel-mcp-server
which panel-mcp-server   # → ~/.local/bin/panel-mcp-server

# In ~/.claude.json, mcpServers.panel:
#   {
#     "command": "/Users/<you>/.local/bin/panel-mcp-server",
#     "args": [],
#     "env": { "GEMINI_API_KEY": "...", "OPENAI_API_KEY": "...", "XAI_API_KEY": "..." }
#   }
```

After source edits, **restart Claude Code** so Panel re-reads the source. The editable install means no reinstall needed.

### Tunable env vars

| Var | Default | Purpose |
|---|---|---|
| `PANEL_MAX_CONCURRENT_API` | 16 | Global cap on concurrent paid API calls |
| `PANEL_MAX_PROVIDER_THREADS` | 32 | Worker thread pool for sync SDK calls |
| `PANEL_API_TIMEOUT_S` | 600 | Per-call SDK timeout (bounds thread lifetime) |
| `PANEL_CLINK_METADATA_CAP` | 2048 | Cap on stderr/stdout in clink metadata |
| `PANEL_CLINK_RAW_OUTPUT_CAP` | 8192 | Cap on raw_output_file in clink metadata |
| `PANEL_DEBUG_CLI_OUTPUT` | unset | If set, skip clink metadata redaction + truncation |
| `PANEL_GRAPH_DB` | `<cwd>/.panel/execution_graph.db` | Path to SQLite execution graph. **Per-repo by default** so each project has its own debate history; the web viewer attached to that Panel instance only sees this repo's runs. Set to an absolute path to pin globally (e.g. legacy `~/.panel/execution_graph.db`); `""` disables. |
| `PANEL_GRAPH_SNAPSHOT_CAP` | 16384 | Per-field cap on stored args/results JSON snapshots |
| `PANEL_WEB_PORT` | 8765 | Local web viewer port. Walks +20 if taken. |
| `PANEL_WEB_HOST` | 127.0.0.1 | Local-only by default. `0.0.0.0` exposes (opt in). |
| `PANEL_WEB_AUTO_OPEN` | 1 | Auto-open browser on Panel boot. `0` disables. |
| `PANEL_WEB_DISABLE` | unset | Skip web server entirely if set. |
| `PANEL_FALLBACK_ON_TIMEOUT` | unset | If set, hung clink CLIs trigger OAuth-to-API fallback. Off by default to avoid double-charge on legitimately slow models. |
| `PANEL_OAUTH_FIRST` | 1 | Wrap every direct-API provider in `OAuthFirstProvider` so models with a CLI route (gpt-5.5, gemini-3.1-pro-preview, claude-opus-4-7, claude-sonnet-4-6) try the free OAuth path first. Set `0` to opt out — every call hits the paid API directly, regardless of CLI auth state. |
| `DISABLED_TOOLS` | unset | Comma-separated tool names to disable |

---

## Validate before committing

```bash
# Syntax
python3 -c "import ast; ast.parse(open('PATH').read()); print('ok')"

# Full import (catches missing imports, registration bugs)
~/.local/share/uv/tools/panel-mcp-server/bin/python3 -c "
import sys; sys.path.insert(0, '/Users/$USER/Projects/panel-mcp-server')
import server; print('tools:', sorted(server.TOOLS.keys()))
"  # should report 28 tools

# Regression suite (~80ms)
~/.local/share/uv/tools/panel-mcp-server/bin/python3 -m pytest tests/test_v1_hardening.py -v

# Live smoke (after Claude Code restart)
#   "use start_task to run panel with codex+grok-4.3, debate_rounds=1,
#    codex as judge, prompt 'name 1 thing'"
#   then poll task_status / task_result. Completes in 60-180s.
```

---

## Coding style

- Python 3.9+; line length ~120; conventional commits (`feat:`, `fix:`, `refactor:`, `chore:`)
- Type hints required for new code; `from __future__ import annotations` preferred
- Match the file you're editing; don't impose new patterns unilaterally
- Comments only for *why*, not *what*
- Validate at boundaries (user input, provider responses); don't add defensive checks for things that can't happen
- Never weaken security: don't re-add `--dangerously-bypass-approvals-and-sandbox` / `--yolo`; don't bypass redaction in committed code; don't introduce a second dispatch path
- Don't commit secrets (API keys live in your MCP client config or `.env`, never in repo)

---

## Auth & cost routing

Each clink CLI uses its own subscription auth. If the user is logged in, those calls are free; if quota runs out, the configured `oauth_fallback_model` retries via paid API and the panel labels the run `oauth_fallback_paid` so cost is honest.

- **Codex CLI** — `codex login` (ChatGPT subscription). API fallback: `gpt-5.5` via `OPENAI_API_KEY`.
- **Gemini CLI** — first run prompts OAuth (Google account). API fallback: `gemini-3.1-pro-preview` via `GEMINI_API_KEY`.
- **Claude CLI** — `claude /login` (Claude subscription). API fallback: configurable via `PANEL_CLAUDE_OAUTH_FALLBACK_MODEL` (default `claude-sonnet-4-6`) via `ANTHROPIC_API_KEY`.
- **Grok** has no OAuth path — always paid via `XAI_API_KEY`.

When the user names "codex" / "gemini" / "claude" without a specific paid model, prefer `clink` (free, with automatic API fallback). When they name a specific paid string (`gpt-5.5`, `gemini-3.1-pro-preview`, `grok-4.3`, `claude-opus-4-7`, etc.) use `chat`/`consensus`. The MCP handshake instructions encode this routing.

---

## Key tools at a glance

| Tool | Purpose | Cost | When to use |
|---|---|---|---|
| `chat` | Single-turn Q&A with a specific model | Paid | Quick second opinion via paid model |
| `clink` | Run codex/gemini CLI as subprocess | OAuth → API on quota | Codex/Gemini consultation |
| `panel` | Parallel multi-model fan-out + optional judge | Mixed | Audits, second opinions, debates |
| `start_task` | Wrap any tool to run in background | Free wrapper | Any call expected >15s |
| `consensus` | Sequential multi-model debate (legacy) | Paid | Prefer `panel` for new use cases |
| `codereview` | Workflow: deep code review | Paid | Single-model deep review |
| `debug` | Workflow: hypothesis-driven debugging | Paid | Stuck on a bug |
| `thinkdeep` | Workflow: extended reasoning | Paid | Hard architectural questions |

---

## Logs

- `logs/mcp_server.log` — main log (verbose: openai/gemini SDK debug ON)
- `logs/mcp_activity.log` — focused tool-call activity
- One-liner: `grep -E "TOOL_CALL|TOOL_COMPLETED|ERROR" logs/mcp_activity.log | tail -50`

---

## Open work queue (handoff state)

The infrastructure is solid: 28 tools, 97 tests passing on the new surfaces, three size gates respect provenance, web viewer is XSS-hardened + lazy-started + streaming-v1. Outstanding for the next session:

1. **Streaming v2 — per-token for direct-API providers.** All four flagships stream by default: Anthropic via `client.messages.stream` (unconditional), OpenAI / xAI via `client.chat.completions.create(stream=True)` (opt out with `PANEL_OPENAI_STREAM=0`), Gemini via `client.models.generate_content_stream` (opt out with `PANEL_GEMINI_STREAM=0`). The shared `utils/stream_progress.py` emitter (a) takes an explicit run_id captured eagerly so it survives `loop.run_in_executor`'s ContextVar drop, (b) writes the actual accumulating text content (not chunk-count pings), (c) throttles by wall time (`DEFAULT_THROTTLE_S = 0.1`) so SQLite doesn't get hammered. The viewer renders aggregated `text_chunk` events in the transcript pane as `panelist_streaming` blocks, and bakes them in once the run completes if the canonical `panelist_answer` hasn't dropped. Still TODO: the OpenAI Responses endpoint (gpt-5.1-codex / o3-pro family — separate streaming API surface).
2. **Multi-step panel workflow** — refactor panel from fire-and-forget into a workflow tool (like `consensus`) so Claude Code can intervene between debate rounds, not just observe. Today `host` is a peer panelist via MCP sampling; multi-step would let Claude see round-1 results, refine the round-2 prompt, weigh in mid-flight, then continue.
3. **TaskManager → execution graph migration** — DONE for completed tasks. Lifecycle is persisted to a `tasks` table in the graph DB (see `ExecutionGraph.upsert_task` / `get_task`); `task_result(task_id)` falls back to the DB on memory miss so finished outputs survive Panel restart. In-flight tasks are NOT recovered — their worker thread is gone — but the API surfaces a clear "interrupted by Panel restart" error rather than "unknown task_id" when a non-terminal record is found in the DB.
4. **Conversation memory persistence.** `utils/conversation_memory.py` is in-memory. Wire `continuation_id` into the graph (schema has space) so it survives restart.
5. **Server-Sent Events** for the viewer instead of 2s polling — DONE. `ExecutionGraph._version` increments on every write; `GET /events` streams `data: <version>\n\n` whenever it changes. The viewer subscribes via `EventSource` and only re-fetches when version differs. Polling remains as a 5s fallback for environments where SSE can't keep a connection open.
6. **multiaudit working_directory_absolute_path → graph alignment.** When multiaudit targets a different repo via `working_directory_absolute_path`, the graph still writes to the launching repo's `.panel/` (cwd-based). Either propagate the override into a per-call graph path, or label runs with the audited repo and document.
7. **Per-CLI custom OAuth failure patterns.** `OAUTH_FAILURE_PATTERNS` in `tools/clink.py` is global. If codex and gemini diverge meaningfully on quota signals, move per-CLI into `clink/constants.py`.
8. **Cancel-aware semaphore release** — DONE. `providers/base.py:agenerate_content` now submits the worker via `executor.submit` and ties `sem.release()` to the underlying `concurrent.futures.Future`'s done-callback (via `loop.call_soon_threadsafe`). Cancelling the asyncio task no longer phantom-releases the slot while the SDK call is still blocking — the slot stays held until the thread actually finishes (success / exception / SDK timeout). Regression test: `test_agenerate_content_holds_semaphore_until_thread_completes_on_cancel`.
9. **Web viewer auth.** Bound to 127.0.0.1 by default; non-localhost binds now refuse to start without `PANEL_WEB_ALLOW_REMOTE=1` (opt-in gate against accidental exposure). Full token auth still TODO when someone actually needs remote — the gate is the safety net.
10. **Tests for cancel/GC dynamic paths.** Existing tests cover static surfaces well; dynamic flows (cancel propagation, GC eviction, debate-round peer mapping under failures) still uncovered.

---

## Process expectations

- Be terse. Don't write summaries the diff already shows.
- Commit early and often with conventional messages. Push to `origin main` (no PR workflow on this fork).
- Never `git push --force` to main without explicit confirmation.
- Don't introduce a new dispatch path. If you find yourself calling `tool.execute()` directly, route through `server.execute_tool()` instead.
- Don't touch `providers/*` for routine work — they're inherited from upstream and stable. Only modify when explicitly justified.
- If a refactor needs a hard architectural call (e.g. "schemas can't be cached safely because they depend on instance state"), stop and surface the choice — don't make it unilaterally.

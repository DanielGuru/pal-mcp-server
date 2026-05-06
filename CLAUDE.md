# CLAUDE.md — PAL MCP Server (custom fork)

Custom fork of `BeehiveInnovations/pal-mcp-server` at `github.com/DanielGuru/pal-mcp-server`. Upstream stalled in December 2025; this fork ships fixes plus orchestration features (background tasks, parallel panels, adversarial debate, observable streaming, OAuth-to-API fallback, central validated dispatch, bounded provider concurrency) that don't exist upstream. **Don't assume parity with upstream PAL** — read this doc, not the upstream README.

If you are an AI agent working on this fork: this file is for you.

---

## What this is

A Model Context Protocol server that lets one AI agent (typically Claude Code) consult, orchestrate, and debate multiple other models. 28 MCP tools in six families:

1. **Direct provider tools** — `chat`, `consensus`, `codereview`, `debug`, `thinkdeep`, `precommit`, `planner`, etc. Hit OpenAI / Gemini / xAI APIs via paid keys. `tools/*.py`, `tools/simple/base.py`, `tools/workflow/`.
2. **Clink** — `clink` runs an external CLI (Codex CLI, Gemini CLI, Claude CLI) as a subprocess. Uses each CLI's own auth, so **OAuth (free)** when the CLI is logged in via subscription. `tools/clink.py` + `clink/`. Includes automatic OAuth-to-API fallback (see below).
3. **Async background tasks** — `start_task`, `task_status`, `task_result`, `cancel_task`. Wrap any other tool so the conversation isn't blocked. `tools/tasks.py`. Admission control, periodic GC, session ownership, push completion notifications.
4. **Panel orchestration** — `panel` fans one prompt to N models in parallel, optional judge synthesis, optional adversarial `debate_rounds`. Reserved panelist name `host` routes through MCP sampling so Claude Code is a peer in the debate. `tools/panel.py`.
5. **PR-shaped audit workflow** — `multiaudit` reads `git diff`, packages it with intent context, and fires `start_task('panel', ...)` with `[host, codex, gemini, grok-4.3]`. `tools/multiaudit.py`.
6. **Execution graph + web viewer** — `list_runs`, `get_run`, `run_tree`, `web_url`. Read-only access to the SQLite-backed durable record of every dispatch + a live HTTP viewer page that auto-opens in the browser on PAL boot. Survives PAL restart. `tools/graph_query.py` + `utils/execution_graph.py` + `utils/web_viewer.py`.

---

## Magic-phrase recognition (for the AI agent reading this)

When the user says any of these phrases, immediately call the `multiaudit` MCP tool — don't ask, don't propose alternatives, just dispatch:

- "multiaudit" / "multiaudit it" / "OK multiaudit it now"
- "audit this" / "audit this PR" / "audit this branch"
- "panel this" / "panel this PR" / "panel this branch"
- "review with all" / "review with all models"
- "fan this out" / "fan it out for review"
- "what do the models think" (about a code change)
- "second opinion from everyone"

`multiaudit` reads the current branch's `git diff`, builds a structured audit prompt with the standard rubric (verdict / bugs / design / security / missing tests / what you'd attack), fires a 4-way panel via `start_task` (host + codex + gemini + grok-4.3, 1 debate round, codex as judge), and returns a task_id + the live web viewer URL. Hand the user the URL immediately, then poll `task_status` / `run_tree` / `task_result` to surface progress and findings.

When the user adds context to the magic phrase ("multiaudit but focus on the auth changes"), pass it through as `extra_context`.

The user wants the audit BEFORE you commit/push code — multiaudit is a gate, not a post-hoc check.

---

## Key architectural invariants

These are the load-bearing rules. Violate them and concurrency, security, or cost guarantees regress:

- **Single dispatch path.** `server.execute_tool(name, arguments)` is the only execution entrypoint. handle_call_tool, TaskManager._run, panel._run_panelist, and clink OAuth fallback all route through it. Never call `tool.execute()` directly from a new caller — file-size + model validation lives in execute_tool, and skipping it lets a 50MB file hit the paid API.
- **TOOLS are factories, not singletons.** `server.TOOLS: dict[str, type[BaseTool]]`. Construct via `make_tool(name)` for execution. Use `TOOL_DESCRIPTORS[name]` for read-only metadata only — never call `.execute()` on a descriptor. This eliminates per-call state corruption under concurrent panel fan-out.
- **Async provider calls bounded on three layers.** `agenerate_content` on `ModelProvider` runs the sync SDK on a thread, gated by:
  - `PAL_MAX_CONCURRENT_API` semaphore (default 16) acquired *before* dispatch
  - `PAL_MAX_PROVIDER_THREADS` ThreadPoolExecutor (default 32) caps thread leakage
  - `PAL_API_TIMEOUT_S` (default 600) forwarded as SDK `timeout=` so threads always self-terminate
- **Sync `generate_content` stays as the canonical method.** Tests mock it directly. The async wrapper delegates; don't delete the sync version.
- **Lazy-init of the executor + semaphore is lock-protected.** `_get_provider_executor` and `_get_api_semaphore` use double-checked locking with `threading.Lock`. Don't strip the lock — concurrent first-burst calls would otherwise race the `if X is None` check and create duplicate executors (leaking threads past the cap) or duplicate semaphores (defeating the global API cap).
- **Execution graph is observability, never load-bearing.** Every `execute_tool` dispatch records a run; nested calls form a tree via the contextvars-tracked parent. Graph writes are best-effort and swallow on failure. Disabled via `PAL_GRAPH_DB=""`. Internal-only graph hints (`_graph_edge_kind`, `_graph_cost_tier`, `_graph_label`) are popped from args at the dispatch boundary so tools never see them.
- **Web viewer is observability, never load-bearing.** Boots in a daemon thread alongside the MCP stdio loop. Bound to `127.0.0.1` by default. Boot failures log and continue. Disabled via `PAL_WEB_DISABLE`.
- **`host` panelist is reserved for MCP sampling.** Routes through `ServerSession.create_message` to ask the connected MCP client to invoke its own LLM. Any clink CLI named `host` would be shadowed (the routing in `_is_clink_agent` returns False for `host`).
- **Host session is reachable via contextvar in nested calls.** `utils/host_session.py` exposes a ContextVar that `server.execute_tool` (live calls) and `TaskManager._run` (background calls) populate. Read with `get_host_session()`. The host panelist fails cleanly with a diagnostic if no session is reachable.
- **OAuth-first, always.** Clink calls the configured CLI via subprocess every time. If the CLI fails for a recoverable reason (TerminalQuotaError, 401, etc.), `_try_oauth_fallback` retries via `oauth_fallback_model`. Fallback failures are SURFACED, not swallowed. When quota replenishes, the next call uses the free path automatically — no state.
- **Clink metadata is redacted + capped.** `_redact_and_cap` strips API-key shapes (sk-/AIza/xai-/sk-ant-), JWTs, and Bearer headers from stdout/stderr/raw_output_file before forwarding to MCP. Truncates at `PAL_CLINK_METADATA_CAP` / `PAL_CLINK_RAW_OUTPUT_CAP`. Opt-out via `PAL_DEBUG_CLI_OUTPUT=1` for local debugging only.

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
- **Clink metadata redaction.** `_redact_only` strips API-key shapes, JWTs, Bearer headers, and HOME paths from CLI stdout/stderr/raw_output_file AND the actual content. Capped via `PAL_CLINK_METADATA_CAP` / `PAL_CLINK_RAW_OUTPUT_CAP`. Parser-supplied metadata is filtered through `_safe_merge_parser_metadata` so a malicious or buggy CLI can't smuggle 50MB through a `command`-shaped field.
- **Panel cost_tier reads structured metadata, not substrings.** `_derive_cost_tier` JSON-parses the response and reads `metadata.oauth_fallback_used` directly — un-spoofable by a model emitting the literal phrase in its content.
- **Durable execution graph** (`utils/execution_graph.py`). SQLite + WAL, append-only events, tree-shaped runs/edges. Survives PAL restart. Powers the `list_runs` / `get_run` / `run_tree` query tools.
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
                           Forwards PAL_API_TIMEOUT_S as SDK timeout=.
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
                           [host, codex, gemini, grok-4.3].
```

---

## Local dev setup

Editable install — source edits propagate without cache games. **Don't use `uvx --from /local/path`** — uv caches built wheels and reuses them, leading to confusing "my edit didn't take effect" debugging.

```bash
uv tool install --editable ~/Projects/pal-mcp-server
which pal-mcp-server   # → ~/.local/bin/pal-mcp-server

# In ~/.claude.json, mcpServers.pal:
#   {
#     "command": "/Users/<you>/.local/bin/pal-mcp-server",
#     "args": [],
#     "env": { "GEMINI_API_KEY": "...", "OPENAI_API_KEY": "...", "XAI_API_KEY": "..." }
#   }
```

After source edits, **restart Claude Code** so PAL re-reads the source. The editable install means no reinstall needed.

### Tunable env vars

| Var | Default | Purpose |
|---|---|---|
| `PAL_MAX_CONCURRENT_API` | 16 | Global cap on concurrent paid API calls |
| `PAL_MAX_PROVIDER_THREADS` | 32 | Worker thread pool for sync SDK calls |
| `PAL_API_TIMEOUT_S` | 600 | Per-call SDK timeout (bounds thread lifetime) |
| `PAL_CLINK_METADATA_CAP` | 2048 | Cap on stderr/stdout in clink metadata |
| `PAL_CLINK_RAW_OUTPUT_CAP` | 8192 | Cap on raw_output_file in clink metadata |
| `PAL_DEBUG_CLI_OUTPUT` | unset | If set, skip clink metadata redaction + truncation |
| `PAL_GRAPH_DB` | `~/.pal/execution_graph.db` | Path to SQLite execution graph. `""` disables. |
| `PAL_GRAPH_SNAPSHOT_CAP` | 16384 | Per-field cap on stored args/results JSON snapshots |
| `PAL_WEB_PORT` | 8765 | Local web viewer port. Walks +20 if taken. |
| `PAL_WEB_HOST` | 127.0.0.1 | Local-only by default. `0.0.0.0` exposes (opt in). |
| `PAL_WEB_AUTO_OPEN` | 1 | Auto-open browser on PAL boot. `0` disables. |
| `PAL_WEB_DISABLE` | unset | Skip web server entirely if set. |
| `PAL_FALLBACK_ON_TIMEOUT` | unset | If set, hung clink CLIs trigger OAuth-to-API fallback. Off by default to avoid double-charge on legitimately slow models. |
| `DISABLED_TOOLS` | unset | Comma-separated tool names to disable |

---

## Validate before committing

```bash
# Syntax
python3 -c "import ast; ast.parse(open('PATH').read()); print('ok')"

# Full import (catches missing imports, registration bugs)
~/.local/share/uv/tools/pal-mcp-server/bin/python3 -c "
import sys; sys.path.insert(0, '/Users/$USER/Projects/pal-mcp-server')
import server; print('tools:', sorted(server.TOOLS.keys()))
"  # should report 23 tools

# Regression suite (~80ms)
~/.local/share/uv/tools/pal-mcp-server/bin/python3 -m pytest tests/test_v1_hardening.py -v

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
- Don't commit secrets (API keys live in `~/.claude.json`, never in repo)

---

## Authentication state expected on this machine

- **Codex CLI** logged in via ChatGPT (`~/.codex/auth.json` `auth_mode: chatgpt`) → free OAuth. Fallback: gpt-5.5 via `OPENAI_API_KEY`.
- **Gemini CLI** logged in via Google (`~/.gemini/oauth_creds.json`) → free OAuth. `gemini-3-flash-preview` has a daily quota. Fallback: gemini-3.1-pro-preview via `GEMINI_API_KEY`.
- **Grok** has no OAuth path — always paid via `XAI_API_KEY`.

When the user names "codex" or "gemini" without a specific paid model, prefer `clink` (free, with automatic API fallback). When they name a specific paid string (`gpt-5.5`, `gemini-3.1-pro-preview`, `grok-4.3`) use `chat`/`consensus`. The MCP handshake instructions encode this routing.

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

## Open work queue

Most of the major work has landed. What remains:

1. **Multi-step panel workflow** — refactor panel from fire-and-forget into a workflow tool (like `consensus`) so Claude Code can intervene between rounds, not just observe. Today: `host` is a peer panelist via MCP sampling. With multi-step: Claude could see round-1 results, refine the round-2 prompt, weigh in itself, then continue. Bigger refactor than a single commit.
2. **TaskManager → execution graph migration.** `TaskManager._tasks` is still an in-memory dict; the execution graph captures its own runs but TaskManager state isn't there yet. Migration would mean `start_task` / `task_status` / `task_result` / `cancel_task` survive restart. Pair with stale-running handling because `_gc()` only evicts terminal records.
3. **Conversation memory persistence.** `utils/conversation_memory.py` is in-memory. The execution graph schema has space for a `messages` table that's not yet populated; wire it so `continuation_id` survives restart.
4. **Server-Sent Events for the web viewer.** Polling at 2s feels live enough but SSE would be tighter. Stdlib http.server can do SSE with effort; or take aiohttp/Starlette as a dep.
5. **Streaming async path** for direct-API providers. Today the async wrapper threads sync `.create()` calls; a true `stream=True` path with incremental MCP progress notifications would unlock per-token UI for direct-API panelists.
6. **Per-CLI custom OAuth failure patterns.** `OAUTH_FAILURE_PATTERNS` in `tools/clink.py` is global. If codex and gemini diverge meaningfully on quota signals, move per-CLI into `clink/constants.py`.
7. **Cancel-aware semaphore release.** When a panel call is cancelled mid-flight, the worker thread holding a real SDK call keeps running (asyncio limitation). The SDK timeout bounds this. Future: track in-flight thread count and refuse new tasks when exhausted.
8. **Tests for cancel/GC dynamic paths.** Existing tests cover static surfaces well; dynamic flows (cancel propagation, GC eviction, debate-round peer mapping under failures) still uncovered.

---

## Process expectations

- Be terse. Don't write summaries the diff already shows.
- Commit early and often with conventional messages. Push to `origin main` (no PR workflow on this fork).
- Never `git push --force` to main without explicit confirmation.
- Don't introduce a new dispatch path. If you find yourself calling `tool.execute()` directly, route through `server.execute_tool()` instead.
- Don't touch `providers/*` for routine work — they're inherited from upstream and stable. Only modify when explicitly justified.
- If a refactor needs a hard architectural call (e.g. "schemas can't be cached safely because they depend on instance state"), stop and surface the choice — don't make it unilaterally.

---

## Auto-memory (for Claude sessions)

User's auto-memory will load two relevant entries:
- `pal_fork.md` — full project context (location, customizations, auth state, roadmap)
- `feedback_uvx_caching.md` — why NOT to use `uvx --from /local/path` for iterative dev

Trust those memory entries; they're maintained alongside this doc.

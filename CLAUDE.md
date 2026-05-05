# CLAUDE.md — PAL MCP Server (custom fork)

This is a customized fork of `BeehiveInnovations/pal-mcp-server` maintained at `github.com/DanielGuru/pal-mcp-server`. Upstream stalled in December 2025; this fork ships ongoing fixes plus new orchestration features (background tasks, parallel panels, adversarial debate, observable streaming, push notifications) that don't exist upstream. **Do not assume parity with upstream PAL** — read this doc, not the upstream README.

If you are an AI agent working on this fork: this file is for you. Read it once before editing.

---

## What this is

A Model Context Protocol (MCP) server that lets one AI agent (typically Claude Code) consult, orchestrate, and debate multiple other models. It exposes ~23 MCP tools that fall into four families:

1. **Direct provider tools** — `chat`, `consensus`, `codereview`, `debug`, `thinkdeep`, `precommit`, `planner`, etc. Call OpenAI / Gemini / xAI APIs via paid keys. Implemented in `tools/*.py`, `tools/simple/base.py`, `tools/workflow/`.
2. **Clink** — `clink` runs an external CLI (Codex CLI, Gemini CLI, Claude CLI) as a subprocess and returns its output. Uses each CLI's own auth, which means **OAuth (free)** when the CLI is logged in via subscription. Implemented in `tools/clink.py` + `clink/agents/*.py` + `clink/parsers/*.py`.
3. **Async background tasks** — `start_task`, `task_status`, `task_result`, `cancel_task`. Wrap any other tool so the conversation isn't blocked while it runs. Implemented in `tools/tasks.py`. Includes admission control, periodic GC, session ownership, push completion notifications.
4. **Panel orchestration** — `panel` fans one prompt to N models in parallel, optionally with a judge synthesizing. Adversarial mode (`debate_rounds`) lets panelists critique and revise after seeing peers. Implemented in `tools/panel.py`.

---

## How this fork differs from upstream PAL

- **Trimmed model registry** to current flagships (gpt-5.5, gpt-5.4, gpt-5.1-codex, gemini-3.1-pro-preview, grok-4.3, grok-4.1-fast). Upstream has stale entries including the dead `gemini-3-pro-preview` (Google shut it down 2026-03-09).
- **Cherry-picked unmerged upstream PRs** for model registry updates and fixes.
- **Safer clink defaults** — removed `--dangerously-bypass-approvals-and-sandbox` (codex) and `--yolo` (gemini) so subprocesses can't silently mutate the filesystem; replaced with `--skip-git-repo-check` (codex) and `-p` argv passing (gemini stream-json) which are not security-relaxing flags.
- **Streaming progress notifications** for clink subprocesses (`utils/progress.py` + parser `describe_event` hook). Long Codex/Gemini calls now emit per-event progress.
- **Async background-task pattern** with push completion notifications.
- **Panel + adversarial debate** as a first-class orchestration tool.
- **MCP handshake instructions** include cost-routing (clink for free, chat for paid), async-routing (long calls go through start_task), and panel-routing (keyword cues for picking modes).

---

## Architecture map

```
server.py                        MCP entry point, TOOLS dict, handle_call_tool dispatcher, handshake instructions
tools/
  shared/base_tool.py            BaseTool — common methods (get_name, get_input_schema, execute, etc.)
  simple/base.py                 SimpleTool — chat-style tools with provider integration; mutates self per call
  workflow/                      Workflow tools (multi-step, e.g. consensus, codereview)
  chat.py / clink.py / etc.      Concrete tool implementations
  tasks.py                       TaskManager + 4 task tools (background pattern)
  panel.py                       Panel orchestration (parallel + judge + adversarial debate)
clink/
  agents/                        Per-CLI agent classes (BaseCLIAgent, GeminiAgent, CodexAgent, ClaudeAgent)
  parsers/                       Per-CLI output parsers + describe_event hooks for progress
  registry.py                    Loads CLI configs from conf/cli_clients/*.json
  constants.py                   INTERNAL_DEFAULTS — per-CLI parser, additional_args, etc.
providers/                       OpenAI / Gemini / xAI direct API providers (mostly inherited from upstream)
conf/
  *_models.json                  Per-provider model registries (capabilities, aliases, intelligence_score)
  cli_clients/*.json             Per-CLI clink configs (command, args, roles)
systemprompts/                   System prompts (per-tool, per-clink-role)
utils/
  progress.py                    MCP progress notifications + contextvar sink override (used by tasks)
```

---

## Local dev setup

This fork uses an **editable install** so source edits propagate without cache games. Do **NOT** use `uvx --from /local/path` — uv caches built wheels and reuses them, leading to confusing "my edit didn't take effect" debugging.

```bash
# Install once:
uv tool install --editable ~/Projects/pal-mcp-server

# Verify:
which pal-mcp-server   # → ~/.local/bin/pal-mcp-server (symlink to ~/.local/share/uv/tools/...)

# In Claude Code's ~/.claude.json, mcpServers.pal looks like:
#   {
#     "command": "/Users/<you>/.local/bin/pal-mcp-server",
#     "args": [],
#     "env": { "GEMINI_API_KEY": "...", "OPENAI_API_KEY": "...", "XAI_API_KEY": "...", ... }
#   }
```

After editing source files, **restart Claude Code** for PAL to re-read the source. (PAL caches config at process startup; the editable install means the next launch sees the new code without a reinstall.)

---

## Validate before committing

After non-trivial edits, always:

```bash
# 1. Syntax check
python3 -c "import ast; ast.parse(open('PATH/TO/CHANGED.py').read()); print('ok')"

# 2. Full import (catches missing imports, registration bugs)
~/.local/share/uv/tools/pal-mcp-server/bin/python3 -c "
import sys; sys.path.insert(0, '/Users/$USER/Projects/pal-mcp-server')
import server
print('tools:', sorted(server.TOOLS.keys()))
"

# 3. Live smoke test (after restarting Claude Code)
#    Ask Claude: "use start_task to run panel with codex, debate_rounds=1, codex as judge,
#                 prompt 'name 1 thing'"
#    Then poll task_status / task_result. Should complete in ~60-180s.
```

---

## Coding style

- Python 3.9+; line length ~120; conventional commits (`feat:`, `fix:`, `refactor:`, `chore:`)
- Type hints required for new code; prefer `from __future__ import annotations`
- Match the file you're editing; don't impose new patterns unilaterally
- Don't add comments that just restate what the code does; reserve comments for *why*
- Don't add error handling for impossible cases — boundaries (user input, provider responses) yes; internal calls no
- Never weaken security: don't re-add `--dangerously-bypass-approvals-and-sandbox` or `--yolo` flags
- Don't commit secrets (API keys live in `~/.claude.json`, never in repo)

---

## Authentication state expected on this machine

- **Codex CLI** logged in via ChatGPT (`auth_mode: chatgpt` in `~/.codex/auth.json`) → free OAuth
- **Gemini CLI** logged in via Google (`~/.gemini/oauth_creds.json`) → free OAuth, **but** `gemini-3-flash-preview` has a daily quota that resets in ~24h. When exhausted, clink calls fail with `TerminalQuotaError`.
- **Grok** has no OAuth path — always paid via `XAI_API_KEY`.
- **Direct provider tools** (chat/consensus/codereview/etc.) always use API keys regardless of CLI OAuth state.

When the user names "codex" or "gemini" without a specific paid model, prefer `clink` (free). When they name a specific paid string (`gpt-5.5`, `gemini-3.1-pro-preview`, `grok-4.3`) use `chat`/`consensus`. The MCP handshake instructions encode this routing, so connecting Claude clients pick it up automatically.

---

## Key tools at a glance

| Tool | Purpose | Cost | When to use |
|---|---|---|---|
| `chat` | Single-turn Q&A with a specific model | Paid API | Quick second opinion via paid model |
| `clink` | Run codex/gemini CLI as subprocess | OAuth (free) | Codex/Gemini consultation when CLIs are logged in |
| `panel` | Parallel multi-model fan-out + optional judge | Mixed | Audits, second opinions, debates |
| `start_task` | Wrap any tool to run in background | Free wrapper | Any call expected >15s |
| `consensus` | Sequential multi-model debate (legacy) | Paid | Prefer `panel` for new use cases |
| `codereview` | Workflow tool: deep code review | Paid | Single-model deep review |
| `debug` | Workflow tool: hypothesis-driven debugging | Paid | Stuck on a bug |
| `thinkdeep` | Workflow tool: extended reasoning | Paid | Hard architectural questions |

---

## Logs

- `logs/mcp_server.log` — main log (debug-level by default; openai/gemini SDK debug logs ON, very verbose)
- `logs/mcp_activity.log` — focused tool-call activity (cleaner)
- Helpful one-liner: `grep -E "TOOL_CALL|TOOL_COMPLETED|ERROR" logs/mcp_activity.log | tail -50`

---

## Known issues / open work queue

The audit by Codex+Grok identified one v1.0 blocker and several adjacent improvements:

1. **Factory-pattern TOOLS refactor** (v1.0 blocker — not yet started). `server.py` registers TOOLS as singleton instances; tools mutate `self._current_arguments`, `self._current_model_name`, `self._model_context` during `execute()`. Concurrent panel calls or multi-client deployments corrupt that state. Band-aided in `tools/panel.py` via `_fresh_tool()`. Real fix: convert TOOLS to a registry of factories or immutable descriptors.

2. **Sync→async provider refactor (C8)**. `providers/openai_compatible.py` calls `self.client.chat.completions.create(...)` synchronously. While in flight, the asyncio event loop can't progress, so parallel panel calls effectively serialize when any panelist uses a paid API. Fix: wrap sync provider calls in `asyncio.to_thread`, or switch to async streaming SDKs.

3. **OAuth-to-API fallback (F1)**. When clink's CLI errors with quota exhaustion (`TerminalQuotaError`), automatically retry via paid API as a fallback. Detect quota errors via stderr patterns; configurable per-CLI fallback target.

4. **Durable task storage**. Tasks live in `TaskManager._tasks` (in-memory). PAL restart loses everything in flight. SQLite-backed storage would make tasks survive crashes.

5. **Per-call cost meter**. PAL doesn't track per-tool spend. Should expose cumulative cost in task summaries and panel output.

6. **Tests for new surfaces**. `tools/tasks.py`, `tools/panel.py`, the `describe_event` hooks, and the streaming runner have no dedicated unit tests yet. The legacy upstream test suite still runs but doesn't cover any of this fork's new code.

---

## Process expectations

- The user has been at this fork for many hours. Be terse. Don't write summaries the diff already shows.
- Commit early and often with conventional messages. Push to `origin main` (no PR workflow on this fork).
- Never run `git push --force` to main without explicit confirmation.
- Don't touch `providers/*` for routine work — they're inherited and largely untouched. Only modify when explicitly part of C8 or F1.
- If a refactor genuinely needs a hard architectural call (e.g. "schemas can't be cached safely because they depend on instance state"), stop and surface the choice — don't make it unilaterally.

---

## Auto-memory (for Claude sessions)

User's auto-memory will load two relevant entries:
- `pal_fork.md` — full project context (this fork, its location, customizations, auth state, roadmap)
- `feedback_uvx_caching.md` — why NOT to use `uvx --from /local/path` for iterative dev

Trust those memory entries; they're maintained alongside this doc.

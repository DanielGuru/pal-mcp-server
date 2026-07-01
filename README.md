# Panel MCP: Many Models, One Decision.

<div align="center">

  <em>Convene a panel of AI models from inside your CLI — fan-out, debate, judge.</em><br />
  <sub><a href="docs/name-change.md">Zen MCP → PAL MCP → Panel MCP</a></sub>

### Your CLI + Multiple Models = Your AI Dev Team

**Use the 🤖 CLI you love:**  
[Claude Code](https://www.anthropic.com/claude-code) · [Gemini CLI](https://github.com/google-gemini/gemini-cli) · [Codex CLI](https://github.com/openai/codex) · [Qwen Code CLI](https://qwenlm.github.io/qwen-code-docs/) · [Cursor](https://cursor.com) · _and more_

**With multiple models within a single prompt:**  
Gemini · OpenAI · Anthropic · Grok · Azure · Ollama · OpenRouter · DIAL · On-Device Model

</div>

---

## 🆕 Now with CLI-to-CLI Bridge

The new **[`clink`](docs/tools/clink.md)** (CLI + Link) tool connects external AI CLIs directly into your workflow:

- **Connect external CLIs** like [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Codex CLI](https://github.com/openai/codex), and [Claude Code](https://www.anthropic.com/claude-code) directly into your workflow
- **CLI Subagents** - Launch isolated CLI instances from _within_ your current CLI! Claude Code can spawn Codex subagents, Codex can spawn Gemini CLI subagents, etc. Offload heavy tasks (code reviews, bug hunting) to fresh contexts while your main session's context window remains unpolluted. Each subagent returns only final results.
- **Context Isolation** - Run separate investigations without polluting your primary workspace
- **Role Specialization** - Spawn `planner`, `codereviewer`, or custom role agents with specialized system prompts
- **Full CLI Capabilities** - Web search, file inspection, MCP tool access, latest documentation lookups
- **Seamless Continuity** - Sub-CLIs participate as first-class members with full conversation context between tools

```bash
# Codex spawns Codex subagent for isolated code review in fresh context
clink with codex codereviewer to audit auth module for security issues
# Subagent reviews in isolation, returns final report without cluttering your context

# Consensus from different AI models → Implementation handoff with full context preservation
Use consensus with gpt-5.5 and gemini-3.1-pro-preview to decide: dark mode or offline support next
Continue with clink gemini - implement the recommended feature
# Gemini receives full debate context and starts coding immediately
```

👉 **[Learn more about clink](docs/tools/clink.md)**

---

## Why Panel MCP?

**Why rely on one AI model when you can orchestrate them all?**

A Model Context Protocol server that supercharges tools like [Claude Code](https://www.anthropic.com/claude-code), [Codex CLI](https://developers.openai.com/codex/cli), and IDE clients such as [Cursor](https://cursor.com) or the [Claude Dev VS Code extension](https://marketplace.visualstudio.com/items?itemName=Anthropic.claude-vscode). **Panel MCP connects your favorite AI tool to multiple AI models** for enhanced code analysis, problem-solving, and collaborative development.

### True AI Collaboration with Conversation Continuity

Panel supports **conversation threading** so your CLI can **discuss ideas with multiple AI models, exchange reasoning, get second opinions, and even run collaborative debates between models** to help you reach deeper insights and better solutions.

Your CLI always stays in control but gets perspectives from the best AI for each subtask. Context carries forward seamlessly across tools and models, enabling complex workflows like: code reviews with multiple models → automated planning → implementation → pre-commit validation.

> **You're in control.** Your CLI of choice orchestrates the AI team, but you decide the workflow. Craft powerful prompts that bring in Gemini 3.1 Pro, GPT-5.5, Claude Opus 4.8, Grok-4.3, or local offline models exactly when needed.

<details>
<summary><b>Reasons to Use Panel MCP</b></summary>

A typical workflow with Claude Code as an example:

1. **Multi-Model Orchestration** - Claude coordinates with Gemini 3.1 Pro, GPT-5.5, Grok-4.3, and Claude Opus 4.8 to get the best analysis for each task

2. **Context Revival Magic** - Even after Claude's context resets, continue conversations seamlessly by having other models "remind" Claude of the discussion

3. **Guided Workflows** - Enforces systematic investigation phases that prevent rushed analysis and ensure thorough code examination

4. **Extended Context Windows** - Break Claude's limits by delegating to Gemini 3.1 Pro (1M tokens) for massive codebases

5. **True Conversation Continuity** - Full context flows across tools and models — Gemini remembers what GPT-5.5 said 10 steps ago

6. **Model-Specific Strengths** - Extended thinking with Gemini 3.1 Pro, strong reasoning with Claude Opus 4.8, adversarial pressure-testing with Grok-4.3, privacy with local Ollama

7. **Professional Code Reviews** - Multi-pass analysis with severity levels, actionable feedback, and consensus from multiple AI experts

8. **Smart Debugging Assistant** - Systematic root cause analysis with hypothesis tracking and confidence levels

9. **Automatic Model Selection** - Claude intelligently picks the right model for each subtask (or you can specify)

10. **Vision Capabilities** - Analyze screenshots, diagrams, and visual content with vision-enabled models

11. **Local Model Support** - Run Llama, Mistral, or other models locally for complete privacy and zero API costs

12. **Bypass MCP Token Limits** - Automatically works around MCP's 25K limit for large prompts and responses

13. **SQLite Execution Graph** - Every tool dispatch is recorded in a per-repo `.panel/execution_graph.db`. Resume panels after restart, audit cost attribution, replay prior runs — durable across process restarts.

**The Killer Feature:** When Claude's context resets, just ask to "continue with gpt-5.5" (or gemini-3.1-pro-preview, grok-4.3, etc.) — the other model's response magically revives Claude's understanding without re-ingesting documents!

#### Example: Multi-Model Code Review Workflow

1. `Perform a codereview using gemini-3.1-pro-preview and gpt-5.5 and use planner to generate a detailed plan, implement the fixes and do a final precommit check by continuing from the previous codereview`
2. This triggers a [`codereview`](docs/tools/codereview.md) workflow where Claude walks the code, looking for all kinds of issues
3. After multiple passes, collects relevant code and makes note of issues along the way
4. Maintains a `confidence` level between `exploring`, `low`, `medium`, `high` and `certain` to track how confidently it's been able to find and identify issues
5. Generates a detailed list of critical -> low issues
6. Shares the relevant files, findings, etc with **Gemini 3.1 Pro** to perform a deep dive for a second [`codereview`](docs/tools/codereview.md)
7. Comes back with a response and next does the same with GPT-5.5, adding to the prompt if a new discovery comes to light
8. When done, Claude takes in all the feedback and combines a single list of all critical -> low issues, including good patterns in your code. The final list includes new findings or revisions in case Claude misunderstood or missed something crucial and one of the other models pointed this out
9. It then uses the [`planner`](docs/tools/planner.md) workflow to break the work down into simpler steps if a major refactor is required
10. Claude then performs the actual work of fixing highlighted issues
11. When done, Claude returns to Gemini 3.1 Pro for a [`precommit`](docs/tools/precommit.md) review

All within a single conversation thread! Gemini Pro in step 11 _knows_ what was recommended by GPT-5.5 in step 7!

**Think of it as Claude Code _for_ Claude Code.** This MCP isn't magic. It's just **super-glue**.

> **Remember:** Claude stays in full control — but **YOU** call the shots.
> Panel is designed to have Claude engage other models only when needed — and to follow through with meaningful back-and-forth.
> **You're** the one who crafts the powerful prompt that makes Claude bring in Gemini, GPT-5.5, Grok-4.3 — or fly solo.
> You're the guide. The prompter. The puppeteer.
> #### You are the AI - **Actually Intelligent**.
</details>

#### Recommended AI Stack

<details>
<summary>For Claude Code Users</summary>

For best results when using [Claude Code](https://claude.ai/code):

- **claude-sonnet-5** or **claude-opus-4-8** - All agentic work and orchestration
- **`gemini-3.1-pro-preview`** (`pro`) OR **`gpt-5.5`** - Deep thinking, additional code reviews, debugging and validations, pre-commit analysis
</details>

<details>
<summary>For Codex Users</summary>

For best results when using [Codex CLI](https://developers.openai.com/codex/cli):

- **`gpt-5.5`** or **`gpt-5.1-codex`** - All agentic work and orchestration
- **`gemini-3.1-pro-preview`** (`pro`) OR **`gpt-5.5`** - Deep thinking, additional code reviews, debugging and validations, pre-commit analysis
</details>

## Quick Start (5 minutes)

**Prerequisites:** Python 3.10+, Git, [uv installed](https://docs.astral.sh/uv/getting-started/installation/)

**1. Pick how you want to authenticate** — at least one of:

**Free OAuth via subscription CLIs** (zero API spend; CLIs use your existing logins):
- **[Codex CLI](https://developers.openai.com/codex/cli)** — `codex login` (uses your ChatGPT subscription)
- **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** — first run prompts Google OAuth
- **[Claude CLI](https://www.anthropic.com/claude-code)** — `claude /login` (uses your Claude subscription)

OAuth CLIs are the cheapest path: `clink`, `panel`, `multiaudit`, and `bugfind` all route through them automatically when available, falling back to paid API only on quota exhaustion.

**Paid API keys** (needed for Grok, and as the OAuth fallback safety net):
- **[Anthropic](https://console.anthropic.com/settings/keys)** — Claude Opus 4.8 / Sonnet 5
- **[OpenAI](https://platform.openai.com/api-keys)** — GPT-5.5 / 5.4 / 5.1-codex
- **[Gemini](https://aistudio.google.com/app/apikey)** — Gemini 3.1 Pro
- **[X.AI](https://console.x.ai/)** — Grok-4.3 / 4.1-fast (no OAuth path; API only)
- **[OpenRouter](https://openrouter.ai/)** — Access multiple models with one API
- **[Azure OpenAI](https://learn.microsoft.com/azure/ai-services/openai/)** — Enterprise OpenAI deployments
- **[DIAL](https://dialx.ai/)** — Vendor-agnostic model access
- **[Ollama](https://ollama.ai/)** — Local models (free)

> **Soft-landing on zero everything:** if you start the server with neither API keys nor authed CLIs, it doesn't crash — it logs a friendly capability summary and the always-available tools (`listmodels`, `version`, `web_url`, graph queries) still work. Set or login to whatever you want, restart your MCP client, and you're off.

**2. Install** (choose one):

**Option A: Clone and Automatic Setup** (recommended)
```bash
git clone https://github.com/DanielGuru/panel-mcp-server.git
cd panel-mcp-server

# Copy and edit config
cp .env.example .env
# Edit .env and add your API keys

# Handles everything: setup, config, API keys from system environment.
# Auto-configures Claude Desktop, Claude Code, Gemini CLI, Codex CLI, Qwen CLI
./run-server.sh
```

**Option B: Instant Setup with [uvx](https://docs.astral.sh/uv/getting-started/installation/)**
```json
// Add to ~/.claude/settings.json or .mcp.json
// Set whichever keys you have — at least one is enough.
// (If you only have authed OAuth CLIs, you can leave keys empty and
// still use clink/panel/multiaudit/bugfind via the free OAuth path,
// modulo the grok-4.3 caveat above.)
{
  "mcpServers": {
    "panel": {
      "command": "bash",
      "args": ["-c", "for p in $(which uvx 2>/dev/null) $HOME/.local/bin/uvx /opt/homebrew/bin/uvx /usr/local/bin/uvx uvx; do [ -x \"$p\" ] && exec \"$p\" --from git+https://github.com/DanielGuru/panel-mcp-server.git panel-mcp-server; done; echo 'uvx not found' >&2; exit 1"],
      "env": {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:~/.local/bin",
        "ANTHROPIC_API_KEY": "your-anthropic-key-or-empty",
        "OPENAI_API_KEY": "your-openai-key-or-empty",
        "GEMINI_API_KEY": "your-gemini-key-or-empty",
        "XAI_API_KEY": "your-xai-key-or-empty",
        "DISABLED_TOOLS": "analyze,refactor,testgen,secaudit,docgen,tracer",
        "DEFAULT_MODEL": "auto"
      }
    }
  }
}
```

**3. Start Using!**

**Magic phrases — the killer features:**
```
"multiaudit it"          # right before commit/push: 4-way panel reviews the diff
"bugfind it: <bug>"      # describe a bug; the panel investigates + proposes a fix
```

**Direct multi-model orchestration:**
```
"Use panel to analyze this code for security issues with gemini-3.1-pro-preview"
"Debug this error with grok-4.3 and then get gpt-5.5 to suggest optimizations"
"Plan the migration strategy with panel, get consensus from multiple models"
"clink with cli_name=\"gemini\" role=\"planner\" to draft a phased rollout plan"
```

👉 **[Complete Setup Guide](docs/getting-started.md)** with detailed installation, configuration for Gemini / Codex / Qwen, and troubleshooting  
👉 **[Cursor & VS Code Setup](docs/getting-started.md#ide-clients)** for IDE integration instructions

## Provider Configuration

Panel activates any provider that has credentials in your `.env`. Copy `.env.example` to `.env` and add your keys. See `.env.example` for the full list of options.

## Core Tools

> **Note:** Each tool comes with its own multi-step workflow, parameters, and descriptions that consume valuable context window space even when not in use. To optimize performance, some tools are disabled by default. See [Tool Configuration](#tool-configuration) below to enable them.

**Collaboration & Planning** *(Enabled by default)*
- **[`clink`](docs/tools/clink.md)** - Bridge requests to external AI CLIs (Gemini planner, codereviewer, etc.)
- **[`chat`](docs/tools/chat.md)** - Brainstorm ideas, get second opinions, validate approaches. With capable models (GPT-5, Gemini 3.1 Pro), generates complete code / implementation
- **[`thinkdeep`](docs/tools/thinkdeep.md)** - Extended reasoning, edge case analysis, alternative perspectives
- **[`planner`](docs/tools/planner.md)** - Break down complex projects into structured, actionable plans
- **[`consensus`](docs/tools/consensus.md)** - Get expert opinions from multiple AI models with stance steering
- **[`panel`](#-panel-tool)** - Fan-out one prompt to N models in parallel, with optional adversarial debate rounds and judge synthesis

**Code Analysis & Quality**
- **[`debug`](docs/tools/debug.md)** - Systematic investigation and root cause analysis
- **[`precommit`](docs/tools/precommit.md)** - Validate changes before committing, prevent regressions
- **[`codereview`](docs/tools/codereview.md)** - Professional reviews with severity levels and actionable feedback
- **[`analyze`](docs/tools/analyze.md)** *(disabled by default - [enable](#tool-configuration))* - Understand architecture, patterns, dependencies across entire codebases

**Development Tools** *(Disabled by default - [enable](#tool-configuration))*
- **[`refactor`](docs/tools/refactor.md)** - Intelligent code refactoring with decomposition focus
- **[`testgen`](docs/tools/testgen.md)** - Comprehensive test generation with edge cases
- **[`secaudit`](docs/tools/secaudit.md)** - Security audits with OWASP Top 10 analysis
- **[`docgen`](docs/tools/docgen.md)** - Generate documentation with complexity analysis

**Async Task System**
- **`start_task`** - Kick off a long-running tool call in the background (non-blocking)
- **`task_status`** - Poll whether a background task is still running
- **`task_result`** - Retrieve the result of a completed background task
- **`cancel_task`** - Cancel a running background task

**Execution Graph** *(SQLite-backed, survives restarts)*
- **`list_runs`** - List recent tool dispatches; filter by status or tool name
- **`get_run`** - Full details for a single run: events, cost, timing, result
- **`run_tree`** - Show the parent→child lineage of a run (panel → panelists → clink fallbacks)

**Utilities**
- **[`apilookup`](docs/tools/apilookup.md)** - Forces current-year API/SDK documentation lookups in a sub-process (saves tokens within the current context window), prevents outdated training data responses
- **[`challenge`](docs/tools/challenge.md)** - Prevent "You're absolutely right!" responses with critical analysis
- **[`tracer`](docs/tools/tracer.md)** *(disabled by default - [enable](#tool-configuration))* - Static analysis prompts for call-flow mapping
- **`listmodels`** - List all available AI models across configured providers
- **`version`** - Show server version and system information

---

## 🔀 Panel Tool

`panel` fans out a single prompt to N models simultaneously, runs optional adversarial debate rounds between them, and synthesises a final verdict via a judge model — all in one tool call.

```
"Use panel with gemini-3.1-pro-preview, gpt-5.5, and grok-4.3 to evaluate three caching strategies,
 then run a debate round and have gemini judge the outcome"
```

Each panelist gets the same prompt, responds independently, and (in debate mode) sees the other responses before giving a revised stance. The judge synthesises the final answer. Results are written to the SQLite execution graph so you can query them later with `get_run` / `run_tree`.

**Special panelist `host`:** Panel routes the agent name `host` through the MCP `sampling/createMessage` primitive, asking the connected MCP client (Claude Code, etc.) to invoke its own LLM as a peer panelist. Means Claude Code can be a true participant in a debate of its own PR, not just the dispatcher. No paid API call — the host's tokens are the host's problem. Cost tier reports as `host_sampling`.

---

## 🚦 multiaudit — magic-phrase PR review

Say one of these to Claude Code (or whatever MCP client you're driving) and Panel fires a 4-way audit panel against the current branch's diff:

> "OK multiaudit it now"
> "audit this PR"
> "panel this branch"
> "review with all models before I push"

The `multiaudit` tool reads `git diff` (vs `main`, falling back to uncommitted/staged), packages it with recent commit messages for intent context, and dispatches a `start_task('panel', ...)` with `[codex, gemini, claude, grok-4.3]`, 1 debate round, codex as judge. Returns the task_id + the live web viewer URL so the user can watch the debate unfold while Claude Code stays available to follow up.

`host` (Claude Code as a peer panelist via MCP sampling) is **opt-in**, not default — pass `panelists=["host", "codex", ...]` explicitly. Most MCP hosts (including Claude Code today) don't advertise the sampling capability, so `host` would fail; the implementation excludes it from defaults to keep the magic-phrase path reliable.

**No `XAI_API_KEY`?** The default panelist set includes `grok-4.3`, which has no OAuth path. If you don't want to set an X.AI key, drop it from the panel: `multiaudit panelists=["codex","gemini","claude"]` (or set `PANEL_MULTIAUDIT_PANELISTS=codex,gemini,claude` once in `.env` to make that the global default).

Default audit rubric (built into the prompt): **VERDICT / BUGS / DESIGN CONCERNS / SECURITY / MISSING TESTS / WHAT YOU'D ATTACK.** Diff truncated at 60KB with a clear marker so panelists know they're reasoning about a subset.

```
multiaudit                                  # default panelists, 1 debate round
multiaudit extra_context="focus on the    # narrow the audit
            new auth flow specifically"
multiaudit panelists=["host","gpt-5.5"]   # override panelist set
multiaudit base_branch="HEAD"             # audit only uncommitted changes
multiaudit debate_rounds=2                # deeper pressure-testing
```

---

## 🐞 bugfind — magic-phrase bug investigation

Sister tool to `multiaudit`, but bug-shaped. Say one of these and Panel fires a 4-way investigation panel against your bug description:

> "bugfind it"
> "find this bug"
> "use panel to find the bug"
> "what's breaking, panel debug it"

The `bugfind` tool takes the user's bug description, auto-attaches debugging context (recent commits, the tail of `logs/mcp_server.log` filtered to ERROR/Traceback/Failed/Exception, optionally explicitly attached files), and dispatches a `start_task('panel', ...)` with `[codex, gemini, claude, grok-4.3]`, 1 debate round, codex as judge. Returns the task_id + live viewer URL.

The investigation rubric is intentionally different from multiaudit's audit rubric:
**REPRO** (exact steps) → **ROOT CAUSE** (file:line + reasoning) → **MINIMAL FIX** (smallest diff, with code snippet, ideally as a unified diff) → **REGRESSION TEST** (the test that would have caught it, with assertion specifics) → **BLAST RADIUS** (what else this affects) → **WHAT YOU MISSED** (what the original implementer likely didn't consider).

The judge synthesises a single fix proposal you can review and apply. When file paths or symbols appear in the description, pass `attached_files=[<absolute paths>]` so the panelists can read the actual code rather than guessing. Skip the log tail (`skip_log_tail=true`) for UI/doc bugs that aren't reflected in logs.

```
bugfind "the viewer header shows 'grok-4.3' instead of 'panel'"
bugfind "..." attached_files=["/abs/path/to/utils/web_viewer.py"]
bugfind "..." debate_rounds=2                 # deeper pressure-testing
bugfind "..." panelists=["claude","codex"]    # 2-way only
bugfind "..." skip_log_tail=true              # UI bug, logs irrelevant
```

**Same `XAI_API_KEY` caveat as multiaudit** — the default panelist set includes `grok-4.3`. Drop it via `panelists=["codex","gemini","claude"]` or set `PANEL_BUGFIND_PANELISTS=codex,gemini,claude` globally if you don't want to set an X.AI key.

**Security:** `attached_files` contents and the auto-attached log tail go verbatim to the configured panelist APIs. Panel applies a redaction pass that strips API-key shapes (sk- / sk-ant- / AIza / xai-), JWTs, Bearer headers, and user-home paths before dispatch — but defence-in-depth: don't attach files containing secrets you wouldn't want in OpenAI / Anthropic / Gemini / xAI request logs.

---

## 🗄️ SQLite Execution Graph

Every tool dispatch — including nested panel fanouts, clink subagents, async tasks, and OAuth-to-API fallbacks — is recorded in a per-repo `.panel/execution_graph.db`. This gives you:

- **Restart-safe panels** — if Panel restarts mid-panel, prior results are still readable
- **Cost attribution** — `run_tree` returns a cost-tier rollup (`oauth_free` / `api_paid` / `oauth_fallback_paid` / `host_sampling`) so you can see exactly which sub-call drove spend
- **Audit trail** — replay prior runs, compare model behaviour over time
- **Run lineage** — `run_tree` shows the full parent→child call graph including `fallback` edges

**Per-repo by default.** Default DB path is `<cwd>/.panel/execution_graph.db` — each project Claude Code opens has its own isolated debate history, so the live web viewer for one repo never shows runs from another. The `.panel/` directory is gitignored.

Override with `PANEL_GRAPH_DB=<absolute path>` for a shared / global view (e.g. the legacy `~/.panel/execution_graph.db`). Set to empty string to disable the graph entirely.

---

## 🪟 Live Web Viewer

Panel serves a tiny local HTTP server (default `http://127.0.0.1:8765/`) that **lazy-starts on the first Panel tool call** — no tab pops if you never use Panel this session. Browser opens automatically when it does start.

Single page renders the execution graph live:
- Every panel run, panelist sub-call, OAuth fallback edge, judge synthesis
- Per-leaf cost tier badges (oauth_free / oauth_fallback_paid / api_paid / host_sampling)
- **Live activity feed for in-flight runs** — every emit_progress event from clink subprocesses (file_read, tool_use, text_chunk, etc.) shows up in real time, colour-coded by type, max-height scrolling so it doesn't grow unbounded
- Auto-selects the most recent active run on first load — you land on the live thing without clicking
- HTML-escaped + class-name-sanitised; bad query params return 400 instead of crashing the daemon

Updates over Server-Sent Events (`/events`) — the viewer subscribes via `EventSource` and only re-fetches when the execution graph version changes; falls back to 5s polling if the SSE connection drops. All four flagship providers (Anthropic / OpenAI / xAI / Gemini) stream by default — direct-API panelists' partial responses appear in the transcript pane as they're written, throttled to ~10 Hz so SQLite stays responsive. Opt out per-provider with `PANEL_OPENAI_STREAM=0` / `PANEL_GEMINI_STREAM=0`. Anthropic streams unconditionally.

| Env var | Default | Effect |
|---|---|---|
| `PANEL_WEB_PORT` | `8765` | Port to bind. Walks forward up to +20 if taken. |
| `PANEL_WEB_HOST` | `127.0.0.1` | Local-only by default. Set `0.0.0.0` to expose (you opt in). |
| `PANEL_WEB_AUTO_OPEN` | `1` | Auto-open browser tab on Panel boot. Set `0` to disable. |
| `PANEL_WEB_DISABLE` | unset | Set to skip the web server entirely. |

The MCP tool `web_url` returns the live URL on demand so Claude Code can hand it to the user mid-conversation.

---

<details>
<summary><b id="tool-configuration">👉 Tool Configuration</b></summary>

### Default Configuration

To optimize context window usage, only essential tools are enabled by default:

**Enabled by default:**
- `chat`, `thinkdeep`, `planner`, `consensus`, `panel` - Core collaboration tools
- `multiaudit` - Magic-phrase PR audit (reads git diff, fans out to panel)
- `bugfind` - Magic-phrase bug investigation (takes a bug description + auto-collected context, fans out to panel for diagnosis + fix proposal)
- `codereview`, `precommit`, `debug` - Essential code quality tools
- `apilookup` - Rapid API/SDK information lookup
- `challenge` - Critical thinking utility
- `clink` - CLI-to-CLI bridge with automatic OAuth-to-API fallback
- `start_task`, `task_status`, `task_result`, `cancel_task` - Async task system
- `list_runs`, `get_run`, `run_tree` - Execution graph queries
- `web_url` - Return the live web viewer URL
- `listmodels`, `version` - Always enabled, cannot be disabled

**Disabled by default:**
- `analyze`, `refactor`, `testgen`, `secaudit`, `docgen`, `tracer`

### Enabling Additional Tools

**Option 1: Edit your .env file**
```bash
# Default configuration (from .env.example)
DISABLED_TOOLS=analyze,refactor,testgen,secaudit,docgen,tracer

# To enable specific tools, remove them from the list
# Example: Enable analyze tool
DISABLED_TOOLS=refactor,testgen,secaudit,docgen,tracer

# To enable ALL tools
DISABLED_TOOLS=
```

**Option 2: Configure in MCP settings**
```json
{
  "mcpServers": {
    "panel": {
      "env": {
        "DISABLED_TOOLS": "refactor,testgen,secaudit,docgen,tracer",
        "DEFAULT_MODEL": "auto",
        "DEFAULT_THINKING_MODE_THINKDEEP": "high",
        "ANTHROPIC_API_KEY": "your-anthropic-key",
        "OPENAI_API_KEY": "your-openai-key",
        "GEMINI_API_KEY": "your-gemini-key",
        "XAI_API_KEY": "your-xai-key",
        "LOG_LEVEL": "INFO",
        "CONVERSATION_TIMEOUT_HOURS": "3",
        "MAX_CONVERSATION_TURNS": "50"
      }
    }
  }
}
```

**Option 3: Enable all tools**
```json
{
  "mcpServers": {
    "panel": {
      "env": {
        "DISABLED_TOOLS": ""
      }
    }
  }
}
```

**Note:**
- Essential tools (`version`, `listmodels`) cannot be disabled
- After changing tool configuration, restart your Claude session for changes to take effect
- Each tool adds to context window usage, so only enable what you need

</details>

<details>
<summary><b>⚙️ Advanced Environment Variables</b></summary>

These variables are not shown by default but control important behaviour:

| Variable | Default | Description |
|---|---|---|
| `PANEL_MAX_CONCURRENT_API` | `16` | Max parallel API calls to model providers |
| `PANEL_MAX_PROVIDER_THREADS` | `32` | Thread pool size for provider workers |
| `PANEL_API_TIMEOUT_S` | `600` | Per-call SDK timeout in seconds |
| `PANEL_GRAPH_DB` | `<cwd>/.panel/execution_graph.db` | Per-repo SQLite execution graph path. Set to an absolute path for a shared/global view; `""` to disable |
| `PANEL_CLINK_METADATA_CAP` | *(internal)* | Max chars of clink metadata injected into prompts |
| `PANEL_CLINK_RAW_OUTPUT_CAP` | *(internal)* | Max chars of raw CLI output returned to MCP client |
| `PANEL_DEBUG_CLI_OUTPUT` | unset | **⚠️ Disables all secret redaction in clink output. Never set in shared environments.** |
| `PANEL_OAUTH_FIRST` | `1` | Try free OAuth (codex/gemini/claude CLI) before paid API for every direct-provider tool, when the model has a CLI route. Set `0` to disable and bill every call against the paid API. |
| `PANEL_MCP_FORCE_ENV_OVERRIDE` | unset | When `true`, **`.env` file values override process env vars** (useful when an MCP client passes stale/cached keys). Default behavior — process env wins. |
| `CONVERSATION_TIMEOUT_HOURS` | `3` | Hours before a conversation thread expires |
| `MAX_CONVERSATION_TURNS` | `50` | Max turns per thread (50 turns = 25 exchanges) |

**Soft landing on zero providers.** If you start the server with no API keys AND no OAuth CLIs installed, it no longer crashes — it starts with limited functionality (`listmodels`, `version`, `web_url`, and graph queries when `PANEL_GRAPH_DB` is not disabled) and logs a friendly summary telling you what to set to unlock more. Set any one of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `XAI_API_KEY` / `OPENROUTER_API_KEY` / `DIAL_API_KEY` / `CUSTOM_API_URL`, or install + login to one of the OAuth CLIs (`codex login` / `gemini` first run / `claude /login`), then restart your MCP client.

</details>

## Key Features

**AI Orchestration**
- **Auto model selection** - Claude picks the right AI for each task
- **Multi-model workflows** - Chain different models in single conversations
- **Conversation continuity** - Context preserved across tools and models
- **[Context revival](docs/context-revival.md)** - Continue conversations even after context resets
- **Panel debates** - Fan-out to N models with adversarial rounds and judge synthesis
- **Async tasks** - Long-running calls run in background, poll for results

**Model Support**
- **Multiple providers** - Anthropic, Gemini, OpenAI, Azure, X.AI, OpenRouter, DIAL, Ollama
- **Latest models** - Claude Opus 4.8 / Sonnet 5, GPT-5.5 / 5.4 / 5.1-codex, Gemini 3.1 Pro, Grok-4.3 / 4.1-fast, local Llama
- **[Thinking modes](docs/advanced-usage.md#thinking-modes)** - Control reasoning depth vs cost
- **Vision support** - Analyze images, diagrams, screenshots

**Developer Experience**
- **Guided workflows** - Systematic investigation prevents rushed analysis
- **Smart file handling** - Auto-expand directories, manage token limits
- **Web search integration** - Access current documentation and best practices
- **[Large prompt support](docs/advanced-usage.md#working-with-large-prompts)** - Bypass MCP's 25K token limit
- **SQLite execution graph** - Durable audit trail at `<cwd>/.panel/execution_graph.db` (per-repo)

## Example Workflows

**Multi-model Code Review:**
```
"Perform a codereview using gemini-3.1-pro-preview and gpt-5.5, then use planner to create a fix strategy"
```
→ Claude reviews code systematically → Consults Gemini 3.1 Pro → Gets GPT-5.5's perspective → Creates unified action plan

**Collaborative Debugging:**
```
"Debug this race condition with max thinking mode, then validate the fix with precommit"
```
→ Deep investigation → Expert analysis → Solution implementation → Pre-commit validation

**Architecture Planning:**
```
"Plan our microservices migration, get consensus from gemini-3.1-pro-preview and claude-opus-4-8 on the approach"
```
→ Structured planning → Multiple expert opinions → Consensus building → Implementation roadmap

**Panel Debate:**
```
"Use panel with gemini-3.1-pro-preview, gpt-5.5, and grok-4.3 to debate: REST vs GraphQL for our API"
```
→ 3 models respond independently → Debate round → Judge synthesises final recommendation

**Async Long Task:**
```
"start_task: run secaudit on the entire codebase with claude-opus-4-8"
# ... keep working ...
"task_status <task_id>" → check progress
"task_result <task_id>" → get full report when done
```

👉 **[Advanced Usage Guide](docs/advanced-usage.md)** for complex workflows, model configuration, and power-user features

## Quick Links

**📖 Documentation**
- [Onboarding](ONBOARDING.md) - Get from `git clone` to a working `multiaudit` in 10 minutes
- [Docs Overview](docs/index.md) - High-level map of major guides
- [Getting Started](docs/getting-started.md) - Complete setup guide
- [Tools Reference](docs/tools/) - All tools with examples
- [Advanced Usage](docs/advanced-usage.md) - Power user features
- [Configuration](docs/configuration.md) - Environment variables, restrictions
- [Adding Providers](docs/adding_providers.md) - Provider-specific setup (OpenAI, Azure, custom gateways)
- [Model Ranking Guide](docs/model_ranking.md) - How intelligence scores drive auto-mode suggestions

**🔧 Setup & Support**
- [WSL Setup](docs/wsl-setup.md) - Windows users
- [Troubleshooting](docs/troubleshooting.md) - Common issues
- [Contributing](docs/contributions.md) - Code standards, PR process

## License

Apache 2.0 License - see [LICENSE](LICENSE) file for details.

## Acknowledgments

Panel MCP is a substantially-rewritten fork of [`BeehiveInnovations/pal-mcp-server`](https://github.com/BeehiveInnovations/pal-mcp-server) (formerly [`zen-mcp-server`](https://github.com/BeehiveInnovations/zen-mcp-server)). The core tool framework, provider abstraction, and several workflow tools (chat, consensus, codereview, debug, thinkdeep, planner, precommit, etc.) were inherited from upstream. Substantial new orchestration surfaces (panel debate, multiaudit, async task system, OAuth-to-API fallback, durable execution graph, live web viewer, per-token streaming, central validated dispatch, bounded provider concurrency) were added in this fork.

Also built on top of:
- [MCP (Model Context Protocol)](https://modelcontextprotocol.com)
- [Codex CLI](https://developers.openai.com/codex/cli)
- [Claude Code](https://claude.ai/code)
- [Gemini](https://ai.google.dev/)
- [OpenAI](https://openai.com/)
- [Anthropic](https://www.anthropic.com/)
- [Azure OpenAI](https://learn.microsoft.com/azure/ai-services/openai/)

### Star History

[![Star History Chart](https://api.star-history.com/svg?repos=DanielGuru/panel-mcp-server&type=Date)](https://www.star-history.com/#DanielGuru/panel-mcp-server&Date)

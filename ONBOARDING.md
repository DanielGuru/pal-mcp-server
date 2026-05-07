# Onboarding — Panel MCP Server (DanielGuru fork)

Get from `git clone` to a working `multiaudit` in under 10 minutes.

This is a **fork** of `BeehiveInnovations/pal-mcp-server` (formerly PAL MCP), substantially rewritten. Upstream stalled in
December 2025; this fork ships fixes plus orchestration features (background
tasks, parallel panels, adversarial debate, observable streaming, OAuth-to-API
fallback, central validated dispatch, bounded provider concurrency, durable
execution graph + live web viewer). Don't assume upstream parity — read
`CLAUDE.md` in this repo for full architectural context.

---

## 1. Clone and install (editable)

```bash
git clone https://github.com/DanielGuru/panel-mcp-server ~/Projects/panel-mcp-server
cd ~/Projects/panel-mcp-server

uv tool install --editable .
which panel-mcp-server   # → ~/.local/bin/panel-mcp-server
```

**Do NOT use `uvx --from /local/path`.** uv caches built wheels and reuses
them — your source edits silently won't take effect. Editable install via
`uv tool install --editable` propagates edits without cache games. After source
changes, just restart Claude Code.

Python 3.10+ is required.

---

## 2. Configure `~/.claude.json`

Panel is launched as an MCP server by Claude Code. Add this block under
`mcpServers` in `~/.claude.json`:

```json
{
  "mcpServers": {
    "panel": {
      "command": "/Users/YOU/.local/bin/panel-mcp-server",
      "args": [],
      "env": {
        "OPENAI_API_KEY": "sk-...",
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "GEMINI_API_KEY": "AIza...",
        "XAI_API_KEY": "xai-...",

        "DEFAULT_MODEL": "auto",
        "DISABLED_TOOLS": "",
        "PANEL_TASK_WAIT_CAP_S": "30",
        "PANEL_CLAUDE_OAUTH_FALLBACK_MODEL": "claude-sonnet-4-6"
      }
    }
  }
}
```

Replace `/Users/YOU` with your actual home path (output of `which
panel-mcp-server`).

### Lock down the file

`~/.claude.json` ends up world-readable on most machines (`0644`). It contains
your API keys. Tighten it:

```bash
chmod 600 ~/.claude.json
```

Audit-flagged: do this before adding keys or immediately after.

### Where to get each credential

| Variable | Get it from | Notes |
|---|---|---|
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys | Used for paid GPT-5.x calls + codex OAuth fallback |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys | Used for paid Claude calls + claude OAuth fallback |
| `GEMINI_API_KEY` | https://aistudio.google.com/app/apikey | Used for paid Gemini calls + gemini OAuth fallback |
| `XAI_API_KEY` | https://console.x.ai/ | Grok has no OAuth path — always paid |

Set only the keys for providers you intend to use. Tools for other providers
will be skipped at startup.

### Optional env tunables

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_MODEL` | `auto` | Model used when a tool call doesn't name one. `auto` = Claude picks. Concrete values like `claude-sonnet-4-6`, `gpt-5.5`, `gemini-3.1-pro-preview`, `grok-4.3` pin a default. |
| `DISABLED_TOOLS` | _unset_ | Comma-separated tool names to disable (e.g. `consensus,thinkdeep`). |
| `PANEL_TASK_WAIT_CAP_S` | `30` | Hard cap on `task_result(wait_seconds=…)`. Don't raise unless you know why — it keeps the conversation channel responsive. |
| `PANEL_CLAUDE_OAUTH_FALLBACK_MODEL` | `claude-sonnet-4-6` | When the `claude` OAuth CLI 401s / quotas out, the API fallback uses this model. Set to `claude-opus-4-7` if you'd rather pay for opus. |
| `PANEL_OPENAI_STREAM` | `1` | Stream OpenAI / xAI responses (gpt-5.x, grok-4.3) so per-chunk deltas land in the live viewer as they're written. Set to `0` to opt out (only useful for the cassette-replay integration tests). Anthropic streams unconditionally. |
| `PANEL_GEMINI_STREAM` | `1` | Stream Gemini responses (gemini-3.x) the same way. Set to `0` to opt out. |
| `PANEL_MULTIAUDIT_JUDGE` | `codex` | Default judge agent for `multiaudit`. Override with `claude`, `gemini`, `grok-4.3`, or any model id. Per-call `judge=` arg always wins. |
| `PANEL_MULTIAUDIT_PANELISTS` | `codex,gemini,claude,grok-4.3` | Comma-separated default panelist list. Per-call `panelists=` arg always wins. |
| `PANEL_GRAPH_DB` | `<cwd>/.panel/execution_graph.db` | Per-repo SQLite store. Set to an absolute path to share across repos; `""` to disable. |
| `PANEL_WEB_PORT` | `8765` | Viewer port. Walks +20 if taken. |
| `PANEL_WEB_HOST` | `127.0.0.1` | Local-only by default. The viewer has no auth, so non-localhost binds also require `PANEL_WEB_ALLOW_REMOTE=1` to start. |
| `PANEL_WEB_ALLOW_REMOTE` | _unset_ | Opt-in gate — set to `1` to allow `PANEL_WEB_HOST=0.0.0.0` (or any non-localhost bind) to start. Without it the viewer refuses to expose the unauthenticated execution graph. |
| `PANEL_WEB_AUTO_OPEN` | `1` | Auto-open the viewer in your browser on first Panel tool call. `0` disables. |
| `PANEL_WEB_DISABLE` | _unset_ | Skip the viewer entirely. |
| `PANEL_MAX_CONCURRENT_API` | `16` | Global cap on concurrent paid API calls. |
| `PANEL_API_TIMEOUT_S` | `600` | Per-call SDK timeout. |

Full list in `CLAUDE.md`.

---

## 3. Authenticate the OAuth CLIs (free tier)

The panel uses three CLIs that authenticate against your subscriptions instead
of consuming API credits. Each one falls back to its paid API path when quota
runs out.

```bash
# Codex CLI (uses your ChatGPT subscription)
codex login                                    # opens browser

# Gemini CLI (uses your Google account / Gemini subscription)
gemini                                         # first run prompts OAuth

# Claude CLI (uses your Claude subscription — claude.ai / Claude Code)
claude /login                                  # inside the CLI
```

Verify auth state:

```bash
cat ~/.codex/auth.json       | jq '.auth_mode'    # → "chatgpt"
cat ~/.gemini/oauth_creds.json | jq 'keys'
ls ~/.claude/                                     # session files present
```

Grok has no OAuth path — it always uses `XAI_API_KEY`.

---

## 4. Restart Claude Code, then verify

```
# In Claude Code, after restarting:
use panel:listmodels
```

Expected output: one entry per provider you set an API key for. The full
multiaudit path uses all four (openai, anthropic, gemini, xai), but you can
start with one — `chat` / `consensus` / `panel` work as long as the model you
name is reachable. If a provider you expected is missing, its API key isn't
loaded — recheck the env block in `~/.claude.json`.

If `panel:` doesn't autocomplete in Claude Code, the MCP server isn't registered.
Re-check `~/.claude.json` syntax (it's strict JSON — no trailing commas) and
that the `command` path matches `which panel-mcp-server`.

---

## 5. First multiaudit (the magic phrase)

Make any small change in a git repo, then in Claude Code:

> multiaudit it

Claude will fire `panel:multiaudit`, which:

1. Reads the current branch's `git diff` vs `main`.
2. Packages it with intent context (recent commits + a structured rubric:
   verdict / bugs / design / security / missing tests / what you'd attack).
3. Fires a 4-way panel via `start_task` — defaults: `codex`, `gemini`,
   `claude`, `grok-4.3`. 1 debate round. Codex judges. `host` (Claude Code
   itself via MCP sampling) is opt-in, since most MCP hosts don't advertise
   the sampling capability today — pass `panelists=["host", ...]`
   explicitly to invite it.
4. Returns a `task_id` + a **live web viewer URL** — open it to watch panelists
   complete in real time, see the debate tree, and drill into any sub-run.

Magic phrases that fire `multiaudit` (no preamble — Claude dispatches
immediately):

- `multiaudit it` / `multiaudit this`
- `audit this` / `audit this PR` / `audit this branch`
- `panel this` / `panel this branch`
- `review with all` / `review with all models`
- `fan this out` / `fan it out for review`
- `what do the models think` / `second opinion from everyone`

Add context inline: `"multiaudit but focus on the auth changes"` — anything
after the trigger phrase is forwarded as `extra_context`.

`multiaudit` is a **gate, not a post-hoc check** — run it BEFORE you commit /
push.

**No `XAI_API_KEY`?** The default panel includes `grok-4.3` which has no
OAuth path. Drop it from the panel: `multiaudit panelists=["codex","gemini","claude"]`,
or set `PANEL_MULTIAUDIT_PANELISTS=codex,gemini,claude` once and forget it.

---

## 5b. First bugfind (sister magic phrase)

Got a bug you can't pin down? Same shape as multiaudit, different rubric.
Describe the symptom; the panel debates root cause + minimal fix:

> bugfind: the viewer header shows 'grok-4.3' instead of 'panel' for multiaudit runs

Or with attached files when the bug is in specific code:

> bugfind: utils/web_viewer.py picker shows wrong run on completion. attached_files: /abs/path/to/utils/web_viewer.py

Magic phrases that fire `bugfind`:

- `bugfind` / `bugfind it` / `bugfind this`
- `find this bug` / `use panel to find the bug`
- `what's breaking` / `panel debug this` / `diagnose with all models`

The rubric the panel sees: REPRO / ROOT CAUSE (file:line) / MINIMAL FIX
(unified diff if possible) / REGRESSION TEST / BLAST RADIUS / WHAT YOU MISSED.
The judge synthesises a single fix proposal you can review and apply.

Auto-collected context: recent commits, the tail of `logs/mcp_server.log`
filtered to ERROR/Traceback/Failed/Exception lines (with secret-shape
redaction applied before dispatch), and any explicitly attached files
(also redacted as a safety net).

Same `XAI_API_KEY` caveat as multiaudit — drop `grok-4.3` from `panelists`
if you don't have an X.AI key.

`PANEL_BUGFIND_PANELISTS` and `PANEL_BUGFIND_JUDGE` env vars work the
same way as the `MULTIAUDIT` ones.

---

## 6. The settings tab — quick toggles

The viewer has a **settings** button next to the run picker. It shows:

- Live env vars you can change without restarting (streaming flags, `PANEL_MULTIAUDIT_JUDGE`/`PANELISTS`, `PANEL_BUGFIND_JUDGE`/`PANELISTS`). Edit the value, click `save` — the next provider/multiaudit/bugfind call picks it up immediately.
- Provider key presence (which API keys are loaded).
- OAuth-CLI login status (codex / gemini / claude).
- Viewer host/port/URL + execution-graph DB path + version + tools registered.
- Read-only env vars that need a Claude Code restart to take effect.

Useful for: flipping streaming off when debugging cassette tests, swapping the multiaudit judge mid-session, or sanity-checking why a provider is missing from `listmodels`.

---

## 7. The viewer — what to expect

- **Lazy-started.** No tab on Claude Code boot. The first Panel tool call pops
  the viewer and keeps it for the rest of the process. Process exit kills it.
- **URL appears in every multiaudit / panel response** — `web_viewer_url`
  field. Open it once; it auto-refreshes.
- **Picker filter.** The run picker hides observation tools
  (`task_status`, `list_runs`, etc.) so the list is signal, not poll noise.
- **Stale-port indicator.** The port number in the viewer header turns **red**
  if you're looking at a stale tab from a previous Panel process. Refresh — the
  current Panel has a new port (PANEL_WEB_PORT walks +20 if taken).
- **Live activity feed.** Running runs render their progress events
  (file_read / tool_use / text_chunk for clink CLIs; per-token streaming is
  on the roadmap for direct-API providers). Completed runs collapse the feed
  to a `<details>` summary.

---

## 7. Default model picker (quick reference)

`DEFAULT_MODEL` controls what Claude reaches for when no model is specified.
Concrete examples:

```json
"DEFAULT_MODEL": "auto"                       # (default) Claude picks per task
"DEFAULT_MODEL": "claude-sonnet-4-6"          # cheap, fast Anthropic
"DEFAULT_MODEL": "claude-opus-4-7"            # premium Anthropic
"DEFAULT_MODEL": "gpt-5.5"                    # premium OpenAI
"DEFAULT_MODEL": "gemini-3.1-pro-preview"     # premium Gemini
"DEFAULT_MODEL": "grok-4.3"                   # xAI flagship
```

`panel:listmodels` shows everything available; aliases work too
(`opus`, `sonnet`, `gpt-5`, `pro`, `flash`, etc.).

---

## 8. Routing recap (free vs paid)

| You say… | Claude picks | Cost |
|---|---|---|
| "ask codex …" / "use codex …" | `clink` (codex CLI subprocess) | Free (ChatGPT subscription); paid fallback if quota |
| "ask gemini …" | `clink` (gemini CLI subprocess) | Free (Google subscription); paid fallback if quota |
| "ask claude …" | `clink` (claude CLI subprocess) | Free (Claude subscription); paid fallback if quota |
| "use gpt-5.5" / "use grok-4.3" | `chat` with that exact model | Paid (`OPENAI_API_KEY` / `XAI_API_KEY`) |
| "consensus across …" | `consensus` workflow | Paid (mixes the named models) |
| "audit this" / magic phrases | `multiaudit` → 4-way panel | Mixed: 3 OAuth (codex / gemini / claude) + 1 paid (grok-4.3) by default |

OAuth-to-API fallback is **always surfaced**, never silent. The panel reads
`metadata.oauth_fallback_used` from each panelist's response and labels its
`cost_tier` as `oauth_fallback_paid` if quota replenishment failed. You'll see
this in the viewer per-run.

---

## 9. Smoke test (60-second sanity check)

```
# In Claude Code:
use panel:chat with prompt "say hello in one word" using model auto
use panel:listmodels
```

If both succeed and `listmodels` reports four providers, you're done.

A live multiaudit completes in 60-180s for a typical small diff. Open the
viewer URL while it runs.

---

## Troubleshooting

- **Tool calls hang past 30s without output.** That's expected for panels —
  use `start_task` (or trigger via `multiaudit`, which already does), then
  `task_status` / `task_result(wait_seconds=30)`. The 30s cap is hard, by
  design.
- **"my edit didn't take effect".** You're on a `uvx --from /path` install,
  not editable. Reinstall via `uv tool install --editable .`.
- **Viewer doesn't open.** Set `PANEL_WEB_AUTO_OPEN=0` and open the URL from
  the multiaudit response manually. If port `8765` is in use, Panel walks +20 —
  check the response for the actual port.
- **`panel:listmodels` shows fewer than four providers.** The missing
  provider's API key isn't being read. Re-check JSON syntax in
  `~/.claude.json`, then restart Claude Code.
- **Codex/Gemini/Claude OAuth keeps falling back to paid.** Quota's exhausted
  for the day. The fallback is doing its job — wait for refill or top up the
  subscription.

---

## Where to go next

- `CLAUDE.md` — full architectural context, invariants, open work queue.
- `README.md` — upstream-flavored feature tour.
- `docs/tools/` — per-tool documentation.
- Logs: `logs/mcp_server.log` (verbose) and `logs/mcp_activity.log`
  (focused tool-call activity).

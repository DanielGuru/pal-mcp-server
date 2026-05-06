# Panel MCP — Name History

This project has been renamed twice:

1. **Zen MCP** → **PAL MCP** (BeehiveInnovations, 2025) — to avoid clashing with another similarly named product and to reflect its role as a Provider Abstraction Layer.
2. **PAL MCP** → **Panel MCP** (DanielGuru fork, 2026) — after a substantial rewrite that re-centered the project on multi-model panel orchestration: parallel fan-out, adversarial debate, judged synthesis, observable streaming. The "panel" abstraction (`tools/panel.py`) is now the load-bearing surface, with `multiaudit`, `consensus`, and `chat` riding on top of it.

If you're upgrading from PAL MCP:

- Environment variables: `PAL_*` → `PANEL_*` (e.g. `PAL_GRAPH_DB` → `PANEL_GRAPH_DB`). Hard cut — no aliases.
- Per-repo execution graph directory: `.pal/` → `.panel/`. Existing `.pal/execution_graph.db` files will not be read; rename the directory or set `PANEL_GRAPH_DB` to the old path.
- CLI binary: `pal-mcp-server` → `panel-mcp-server`. Reinstall via `uv tool install --editable .` and update your MCP client config to point at the new binary.
- MCP server identifier: `pal` → `panel` (the key under `mcpServers` in your MCP client config is conventionally renamed too, but this is just a label).
- Slash-command prefix in MCP prompts: `/pal:` → `/panel:`.

This is **not just a rename**. Beyond the upstream PAL/Zen surface (chat, consensus, codereview, debug, thinkdeep, planner, precommit and the provider abstraction), the Panel fork adds:

- `panel` (parallel fan-out + adversarial debate + judge synthesis), `multiaudit` (magic-phrase PR audit), `start_task`/`task_status`/`task_result`/`cancel_task` (async background tasks), `list_runs`/`get_run`/`run_tree`/`web_url` (durable execution-graph queries).
- Central validated dispatch via `server.execute_tool()` — internal callers can no longer bypass MCP-boundary validation.
- Bounded async provider concurrency: semaphore + ThreadPoolExecutor + per-call SDK timeout, with cancel-aware semaphore release.
- Provenance-based size-check bypass (replaces the depth-based bypass that was exploitable).
- OAuth-to-API fallback for clink with honest `cost_tier` reporting (`oauth_free` / `oauth_fallback_paid` / `api_paid` / `host_sampling`).
- Durable SQLite execution graph (`.panel/execution_graph.db`, per-repo by default), live HTTP viewer with SSE streaming, redacted clink metadata, opt-in remote-bind gate.
- First-party Anthropic provider, per-token streaming for all four flagships, Anthropic OAuth-to-API fallback.
- Trimmed model registry (current flagships only), safer clink defaults (no `--dangerously-bypass-approvals-and-sandbox` / `--yolo`).

See `CLAUDE.md` for the full architecture; see `README.md` Acknowledgments for the upstream lineage.

# Panel MCP — Name History

This project has been renamed twice:

1. **Zen MCP** → **PAL MCP** (BeehiveInnovations, 2025) — to avoid clashing with another similarly named product and to reflect its role as a Provider Abstraction Layer.
2. **PAL MCP** → **Panel MCP** (DanielGuru fork, 2026) — after a substantial rewrite that re-centered the project on multi-model panel orchestration: parallel fan-out, adversarial debate, judged synthesis, observable streaming. The "panel" abstraction (`tools/panel.py`) is now the load-bearing surface, with `multiaudit`, `consensus`, and `chat` riding on top of it.

If you're upgrading from PAL MCP:

- Environment variables: `PAL_*` → `PANEL_*` (e.g. `PAL_GRAPH_DB` → `PANEL_GRAPH_DB`). Hard cut — no aliases.
- Per-repo execution graph directory: `.pal/` → `.panel/`. Existing `.pal/execution_graph.db` files will not be read; rename the directory or set `PANEL_GRAPH_DB` to the old path.
- CLI binary: `pal-mcp-server` → `panel-mcp-server`. Reinstall via `uv tool install --editable .` and update your MCP client config to point at the new binary.
- MCP server identifier: `pal` → `panel` (the key under `mcpServers` in `~/.claude.json` is conventionally renamed too, but this is just a label).
- Slash-command prefix in MCP prompts: `/pal:` → `/panel:`.

Code is otherwise identical to the last PAL MCP release — only names changed.

# AGENTS.md

This is a customized fork of `BeehiveInnovations/pal-mcp-server`. Upstream is stalled; this fork ships ongoing improvements (background tasks, parallel panels, adversarial debate, streaming progress, push notifications) that don't exist upstream.

**Read [`CLAUDE.md`](./CLAUDE.md) first.** It is the canonical guide for any AI agent working on this codebase. This file is a thin pointer + the rules that matter most.

## Hard rules

- **Editable install only.** Do not use `uvx --from /local/path`; uv caches built wheels and reuses them, breaking iterative dev. Install once with `uv tool install --editable ~/Projects/pal-mcp-server`.
- **Restart Claude Code after source edits.** PAL caches config at process startup; the editable install ensures next launch sees new code without reinstall.
- **Never re-add `--dangerously-bypass-approvals-and-sandbox` (codex) or `--yolo` (gemini)** to clink configs. Those allow sub-agents to silently mutate the filesystem.
- **Never commit API keys.** They live in `~/.claude.json`, never in repo. Don't `cat .env`-style debug outputs into chat either.
- **Conventional commits, push to `origin main`.** No PR workflow on this fork.
- **Validate before committing.** `python3 -c "import ast; ast.parse(open('FILE').read())"` for any non-trivial edit; full import via `~/.local/share/uv/tools/pal-mcp-server/bin/python3 -c "import server"` after architectural changes.

## Project structure (one line)

`server.py` (entry, TOOLS factory dict, `make_tool()`, **`execute_tool()` is the only dispatch path**) → `tools/` (28 MCP tools incl. tasks/panel/multiaudit/web_url/graph queries) → `clink/` (subprocess CLI agents + parsers + OAuth-to-API fallback + redacted metadata) → `providers/` (direct API providers + async wrapper bounded by semaphore + ThreadPoolExecutor + per-call SDK timeout) → `utils/` (execution_graph + web_viewer + host_session) → `conf/` (model + CLI configs).

## Style

Python 3.10+, line length ~120, type hints required for new code, `from __future__ import annotations`, conventional commits, no comments restating the code.

## Magic-phrase recognition

When the user says "multiaudit" / "audit this" / "audit this PR" / "panel this branch" / "review with all" / similar — call the `multiaudit` MCP tool immediately. It reads the diff, fires a 4-way panel ([host, codex, gemini, grok-4.3]), and returns a task_id + the live web viewer URL. Hand the URL to the user, then poll for findings. See CLAUDE.md "Magic-phrase recognition" for the full list.

## What's open

Multi-step panel workflow (Claude can intervene between debate rounds), TaskManager → execution-graph migration (restart-safe in-flight tasks), conversation memory persistence (continuation_id survives restart), SSE for the web viewer (currently 2s polling), true `stream=True` async path for direct-API providers, per-CLI custom OAuth patterns, cancel-aware semaphore release, dynamic-flow tests. See `CLAUDE.md` for full rationale.

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

`server.py` (entry, TOOLS dict, dispatcher) → `tools/` (23 MCP tools incl. tasks/panel) → `clink/` (subprocess CLI agents + parsers) → `providers/` (direct API providers, mostly untouched) → `conf/` (model + CLI configs).

## Style

Python 3.9+, line length ~120, type hints required for new code, `from __future__ import annotations`, conventional commits, no comments restating the code.

## What's open

The factory-pattern TOOLS refactor (v1.0 blocker — singleton tools mutate per-call state), sync→async provider refactor (C8), OAuth-to-API fallback (F1), durable task storage, per-call cost meter. See `CLAUDE.md` for the full queue and rationale.

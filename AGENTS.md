# AGENTS.md

**Read [`CLAUDE.md`](./CLAUDE.md) first.** It is the canonical guide for any AI agent working on this codebase. This file exists so tools that look for `AGENTS.md` find a pointer.

## The rules that matter most

- **`server.execute_tool()` is the only dispatch path.** Never call `tool.execute()` directly — file-size + model validation lives in `execute_tool`, and skipping it lets a 50MB file hit the paid API.
- **Editable install only.** Do not use `uvx --from /local/path`; uv caches built wheels and reuses them, breaking iterative dev. Use `uv tool install --editable .`.
- **Never re-add `--dangerously-bypass-approvals-and-sandbox` (codex) or `--yolo` (gemini)** to clink configs — those allow sub-agents to silently mutate the filesystem.
- **Never commit secrets.** API keys live in your MCP client config or `.env`, never in repo.
- **Conventional commits.** `feat:` / `fix:` / `refactor:` / `chore:` / `docs:`.

Everything else — architecture invariants, magic-phrase recognition, env vars, open work queue, coding style — is in `CLAUDE.md`.

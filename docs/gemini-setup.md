# Gemini CLI Setup

> **Note**: While Panel MCP Server connects successfully to Gemini CLI, tool invocation is not working
> correctly yet. We'll update this guide once the integration is fully functional.

This guide explains how to configure Panel MCP Server to work with [Gemini CLI](https://github.com/google-gemini/gemini-cli).

## Prerequisites

- Panel MCP Server installed and configured
- Gemini CLI installed
- At least one API key configured in your `.env` file

## Configuration

1. Edit `~/.gemini/settings.json` and add:

```json
{
  "mcpServers": {
    "panel": {
      "command": "/path/to/panel-mcp-server/panel-mcp-server"
    }
  }
}
```

2. Replace `/path/to/panel-mcp-server` with your actual Panel MCP installation path (the folder name may still be `panel-mcp-server`).

3. If the `panel-mcp-server` wrapper script doesn't exist, create it:

```bash
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
exec .pal_venv/bin/python server.py "$@"
```

Then make it executable: `chmod +x panel-mcp-server`

4. Restart Gemini CLI.

All 15 Panel tools are now available in your Gemini CLI session.

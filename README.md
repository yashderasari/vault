# Vault — MCP File Server

An MCP (Model Context Protocol) server that lets any AI client — Claude Desktop, ChatGPT, Claude Code, etc. — download files, save content, and manage folders on your local machine.

## Features

| Tool | Description |
|---|---|
| `download_file` | Download from any URL to a local folder |
| `save_content` | Save AI-generated text/code/markdown to a **new** file |
| `list_files` | Browse directories with glob patterns |
| `create_directory` | Create new folders |
| `get_file_info` | File metadata, size, hash, MIME type |
| `copy_file` | Copy a file to a new location |
| `move_file` | Move or rename files |
| `read_file` | Read text file contents |
| `get_server_config` | Show current server settings |

## Security & Guardrails

- **Sandboxed** — writes are restricted to allowed root directories (default: `~/Downloads/mcp-files`)
- **Path traversal protection** — all paths are resolved and validated
- **Blocked extensions** — `.exe`, `.bat`, `.ps1`, etc. are blocked by default
- **Size limits** — configurable max download size (default: 500 MB)
- **No in-place edits** — `save_content` refuses to write if the target file already exists; always save under a new filename
- **No silent overwrites on move** — `move_file` refuses if the destination already exists
- **No delete** — `delete_file` has been removed; file deletion must be done manually

## Quick Start

### 1. Install

```bash
# Clone or copy the project
cd mcp-file-server

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

### 2. Run

**stdio mode** (for Claude Desktop, Claude Code):
```bash
uv run python server.py
# or
python server.py
```

**HTTP mode** (for remote/web clients):
```bash
uv run python server.py --transport http --port 8000
# Server will be available at http://127.0.0.1:8000/mcp
```

## Client Setup

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "file-server": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/absolute/path/to/mcp-file-server",
        "python", "server.py"
      ]
    }
  }
}
```

**Config file location:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

### Claude Code

```bash
claude mcp add file-server -- uv run --directory /path/to/mcp-file-server python server.py
```

Or for HTTP transport:
```bash
# Start the server first
uv run python server.py --transport http --port 8000

# Then add in Claude Code
claude mcp add --transport http file-server http://127.0.0.1:8000/mcp
```

### ChatGPT / Other MCP Clients (HTTP mode)

Start the server in HTTP mode and point your client to:
```
http://127.0.0.1:8000/mcp
```

## Configuration

All config is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `MCP_FILE_SERVER_BASE_DIR` | `~/Downloads/mcp-files` | Default save location |
| `MCP_FILE_SERVER_MAX_SIZE_MB` | `500` | Max download size in MB |
| `MCP_FILE_SERVER_ALLOWED_ROOTS` | Same as base dir | Colon-separated list of allowed directories |

### Example: Allow multiple directories

```bash
export MCP_FILE_SERVER_BASE_DIR=~/Downloads/mcp-files
export MCP_FILE_SERVER_ALLOWED_ROOTS="$HOME/Downloads/mcp-files:$HOME/Documents/ai-output:$HOME/Projects"
python server.py
```

## Usage Examples

Once connected, just ask your AI naturally:

> "Download this PDF: https://example.com/report.pdf"

> "Save this Python script as `sort.py` in the `scripts` folder"

> "List all markdown files in my projects folder"

> "What files do I have saved?"

> "Move report.pdf to the archive folder"

## Development

```bash
# Run in dev mode with the MCP Inspector
uv run mcp dev server.py

# Test with Inspector UI
npx -y @modelcontextprotocol/inspector
# Connect to http://localhost:8000/mcp
```

## License

MIT

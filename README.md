# Vault — MCP File Server

An MCP (Model Context Protocol) server that gives any AI client — Claude Desktop, Claude Code, ChatGPT, etc. — direct access to your local filesystem. Save AI-generated content, copy files between folders, download from URLs, and organize your machine using natural language.

## What makes it different

Most MCP file tools require you to know and type exact paths. Vault's `find_folder` tool lets you say *"save this to my distributed systems folder"* and Claude finds the right path itself — no copy-paste required.

---

## Quickstart

### 1. Install

```bash
git clone https://github.com/yashderasari/vault.git
cd vault
pip install .
# or with uv (recommended)
uv sync
```

### 2. Add to your MCP client

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):

```json
{
  "mcpServers": {
    "vault": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/vault", "python", "server.py"]
    }
  }
}
```

**Claude Code:**
```bash
claude mcp add vault -- uv run --directory /path/to/vault python server.py
```

**HTTP mode** (for remote clients):
```bash
uv run python server.py --transport http --host 0.0.0.0 --port 8000
```

### 3. First-run setup

The first time you ask Claude to do anything with files, it'll say:

> *"Where would you like to save your files? You can point me to an existing folder (e.g. ~/Documents) or I can create a fresh ~/Documents/Vault folder just for AI-generated files."*

Answer in plain English. Claude calls `configure` under the hood, saves your choice to `~/.vault-mcp/config.json`, and never asks again.

---

## Tool reference

| Tool | What it does |
|---|---|
| `configure` | First-run setup — sets allowed roots and default save location |
| `find_folder` | Find a folder by name without knowing the exact path |
| `save_content` | Save text, markdown, or code to a file |
| `save_binary` | Save base64-encoded binary content (images, zips) |
| `download_file` | Download any URL to a local folder |
| `copy_file` | Copy a file from anywhere on the machine into your vault |
| `move_file` | Move or rename a file |
| `list_files` | Browse a directory (with optional glob filter) |
| `create_directory` | Create a new folder |
| `read_file` | Read a text file's contents |
| `get_file_info` | File metadata: size, hash, MIME type, timestamps |
| `get_server_config` | Show current configuration |

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `VAULT_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `VAULT_RATE_LIMIT_PER_MINUTE` | `60` | Max tool calls per tool per minute |
| `MCP_FILE_SERVER_MAX_SIZE_MB` | `500` | Max download size in MB |
| `MCP_FILE_SERVER_ALLOWED_ROOTS` | *(from config file)* | Override allowed roots (colon-separated paths) |
| `MCP_FILE_SERVER_BASE_DIR` | *(from config file)* | Override default save location |

Env vars take priority over `~/.vault-mcp/config.json`.

---

## Security model

- **Sandboxed writes** — all file operations are restricted to paths within your configured allowed roots
- **Path traversal protection** — every path is resolved and validated before use; `../` escapes are blocked
- **Blocked extensions** — `.exe`, `.bat`, `.ps1`, `.cmd`, `.msi`, `.js`, and other dangerous types are always rejected
- **No silent overwrites** — `save_content` and `copy_file` refuse to overwrite existing files
- **No delete tool** — file deletion must be done manually; Claude cannot delete files
- **Rate limiting** — configurable per-tool call rate limit prevents runaway loops
- **Unrestricted source reads** — `copy_file` and `move_file` can read from anywhere on the machine; only the destination is sandboxed

---

## Docker

```bash
docker build -t vault .
docker run -p 8000:8000 \
  -e VAULT_RATE_LIMIT_PER_MINUTE=30 \
  -v ~/.vault-mcp:/home/vault/.vault-mcp \
  vault
```

Point your MCP client to `http://localhost:8000/mcp`.

---

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Inspect logs (logs go to stderr)
uv run python server.py 2>vault.log

# MCP Inspector
uv run mcp dev server.py
```

### Trust

This server can be submitted to the [MCP Trust Framework](https://nimblebrain.ai) for an independent security assessment once you're ready to ship publicly.

---

## License

MIT

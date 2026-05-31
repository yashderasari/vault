# Vault — MCP File Server: Build Report

**Author:** Yash Derasari  
**Version:** 0.1.9  
**Published:** [ai-vault-mcp on PyPI](https://pypi.org/project/ai-vault-mcp/)  
**License:** MIT

---

## What We Built

Vault is a local MCP (Model Context Protocol) server that gives any AI client — Claude Desktop, ChatGPT, etc. — direct, sandboxed access to your filesystem. The core insight: most MCP file tools require you to know and type exact paths. Vault's `find_folder` resolves folder names semantically so you never have to.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              AI Client                      │
│         (Claude Desktop / ChatGPT)          │
└────────────────┬────────────────────────────┘
                 │  MCP Protocol
                 │  (stdio or HTTP)
┌────────────────▼────────────────────────────┐
│              vault (server.py)              │
│                                             │
│  ┌──────────────────────────────────────┐   │
│  │           FastMCP Layer              │   │
│  │   Registers tools, handles protocol  │   │
│  └──────────────┬───────────────────────┘   │
│                 │                           │
│  ┌──────────────▼───────────────────────┐   │
│  │          Security Layer              │   │
│  │  • _resolve_path() — ALLOWED_ROOTS   │   │
│  │  • _check_extension() — blocklist    │   │
│  │  • _check_rate_limit() — per tool    │   │
│  │  • SSRF guard — redirect validation  │   │
│  └──────────────┬───────────────────────┘   │
│                 │                           │
│  ┌──────────────▼───────────────────────┐   │
│  │           Tool Layer (12 tools)      │   │
│  │  find_folder   save_content          │   │
│  │  download_file save_binary           │   │
│  │  copy_file     move_file             │   │
│  │  list_files    read_file             │   │
│  │  create_dir    get_file_info         │   │
│  │  configure     get_server_config     │   │
│  └──────────────┬───────────────────────┘   │
│                 │                           │
│  ┌──────────────▼───────────────────────┐   │
│  │         Config Store                 │   │
│  │    ~/.vault-mcp/config.json          │   │
│  │    allowed_roots, base_dir           │   │
│  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
                 │
                 ▼
        Local Filesystem
     (sandboxed to allowed roots)
```

---

## Transport Modes

### stdio (default)
Claude Desktop spawns vault as a child process. Communication happens over stdin/stdout. This is the standard MCP transport for local use.

```
Claude Desktop ──stdin/stdout──► vault process ──► filesystem
```

### HTTP (remote access)
Vault runs as a persistent HTTP server. Any MCP-compatible client can connect over the network.

```
Café MacBook ──HTTP──► Home machine (vault) ──► filesystem
```

Start with:
```bash
uvx ai-vault-mcp --transport http --host 0.0.0.0 --port 8000
```

---

## Setup Flow

```
User runs: uvx ai-vault-mcp setup
                │
                ▼
    Interactive folder selector
    (questionary checkbox — arrow keys)
    Documents / Desktop / Downloads / Home / Custom
                │
                ▼
    Writes ~/.vault-mcp/config.json
    { allowed_roots: [...], base_dir: ... }
                │
                ▼
    Merges vault entry into
    ~/Library/Application Support/Claude/claude_desktop_config.json
                │
                ▼
    "Restart Claude Desktop and you're good to go."
```

---

## Tool Reference

| Tool | Description |
|---|---|
| `find_folder` | Find a folder by name across all allowed roots — no exact path needed |
| `save_content` | Save text, markdown, or code to a file |
| `save_binary` | Save base64-encoded binary content |
| `download_file` | Download any URL to a local folder |
| `copy_file` | Copy a file from anywhere in home into an allowed root |
| `move_file` | Move or rename a file |
| `list_files` | Browse a directory with optional glob filter |
| `read_file` | Read a text file's contents |
| `create_directory` | Create a new folder |
| `get_file_info` | File metadata: size, hash, MIME type, timestamps |
| `configure` | Set allowed roots and base dir at runtime |
| `get_server_config` | Show current configuration |

---

## Security Model

### Sandboxing
Every write operation resolves through `_resolve_path()`, which validates the target is within `ALLOWED_ROOTS`. Path traversal (`../`) is blocked by resolving to absolute paths before checking.

### What was fixed in 0.1.9
| Vulnerability | Fix |
|---|---|
| SSRF via HTTP redirects | Manual redirect loop — validates each hop, blocks internal IPs (169.254.x, 127.x, 10.x, etc.) |
| Double extension bypass (`evil.exe.pdf`) | Check all suffixes, not just the last |
| `configure` accepts `/` as root | Reject any root outside `Path.home()` |
| `copy_file`/`move_file` could read `/etc/passwd` | Source restricted to `Path.home()` |
| Null byte in URL filename | Strip `\x00` from inferred filenames |
| Wrong config path in `setup` | `.ai-vault-mcp` → `.vault-mcp` (matched runtime read path) |

### Remaining guardrails
- No delete tool — Claude cannot delete files
- Blocked extensions: `.exe`, `.bat`, `.ps1`, `.cmd`, `.msi`, `.js`, and more
- No silent overwrites — `save_content` and `copy_file` refuse to overwrite existing files
- Rate limiting — 60 calls/tool/minute by default (configurable)

---

## Key Design Decisions

**Why local-only by default?**  
No cloud dependency, no data leaving the machine, no API keys for the file layer. The user's files stay on their hardware.

**Why `find_folder` instead of requiring paths?**  
The UX insight: users know their folder names but not their full paths. Natural language interfaces shouldn't require path knowledge. `find_folder` does a recursive search across all allowed roots and returns exact paths for Claude to use.

**Why `uvx` instead of `pip install`?**  
Zero permanent install. `uvx ai-vault-mcp setup` runs from PyPI in an isolated environment — nothing is added to the system Python. Subsequent runs use a local cache so it's instant.

**Why no authentication in stdio mode?**  
In stdio mode, vault is spawned by Claude Desktop as a child process on the same machine. The OS-level process isolation is the auth boundary. HTTP mode over the internet requires additional hardening (reverse proxy + TLS + token auth).

---

## vs. Anthropic's Filesystem MCP

| Feature | Anthropic filesystem | Vault |
|---|---|---|
| Find folder by name | No — exact path required | Yes — `find_folder` |
| One-command setup | No — manual JSON editing | Yes — `uvx ai-vault-mcp setup` |
| Download from URL | No | Yes |
| Multi-root support | Yes | Yes |
| Runtime config | No | Yes — `configure` tool |
| Language | Node.js | Python |
| Security hardening | Basic | SSRF guard, extension check, home restriction |

---

## Tech Stack

| Component | Choice |
|---|---|
| MCP framework | `mcp[cli]` (Anthropic's official Python SDK) |
| HTTP client | `httpx` (async, streaming) |
| CLI prompts | `questionary` (arrow-key selector) |
| Transport | stdio (default) / streamable-http |
| Config | JSON at `~/.vault-mcp/config.json` |
| Distribution | PyPI via `uv build` + `uv publish` |

---

## Changelog

| Version | Changes |
|---|---|
| 0.1.0 | Initial release — core tools, stdio transport |
| 0.1.1 | Home directory detection in setup message |
| 0.1.2–3 | `setup` CLI subcommand, TTY detection |
| 0.1.4–5 | Interactive folder selector with `questionary` |
| 0.1.6–8 | Multi-select folders, Downloads option, instruction text fix |
| 0.1.9 | Security hardening — 6 vulnerabilities fixed |

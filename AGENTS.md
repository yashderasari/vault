# AGENTS.md — Vault MCP

Vault gives AI assistants direct read/write access to the user's local filesystem. This document tells you how to use it correctly.

## First-run setup

Always call `get_server_config` before any other tool. If it reports "not configured":

1. Ask the user: *"Where do you keep your files? You can give me a path like `~/Documents`, or I can create a `~/Documents/Vault` folder for you."*
2. Call `configure` with their answer. This writes `~/.vault-mcp/config.json` and hot-reloads the server — **you only do this once**.

If any tool returns a message containing "set up" or "⚠️", it means the server is unconfigured. Run the setup flow above.

## Tool selection guide

| Goal | Right tool |
|---|---|
| Locate a folder by name | `find_folder("job applications")` — never ask the user for a path |
| Save LLM-generated text/markdown/code | `save_content(content, filepath)` |
| Copy a file already on disk | `copy_file(source, dest_dir)` |
| Move or rename a file | `move_file(source, dest)` |
| Download from a URL | `download_file(url, dest_dir)` |
| Save raw binary (image bytes, zip) | `save_binary(base64_content, filename, dest_dir)` |
| Browse a directory | `list_files(path)` |
| Read a text file | `read_file(path)` |
| Create a new folder | `create_directory(path)` |
| Check file metadata | `get_file_info(path)` |

### `find_folder` is your path resolver

Never ask the user "what's the path to your distributed systems folder?" Call `find_folder("distributed systems")` and use the returned path. It normalizes case, spaces, dashes, and underscores.

### `save_binary` is last resort

Binary encoding is token-expensive. For files Claude generates that the user would normally download, prefer: let them download to `~/Downloads`, then use `copy_file` to move it to the right place. Use `save_binary` only when you genuinely have raw bytes in memory with no other path.

## Safety constraints you cannot override

- All write destinations must be inside the user's configured allowed roots. Paths outside those roots are rejected by the server, not by you — don't try to work around this.
- There is **no delete tool**. File deletion must be done manually by the user.
- The server blocks `.exe`, `.bat`, `.ps1`, `.cmd`, `.msi`, `.js`, and other dangerous extensions — don't attempt to save those types.
- `save_content` and `copy_file` refuse to overwrite existing files. If the user wants to replace a file, tell them to delete it first.

## HTTP health check

When running in HTTP mode, `GET /health` returns:

```json
{"status": "ok", "configured": true, "version": "0.1.0"}
```

# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-25

### Added
- **First-run setup flow** — all tools block with a clear setup prompt until `configure` is called; no silent defaults
- `configure` tool — persists allowed roots to `~/.vault-mcp/config.json`, hot-reloads without restart, and returns the full folder tree so Claude knows exactly where to save files
- `find_folder` tool — fuzzy folder search across allowed roots so Claude never needs to ask the user for a file path
- `save_binary` tool — save base64-encoded binary content (images, zips) directly to disk
- Structured JSON logging to stderr (`VAULT_LOG_LEVEL` env var, default `INFO`)
- Per-tool sliding-window rate limiting (`VAULT_RATE_LIMIT_PER_MINUTE` env var, default 60/min)
- Docker support with non-root user and HTTP transport
- Test suite covering path traversal, configure flow, save/read, rate limiting, and unconfigured state

### Changed
- `save_content` — now takes a full absolute `filepath` instead of `filename` + `subfolder`; files land exactly where specified
- `save_binary` — same: full `filepath` instead of `filename` + `subfolder`
- `download_file` — takes `directory` (full absolute path) instead of `subfolder`
- `create_directory` — takes full absolute `path` instead of anchoring to base dir
- `list_files` — defaults to `~/Documents` instead of `~/Downloads/mcp-files`; truncation fixed (was slicing after appending)
- `copy_file` / `move_file` — source can now be any path on the machine, not just within allowed roots
- `_resolve_path` — fixed false-positive where `/Documents` would match `/Documents2`
- Allowed roots and base dir now loaded from `~/.vault-mcp/config.json` on startup; env vars still override

### Removed
- Hardcoded `~/Downloads/mcp-files` default — replaced by first-run setup flow

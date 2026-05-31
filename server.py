"""
MCP File Server — download, save, and manage files on your local machine
from any MCP-compatible AI client (Claude Desktop, ChatGPT, etc.)

Supports both stdio and streamable-http transports.

Usage:
    stdio:              python server.py
    streamable-http:    python server.py --transport http --port 8000
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import mimetypes
import hashlib
import argparse
import base64
from collections import defaultdict, deque
from pathlib import Path
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
import questionary
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

# ─── Configuration ──────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".vault-mcp" / "config.json"

MAX_FILE_SIZE_MB = int(os.environ.get("MCP_FILE_SERVER_MAX_SIZE_MB", "500"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

RATE_LIMIT = int(os.environ.get("VAULT_RATE_LIMIT_PER_MINUTE", "60"))

# Blocked file extensions (security)
BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".com", ".scr", ".pif",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
    ".msi", ".msp", ".ps1", ".psm1",
}


# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("VAULT_LOG_LEVEL", "INFO"),
    stream=sys.stderr,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("vault")

# ─── Rate limiting ───────────────────────────────────────────────────────────

_call_history: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(tool_name: str):
    now = time.time()
    history = _call_history[tool_name]
    while history and history[0] < now - 60:
        history.popleft()
    if len(history) >= RATE_LIMIT:
        raise ValueError(
            f"Rate limit exceeded for '{tool_name}': max {RATE_LIMIT} calls/min. Try again shortly."
        )
    history.append(now)


# ─── Config persistence ──────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load persisted config from ~/.vault-mcp/config.json."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_config(data: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def _init_roots() -> tuple[list[str], str]:
    """
    Resolve ALLOWED_ROOTS and DEFAULT_BASE_DIR from (in priority order):
      1. Environment variables
      2. ~/.vault-mcp/config.json
      3. Unconfigured (empty) — triggers setup flow
    """
    # Env vars take priority
    if os.environ.get("MCP_FILE_SERVER_ALLOWED_ROOTS"):
        roots = [
            os.path.normpath(os.path.expanduser(p))
            for p in os.environ["MCP_FILE_SERVER_ALLOWED_ROOTS"].split(os.pathsep)
        ]
        base = os.path.normpath(os.path.expanduser(
            os.environ.get("MCP_FILE_SERVER_BASE_DIR", roots[0])
        ))
        return roots, base

    cfg = _load_config()
    if cfg.get("allowed_roots"):
        roots = [os.path.normpath(os.path.expanduser(p)) for p in cfg["allowed_roots"]]
        base = os.path.normpath(os.path.expanduser(cfg.get("base_dir", roots[0])))
        return roots, base

    return [], ""  # unconfigured


ALLOWED_ROOTS, DEFAULT_BASE_DIR = _init_roots()

# ─── Helpers ────────────────────────────────────────────────────────────────


_HOME = Path.home()
SETUP_REQUIRED_MSG = (
    "⚠️ Vault isn't set up yet.\n\n"
    f"The user's home directory is: {_HOME}\n\n"
    "Before I can save or manage any files, I need to know where you keep your stuff. "
    "Please ask the user:\n\n"
    f"\"Where would you like to save your files? You can:\n"
    f"  1. Point me to an existing folder (e.g. {_HOME}/Documents or {_HOME}/Desktop)\n"
    f"  2. Create a fresh {_HOME}/Documents/Vault folder just for AI-generated files\"\n\n"
    "Once they answer, call the configure tool with their choice. "
    f"If they say 'Documents', use {_HOME}/Documents. "
    f"If they say 'Desktop', use {_HOME}/Desktop. "
    "Resolve ~ paths to their full absolute form before calling configure."
)


def _ensure_base_dir():
    if DEFAULT_BASE_DIR:
        os.makedirs(DEFAULT_BASE_DIR, exist_ok=True)


def _resolve_path(target: str) -> Path:
    """
    Resolve a user-supplied path, ensuring it stays within ALLOWED_ROOTS.
    Raises ValueError on path-traversal attempts or unconfigured state.
    """
    if not ALLOWED_ROOTS:
        raise ValueError(SETUP_REQUIRED_MSG)

    if not os.path.isabs(target):
        target = os.path.join(DEFAULT_BASE_DIR, target)

    resolved = Path(target).resolve()

    for root in ALLOWED_ROOTS:
        if str(resolved) == root or str(resolved).startswith(root + os.sep):
            return resolved

    raise ValueError(
        f"Path '{resolved}' is outside allowed directories. "
        f"Allowed roots: {ALLOWED_ROOTS}"
    )


def _check_extension(filename: str):
    """Block potentially dangerous file extensions (checks all suffixes)."""
    for ext in [s.lower() for s in Path(filename).suffixes]:
        if ext in BLOCKED_EXTENSIONS:
            raise ValueError(
                f"File extension '{ext}' is blocked for security reasons. "
                f"Blocked: {', '.join(sorted(BLOCKED_EXTENSIONS))}"
            )


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _file_info(path: Path) -> dict:
    """Gather metadata about a file."""
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size": _human_size(stat.st_size),
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        "is_directory": path.is_dir(),
        "extension": path.suffix.lower() if path.is_file() else None,
    }


# ─── MCP Server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Vault",
    instructions=(
        "You are a file management assistant with access to the user's local machine. "
        "CRITICAL: Never ask the user for a file path — use find_folder to locate folders by name instead. "
        "If the user says 'save to my distributed systems folder', call find_folder('distributed systems') "
        "to get the exact path, then save there. "
        "If any tool returns a setup message, ask the user where they keep their files, "
        "then call configure. This only happens once. "
        "IMPORTANT: copy_file and move_file can read from ANYWHERE on the machine — including ~/Downloads, "
        "~/Desktop, or any absolute path — even if it is outside the configured allowed roots. "
        "Only the destination must be inside the allowed roots. Never tell the user you cannot access "
        "a source path like ~/Downloads; just call copy_file or move_file directly."
    ),
)


# ── Tool: download_file ────────────────────────────────────────────────────


@mcp.tool()
async def download_file(
    url: str,
    directory: str,
    filename: Optional[str] = None,
) -> str:
    """
    Download a file from a URL and save it to a local folder.
    Always use a full absolute path for the directory.

    Args:
        url: The URL to download from (http/https).
        directory: Full absolute path of the folder to save into (e.g. "/Users/you/Documents/Papers").
        filename: Optional filename override. If omitted, inferred from the URL.
    """
    try:
        _check_rate_limit("download_file")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"downloading %s"', url)
    _ensure_base_dir()

    # Validate URL
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"❌ Only http/https URLs are supported. Got: {parsed.scheme}"

    # Determine filename
    if not filename:
        url_path = parsed.path.rstrip("/")
        filename = os.path.basename(url_path) or "downloaded_file"
        filename = re.sub(r"[?#].*", "", filename)
        filename = filename.replace("\x00", "")

    _check_extension(filename)

    try:
        target_dir = str(_resolve_path(directory))
    except ValueError as e:
        return f"❌ {e}"
    os.makedirs(target_dir, exist_ok=True)

    target_path = _resolve_path(os.path.join(target_dir, filename))

    # Handle name collisions
    if target_path.exists():
        stem = target_path.stem
        ext = target_path.suffix
        counter = 1
        while target_path.exists():
            target_path = target_path.parent / f"{stem}_{counter}{ext}"
            counter += 1

    # Stream download
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=60) as client:
            # Manually follow redirects, validating each hop
            current_url = url
            for _ in range(10):
                response = await client.send(
                    client.build_request("GET", current_url),
                    stream=True,
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    hop = urlparse(location)
                    if hop.scheme not in ("http", "https"):
                        return f"❌ Redirect to non-http(s) URL blocked: {location}"
                    host = hop.hostname or ""
                    _BLOCKED_HOSTS = ("169.254.", "10.", "192.168.", "172.", "127.", "::1", "localhost")
                    if any(host.startswith(b) or host == b.rstrip(".") for b in _BLOCKED_HOSTS):
                        return f"❌ Redirect to internal/private address blocked: {location}"
                    await response.aclose()
                    current_url = location
                    continue
                break
            else:
                return "❌ Too many redirects."
            async with response:
                response.raise_for_status()

                # Check content-length
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_FILE_SIZE:
                    return (
                        f"❌ File too large ({_human_size(int(content_length))}). "
                        f"Max allowed: {MAX_FILE_SIZE_MB} MB."
                    )

                downloaded = 0
                with open(target_path, "wb") as f:
                    async for chunk in response.aiter_bytes(8192):
                        downloaded += len(chunk)
                        if downloaded > MAX_FILE_SIZE:
                            f.close()
                            target_path.unlink()
                            return f"❌ Download exceeded {MAX_FILE_SIZE_MB} MB limit. Aborted."
                        f.write(chunk)

        # Compute checksum
        sha256 = hashlib.sha256()
        with open(target_path, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                sha256.update(block)

        info = _file_info(target_path)
        return (
            f"✅ Downloaded successfully!\n"
            f"   File: {info['name']}\n"
            f"   Path: {info['path']}\n"
            f"   Size: {info['size']}\n"
            f"   SHA-256: {sha256.hexdigest()[:16]}…\n"
        )

    except httpx.HTTPStatusError as e:
        return f"❌ HTTP error {e.response.status_code}: {e.response.reason_phrase}"
    except httpx.RequestError as e:
        return f"❌ Request failed: {e}"
    except Exception as e:
        return f"❌ Download failed: {e}"


# ── Tool: save_content ─────────────────────────────────────────────────────


@mcp.tool()
async def save_content(
    content: str,
    filepath: str,
    encoding: str = "utf-8",
) -> str:
    """
    Save text/code/markdown content to a local file.
    Always pass a full absolute path so the file lands exactly where you want it.
    Will NOT overwrite an existing file — choose a different filename.

    Args:
        content: The text content to save.
        filepath: Full absolute path for the file (e.g. "/Users/yashderasari/Documents/Jobs/coverletter.md").
        encoding: File encoding (default: utf-8).
    """
    try:
        _check_rate_limit("save_content")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"saving content to %s"', filepath)
    _ensure_base_dir()

    path = Path(filepath).expanduser()
    if not path.is_absolute():
        path = Path(DEFAULT_BASE_DIR) / path

    _check_extension(path.name)

    try:
        target_path = _resolve_path(str(path))
    except ValueError as e:
        return f"❌ {e}"

    os.makedirs(target_path.parent, exist_ok=True)
    target_path = _resolve_path(str(path))

    if target_path.exists():
        return (
            f"❌ File already exists: {target_path}\n"
            f"   Existing files cannot be modified in place. "
            f"Save under a new filename instead."
        )

    try:
        with open(target_path, "w", encoding=encoding) as f:
            f.write(content)

        info = _file_info(target_path)
        return (
            f"✅ Saved successfully!\n"
            f"   File: {info['name']}\n"
            f"   Path: {info['path']}\n"
            f"   Size: {info['size']}\n"
        )
    except Exception as e:
        return f"❌ Failed to save: {e}"


# ── Tool: save_binary ─────────────────────────────────────────────────────


@mcp.tool()
async def save_binary(
    content_base64: str,
    filepath: str,
) -> str:
    """
    Save binary content (e.g. a PDF, image, zip) to a local file.
    Pass the file bytes as a base64-encoded string.
    Always use a full absolute path.

    Args:
        content_base64: Base64-encoded binary content.
        filepath: Full absolute path for the file (e.g. "/Users/you/Documents/report.pdf").
    """
    try:
        _check_rate_limit("save_binary")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"saving binary to %s"', filepath)
    _ensure_base_dir()

    path = Path(filepath).expanduser()
    if not path.is_absolute():
        path = Path(DEFAULT_BASE_DIR) / path

    _check_extension(path.name)

    try:
        target_path = _resolve_path(str(path))
    except ValueError as e:
        return f"❌ {e}"

    os.makedirs(target_path.parent, exist_ok=True)

    if target_path.exists():
        return (
            f"❌ File already exists: {target_path}\n"
            f"   Save under a different filename."
        )

    try:
        data = base64.b64decode(content_base64)
    except Exception as e:
        return f"❌ Invalid base64 content: {e}"

    if len(data) > MAX_FILE_SIZE:
        return f"❌ Content exceeds {MAX_FILE_SIZE_MB} MB limit."

    try:
        with open(target_path, "wb") as f:
            f.write(data)

        info = _file_info(target_path)
        return (
            f"✅ Saved successfully!\n"
            f"   File: {info['name']}\n"
            f"   Path: {info['path']}\n"
            f"   Size: {info['size']}\n"
        )
    except Exception as e:
        return f"❌ Failed to save: {e}"


# ── Tool: list_files ───────────────────────────────────────────────────────


@mcp.tool()
async def list_files(
    directory: Optional[str] = None,
    pattern: Optional[str] = None,
    recursive: bool = False,
) -> str:
    """
    List files and directories at any path within the allowed roots.
    Pass a full absolute path to browse your real Documents/Desktop/Downloads folders.
    To navigate into a subfolder, pass its full absolute path as directory.
    Defaults to ~/Documents if no directory is given.
    Use recursive=True only for small/specific subdirectories — avoid on large roots.

    Args:
        directory: Full absolute path to list (e.g. "/Users/yashderasari/Documents/SCU").
        pattern: Optional glob pattern to filter (e.g. "*.pdf", "*.py").
        recursive: If True, list files recursively. Keep False for large directories.
    """
    try:
        _check_rate_limit("list_files")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"listing %s"', directory or "~/Documents")
    _ensure_base_dir()

    if directory:
        path = Path(directory).expanduser().resolve()
        try:
            target_dir = str(_resolve_path(str(path)))
        except ValueError as e:
            return f"❌ {e}"
    else:
        target_dir = os.path.expanduser("~/Documents")

    target = Path(target_dir)
    if not target.exists():
        return f"📁 Directory does not exist: {target}"

    if pattern:
        if recursive:
            items = sorted(target.rglob(pattern))
        else:
            items = sorted(target.glob(pattern))
    else:
        if recursive:
            items = sorted(target.rglob("*"))
        else:
            items = sorted(target.iterdir())

    if not items:
        return f"📁 Empty directory: {target}"

    MAX_ENTRIES = 200
    all_dirs = [i for i in items if i.is_dir()]
    all_files = [i for i in items if i.is_file()]
    total = len(all_dirs) + len(all_files)
    truncated = total > MAX_ENTRIES

    shown_dirs = all_dirs[:MAX_ENTRIES]
    shown_files = all_files[:max(0, MAX_ENTRIES - len(shown_dirs))]

    lines = [f"📁 Listing: {target}\n"]
    for d in shown_dirs:
        lines.append(f"  📂 {d.relative_to(target)}/")
    for f in shown_files:
        size = _human_size(f.stat().st_size)
        lines.append(f"  📄 {f.relative_to(target)}  ({size})")

    summary = f"\n  {len(shown_dirs)} folder(s), {len(shown_files)} file(s)"
    if truncated:
        summary += f" (truncated — {total} total, narrow down with a subdirectory path)"
    lines.append(summary)
    return "\n".join(lines)


# ── Tool: create_directory ─────────────────────────────────────────────────


@mcp.tool()
async def create_directory(path: str) -> str:
    """
    Create a new directory at the given path.
    Always use a full absolute path.

    Args:
        path: Full absolute path of the directory to create (e.g. "/Users/you/Documents/Projects/new-app").
    """
    try:
        _check_rate_limit("create_directory")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"creating directory %s"', path)
    _ensure_base_dir()

    target_path = Path(path).expanduser()
    if not target_path.is_absolute():
        target_path = Path(DEFAULT_BASE_DIR) / target_path

    try:
        target = _resolve_path(str(target_path))
    except ValueError as e:
        return f"❌ {e}"

    try:
        os.makedirs(target, exist_ok=True)
        return f"✅ Directory created: {target}"
    except Exception as e:
        return f"❌ Failed to create directory: {e}"


# ── Tool: file_info ────────────────────────────────────────────────────────


@mcp.tool()
async def get_file_info(filepath: str) -> str:
    """
    Get detailed information about a specific file.

    Args:
        filepath: Path to the file (relative to base dir, or absolute within allowed roots).
    """
    try:
        _check_rate_limit("get_file_info")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"getting info for %s"', filepath)
    try:
        target = _resolve_path(filepath)
    except ValueError as e:
        return f"❌ {e}"

    if not target.exists():
        return f"❌ File not found: {target}"

    info = _file_info(target)

    if target.is_file():
        sha256 = hashlib.sha256()
        with open(target, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                sha256.update(block)
        info["sha256"] = sha256.hexdigest()
        info["mime_type"] = mimetypes.guess_type(str(target))[0] or "unknown"

    lines = [f"📄 File Info: {info['name']}\n"]
    for key, val in info.items():
        if key != "name":
            lines.append(f"  {key}: {val}")
    return "\n".join(lines)


# ── Tool: move_file ────────────────────────────────────────────────────────


@mcp.tool()
async def move_file(source: str, destination: str) -> str:
    """
    Move or rename a file. Source can be anywhere on the machine.
    Destination must be within an allowed root.

    Args:
        source: Absolute path to the source file (anywhere on this machine).
        destination: Destination path within the managed area (directory or full filepath).
    """
    try:
        _check_rate_limit("move_file")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"moving %s to %s"', source, destination)
    src = Path(source).expanduser().resolve()
    if not src.is_relative_to(Path.home()):
        return f"❌ Source outside home directory is not permitted: {src}"
    try:
        dst = _resolve_path(destination)
    except ValueError as e:
        return f"❌ {e}"

    if not src.exists():
        return f"❌ Source not found: {src}"

    # If destination is a directory, keep the original filename
    if dst.is_dir():
        dst = dst / src.name

    _check_extension(dst.name)

    if dst.exists():
        return (
            f"❌ Destination already exists: {dst}\n"
            f"   Choose a different destination name to avoid overwriting."
        )

    try:
        os.makedirs(dst.parent, exist_ok=True)
        src.rename(dst)
        return f"✅ Moved: {src.name} → {dst}"
    except Exception as e:
        return f"❌ Move failed: {e}"


# ── Tool: copy_file ───────────────────────────────────────────────────────


@mcp.tool()
async def copy_file(source: str, destination: str) -> str:
    """
    Copy any file from anywhere on this machine into the managed file area.
    The source can be any absolute path on the local filesystem.
    The destination must be within an allowed root.
    Will NOT overwrite an existing destination file.

    Args:
        source: Absolute path to the source file (anywhere on this machine).
        destination: Destination path within the managed area (directory or full filepath).
    """
    try:
        _check_rate_limit("copy_file")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"copying %s to %s"', source, destination)
    src = Path(source).expanduser().resolve()
    if not src.is_relative_to(Path.home()):
        return f"❌ Source outside home directory is not permitted: {src}"
    try:
        dst = _resolve_path(destination)
    except ValueError as e:
        return f"❌ {e}"

    if not src.exists():
        return f"❌ Source not found: {src}"
    if not src.is_file():
        return f"❌ Source is not a file: {src}"

    if dst.is_dir():
        dst = dst / src.name

    _check_extension(dst.name)

    if dst.exists():
        return (
            f"❌ Destination already exists: {dst}\n"
            f"   Choose a different destination name to avoid overwriting."
        )

    try:
        os.makedirs(dst.parent, exist_ok=True)
        shutil.copy2(src, dst)
        info = _file_info(dst)
        return (
            f"✅ Copied: {src.name} → {dst}\n"
            f"   Size: {info['size']}\n"
        )
    except Exception as e:
        return f"❌ Copy failed: {e}"


# ── Tool: read_file ────────────────────────────────────────────────────────


@mcp.tool()
async def read_file(
    filepath: str,
    max_lines: Optional[int] = None,
    encoding: str = "utf-8",
) -> str:
    """
    Read the contents of a text file.

    Args:
        filepath: Path to the file.
        max_lines: Optional limit on lines to return (from the start).
        encoding: File encoding (default: utf-8).
    """
    try:
        _check_rate_limit("read_file")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"reading %s"', filepath)
    try:
        target = _resolve_path(filepath)
    except ValueError as e:
        return f"❌ {e}"

    if not target.exists():
        return f"❌ File not found: {target}"
    if not target.is_file():
        return f"❌ Not a file: {target}"

    try:
        with open(target, "r", encoding=encoding) as f:
            if max_lines:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append(f"\n… (truncated at {max_lines} lines)")
                        break
                    lines.append(line)
                content = "".join(lines)
            else:
                content = f.read()

        # Guard against returning huge payloads
        if len(content) > 100_000:
            content = content[:100_000] + "\n\n… (truncated at 100KB)"

        return f"📄 {target.name}:\n\n{content}"
    except UnicodeDecodeError:
        return f"❌ Cannot read as text (binary file?). Try a different encoding."
    except Exception as e:
        return f"❌ Read failed: {e}"


# ── Tool: configure ───────────────────────────────────────────────────────


@mcp.tool()
async def configure(
    allowed_roots: list[str],
    base_dir: Optional[str] = None,
) -> str:
    """
    Set up the vault for this user. Call this once after asking the user where
    they keep their files. Persists to ~/.vault-mcp/config.json and takes
    effect immediately — no restart needed.

    Args:
        allowed_roots: List of absolute folder paths the vault can read/write
                       (e.g. ["/Users/alice/Documents"]).
        base_dir: Default folder for saving new files. Defaults to allowed_roots[0].
    """
    logger.info('"configuring vault with roots %s"', allowed_roots)
    global ALLOWED_ROOTS, DEFAULT_BASE_DIR

    resolved_roots = []
    for p in allowed_roots:
        path = Path(p).expanduser().resolve()
        if not path.is_relative_to(Path.home()):
            return f"❌ Refusing root outside home directory: {path}"
        path.mkdir(parents=True, exist_ok=True)
        resolved_roots.append(str(path))

    resolved_base = str(
        Path(base_dir).expanduser().resolve() if base_dir else Path(resolved_roots[0])
    )
    Path(resolved_base).mkdir(parents=True, exist_ok=True)

    _save_config({"allowed_roots": resolved_roots, "base_dir": resolved_base})

    ALLOWED_ROOTS = resolved_roots
    DEFAULT_BASE_DIR = resolved_base

    # Show top-level structure so Claude knows existing folders immediately
    lines = [
        f"✅ Vault configured!",
        f"   Allowed roots: {', '.join(resolved_roots)}",
        f"   Default save location: {resolved_base}",
        f"   Config saved to: {CONFIG_PATH}",
        f"",
        f"📁 Your folder structure:",
    ]
    for root in resolved_roots:
        root_path = Path(root)
        try:
            entries = sorted(root_path.iterdir())
            dirs = [e for e in entries if e.is_dir()]
            for d in dirs[:30]:
                lines.append(f"  📂 {d.relative_to(root_path)}/")
                # One level deeper
                try:
                    subdirs = sorted([s for s in d.iterdir() if s.is_dir()])
                    for s in subdirs[:10]:
                        lines.append(f"      📂 {s.relative_to(root_path)}/")
                except PermissionError:
                    pass
        except PermissionError:
            lines.append(f"  ⚠️ Permission denied: {root}")

    lines.append(f"\nUse these exact paths when saving files.")
    return "\n".join(lines)


# ── Tool: find_folder ─────────────────────────────────────────────────────


@mcp.tool()
async def find_folder(name: str) -> str:
    """
    Find a folder by name (or partial name) across all allowed roots.
    Use this instead of asking the user for a path — always call this first
    when the user mentions a folder by name (e.g. 'distributed systems', 'job applications').

    Args:
        name: Folder name or partial name to search for (case-insensitive).
    """
    try:
        _check_rate_limit("find_folder")
    except ValueError as e:
        return f"❌ {e}"
    logger.info('"searching for folder %s"', name)
    if not ALLOWED_ROOTS:
        return SETUP_REQUIRED_MSG

    needle = name.lower().replace(" ", "").replace("-", "").replace("_", "")
    matches = []

    for root in ALLOWED_ROOTS:
        for dirpath in Path(root).rglob("*"):
            if dirpath.is_dir():
                normalized = dirpath.name.lower().replace(" ", "").replace("-", "").replace("_", "")
                if needle in normalized:
                    matches.append(str(dirpath))

    if not matches:
        return (
            f"No folder matching '{name}' found in: {', '.join(ALLOWED_ROOTS)}\n"
            f"You can create one with create_directory, or ask the user to confirm the folder name."
        )

    lines = [f"Found {len(matches)} match(es) for '{name}':"]
    for m in matches[:10]:
        lines.append(f"  {m}")
    if len(matches) > 10:
        lines.append(f"  … and {len(matches) - 10} more")
    lines.append("\nUse the exact path above when saving files.")
    return "\n".join(lines)


# ── Tool: get_server_config ────────────────────────────────────────────────


@mcp.tool()
async def get_server_config() -> str:
    """
    Show the current vault configuration. Always call this first to check
    if the vault is configured before using any other tool.
    """
    if not ALLOWED_ROOTS:
        return SETUP_REQUIRED_MSG
    return (
        f"⚙️ Vault Config\n"
        f"  Default save location: {DEFAULT_BASE_DIR}\n"
        f"  Allowed roots:         {', '.join(ALLOWED_ROOTS)}\n"
        f"  Max file size:         {MAX_FILE_SIZE_MB} MB\n"
        f"  Config file:           {CONFIG_PATH}\n"
    )


# ─── Health check ───────────────────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({
        "status": "ok",
        "configured": bool(ALLOWED_ROOTS),
        "version": "0.1.0",
    })


# ─── Entrypoint ─────────────────────────────────────────────────────────────


def _claude_config_path() -> Path:
    """Return the Claude Desktop config file path for the current OS."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _cmd_setup():
    """Ask for a folder, write vault config, and register in Claude Desktop."""
    home = Path.home()

    named_options = {
        f"Documents  ({home}/Documents)":  home / "Documents",
        f"Desktop    ({home}/Desktop)":    home / "Desktop",
        f"Downloads  ({home}/Downloads)":  home / "Downloads",
        f"Home       ({home})":            home,
    }
    CUSTOM = "Custom path…"

    selected = questionary.checkbox(
        "Where should vault be allowed to save and manage files?",
        choices=[
            f"Documents  ({home}/Documents)",
            f"Desktop    ({home}/Desktop)",
            f"Downloads  ({home}/Downloads)",
            f"Home       ({home})",
            CUSTOM,
        ],
        instruction="(Use arrow keys to move, <space> to select, Enter to confirm)",
        style=questionary.Style([
            ("highlighted", "fg:green bold"),
            ("selected", "fg:green"),
            ("pointer", "fg:green bold"),
        ]),
    ).ask()

    if selected is None:
        print("Setup cancelled.")
        sys.exit(0)

    folders = []
    for item in selected:
        if item == CUSTOM:
            raw = questionary.text("Enter custom path:").ask() or ""
            folders.append(Path(raw).expanduser() if raw else home / "Documents")
        else:
            folders.append(named_options[item])

    if not folders:
        print("No folders selected, defaulting to Documents.")
        folders = [home / "Documents"]

    resolved = [str(f.resolve()) for f in folders]
    for r in resolved:
        Path(r).mkdir(parents=True, exist_ok=True)

    vault_config_dir = home / ".vault-mcp"
    vault_config_dir.mkdir(exist_ok=True)
    (vault_config_dir / "config.json").write_text(
        json.dumps({"allowed_roots": resolved, "base_dir": resolved[0]}, indent=2),
        encoding="utf-8",
    )
    print(f"\n✅ Vault folders set to: {', '.join(resolved)}")

    # Register in Claude Desktop config
    config_path = _claude_config_path()

    if not config_path.parent.exists():
        print(f"\nClaude Desktop config directory not found: {config_path.parent}")
        print("Make sure Claude Desktop is installed, then re-run 'uvx ai-vault-mcp setup'.")
        sys.exit(1)

    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"Could not parse {config_path} — please fix the JSON and re-run.")
            sys.exit(1)

    servers = config.setdefault("mcpServers", {})

    if "vault" in servers:
        print("✅ vault already registered in Claude Desktop config")
    else:
        servers["vault"] = {"command": "uvx", "args": ["ai-vault-mcp"]}
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print("✅ vault added to Claude Desktop config")

    print("\nRestart Claude Desktop and you're good to go.")


def main():
    parser = argparse.ArgumentParser(description="MCP File Server (ai-vault-mcp)")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("setup", help="Register vault in Claude Desktop's MCP config")

    serve_parser = subparsers.add_parser("serve", help="Start the MCP server (default)")
    serve_parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port for HTTP transport (default: PORT env var or 8000)",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for HTTP transport (default: 127.0.0.1)",
    )

    # Keep top-level --transport/--port/--host for backwards compatibility
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default="127.0.0.1")

    args = parser.parse_args()

    if args.command == "setup":
        _cmd_setup()
        return

    _ensure_base_dir()

    if args.transport == "stdio" and sys.stdin.isatty():
        print("vault MCP server")
        print()
        print("This process is meant to be launched by an MCP client (e.g. Claude Desktop),")
        print("not run directly. To register vault with Claude Desktop, run:")
        print()
        print("  uvx ai-vault-mcp setup")
        print()
        print("To start the server over HTTP instead:")
        print()
        print("  uvx ai-vault-mcp --transport http")
        sys.exit(0)

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

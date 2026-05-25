"""
MCP File Server — download, save, and manage files on your local machine
from any MCP-compatible AI client (Claude Desktop, ChatGPT, etc.)

Supports both stdio and streamable-http transports.

Usage:
    stdio:              python server.py
    streamable-http:    python server.py --transport http --port 8000
"""

import os
import sys
import re
import json
import shutil
import mimetypes
import hashlib
import asyncio
import argparse
import base64
from pathlib import Path
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

# ─── Configuration ──────────────────────────────────────────────────────────

DEFAULT_BASE_DIR = os.environ.get(
    "MCP_FILE_SERVER_BASE_DIR",
    os.path.expanduser("~/Downloads/mcp-files"),
)

MAX_FILE_SIZE_MB = int(os.environ.get("MCP_FILE_SERVER_MAX_SIZE_MB", "500"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024  # bytes


# Directories the server is allowed to write into (sandboxing)
ALLOWED_ROOTS: list[str] = [
    os.path.normpath(os.path.expanduser(p))
    for p in os.environ.get(
        "MCP_FILE_SERVER_ALLOWED_ROOTS",
        DEFAULT_BASE_DIR,
    ).split(os.pathsep)
]

# Blocked file extensions (security)
BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".com", ".scr", ".pif",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
    ".msi", ".msp", ".ps1", ".psm1",
}

# ─── Helpers ────────────────────────────────────────────────────────────────


def _ensure_base_dir():
    """Create the default base directory if it doesn't exist."""
    os.makedirs(DEFAULT_BASE_DIR, exist_ok=True)


def _resolve_path(target: str) -> Path:
    """
    Resolve a user-supplied path, ensuring it stays within ALLOWED_ROOTS.
    Raises ValueError on path-traversal attempts.
    """
    # If relative, anchor to DEFAULT_BASE_DIR
    if not os.path.isabs(target):
        target = os.path.join(DEFAULT_BASE_DIR, target)

    resolved = Path(target).resolve()

    for root in ALLOWED_ROOTS:
        if str(resolved).startswith(root):
            return resolved

    raise ValueError(
        f"Path '{resolved}' is outside allowed directories. "
        f"Allowed roots: {ALLOWED_ROOTS}"
    )


def _check_extension(filename: str):
    """Block potentially dangerous file extensions."""
    ext = Path(filename).suffix.lower()
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
        "Download files from URLs, save AI-generated content, "
        "and manage files/folders on your local machine."
    ),
)


# ── Tool: download_file ────────────────────────────────────────────────────


@mcp.tool()
async def download_file(
    url: str,
    filename: Optional[str] = None,
    subfolder: Optional[str] = None,
) -> str:
    """
    Download a file from a URL and save it to a local folder.

    Args:
        url: The URL to download from (http/https).
        filename: Optional filename override. If omitted, inferred from the URL.
        subfolder: Optional subfolder inside the base directory (e.g. "projects/web").
    """
    _ensure_base_dir()

    # Validate URL
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"❌ Only http/https URLs are supported. Got: {parsed.scheme}"

    # Determine filename
    if not filename:
        url_path = parsed.path.rstrip("/")
        filename = os.path.basename(url_path) or "downloaded_file"
        # Strip query params from filename
        filename = re.sub(r"[?#].*", "", filename)

    _check_extension(filename)

    # Build target path
    target_dir = DEFAULT_BASE_DIR
    if subfolder:
        target_dir = str(_resolve_path(subfolder))
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
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            async with client.stream("GET", url) as response:
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
    filename: str,
    subfolder: Optional[str] = None,
) -> str:
    """
    Save binary content (e.g. a PDF, image, zip) to a local file.
    Pass the file bytes as a base64-encoded string.

    Args:
        content_base64: Base64-encoded binary content.
        filename: Name for the file (e.g. "report.pdf", "photo.png").
        subfolder: Optional subfolder inside the base directory.
    """
    _ensure_base_dir()
    _check_extension(filename)

    target_dir = DEFAULT_BASE_DIR
    if subfolder:
        target_dir = str(_resolve_path(subfolder))
    os.makedirs(target_dir, exist_ok=True)

    target_path = _resolve_path(os.path.join(target_dir, filename))

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

    lines = [f"📁 Listing: {target}\n"]
    dirs = [i for i in items if i.is_dir()]
    files = [i for i in items if i.is_file()]

    for d in dirs:
        rel = d.relative_to(target)
        lines.append(f"  📂 {rel}/")

    for f in files:
        rel = f.relative_to(target)
        size = _human_size(f.stat().st_size)
        lines.append(f"  📄 {rel}  ({size})")

    MAX_ENTRIES = 200
    total = len(dirs) + len(files)
    dirs = dirs[:MAX_ENTRIES]
    files = files[:max(0, MAX_ENTRIES - len(dirs))]
    truncated = total > MAX_ENTRIES

    lines.append(f"\n  {len(dirs)} folder(s), {len(files)} file(s)" + (f" (truncated — {total} total, use a subdirectory path to narrow down)" if truncated else ""))
    return "\n".join(lines)


# ── Tool: create_directory ─────────────────────────────────────────────────


@mcp.tool()
async def create_directory(
    name: str,
    subfolder: Optional[str] = None,
) -> str:
    """
    Create a new directory inside the managed file area.

    Args:
        name: Name of the directory to create (e.g. "projects/new-app").
        subfolder: Optional parent subfolder.
    """
    _ensure_base_dir()

    base = DEFAULT_BASE_DIR
    if subfolder:
        base = str(_resolve_path(subfolder))

    target = _resolve_path(os.path.join(base, name))

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
    Move or rename a file within the managed file area.

    Args:
        source: Current path of the file.
        destination: New path (can be a directory or full filename).
    """
    try:
        src = _resolve_path(source)
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
    src = Path(source).expanduser().resolve()
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


# ── Tool: get_server_config ────────────────────────────────────────────────


@mcp.tool()
async def get_server_config() -> str:
    """
    Show the current server configuration (base directory, limits, etc.).
    """
    return (
        f"⚙️ MCP File Server Config\n"
        f"  Base directory:   {DEFAULT_BASE_DIR}\n"
        f"  Allowed roots:    {', '.join(ALLOWED_ROOTS)}\n"
        f"  Max file size:    {MAX_FILE_SIZE_MB} MB\n"
        f"  Blocked exts:     {', '.join(sorted(BLOCKED_EXTENSIONS))}\n"
    )


# ─── Entrypoint ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="MCP File Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP transport (default: 8000)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for HTTP transport (default: 127.0.0.1)",
    )
    args = parser.parse_args()

    _ensure_base_dir()

    if args.transport == "http":
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
        )
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

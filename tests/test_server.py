"""
Vault MCP Server — test suite
Run: uv run pytest tests/ -v
"""

import os
import json
import pytest
import importlib
from pathlib import Path


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_server(tmp_path, monkeypatch):
    """
    Reset server module state for each test:
    - Point CONFIG_PATH to a temp dir
    - Clear ALLOWED_ROOTS and DEFAULT_BASE_DIR
    - Clear rate limit history
    """
    import server

    tmp_config = tmp_path / ".vault-mcp" / "config.json"
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_config)
    monkeypatch.setattr(server, "ALLOWED_ROOTS", [])
    monkeypatch.setattr(server, "DEFAULT_BASE_DIR", "")
    server._call_history.clear()

    yield server


@pytest.fixture
def configured_server(isolated_server, tmp_path):
    """Server with a configured root pointing to tmp_path."""
    root = str(tmp_path)
    isolated_server.ALLOWED_ROOTS = [root]
    isolated_server.DEFAULT_BASE_DIR = root
    return isolated_server, tmp_path


# ─── Path resolution ─────────────────────────────────────────────────────────


def test_resolve_path_inside_root(configured_server):
    server, tmp_path = configured_server
    result = server._resolve_path(str(tmp_path / "notes.md"))
    assert result == tmp_path / "notes.md"


def test_resolve_path_rejects_traversal(configured_server):
    server, tmp_path = configured_server
    with pytest.raises(ValueError, match="outside allowed"):
        server._resolve_path(str(tmp_path / ".." / "etc" / "passwd"))


def test_resolve_path_no_false_positive(tmp_path, isolated_server):
    """'/Documents' must not match '/Documents2'."""
    docs = tmp_path / "Documents"
    docs.mkdir()
    docs2 = tmp_path / "Documents2"
    docs2.mkdir()

    isolated_server.ALLOWED_ROOTS = [str(docs)]
    isolated_server.DEFAULT_BASE_DIR = str(docs)

    with pytest.raises(ValueError, match="outside allowed"):
        isolated_server._resolve_path(str(docs2 / "file.txt"))


def test_resolve_path_unconfigured(isolated_server):
    with pytest.raises(ValueError, match="set up"):
        isolated_server._resolve_path("/tmp/anything")


# ─── Unconfigured state blocks all tools ─────────────────────────────────────


@pytest.mark.anyio
async def test_unconfigured_blocks_save_content(isolated_server):
    result = await isolated_server.save_content("hello", "/tmp/test.md")
    assert "set up" in result.lower() or "⚠️" in result


@pytest.mark.anyio
async def test_unconfigured_blocks_list_files(isolated_server):
    result = await isolated_server.list_files("/tmp")
    assert "set up" in result.lower() or "⚠️" in result


@pytest.mark.anyio
async def test_unconfigured_blocks_find_folder(isolated_server):
    result = await isolated_server.find_folder("documents")
    assert "set up" in result.lower() or "⚠️" in result


# ─── Configure ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_configure_writes_config(isolated_server, tmp_path):
    root = str(tmp_path / "vault")
    result = await isolated_server.configure([root])

    assert "✅" in result
    assert isolated_server.ALLOWED_ROOTS == [str(Path(root).resolve())]

    config_data = json.loads(isolated_server.CONFIG_PATH.read_text())
    assert str(Path(root).resolve()) in config_data["allowed_roots"]


@pytest.mark.anyio
async def test_configure_hot_reloads(isolated_server, tmp_path):
    root = str(tmp_path)
    await isolated_server.configure([root])

    assert isolated_server.ALLOWED_ROOTS == [str(Path(root).resolve())]
    assert isolated_server.DEFAULT_BASE_DIR == str(Path(root).resolve())


@pytest.mark.anyio
async def test_configure_shows_folder_tree(isolated_server, tmp_path):
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Notes").mkdir()

    result = await isolated_server.configure([str(tmp_path)])
    assert "Projects" in result
    assert "Notes" in result


# ─── save_content ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_save_content_writes_file(configured_server):
    server, tmp_path = configured_server
    target = str(tmp_path / "hello.md")
    result = await server.save_content("# Hello", target)

    assert "✅" in result
    assert Path(target).read_text() == "# Hello"


@pytest.mark.anyio
async def test_save_content_refuses_overwrite(configured_server):
    server, tmp_path = configured_server
    target = str(tmp_path / "exists.md")
    Path(target).write_text("original")

    result = await server.save_content("new content", target)
    assert "❌" in result
    assert Path(target).read_text() == "original"


@pytest.mark.anyio
async def test_save_content_blocks_dangerous_extension(configured_server):
    server, tmp_path = configured_server
    target = str(tmp_path / "malware.exe")

    with pytest.raises(ValueError, match="blocked"):
        server._check_extension("malware.exe")


@pytest.mark.anyio
async def test_save_content_creates_parent_dirs(configured_server):
    server, tmp_path = configured_server
    target = str(tmp_path / "a" / "b" / "c" / "note.md")
    result = await server.save_content("deep", target)

    assert "✅" in result
    assert Path(target).exists()


# ─── find_folder ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_find_folder_exact_match(configured_server):
    server, tmp_path = configured_server
    (tmp_path / "Job Applications").mkdir()

    result = await server.find_folder("job applications")
    assert "Job Applications" in result


@pytest.mark.anyio
async def test_find_folder_normalizes_separators(configured_server):
    server, tmp_path = configured_server
    (tmp_path / "distributed-systems").mkdir()

    result = await server.find_folder("distributed systems")
    assert "distributed-systems" in result


@pytest.mark.anyio
async def test_find_folder_not_found(configured_server):
    server, tmp_path = configured_server
    result = await server.find_folder("nonexistent-xyz-folder")
    assert "No folder" in result or "not found" in result.lower()


# ─── list_files ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_files_shows_contents(configured_server):
    server, tmp_path = configured_server
    (tmp_path / "notes.md").write_text("hi")
    (tmp_path / "subdir").mkdir()

    result = await server.list_files(str(tmp_path))
    assert "notes.md" in result
    assert "subdir" in result


@pytest.mark.anyio
async def test_list_files_truncation(configured_server):
    server, tmp_path = configured_server
    for i in range(250):
        (tmp_path / f"file_{i:03}.txt").write_text("")

    result = await server.list_files(str(tmp_path))
    assert "truncated" in result


# ─── download_file ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_download_file_rejects_non_http(configured_server):
    server, tmp_path = configured_server
    result = await server.download_file("ftp://evil.com/file.txt", str(tmp_path))
    assert "❌" in result
    assert "http" in result.lower()


# ─── Rate limiting ────────────────────────────────────────────────────────────


def test_rate_limit_blocks_after_threshold(configured_server, monkeypatch):
    server, _ = configured_server
    monkeypatch.setattr(server, "RATE_LIMIT", 3)

    server._check_rate_limit("test_tool")
    server._check_rate_limit("test_tool")
    server._check_rate_limit("test_tool")

    with pytest.raises(ValueError, match="Rate limit"):
        server._check_rate_limit("test_tool")


def test_rate_limit_per_tool_independent(configured_server, monkeypatch):
    server, _ = configured_server
    monkeypatch.setattr(server, "RATE_LIMIT", 2)

    server._check_rate_limit("tool_a")
    server._check_rate_limit("tool_a")
    # tool_b should still work
    server._check_rate_limit("tool_b")

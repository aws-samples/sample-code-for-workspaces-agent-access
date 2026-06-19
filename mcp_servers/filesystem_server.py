# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Filesystem MCP server — Python equivalent of @modelcontextprotocol/server-filesystem."""
import os
import sys

if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.FreeConsole()

from fastmcp import FastMCP

mcp = FastMCP("filesystem")
ALLOWED_DIRS = ["C:\\Users\\Public\\Documents"]


def _validate(path):
    full = os.path.normpath(os.path.abspath(path))
    if not any(full.startswith(os.path.normpath(d)) for d in ALLOWED_DIRS):
        raise ValueError(f"Path outside allowed directories: {path}")
    return full


@mcp.tool
def read_file(path: str) -> str:
    """Read complete contents of a file."""
    with open(_validate(path), "r", encoding="utf-8") as f:
        return f.read()


@mcp.tool
def write_file(path: str, content: str) -> str:
    """Write content to a file (creates parent dirs if needed)."""
    full = _validate(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Written {len(content)} bytes to {path}"


@mcp.tool
def list_directory(path: str = "C:\\Users\\Public\\Documents") -> list:
    """List directory contents with [FILE] or [DIR] prefix."""
    full = _validate(path)
    entries = []
    for entry in os.scandir(full):
        prefix = "[DIR]" if entry.is_dir() else "[FILE]"
        entries.append(f"{prefix} {entry.name}")
    return entries


@mcp.tool
def create_directory(path: str) -> str:
    """Create a directory (and parents)."""
    full = _validate(path)
    os.makedirs(full, exist_ok=True)
    return f"Created {path}"


@mcp.tool
def move_file(source: str, destination: str) -> str:
    """Move or rename a file."""
    src = _validate(source)
    dst = _validate(destination)
    os.rename(src, dst)
    return f"Moved {source} -> {destination}"


@mcp.tool
def get_file_info(path: str) -> dict:
    """Get file metadata (size, modified time, type)."""
    full = _validate(path)
    stat = os.stat(full)
    return {
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "is_file": os.path.isfile(full),
        "is_dir": os.path.isdir(full),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")

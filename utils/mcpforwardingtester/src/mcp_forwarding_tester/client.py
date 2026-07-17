# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""stdio MCP client that mimics how the service connects to servers.

Spawns a stdio child, uses a default MCP client (no roots/sampling/elicitation),
inherits the full environment, and lists tools with pagination. Main divergence from
the service: the negotiated protocol version.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class ServerProbe:
    """Everything we learned by connecting to one server, for the checks to judge."""

    # Connection outcome.
    spawned: bool = False
    initialized: bool = False
    connect_error: str | None = None

    # Handshake results.
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    server_capabilities: list[str] = field(default_factory=list)
    instructions: str | None = None

    # Tool discovery (names as the server reports them, before namespacing).
    tools: list["ToolInfo"] = field(default_factory=list)
    list_error: str | None = None

    # Timings, seconds.
    init_seconds: float | None = None
    list_seconds: float | None = None

    # Active tool-call results, keyed by tool name.
    calls: dict[str, "CallOutcome"] = field(default_factory=dict)

    # stderr captured during the session (the service inherits stderr; we surface it).
    stderr_tail: str | None = None


@dataclass
class ToolInfo:
    name: str
    description: str | None
    input_schema: dict | None


@dataclass
class CallOutcome:
    ok: bool
    is_error: bool  # the MCP ``isError`` result flag
    seconds: float | None
    error: str | None
    content_summary: str | None


# The service inherits the full parent environment. The Python SDK default
# scrubs it to a subset, so we pass the full environment to match.
def _full_env() -> dict[str, str]:
    return dict(os.environ)


_STDERR_TAIL_LIMIT = 2000


@asynccontextmanager
async def connect(command: str, args: list[str]):
    """Open a session the way the service would. Yields (ServerProbe, ClientSession|None).

    The probe is always yielded so callers can inspect partial results on failure.
    The child's stderr is captured into ``probe.stderr_tail``.
    """
    probe = ServerProbe()

    # command + args only (the config has no env/cwd). Pass the full environment so
    # PATH-resolved commands behave as they do on the instance.
    params = StdioServerParameters(command=command, args=args, env=_full_env())

    # stdio_client writes to this fd directly, so it must be a real file (needs fileno()).
    errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                probe.spawned = True
                # No callbacks => no client capabilities advertised, matching the service.
                async with ClientSession(read, write) as session:
                    yield probe, session
        except Exception as exc:  # noqa: BLE001
            if not probe.spawned:
                probe.connect_error = f"failed to spawn '{command}': {exc}"
            elif not probe.initialized:
                probe.connect_error = f"session error before/at initialize: {exc}"
            else:
                probe.connect_error = f"session error: {exc}"
            yield probe, None
    finally:
        probe.stderr_tail = _read_tail(errlog)
        errlog.close()


def _read_tail(fileobj) -> str | None:
    try:
        fileobj.flush()
        fileobj.seek(0)
        text = fileobj.read().strip()
    except (OSError, ValueError):
        return None
    if not text:
        return None
    return text if len(text) <= _STDERR_TAIL_LIMIT else "..." + text[-_STDERR_TAIL_LIMIT:]


async def initialize(probe: ServerProbe, session: ClientSession) -> None:
    """Run the handshake and record the negotiated protocol + server info."""
    start = time.monotonic()
    result = await session.initialize()
    probe.init_seconds = time.monotonic() - start
    probe.initialized = True

    probe.protocol_version = getattr(result, "protocolVersion", None)
    info = getattr(result, "serverInfo", None)
    if info is not None:
        probe.server_name = getattr(info, "name", None)
        probe.server_version = getattr(info, "version", None)
    probe.instructions = getattr(result, "instructions", None)

    caps = getattr(result, "capabilities", None)
    if caps is not None:
        for field_name in ("tools", "resources", "prompts", "logging", "completions"):
            if getattr(caps, field_name, None) is not None:
                probe.server_capabilities.append(field_name)


async def list_tools_paginated(probe: ServerProbe, session: ClientSession) -> None:
    """List all tools, following ``nextCursor``, as the service does."""
    start = time.monotonic()
    try:
        cursor: str | None = None
        collected: list[ToolInfo] = []
        seen_cursors: set[str] = set()
        while True:
            result = await session.list_tools(cursor=cursor)
            for tool in result.tools:
                collected.append(
                    ToolInfo(
                        name=tool.name,
                        description=getattr(tool, "description", None),
                        input_schema=getattr(tool, "inputSchema", None),
                    )
                )
            cursor = getattr(result, "nextCursor", None)
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        probe.tools = collected
    except Exception as exc:  # noqa: BLE001
        probe.list_error = str(exc)
    finally:
        probe.list_seconds = time.monotonic() - start


def _summarize_content(result) -> str:
    """Produce a short human summary of a CallToolResult's content blocks."""
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        btype = getattr(block, "type", "?")
        if btype == "text":
            text = getattr(block, "text", "") or ""
            parts.append(text if len(text) <= 200 else text[:197] + "...")
        else:
            parts.append(f"<{btype}>")
    structured = getattr(result, "structuredContent", None)
    if structured is not None and not parts:
        return f"<structured: {structured!r}>"
    return " | ".join(parts) if parts else "<no content>"


async def call_tool(
    probe: ServerProbe,
    session: ClientSession,
    name: str,
    arguments: dict | None,
) -> CallOutcome:
    """Invoke a tool and record the outcome under ``probe.calls[name]``."""
    start = time.monotonic()
    try:
        result = await session.call_tool(name, arguments=arguments or {})
        seconds = time.monotonic() - start
        is_error = bool(getattr(result, "isError", False))
        outcome = CallOutcome(
            ok=True,
            is_error=is_error,
            seconds=seconds,
            error=None,
            content_summary=_summarize_content(result),
        )
    except Exception as exc:  # noqa: BLE001
        outcome = CallOutcome(
            ok=False,
            is_error=True,
            seconds=time.monotonic() - start,
            error=str(exc),
            content_summary=None,
        )
    probe.calls[name] = outcome
    return outcome

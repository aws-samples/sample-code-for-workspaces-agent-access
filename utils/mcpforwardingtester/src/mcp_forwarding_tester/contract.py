# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP tool forwarding contract constants: the requirements the service enforces."""

from __future__ import annotations

import sys

# --- Timeouts (the service's manifest fetch and tool calls) --------------
# The service wraps tools/list and each tool call in this timeout; past it
# the call is cancelled and the client gets no tools. It does NOT bound spawn +
# initialize.
CALL_TIMEOUT_S: float = 5.0
# Whole-environment teardown budget.
TEARDOWN_TIMEOUT_S: float = 15.0
# Per-server graceful close on teardown.
GRACEFUL_SHUTDOWN_S: float = 1.0
# Advisory threshold for a slow spawn + initialize. The service does not bound
# this, but a slow handshake makes the cold tools/list more likely to exceed
# CALL_TIMEOUT_S. WARN only, never FAIL.
SLOW_INITIALIZE_WARN_S: float = 5.0

# --- Tool namespacing (the service) --------------------------------------
# Every tool is re-exposed under this name. The service does no name validation,
# but a tool name containing "___" makes the forwarded name ambiguous.
FORWARD_SEPARATOR: str = "___"
FORWARD_PREFIX: str = "forwarded"


def forwarded_tool_name(server: str, tool: str) -> str:
    """Reproduce how the service names a forwarded tool."""
    return f"{FORWARD_PREFIX}{FORWARD_SEPARATOR}{server}{FORWARD_SEPARATOR}{tool}"


# --- Config schema (the service's manifest parser) -----------------------
# A server entry honors ONLY these fields; anything else (notably ``env`` and
# ``cwd``) is silently ignored.
CONFIG_TOP_LEVEL_KEY: str = "mcpServers"
SERVER_REQUIRED_KEYS: tuple[str, ...] = ("command",)
SERVER_OPTIONAL_KEYS: tuple[str, ...] = ("args",)
SERVER_KNOWN_KEYS: frozenset[str] = frozenset(SERVER_REQUIRED_KEYS + SERVER_OPTIONAL_KEYS)
CONFIG_FILE_NAME: str = "mcp_server_redirection_config.json"


def default_config_path() -> str:
    """Return the default config path the service reads for the current OS."""
    if sys.platform == "win32":
        return rf"C:\ProgramData\NICE\dcv\{CONFIG_FILE_NAME}"
    return f"/etc/dcv/{CONFIG_FILE_NAME}"

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Load and statically validate an MCP tool forwarding config.

Lenient like the service's parser: unknown keys don't stop the load but are
recorded so we can warn (``env``/``cwd`` are silently dropped).
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass, field
from pathlib import Path

# Byte-order marks, checked longest-first so a UTF-32-LE file (FF FE 00 00) is not
# mis-detected as UTF-16-LE (FF FE). The service expects plain UTF-8 with no BOM.
_BOMS: tuple[tuple[bytes, str, str], ...] = (
    (codecs.BOM_UTF32_LE, "UTF-32-LE", "utf-32"),
    (codecs.BOM_UTF32_BE, "UTF-32-BE", "utf-32"),
    (codecs.BOM_UTF8, "UTF-8", "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "UTF-16-LE", "utf-16"),
    (codecs.BOM_UTF16_BE, "UTF-16-BE", "utf-16"),
)

from .contract import (
    CONFIG_TOP_LEVEL_KEY,
    SERVER_KNOWN_KEYS,
)


class ConfigError(Exception):
    """Raised when a config cannot be parsed into the forwarding config shape at all."""


@dataclass
class ServerSpec:
    """One entry of ``mcpServers``: a single MCP server the service would launch."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    # Keys present in the JSON entry that the service ignores (e.g. "env", "cwd").
    ignored_keys: list[str] = field(default_factory=list)


@dataclass
class ForwardingConfig:
    servers: list[ServerSpec]
    source: str  # path or "<command-line>"
    # BOM found at the start of the file, if any (e.g. "UTF-8"). The service
    # rejects a BOM (reported as FAIL); the tester strips it to keep checking the rest.
    bom: str | None = None


def _decode_config_bytes(data: bytes) -> tuple[str, str | None]:
    """Decode raw config bytes to text, returning (text, bom_name).

    A leading BOM is stripped so the remaining checks can still run, but its presence
    is reported so the caller can flag it; the service would fail to load the
    file at all.
    """
    for marker, name, encoding in _BOMS:
        if data.startswith(marker):
            return data.decode(encoding), name
    return data.decode("utf-8"), None


def load_config(path: str | Path) -> ForwardingConfig:
    """Parse a ``mcp_server_redirection_config.json`` file.

    Raises ConfigError if the file is missing, not decodable, not JSON, or not shaped
    like a forwarding config. Per-server field problems are surfaced as checks later,
    not raised here, except where the entry is so malformed the parser would reject the
    whole file. A BOM is recorded (not raised) so the report can explain it.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config file '{path}': {exc}") from exc

    try:
        raw, bom = _decode_config_bytes(data)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file '{path}' is not valid UTF-8 text: {exc}") from exc

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file '{path}' is not valid JSON: {exc}") from exc

    config = _parse_doc(doc, source=str(path))
    config.bom = bom
    return config


def parse_config_obj(doc: object, source: str) -> ForwardingConfig:
    """Validate an already-decoded config object (used by tests)."""
    return _parse_doc(doc, source=source)


def _parse_doc(doc: object, source: str) -> ForwardingConfig:
    if not isinstance(doc, dict):
        raise ConfigError("config root must be a JSON object")

    servers_obj = doc.get(CONFIG_TOP_LEVEL_KEY)
    if servers_obj is None:
        raise ConfigError(f"config is missing the required '{CONFIG_TOP_LEVEL_KEY}' object")
    if not isinstance(servers_obj, dict):
        raise ConfigError(f"'{CONFIG_TOP_LEVEL_KEY}' must be a JSON object of server entries")

    servers: list[ServerSpec] = []
    for name, entry in servers_obj.items():
        servers.append(_parse_server(name, entry))

    return ForwardingConfig(servers=servers, source=source)


def _parse_server(name: str, entry: object) -> ServerSpec:
    if not isinstance(entry, dict):
        # The parser would fail to deserialize this into the server config.
        raise ConfigError(f"server '{name}': entry must be a JSON object")

    command = entry.get("command")
    if not isinstance(command, str) or not command:
        # ``command`` is required and typed as a string in the server config.
        raise ConfigError(f"server '{name}': missing or non-string 'command'")

    args_val = entry.get("args")
    args: list[str] = []
    if args_val is not None:
        if not isinstance(args_val, list) or not all(isinstance(a, str) for a in args_val):
            raise ConfigError(f"server '{name}': 'args' must be an array of strings")
        args = list(args_val)

    ignored = [k for k in entry.keys() if k not in SERVER_KNOWN_KEYS]

    return ServerSpec(name=name, command=command, args=args, ignored_keys=ignored)

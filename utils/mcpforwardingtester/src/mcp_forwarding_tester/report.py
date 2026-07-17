# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render a ConfigReport as human-readable text or JSON, and decide the exit code."""

from __future__ import annotations

import json
import os
import sys

from .checks import Level
from .runner import ConfigReport, ServerReport

# Exit-code policy.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

_COLORS = {
    Level.PASS: "\033[32m",   # green
    Level.WARN: "\033[33m",   # yellow
    Level.FAIL: "\033[31m",   # red
    Level.INFO: "\033[36m",   # cyan
}
_RESET = "\033[0m"


def _stream_supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not (hasattr(stream, "isatty") and stream.isatty()):
        return False
    if sys.platform == "win32":
        # Legacy Windows consoles (e.g. Windows PowerShell 5.1 / conhost) do not
        # interpret ANSI escapes and would print them literally. Only enable color on
        # terminals known to support VT sequences.
        return bool(
            os.environ.get("WT_SESSION")
            or os.environ.get("ANSICON")
            or os.environ.get("TERM")
        )
    return True


def _tag(level: Level, color: bool) -> str:
    label = f"{level.value:>4}"
    if color:
        return f"{_COLORS[level]}{label}{_RESET}"
    return label


def render_text(report: ConfigReport, stream=sys.stdout) -> None:
    color = _stream_supports_color(stream)
    w = stream.write

    w("\nMCP tool forwarding compatibility report\n")
    w(f"source: {report.source}\n")
    w("=" * 72 + "\n")

    if report.file_checks:
        w("Config file:\n")
        for c in report.file_checks:
            w(f"  [{_tag(c.level, color)}] {c.id}: {c.message}\n")

    for srv in report.servers:
        _render_server(srv, w, color)

    w("\n" + "-" * 72 + "\n")
    w("Aggregate (whole config):\n")
    for c in report.aggregate:
        w(f"  [{_tag(c.level, color)}] {c.id}: {c.message}\n")

    verdict = "INCOMPATIBLE" if report.failed else "OK"
    vlevel = Level.FAIL if report.failed else Level.PASS
    w("\n" + "=" * 72 + "\n")
    w(f"Verdict: [{_tag(vlevel, color)}] {verdict}\n")
    if report.failed:
        w("One or more checks FAILED; this config would not work as intended "
          "with MCP tool forwarding.\n")
    else:
        w("No failures. Warnings (if any) are worth reviewing but are not blocking.\n")
    w("\n")


def _render_server(srv: ServerReport, w, color: bool) -> None:
    w(f"\nServer '{srv.name}'  (command: {srv.spec.command} "
      f"{' '.join(srv.spec.args)})\n")
    probe = srv.probe
    if probe.protocol_version or probe.server_name:
        w(f"  server: {probe.server_name or '?'} v{probe.server_version or '?'} | "
          f"protocol: {probe.protocol_version or '?'}\n")
    for c in srv.results:
        w(f"  [{_tag(c.level, color)}] {c.id}: {c.message}\n")
    if probe.stderr_tail:
        w("  --- server stderr (the service inherits this) ---\n")
        for line in probe.stderr_tail.splitlines():
            w(f"    {line}\n")


def render_json(report: ConfigReport, stream=sys.stdout) -> None:
    doc = {
        "source": report.source,
        "failed": report.failed,
        "file_checks": [
            {"id": c.id, "level": c.level.value, "message": c.message, "data": c.data}
            for c in report.file_checks
        ],
        "servers": [
            {
                "name": s.name,
                "command": s.spec.command,
                "args": s.spec.args,
                "ignored_config_keys": s.spec.ignored_keys,
                "server_info": {
                    "name": s.probe.server_name,
                    "version": s.probe.server_version,
                    "protocol_version": s.probe.protocol_version,
                    "capabilities": s.probe.server_capabilities,
                },
                "tools": [t.name for t in s.probe.tools],
                "checks": [
                    {"id": c.id, "level": c.level.value, "message": c.message, "data": c.data}
                    for c in s.results
                ],
            }
            for s in report.servers
        ],
        "aggregate": [
            {"id": c.id, "level": c.level.value, "message": c.message, "data": c.data}
            for c in report.aggregate
        ],
    }
    json.dump(doc, stream, indent=2)
    stream.write("\n")


def exit_code(report: ConfigReport) -> int:
    return EXIT_FAIL if report.failed else EXIT_OK

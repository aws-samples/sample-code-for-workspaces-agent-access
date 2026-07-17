# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compatibility checks. Each turns one forwarding requirement into a PASS/WARN/FAIL result.

Checks are pure functions over a ServerProbe (and ServerSpec); the runner connects and
populates the probe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from . import contract
from .client import ServerProbe
from .config import ForwardingConfig, ServerSpec


class Level(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    INFO = "INFO"


@dataclass
class CheckResult:
    id: str
    level: Level
    message: str
    data: dict = field(default_factory=dict)

    @property
    def is_fail(self) -> bool:
        return self.level is Level.FAIL


# --- File-level (whole config file) ----------------------------------------------

def check_config_file(config: ForwardingConfig) -> list[CheckResult]:
    """Checks about the config file itself, independent of any single server."""
    results: list[CheckResult] = []

    if config.bom is not None:
        # The service's JSON parser rejects a BOM and fails to load the whole config.
        results.append(
            CheckResult(
                "config.encoding",
                Level.FAIL,
                f"file starts with a {config.bom} byte-order mark (BOM). The service "
                "rejects a BOM and would fail to load the entire file. Re-save it as "
                "UTF-8 without a BOM. PowerShell 7+: "
                "Set-Content -Path <file> -Value (Get-Content -Raw <file>) -Encoding utf8NoBOM. "
                "Windows PowerShell 5.1 (its -Encoding utf8 still writes a BOM): "
                "[IO.File]::WriteAllText('<file>', (Get-Content -Raw '<file>'), "
                "(New-Object Text.UTF8Encoding $false)). "
                "Or in VS Code: 'Save with Encoding' -> 'UTF-8'.",
                {"bom": config.bom},
            )
        )
    else:
        results.append(
            CheckResult(
                "config.encoding",
                Level.PASS,
                "file is UTF-8 without a BOM",
            )
        )

    return results


# --- Static (per-server entry, no process spawned) -------------------------------

def check_static_server(spec: ServerSpec) -> list[CheckResult]:
    """Validate a single server entry's shape before launching it."""
    results: list[CheckResult] = []

    results.append(
        CheckResult(
            "config.command",
            Level.PASS,
            f"command is set: {spec.command!r}",
            {"command": spec.command, "args": spec.args},
        )
    )

    if spec.ignored_keys:
        # env/cwd are silently dropped by the service.
        results.append(
            CheckResult(
                "config.ignored-keys",
                Level.WARN,
                "the service ignores these keys in a server entry (only "
                f"'command' and 'args' are honored): {', '.join(sorted(spec.ignored_keys))}",
                {"ignored": sorted(spec.ignored_keys)},
            )
        )

    return results


# --- Per-server runtime ----------------------------------------------------------

def check_spawn(probe: ServerProbe) -> CheckResult:
    if probe.spawned:
        return CheckResult("spawn", Level.PASS, "process launched from command + args")
    return CheckResult(
        "spawn",
        Level.FAIL,
        probe.connect_error or "process failed to launch",
    )


def check_initialize(probe: ServerProbe) -> CheckResult:
    if not probe.spawned:
        return CheckResult("initialize", Level.FAIL, "skipped: process did not launch")
    if not probe.initialized:
        return CheckResult(
            "initialize",
            Level.FAIL,
            probe.connect_error or "MCP initialize handshake did not complete",
        )
    return CheckResult(
        "initialize",
        Level.PASS,
        f"initialized; server {probe.server_name or '?'} "
        f"v{probe.server_version or '?'}",
        {
            "server_name": probe.server_name,
            "server_version": probe.server_version,
            "capabilities": probe.server_capabilities,
        },
    )


def check_protocol_version(probe: ServerProbe) -> CheckResult:
    """Report the negotiated protocol version (INFO, not pass/fail; the service may
    negotiate a different version)."""
    if not probe.initialized:
        return CheckResult(
            "protocol-version", Level.INFO, "skipped: not initialized"
        )
    return CheckResult(
        "protocol-version",
        Level.INFO,
        f"negotiated protocol version {probe.protocol_version!r} "
        "(note: the service may negotiate a different MCP protocol version; "
        "confirm your server accepts the version it negotiates)",
        {"protocol_version": probe.protocol_version},
    )


def check_tools_list(probe: ServerProbe) -> CheckResult:
    """The service drops a server that lists no tools, and forwards nothing if none do."""
    if not probe.initialized:
        return CheckResult("tools-list", Level.FAIL, "skipped: not initialized")
    if probe.list_error is not None:
        return CheckResult(
            "tools-list", Level.FAIL, f"tools/list failed: {probe.list_error}"
        )
    n = len(probe.tools)
    if n == 0:
        return CheckResult(
            "tools-list",
            Level.FAIL,
            "server returned 0 tools; the service drops a server that lists "
            "no tools, and forwards nothing if no server has any tools",
        )
    return CheckResult(
        "tools-list",
        Level.PASS,
        f"server returned {n} tool(s): {', '.join(t.name for t in probe.tools)}",
        {"tools": [t.name for t in probe.tools]},
    )


def check_tool_names(probe: ServerProbe, server_name: str) -> CheckResult:
    """The service does not validate names, but '___' in a name makes the forwarded name ambiguous."""
    if not probe.tools:
        return CheckResult("tool-names", Level.INFO, "skipped: no tools to inspect")

    suspicious: list[str] = []
    long_names: list[str] = []
    for t in probe.tools:
        if contract.FORWARD_SEPARATOR in t.name:
            suspicious.append(t.name)
        if len(contract.forwarded_tool_name(server_name, t.name)) > 128:
            long_names.append(t.name)

    if suspicious or long_names:
        bits = []
        if suspicious:
            bits.append(
                f"contain '{contract.FORWARD_SEPARATOR}': {', '.join(suspicious)}"
            )
        if long_names:
            bits.append(f"forwarded name exceeds 128 chars: {', '.join(long_names)}")
        return CheckResult(
            "tool-names",
            Level.WARN,
            "some tool names may be problematic once namespaced: " + "; ".join(bits),
            {"suspicious": suspicious, "long": long_names},
        )
    return CheckResult(
        "tool-names",
        Level.PASS,
        "tool names are clean for namespacing as "
        f"{contract.FORWARD_PREFIX}{contract.FORWARD_SEPARATOR}{server_name}"
        f"{contract.FORWARD_SEPARATOR}<tool>",
    )


def check_tools_list_latency(probe: ServerProbe) -> CheckResult:
    """tools/list must complete within the 5 s budget.

    The service bounds only the manifest fetch (tools/list). Over the budget,
    the call is cancelled and the client sees no tools.
    """
    if probe.list_seconds is None:
        return CheckResult("tools-list-latency", Level.INFO, "skipped: tools/list not reached")
    secs = probe.list_seconds
    budget = contract.CALL_TIMEOUT_S
    data = {"list_seconds": round(secs, 3), "budget_seconds": budget}
    if secs > budget:
        return CheckResult(
            "tools-list-latency",
            Level.FAIL,
            f"tools/list took {secs:.2f}s, over the {budget:.0f}s budget; the service "
            "would cancel the call and the client would see no tools. Reduce "
            "cold-start cost (see README: slow servers & the 5 s budget).",
            data,
        )
    if secs > budget * 0.6:
        return CheckResult(
            "tools-list-latency",
            Level.WARN,
            f"tools/list took {secs:.2f}s, close to the {budget:.0f}s budget; a cold "
            "first call on the instance may be slower and get cancelled.",
            data,
        )
    return CheckResult(
        "tools-list-latency",
        Level.PASS,
        f"tools/list took {secs:.2f}s (budget {budget:.0f}s)",
        data,
    )


def check_initialize_latency(probe: ServerProbe) -> CheckResult:
    """Report spawn + initialize time.

    The service does not bound this, so it is WARN-only. A slow handshake makes the
    cold tools/list more likely to exceed the 5 s budget; common causes are telemetry
    or socket connects at startup.
    """
    if probe.init_seconds is None:
        return CheckResult("initialize-latency", Level.INFO, "skipped: not initialized")
    secs = probe.init_seconds
    threshold = contract.SLOW_INITIALIZE_WARN_S
    data = {"init_seconds": round(secs, 3), "warn_threshold_seconds": threshold}
    if secs > threshold:
        return CheckResult(
            "initialize-latency",
            Level.WARN,
            f"spawn + initialize took {secs:.2f}s (> {threshold:.0f}s). The service "
            "does not time this out, but a slow startup delays tools and risks the "
            "cold tools/list exceeding 5 s. Check for telemetry/network or socket "
            "connects during startup (e.g. set DISABLE_TELEMETRY=true).",
            data,
        )
    return CheckResult(
        "initialize-latency",
        Level.PASS,
        f"spawn + initialize took {secs:.2f}s",
        data,
    )


def check_tool_call(probe: ServerProbe, tool_name: str) -> CheckResult:
    """Judge an opt-in active tool invocation."""
    outcome = probe.calls.get(tool_name)
    if outcome is None:
        return CheckResult(
            f"call:{tool_name}", Level.INFO, "not invoked"
        )
    if not outcome.ok:
        return CheckResult(
            f"call:{tool_name}",
            Level.FAIL,
            f"call raised a protocol/transport error: {outcome.error}",
        )
    over = outcome.seconds is not None and outcome.seconds > contract.CALL_TIMEOUT_S
    if outcome.is_error:
        level = Level.WARN
        msg = "tool returned an MCP error result (isError=true)"
    elif over:
        level = Level.FAIL
        msg = f"call took {outcome.seconds:.2f}s, over the {contract.CALL_TIMEOUT_S:.0f}s budget"
    else:
        level = Level.PASS
        msg = f"call returned in {outcome.seconds:.2f}s"
    return CheckResult(
        f"call:{tool_name}",
        level,
        f"{msg}; content: {outcome.content_summary}",
        {"seconds": outcome.seconds, "is_error": outcome.is_error},
    )


# --- Config-level aggregate ------------------------------------------------------

def check_aggregate(probes: dict[str, ServerProbe]) -> list[CheckResult]:
    """Cross-server rule: at least one tool total across all servers."""
    results: list[CheckResult] = []

    total_tools = sum(len(p.tools) for p in probes.values())
    if total_tools == 0:
        results.append(
            CheckResult(
                "aggregate.tools",
                Level.FAIL,
                "no server returned any tools; the service would fail with "
                '"No available MCP server to redirect has listed its tools."',
            )
        )
    else:
        results.append(
            CheckResult(
                "aggregate.tools",
                Level.PASS,
                f"{total_tools} tool(s) total across {len(probes)} server(s)",
                {"total_tools": total_tools},
            )
        )

    # Forwarded names are server-prefixed, so cross-server collisions can't happen.
    if len(probes) > 1:
        results.append(
            CheckResult(
                "aggregate.namespacing",
                Level.INFO,
                "the service prefixes every tool with its server name, so "
                "identical tool names across servers do not collide",
            )
        )

    return results

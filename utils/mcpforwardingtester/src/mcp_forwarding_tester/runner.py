# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Async orchestration: connect to each server, run the checks, aggregate."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import checks, client
from .checks import CheckResult, Level
from .client import ServerProbe
from .config import ForwardingConfig, ServerSpec


@dataclass
class CallRequest:
    tool: str
    arguments: dict | None = None


@dataclass
class RunOptions:
    # Active invocation (opt-in).
    calls: list[CallRequest] = field(default_factory=list)
    # Smoke-call every tool whose input schema has no required properties.
    probe_tools: bool = False


@dataclass
class ServerReport:
    name: str
    spec: ServerSpec
    probe: ServerProbe
    results: list[CheckResult]

    @property
    def worst(self) -> Level:
        return _worst([r.level for r in self.results])


@dataclass
class ConfigReport:
    source: str
    servers: list[ServerReport]
    aggregate: list[CheckResult]
    # Checks about the config file itself (e.g. encoding/BOM). Empty for ad-hoc runs.
    file_checks: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        if any(r.is_fail for r in self.file_checks):
            return True
        if any(r.is_fail for r in self.aggregate):
            return True
        return any(any(c.is_fail for c in s.results) for s in self.servers)


def _worst(levels: list[Level]) -> Level:
    order = {Level.INFO: 0, Level.PASS: 1, Level.WARN: 2, Level.FAIL: 3}
    return max(levels, key=lambda lv: order[lv], default=Level.PASS)


def _schema_has_no_required_args(schema: dict | None) -> bool:
    if not schema:
        return True
    required = schema.get("required")
    return not required


async def run_server(spec: ServerSpec, opts: RunOptions) -> ServerReport:
    """Connect to one server and evaluate the full per-server check suite."""
    results: list[CheckResult] = list(checks.check_static_server(spec))

    async with client.connect(spec.command, spec.args) as (probe, session):
        if session is not None:
            try:
                await client.initialize(probe, session)
                if probe.initialized:
                    await client.list_tools_paginated(probe, session)
                    await _run_calls(probe, session, opts)
            except Exception as exc:  # noqa: BLE001
                if probe.connect_error is None:
                    probe.connect_error = f"unexpected error during session: {exc}"

    results.extend(_evaluate(spec, probe, opts))
    return ServerReport(name=spec.name, spec=spec, probe=probe, results=results)


async def _run_calls(
    probe: ServerProbe, session, opts: RunOptions
) -> None:
    # Explicit calls requested on the command line.
    for req in opts.calls:
        await client.call_tool(probe, session, req.tool, req.arguments)
    # Auto-probe no-arg tools if asked.
    if opts.probe_tools:
        for tool in probe.tools:
            if tool.name in probe.calls:
                continue
            if _schema_has_no_required_args(tool.input_schema):
                await client.call_tool(probe, session, tool.name, {})


def _evaluate(spec: ServerSpec, probe: ServerProbe, opts: RunOptions) -> list[CheckResult]:
    results: list[CheckResult] = [
        checks.check_spawn(probe),
        checks.check_initialize(probe),
        checks.check_protocol_version(probe),
        checks.check_tools_list(probe),
        checks.check_tool_names(probe, spec.name),
        checks.check_initialize_latency(probe),
        checks.check_tools_list_latency(probe),
    ]
    for tool_name in probe.calls:
        results.append(checks.check_tool_call(probe, tool_name))
    return results


async def run_config(config: ForwardingConfig, opts: RunOptions) -> ConfigReport:
    """Evaluate every server in a config and compute the cross-server aggregate."""
    file_checks = checks.check_config_file(config)

    server_reports: list[ServerReport] = []
    probes: dict[str, ServerProbe] = {}
    # Sequential; configs are small.
    for spec in config.servers:
        report = await run_server(spec, opts)
        server_reports.append(report)
        probes[spec.name] = report.probe

    aggregate = checks.check_aggregate(probes)
    return ConfigReport(
        source=config.source,
        servers=server_reports,
        aggregate=aggregate,
        file_checks=file_checks,
    )

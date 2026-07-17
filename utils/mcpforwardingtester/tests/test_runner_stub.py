# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end tests that launch the bundled stub MCP server.

These require the ``mcp`` SDK (a runtime dependency) and spawn a real child process,
so they exercise the same path the service uses: spawn -> initialize -> list -> call.
"""

import sys
from pathlib import Path

import pytest

from mcp_forwarding_tester.checks import Level
from mcp_forwarding_tester.config import ForwardingConfig, ServerSpec
from mcp_forwarding_tester.runner import CallRequest, RunOptions, run_config

FIXTURES = Path(__file__).parent / "fixtures"
STUB = str(FIXTURES / "stub_server.py")


def _check(report_servers, server_name, check_id):
    srv = next(s for s in report_servers if s.name == server_name)
    return next(c for c in srv.results if c.id == check_id)


def _config(spec: ServerSpec) -> ForwardingConfig:
    return ForwardingConfig(servers=[spec], source="<test>")


@pytest.mark.asyncio
async def test_good_server_passes():
    spec = ServerSpec(name="stub", command=sys.executable, args=[STUB, "good"])
    report = await run_config(_config(spec), RunOptions())

    assert _check(report.servers, "stub", "spawn").level is Level.PASS
    assert _check(report.servers, "stub", "initialize").level is Level.PASS
    tools = _check(report.servers, "stub", "tools-list")
    assert tools.level is Level.PASS
    assert "echo" in tools.data["tools"]
    assert not report.failed


@pytest.mark.asyncio
async def test_empty_server_fails_tools_and_aggregate():
    spec = ServerSpec(name="empty", command=sys.executable, args=[STUB, "empty"])
    report = await run_config(_config(spec), RunOptions())

    assert _check(report.servers, "empty", "tools-list").level is Level.FAIL
    agg = next(c for c in report.aggregate if c.id == "aggregate.tools")
    assert agg.level is Level.FAIL
    assert report.failed


@pytest.mark.asyncio
async def test_nonexistent_command_fails_spawn():
    spec = ServerSpec(name="missing", command="/nonexistent/please/no", args=[])
    report = await run_config(_config(spec), RunOptions())

    assert _check(report.servers, "missing", "spawn").level is Level.FAIL
    assert report.failed


@pytest.mark.asyncio
async def test_active_tool_call():
    spec = ServerSpec(name="stub", command=sys.executable, args=[STUB, "good"])
    opts = RunOptions(calls=[CallRequest(tool="echo", arguments={"text": "hi"})])
    report = await run_config(_config(spec), opts)

    call = _check(report.servers, "stub", "call:echo")
    assert call.level is Level.PASS
    assert not report.failed


@pytest.mark.asyncio
async def test_probe_tools_calls_no_arg_tools():
    spec = ServerSpec(name="stub", command=sys.executable, args=[STUB, "good"])
    report = await run_config(_config(spec), RunOptions(probe_tools=True))

    # ``ping`` has no required args and should have been probed; ``echo`` requires
    # ``text`` so it should be skipped.
    ids = {c.id for s in report.servers for c in s.results}
    assert "call:ping" in ids
    assert "call:echo" not in ids


@pytest.mark.asyncio
async def test_good_server_both_latency_checks_pass():
    spec = ServerSpec(name="stub", command=sys.executable, args=[STUB, "good"])
    report = await run_config(_config(spec), RunOptions())

    assert _check(report.servers, "stub", "initialize-latency").level is Level.PASS
    assert _check(report.servers, "stub", "tools-list-latency").level is Level.PASS


@pytest.mark.asyncio
async def test_slow_list_fails_tools_list_latency():
    # tools/list takes ~6 s (> the 5 s budget) -> FAIL.
    spec = ServerSpec(name="slow", command=sys.executable, args=[STUB, "slow-list", "6"])
    report = await run_config(_config(spec), RunOptions())

    assert _check(report.servers, "slow", "tools-list-latency").level is Level.FAIL
    # initialize itself was fast, so that check should not FAIL.
    assert _check(report.servers, "slow", "initialize-latency").level is not Level.FAIL
    assert report.failed


@pytest.mark.asyncio
async def test_slow_init_warns_but_does_not_fail():
    # spawn+initialize takes ~6 s; the service does not time this out, so it is a WARN
    # and the overall run is NOT failed (tools still list fine afterwards).
    spec = ServerSpec(name="slowi", command=sys.executable, args=[STUB, "slow-init", "6"])
    report = await run_config(_config(spec), RunOptions())

    assert _check(report.servers, "slowi", "initialize-latency").level is Level.WARN
    assert _check(report.servers, "slowi", "tools-list-latency").level is Level.PASS
    assert _check(report.servers, "slowi", "tools-list").level is Level.PASS
    assert not report.failed

#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Batch several desktop actions in one MCP call with execute_sequence.

Connects, then opens Notepad and types a note with one
agentaccess___execute_sequence call, prints the response and saves the
screenshot the sequence returns. It then reads the sequence limits from
tools/list and sends one sequence over each limit, to show the rejection shape.
No model is involved: this is the call an agent makes when it batches actions.
Self-contained like quickstart.py, with no imports from lib/.

execute_sequence is for actions you can plan without seeing intermediate
results. Connect with a screenshot first so the desktop is attached, then batch.

Prerequisites:
    pip install "mcp>=2" boto3

Usage:
    # Generate a streaming URL (valid 1 hour):
    STREAMING_URL=$(scripts/streaming_url.sh)

    # Run it:
    python3 tool_sequencing.py "$STREAMING_URL"
"""

import asyncio
import base64
import json
import os
import sys
import tempfile

import boto3
import httpx2
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

# ─── Configuration ────────────────────────────────────────────────────────────

REGION = "us-east-1"
MCP_ENDPOINT = f"https://agentaccess-mcp.{REGION}.api.aws/mcp"
MCP_SERVICE = "agentaccess-mcp"
# Stateless MCP only: this sample passes mcpSessionId itself, so it cannot run on a stateful fallback.
MCP_PROTOCOL = "2026-07-28"
SCREENSHOT_PATH = os.path.join(tempfile.gettempdir(), "tool_sequencing.png")

# The execute_sequence description states three limits that are not in the tool schema: total wait, total typed
# text and a 60-second deadline. The first two are below; the service checks the deadline as the sequence runs.
# The action count and per-wait cap are read from the schema below.
MAX_TOTAL_WAIT_SECONDS = 10
MAX_TOTAL_TEXT_CHARS = 10_000

# Open Notepad from the Run dialog and type a note, all in one execute_sequence. Each action
# names the tool to run, as you would call it directly, with that tool's arguments beside "type".
OPEN_NOTEPAD_AND_TYPE = [
    {"type": "agentaccess___key", "keys": "super+r"},
    {"type": "agentaccess___wait", "duration": 1},
    {"type": "agentaccess___type_text", "text": "notepad"},
    {"type": "agentaccess___wait", "duration": 1},
    {"type": "agentaccess___key", "keys": "Return"},
    {"type": "agentaccess___wait", "duration": 3},
    {"type": "agentaccess___type_text", "text": "Opened and typed by one execute_sequence call."},
]

if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
    print("Usage: python3 tool_sequencing.py <STREAMING_URL>")
    print("       STREAMING_URL=$(scripts/streaming_url.sh)")
    sys.exit(2 if len(sys.argv) > 1 else 1)

streaming_url = sys.argv[1]

# ─── 1. Sign requests for the Agent Access MCP Server ────────────────────────

class SigV4(httpx2.Auth):
    requires_request_body = True

    def __init__(self):
        self.credentials = boto3.Session().get_credentials()
        if self.credentials is None:
            raise RuntimeError("No AWS credentials found for MCP signing (check aws sts get-caller-identity).")

    def auth_flow(self, request):
        headers = {k: v for k, v in request.headers.items() if k.lower() != "connection"}
        aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(self.credentials.get_frozen_credentials(), MCP_SERVICE, REGION).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request

# ─── 2. Connect, batch a note into Notepad, show the limits, then close ──────

async def main(streaming_url):
    async with httpx2.AsyncClient(
        auth=SigV4(),
        headers={"X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url},
        timeout=httpx2.Timeout(30.0, read=300.0),
    ) as http_client:
        async with Client(streamable_http_client(MCP_ENDPOINT, http_client=http_client), mode=MCP_PROTOCOL) as mcp:
            session_id = await open_session(mcp)
            try:
                limits = await read_sequence_limits(mcp)
                if limits is None:
                    raise RuntimeError("execute_sequence is not listed; is tool sequencing enabled for this account?")
                print_sequence_limits(limits)

                typed = await run_sequence(
                    mcp, session_id, "Open Notepad and type a note, in one execute_sequence", OPEN_NOTEPAD_AND_TYPE)
                if typed.is_error:
                    raise RuntimeError(f"The open-and-type sequence failed: {typed.content}")
                save_screenshot(typed)

                # Each of these is over a limit, so the service rejects it before any action runs.
                for title, actions in over_limit_sequences(limits).items():
                    result = await run_sequence(mcp, session_id, f"Over a limit: {title}", actions)
                    if not result.is_error:
                        raise RuntimeError(f"The service accepted a sequence over a limit ({title})")
            finally:
                await close_session(mcp, session_id)


async def open_session(mcp):
    """Open the stateless (2026-07-28) session and return its id.

    The screenshot attaches the desktop and returns the session id. The first call passes
    mcpSessionId "NEW"; every later call passes the id it returns.
    """
    opened = await mcp.call_tool("agentaccess___screenshot", {"mcpSessionId": "NEW"})
    session_id = find_session_id(opened)
    if session_id is None:
        raise RuntimeError(f"Could not open a stateless session ({MCP_PROTOCOL}): {opened.content}")
    if opened.is_error:
        # The session exists even though its first screenshot failed: close it before giving up.
        await close_session(mcp, session_id)
        raise RuntimeError(f"The first screenshot failed: {opened.content}")
    return session_id


async def read_sequence_limits(mcp):
    """The schema-advertised sequence limits, or None when the tools are not listed.

    Returns (max_actions, min_wait, max_wait), read from the execute_sequence and wait schemas in
    tools/list. The cumulative wait, cumulative typed text and overall deadline are not in the schema; the
    first two are the MAX_TOTAL_* constants above, taken from the execute_sequence description.
    """
    tools = {t.name: t for t in await list_all_tools(mcp)}
    seq = tools.get("agentaccess___execute_sequence")
    wait = tools.get("agentaccess___wait")
    if seq is None or wait is None:
        return None
    actions = seq.input_schema.get("properties", {}).get("actions", {})
    duration = wait.input_schema.get("properties", {}).get("duration", {})
    max_actions, min_wait, max_wait = actions.get("maxItems"), duration.get("minimum"), duration.get("maximum")
    if None in (max_actions, min_wait, max_wait):
        raise RuntimeError("tools/list did not advertise the expected execute_sequence/wait limits")
    return max_actions, min_wait, max_wait


async def list_all_tools(mcp):
    """Every tool in tools/list. The list can come back in pages, each pointing to the next with next_cursor."""
    tools, cursor = [], None
    while True:
        page = await mcp.list_tools(cursor=cursor)
        tools.extend(page.tools)
        if page.next_cursor is None:
            return tools
        cursor = page.next_cursor


def print_sequence_limits(limits):
    """Print the sequence limits and where each one is stated."""
    max_actions, min_wait, max_wait = limits
    print("── Sequence limits")
    print(f"  from the schema: at most {max_actions} actions, each wait {min_wait} to {max_wait} seconds")
    print(f"  from the execute_sequence description: total wait {MAX_TOTAL_WAIT_SECONDS}s, "
          f"total typed text {MAX_TOTAL_TEXT_CHARS} characters, plus an overall deadline")


def over_limit_sequences(limits):
    """One sequence over each limit, built from the limits just read. Each is rejected before any action runs."""
    max_actions, min_wait, max_wait = limits
    # The fewest waits at the per-wait cap whose total is over the cumulative cap.
    waits_over_total = int(MAX_TOTAL_WAIT_SECONDS // max_wait) + 1
    sequences = {
        "too many actions": [{"type": "agentaccess___wait", "duration": min_wait} for _ in range(max_actions + 1)],
        "one wait over the per-wait cap": [{"type": "agentaccess___wait", "duration": max_wait + 1}],
    }
    # With more waits than a sequence may hold, it would be rejected for the action count, not the total wait.
    if waits_over_total <= max_actions:
        sequences["all waits over the cumulative cap"] = [
            {"type": "agentaccess___wait", "duration": max_wait} for _ in range(waits_over_total)]
    sequences["all typed text over the character cap"] = [
        {"type": "agentaccess___type_text", "text": "x" * (MAX_TOTAL_TEXT_CHARS + 1000)}]
    return sequences


async def run_sequence(mcp, session_id, title, actions):
    """Send one execute_sequence and print its result. Returns the result."""
    result = await mcp.call_tool(
        "agentaccess___execute_sequence", {"mcpSessionId": session_id, "actions": actions})
    print_sequence_result(title, result)
    return result


async def close_session(mcp, session_id):
    """Close the stateless session (it sends no HTTP DELETE), reporting a failed close."""
    try:
        closed = await mcp.call_tool("agentaccess___close_session", {"mcpSessionId": session_id})
    except Exception as e:
        print(f"\nclose_session errored for {session_id}: {type(e).__name__}: {e}", file=sys.stderr)
        return
    if closed.is_error:
        print(f"\nclose_session failed for {session_id}: {closed.content}", file=sys.stderr)
    else:
        print(f"\nClosed session {session_id}")


def find_session_id(result):
    """The id in the {"mcpSessionId": ...} text block the service adds to every stateless result, or None."""
    for block in reversed(result.content):
        if block.type != "text":
            continue
        try:
            value = json.loads(block.text)
        except ValueError:
            continue
        if isinstance(value, dict) and "mcpSessionId" in value:
            return value["mcpSessionId"]
    return None


def print_sequence_result(title, result):
    """Print isError and the JSON in the first content block: sequence_summary and action_results."""
    print(f"\n── {title} (isError: {result.is_error})")
    if not result.content or result.content[0].type != "text":
        print(result.content)
        return
    try:
        print(json.dumps(json.loads(result.content[0].text), indent=2))
    except ValueError:
        print(result.content[0].text)


def save_screenshot(result):
    """Write the screenshot a successful sequence returns after its last action to SCREENSHOT_PATH."""
    for block in result.content:
        if block.type == "image":
            png = base64.b64decode(block.data)
            with open(SCREENSHOT_PATH, "wb") as f:
                f.write(png)
            print(f"Screenshot after the last action: {SCREENSHOT_PATH} ({len(png)} bytes)")
            return
    print("No screenshot in this result.")


asyncio.run(main(streaming_url))

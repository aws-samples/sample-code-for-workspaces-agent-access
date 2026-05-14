# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda target for AgentCore Gateway that proxies tool calls to the Agent Access MCP server.

Each invocation receives a single tool call (event = tool arguments, context has tool name).
The Lambda manages streaming URL lifecycle and forwards calls to the MCP endpoint.
"""

import json
import logging
import os
import time

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession
from urllib3 import PoolManager

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
FLEET_NAME = os.environ.get("FLEET_NAME", "WorkspacesAgentDemo")
STACK_NAME = os.environ.get("STACK_NAME", "Workspaces-Apps-AgentDemo")
MCP_ENDPOINT = os.environ.get("MCP_ENDPOINT", f"https://agentaccess-mcp.{REGION}.api.aws/mcp")
MCP_SERVICE = "agentaccess-mcp"

appstream_client = boto3.client("appstream", region_name=REGION)
http = PoolManager()

# Session cache (reused across warm invocations)
_session_cache = {"url": None, "expires": 0, "mcp_session_id": None}


def handler(event, context):
    """Gateway Lambda target handler."""
    # Extract tool name from context
    custom = {}
    if context and hasattr(context, "client_context") and context.client_context:
        custom = getattr(context.client_context, "custom", {}) or {}

    raw_tool_name = custom.get("bedrockAgentCoreToolName", "")
    delimiter = "___"
    tool_name = raw_tool_name.split(delimiter, 1)[-1] if delimiter in raw_tool_name else raw_tool_name

    logger.info(f"Tool call: {tool_name} args={json.dumps(event)[:200]}")

    # Get or create streaming URL
    streaming_url = _get_streaming_url()

    # Forward tool call to MCP server (tools are prefixed with agentaccess___)
    mcp_tool_name = f"agentaccess___{tool_name}"
    result = _call_mcp_tool(streaming_url, mcp_tool_name, event)
    return result


def _get_streaming_url():
    """Get a valid streaming URL, creating one if needed."""
    now = time.time()
    if _session_cache["url"] and _session_cache["expires"] > now + 300:
        return _session_cache["url"]

    logger.info("Creating new streaming URL...")
    resp = appstream_client.create_streaming_url(
        StackName=STACK_NAME,
        FleetName=FLEET_NAME,
        UserId=f"harness-{int(now)}",
        Validity=3600,
    )
    _session_cache["url"] = resp["StreamingURL"]
    _session_cache["expires"] = now + 3600
    _session_cache["mcp_session_id"] = None  # Reset MCP session
    logger.info("Streaming URL created.")
    return _session_cache["url"]


def _call_mcp_tool(streaming_url, tool_name, arguments):
    """Call a tool on the MCP server via SigV4-signed HTTP."""
    # Initialize session if needed
    if not _session_cache["mcp_session_id"]:
        _mcp_initialize(streaming_url)

    # Build tools/call request
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments or {}},
    })

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url,
    }
    if _session_cache["mcp_session_id"]:
        headers["Mcp-Session-Id"] = _session_cache["mcp_session_id"]

    resp = _sigv4_request("POST", MCP_ENDPOINT, body, headers)

    if resp.status >= 400:
        error_body = resp.data.decode("utf-8", errors="replace")
        logger.error(f"MCP error {resp.status}: {error_body[:500]}")
        return {"error": f"MCP server returned {resp.status}"}

    # Parse response - handle SSE or JSON
    content_type = resp.headers.get("Content-Type", "")
    raw = resp.data.decode("utf-8")

    if "text/event-stream" in content_type:
        result = _parse_sse(raw)
    else:
        result = json.loads(raw)

    # Extract MCP session ID from response headers
    mcp_sid = resp.headers.get("Mcp-Session-Id")
    if mcp_sid:
        _session_cache["mcp_session_id"] = mcp_sid

    # Return the tool result content
    mcp_result = result.get("result", {})
    content = mcp_result.get("content", [])

    if result.get("error"):
        logger.error(f"MCP error: {json.dumps(result['error'])[:500]}")

    if not content:
        return {"result": "OK"}

    return {"content": content}


def _mcp_initialize(streaming_url):
    """Send MCP initialize request and wait for tools to be ready."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "gateway-proxy", "version": "1.0.0"},
        },
    })

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url,
    }

    resp = _sigv4_request("POST", MCP_ENDPOINT, body, headers)

    if resp.status < 400:
        mcp_sid = resp.headers.get("Mcp-Session-Id")
        if mcp_sid:
            _session_cache["mcp_session_id"] = mcp_sid
            logger.info(f"MCP session established: {mcp_sid}")

        # Send initialized notification
        notif_body = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        headers["Mcp-Session-Id"] = mcp_sid or ""
        _sigv4_request("POST", MCP_ENDPOINT, notif_body, headers)

        # Wait for DCV tools to be ready (session needs time to connect)
        _wait_for_tools(headers)
    else:
        logger.error(f"MCP initialize failed: {resp.status} {resp.data.decode()[:300]}")


def _wait_for_tools(headers):
    """Poll tools/list until DCV tools are available."""
    import time as _time
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    })
    for attempt in range(30):  # Up to 30 attempts, ~60s total
        resp = _sigv4_request("POST", MCP_ENDPOINT, body, headers)
        if resp.status == 200:
            data = json.loads(resp.data.decode("utf-8"))
            tools = data.get("result", {}).get("tools", [])
            if tools:
                logger.info(f"DCV tools ready after {attempt + 1} attempts: {[t.get('name') for t in tools[:3]]}...")
                return
        logger.info(f"Waiting for DCV tools (attempt {attempt + 1})...")
        _time.sleep(2)
    logger.warning("DCV tools not ready after 60s")


def _sigv4_request(method, url, body, headers):
    """Make a SigV4-signed request."""
    creds = BotocoreSession().get_credentials().get_frozen_credentials()
    req = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(creds, MCP_SERVICE, REGION).add_auth(req)

    return http.request(
        method,
        url,
        body=body.encode("utf-8"),
        headers=dict(req.headers),
        timeout=120.0,
    )


def _parse_sse(raw):
    """Parse SSE response to extract the JSON-RPC result."""
    for line in raw.split("\n"):
        if line.startswith("data: "):
            data = line[6:].strip()
            if data:
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    continue
    # Fallback: try parsing the whole thing as JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"result": {"content": [{"type": "text", "text": raw}]}}

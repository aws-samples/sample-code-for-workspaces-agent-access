#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Minimal agent that opens Notepad and types 'Hello World' on a remote desktop.

This is a self-contained example — no imports from lib/. Copy-paste it into your
own project to integrate Amazon WorkSpaces agent access into an existing codebase.

Prerequisites:
    pip install strands-agents "mcp>=2" boto3

Usage:
    # Generate a streaming URL (valid 1 hour):
    STREAMING_URL=$(scripts/streaming_url.sh)

    # Run the agent:
    python3 quickstart.py "$STREAMING_URL"
"""

import sys
from contextlib import asynccontextmanager

import boto3
import httpx2
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.client.streamable_http import streamable_http_client
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.executors import SequentialToolExecutor
from strands.tools.mcp import MCPClient

# ─── Configuration ────────────────────────────────────────────────────────────

REGION = "us-east-1"
MCP_ENDPOINT = f"https://agentaccess-mcp.{REGION}.api.aws/mcp"
MCP_SERVICE = "agentaccess-mcp"
MODEL_ID = "global.anthropic.claude-sonnet-5-5"

if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
    print("Usage: python3 quickstart.py <STREAMING_URL>")
    print("       STREAMING_URL=$(scripts/streaming_url.sh)")
    print("Domain-joined fleets: use agents/generic_cua/agent.py --saml-assertion-file ... --stack-arn ...")
    sys.exit(2 if len(sys.argv) > 1 else 1)

streaming_url = sys.argv[1]

# ─── 1. Connect to the Agent Access MCP Server ───────────────────────────────

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


@asynccontextmanager
async def transport():
    async with httpx2.AsyncClient(
        auth=SigV4(),
        headers={"X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url},
        timeout=httpx2.Timeout(30.0, read=300.0),
    ) as http_client:
        async with streamable_http_client(MCP_ENDPOINT, http_client=http_client) as streams:
            yield streams


mcp_client = MCPClient(transport)

# ─── 2. Create an agent with a Bedrock model + MCP tools ─────────────────────

model = BedrockModel(
    model_id=MODEL_ID,
    streaming=True,
)

agent = Agent(
    model=model,
    tools=[mcp_client],
    system_prompt="You control a Windows desktop via MCP tools. Use screenshots to observe the screen and keyboard/mouse actions to interact.",
    # One tool call at a time, so the first NEW call returns the mcpSessionId before the next call is made.
    # Parallel NEW calls would open two desktop sessions.
    tool_executor=SequentialToolExecutor(),
)

# ─── 3. Run a task ────────────────────────────────────────────────────────────

result = agent("Open Notepad from the Start Menu and type 'Hello World'. When you are done, call close_session.")
print(f"\nDone. Result: {result}")

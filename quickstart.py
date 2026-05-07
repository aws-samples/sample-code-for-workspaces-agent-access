#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Minimal agent that opens Notepad and types 'Hello World' on a remote desktop.

This is a self-contained example — no imports from lib/. Copy-paste it into your
own project to integrate Amazon WorkSpaces agent access into an existing codebase.

Prerequisites:
    pip install strands-agents mcp-proxy-for-aws boto3

Usage:
    # Generate a streaming URL (valid 1 hour):
    STREAMING_URL=$(scripts/streaming_url.sh)

    # Run the agent:
    python3 quickstart.py "$STREAMING_URL"
"""

import sys

from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

# ─── Configuration ────────────────────────────────────────────────────────────

REGION = "us-east-1"
MCP_ENDPOINT = f"https://agentaccess-mcp.{REGION}.api.aws/mcp"
MODEL_ID = "global.anthropic.claude-sonnet-4-6"

if len(sys.argv) < 2:
    print("Usage: python3 quickstart.py <STREAMING_URL>")
    print("       STREAMING_URL=$(scripts/streaming_url.sh)")
    sys.exit(1)

streaming_url = sys.argv[1]

# ─── 1. Connect to the Agent Access MCP Server ───────────────────────────────

mcp_client = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint=MCP_ENDPOINT,
    aws_service="agentaccess-mcp",
    aws_region=REGION,
    headers={"X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url},
))

# ─── 2. Create an agent with Claude + MCP tools ──────────────────────────────

model = BedrockModel(
    model_id=MODEL_ID,
    streaming=True,
)

agent = Agent(
    model=model,
    tools=[mcp_client],
    system_prompt="You control a Windows desktop via MCP tools. Use screenshots to observe the screen and keyboard/mouse actions to interact.",
)

# ─── 3. Run a task ────────────────────────────────────────────────────────────

result = agent("Open Notepad from the Start Menu and type 'Hello World'")
print(f"\nDone. Result: {result}")

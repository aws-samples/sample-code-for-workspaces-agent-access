# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP client factory, transport setup, and tool name sanitization."""

import json
import logging as _logging
import os
import re
import sys

from strands.tools.mcp import MCPClient


def _load_config():
    """Load scripts/config.json, returning a dict."""
    for candidate in [
        os.path.join(os.path.dirname(__file__), '..', 'scripts', 'config.json'),
        os.path.join(os.getcwd(), 'scripts', 'config.json'),
    ]:
        path = os.path.normpath(candidate)
        if os.path.isfile(path):
            try:
                raw = open(path).read()
                raw = re.sub(r'(?m)^\s*//.*$', '', raw)
                return json.loads(raw)
            except Exception:
                pass
    return {}


_config = _load_config()
_mcp_cfg = _config.get("mcp", {}) if isinstance(_config.get("mcp"), dict) else {}

DEFAULT_MCP_ENDPOINT = (
    _mcp_cfg.get("endpoint")
    or _config.get("mcpEndpoint")
    or os.environ.get("MCP_ENDPOINT")
    or ""
)
DEFAULT_MCP_REGION_OVERRIDE = _mcp_cfg.get("region")
DEFAULT_MCP_SERVICE = (
    _mcp_cfg.get("service")
    or os.environ.get("AWS_SERVICE_NAME")
    or ""
)


def resolve_mcp_region(args):
    """Pick the MCP signing region."""
    if getattr(args, 'mcp_region', None):
        return args.mcp_region
    if DEFAULT_MCP_REGION_OVERRIDE:
        return DEFAULT_MCP_REGION_OVERRIDE
    return os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-east-1'


def _is_remote_mcp(args):
    """Check if the agent should use a remote MCP endpoint."""
    return bool(getattr(args, 'mcp_endpoint', None))


def create_mcp_client_factory(args, root_dir=None):
    """Create the MCP client transport factory for the Agent Access MCP Server.

    Supports two auth paths:
      - Streaming URL (standard): passes URL as header
      - Domain Join (SAML): injects assertion via _meta on initialize
    """
    if not _is_remote_mcp(args):
        raise RuntimeError(
            "No MCP endpoint configured.\n"
            "  Check scripts/config.json or set --mcp-endpoint."
        )

    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    endpoint = args.mcp_endpoint
    mcp_profile = getattr(args, 'mcp_profile', None)
    mcp_region = resolve_mcp_region(args)
    mcp_service = DEFAULT_MCP_SERVICE
    if not mcp_service:
        raise RuntimeError("AWS_SERVICE_NAME is required.")
    endpoint = endpoint.replace("{region}", mcp_region)

    # Check for Domain Join mode (SAML assertion)
    saml_assertion = getattr(args, 'saml_assertion', None)
    if saml_assertion:
        stack_arn = getattr(args, 'stack_arn', None)
        if not stack_arn:
            raise RuntimeError("--stack-arn is required for Domain Join mode.")

        print(f"  MCP transport: Domain Join via _meta ({endpoint})")
        sys.stdout.flush()

        from contextlib import asynccontextmanager
        from mcp.shared.message import SessionMessage

        meta_values = {
            "aws.agentaccess/workspacesApplicationsSamlAssertion": saml_assertion,
            "aws.agentaccess/workspacesApplicationsStackArn": stack_arn,
        }

        class _MetaInjectingWriteStream:
            """Wraps MCP write stream to inject _meta into the initialize request."""

            def __init__(self, write_stream):
                self._write_stream = write_stream
                self._injected = False

            async def send(self, session_message: SessionMessage) -> None:
                if not self._injected:
                    msg = session_message.message.root
                    if hasattr(msg, "method") and msg.method == "initialize":
                        params = msg.params
                        if isinstance(params, dict):
                            params.setdefault("_meta", {}).update(meta_values)
                        elif hasattr(params, "model_extra"):
                            if not hasattr(params, "_meta") or params._meta is None:
                                params._meta = {}
                            params._meta.update(meta_values)
                        self._injected = True
                await self._write_stream.send(session_message)

            async def aclose(self) -> None:
                await self._write_stream.aclose()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                await self.aclose()

        def factory():
            @asynccontextmanager
            async def wrapped():
                async with aws_iam_streamablehttp_client(
                    endpoint=endpoint,
                    aws_service=mcp_service,
                    aws_region=mcp_region,
                    aws_profile=mcp_profile,
                    timeout=300,
                    sse_read_timeout=300,
                ) as (read_stream, write_stream, get_session_id):
                    yield read_stream, _MetaInjectingWriteStream(write_stream), get_session_id

            return wrapped()

        return factory

    # Standard path: streaming URL as header
    streaming_url = getattr(args, 'streaming_url', None) or ""

    print(f"  MCP transport: remote ({endpoint}, signed for {mcp_service}/{mcp_region})")
    sys.stdout.flush()

    # Log server-side MCP session ID.
    _mcp_logger = _logging.getLogger("mcp.client.streamable_http")
    _mcp_logger.setLevel(_logging.INFO)
    if not _mcp_logger.handlers:
        _h = _logging.StreamHandler(sys.stdout)
        _h.setFormatter(_logging.Formatter("  MCP server session: %(message)s"))

        class _SessionIdFilter(_logging.Filter):
            def filter(self, record):
                if "Received session ID" in record.getMessage():
                    record.msg = record.msg.replace("Received session ID: ", "")
                    return True
                return False

        _h.addFilter(_SessionIdFilter())
        _mcp_logger.addHandler(_h)

    def factory():
        return aws_iam_streamablehttp_client(
            endpoint=endpoint,
            aws_service=mcp_service,
            aws_region=mcp_region,
            aws_profile=mcp_profile,
            headers={
                "X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url,
            },
        )
    return factory


class _SanitizedMCPClient(MCPClient):
    """MCPClient that sanitizes tool names for Bedrock compatibility.

    Bedrock Converse API requires tool names to match [a-zA-Z0-9_-]+.
    Forwarded MCP tools use dots which must be replaced with dashes.
    """

    def list_tools_sync(self, *args, **kwargs):
        tools = super().list_tools_sync(*args, **kwargs)
        for tool in tools:
            if "." in tool.tool_name:
                tool._agent_tool_name = tool._agent_tool_name.replace(".", "-")
        return tools


def build_mcp_client(mcp_factory, startup_timeout, label=None):
    """Construct an MCPClient with tool name sanitization and session logging."""
    client = _SanitizedMCPClient(mcp_factory, startup_timeout=startup_timeout)
    session_id = getattr(client, "_session_id", None)
    if session_id:
        prefix = f"[{label}] " if label else ""
        sys.stdout.write(f"  {prefix}Strands session: {session_id}\n")
        sys.stdout.flush()
    return client

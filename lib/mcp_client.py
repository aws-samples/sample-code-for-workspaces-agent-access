# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP client factory, transport setup, and tool name sanitization."""

import atexit
import contextlib
import json
import logging as _logging
import os
import re
import sys
import threading
import weakref
from datetime import timedelta

import anyio
from mcp.shared._httpx_utils import create_mcp_http_client
from pydantic import ValidationError
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.types import PaginatedList

from .domain_join_auth import DomainJoinAuth, build_meta


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


def resolve_mcp_endpoint(args):
    """The MCP endpoint URL with ``{region}`` filled in."""
    return args.mcp_endpoint.replace("{region}", resolve_mcp_region(args))


def _is_remote_mcp(args):
    """Check if the agent should use a remote MCP endpoint."""
    return bool(getattr(args, 'mcp_endpoint', None))


def _log_server_session_id():
    """Print the server-side MCP session ID when the transport receives it (useful to support)."""
    logger = _logging.getLogger("mcp.client.streamable_http")
    logger.setLevel(_logging.INFO)
    if logger.handlers:
        return
    handler = _logging.StreamHandler(sys.stdout)
    handler.setFormatter(_logging.Formatter("  MCP server session: %(message)s"))

    class _SessionIdFilter(_logging.Filter):
        def filter(self, record):
            if "Received session ID" in record.getMessage():
                record.msg = record.msg.replace("Received session ID: ", "")
                return True
            return False

    handler.addFilter(_SessionIdFilter())
    logger.addHandler(handler)


_EXPIRE_NOTE = "; the desktop session is expired when the agent exits"
EXPIRE_SESSION_HEADER = "X-Amzn-AgentAccess-Expire-Streaming-Session-On-Delete"


def session_headers(args):
    """Headers every MCP request carries besides authentication (today: expire the session on exit)."""
    return {EXPIRE_SESSION_HEADER: "true"} if getattr(args, 'expire_session_on_exit', False) else {}


# How long one HTTP exchange with the service may take, the wait for its reply included. The proxy's default
# (30 s) is shorter than the first tools/list on a stack with application settings persistence, which waits
# for the user's profile to load. Longer than DEFAULT_TOOL_TIMEOUT, so a slow desktop action is given up on
# by the per-call timeout, which leaves the connection usable.
HTTP_TIMEOUT = 300
_END_SESSION_TIMEOUT = 10


@contextlib.asynccontextmanager
async def _end_session_on_failure(endpoint, connect, http_client_factory=create_mcp_http_client):
    """Open the transport ``connect(httpx_client_factory)``; if it fails, end its MCP session with a DELETE.

    mcp sends the closing DELETE from inside the transport's task group, so when a request fails there
    (a read timeout, a dropped connection) the cancellation that tears the group down cancels the DELETE
    too. The service then kept the session and its hold on the desktop, and refused every later
    connection with CONNECTION_LIMIT_REACHED ("Another agentic client is already connected") until it
    timed the session out. The DELETE sent here leaves out the expire header: a failed attempt is
    usually retried, and the retry needs the desktop.
    """
    clients, seen = [], {}

    async def remember(response):
        request = response.request
        if request.headers.get("mcp-protocol-version"):
            seen["version"] = request.headers["mcp-protocol-version"]
        if request.method == "DELETE":
            seen["deleted"] = True

    def recording_factory(headers=None, timeout=None, auth=None):
        client = http_client_factory(headers=headers, timeout=timeout, auth=auth)
        client.event_hooks.setdefault("response", []).append(remember)
        clients.append(client)
        return client

    session_id = None
    try:
        async with connect(recording_factory) as streams:
            try:
                yield streams
            finally:
                session_id = streams[2]()
    except BaseException:
        if session_id and clients and not seen.get("deleted"):
            with anyio.move_on_after(_END_SESSION_TIMEOUT, shield=True):
                await _delete_session(clients[-1], endpoint, session_id, seen.get("version"))
        raise
    finally:
        with anyio.CancelScope(shield=True):
            for client in clients:      # the proxy hands the client to mcp, which never closes a client it was given
                await client.aclose()


async def _delete_session(client, endpoint, session_id, protocol_version):
    """Send the DELETE that ends MCP session ``session_id``. Never raises."""
    try:
        request = client.build_request("DELETE", endpoint, headers={"mcp-session-id": session_id})
        if protocol_version:
            request.headers["mcp-protocol-version"] = protocol_version
        request.headers.pop(EXPIRE_SESSION_HEADER, None)
        response = await client.send(request)
        print(f"  MCP server session {session_id} ended after the connection failed (HTTP {response.status_code})")
    except Exception as e:
        print(f"  Could not end MCP server session {session_id}: {type(e).__name__}: {e}")
    sys.stdout.flush()


def create_mcp_client_factory(args, root_dir=None):
    """Create the MCP client transport factory for the Agent Access MCP Server.

    Supports two auth paths:
      - Streaming URL (standard): passes URL as header
      - Domain Join (SAML): adds the assertion and stack ARN to the ``_meta`` of every
        request, before it is SigV4-signed (see ``domain_join_auth``)
    """
    if not _is_remote_mcp(args):
        raise RuntimeError(
            "No MCP endpoint configured.\n"
            "  Check scripts/config.json or set --mcp-endpoint."
        )

    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    mcp_profile = getattr(args, 'mcp_profile', None)
    mcp_region = resolve_mcp_region(args)
    mcp_service = DEFAULT_MCP_SERVICE
    if not mcp_service:
        raise RuntimeError("AWS_SERVICE_NAME is required.")
    endpoint = resolve_mcp_endpoint(args)
    _log_server_session_id()

    # Check for Domain Join mode (SAML assertion)
    saml_assertion = getattr(args, 'saml_assertion', None)
    if saml_assertion:
        stack_arn = getattr(args, 'stack_arn', None)
        if not stack_arn:
            raise RuntimeError("--stack-arn is required for Domain Join mode.")

        print(f"  MCP transport: Domain Join via _meta ({endpoint}, signed for {mcp_service}/{mcp_region})"
              + _EXPIRE_NOTE * bool(session_headers(args)))
        sys.stdout.flush()

        meta = build_meta(saml_assertion, stack_arn)

        # Not the proxy's metadata= option: it edits the body after the request is signed.
        def http_client_factory(headers=None, timeout=None, auth=None):
            client = create_mcp_http_client(
                headers=headers, timeout=timeout, auth=DomainJoinAuth(auth, meta))
            # mcp 1.27 follows redirects inside a single auth pass: a 307 would replay the
            # assertion to wherever Location points, and with a signature that no longer
            # matches. The endpoint does not redirect, so refuse to follow.
            client.follow_redirects = False
            return client

        def factory():
            return _end_session_on_failure(endpoint, lambda httpx_client_factory: aws_iam_streamablehttp_client(
                endpoint=endpoint,
                aws_service=mcp_service,
                aws_region=mcp_region,
                aws_profile=mcp_profile,
                timeout=HTTP_TIMEOUT,
                sse_read_timeout=HTTP_TIMEOUT,
                headers=session_headers(args) or None,
                httpx_client_factory=httpx_client_factory,
            ), http_client_factory)

        return factory

    # Standard path: streaming URL as header
    streaming_url = getattr(args, 'streaming_url', None) or ""

    print(f"  MCP transport: remote ({endpoint}, signed for {mcp_service}/{mcp_region})"
          + _EXPIRE_NOTE * bool(session_headers(args)))
    sys.stdout.flush()

    def factory():
        return _end_session_on_failure(endpoint, lambda httpx_client_factory: aws_iam_streamablehttp_client(
            endpoint=endpoint,
            aws_service=mcp_service,
            aws_region=mcp_region,
            aws_profile=mcp_profile,
            timeout=HTTP_TIMEOUT,
            sse_read_timeout=HTTP_TIMEOUT,
            headers={
                "X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url,
                **session_headers(args),
            },
            httpx_client_factory=httpx_client_factory,
        ))
    return factory


# One desktop action (a click, a screenshot, a wait) that takes longer than this is given up on. Without a
# limit a request the service never answers would hold the whole task until the wall-clock limit.
DEFAULT_TOOL_TIMEOUT = 120

_INVALID_TOOL_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_tool_name(name):
    """Return ``name`` with every character Converse rejects replaced by ``-``."""
    return _INVALID_TOOL_NAME_CHARS.sub("-", name)


class ToolListRefused(RuntimeError):
    """The server answered tools/list with an error result (for example the desktop is not ready, or
    another agentic client holds it) instead of a tool list."""


def _refusal_text(error):
    """The server's message when ``error`` is mcp failing to read a tool *error* result as a tool list."""
    for detail in error.errors():
        result = detail.get("input")
        if isinstance(result, dict) and result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content") or () if isinstance(c, dict)]
            return " ".join(t for t in texts if t).strip() or "no message"
    return None


class _SanitizedMCPClient(MCPClient):
    """MCPClient that sanitizes tool names for Bedrock compatibility.

    Bedrock Converse requires tool names to match [a-zA-Z0-9_-]+ (max 64).
    Forwarded MCP tools use dots, which must be replaced with dashes. Tools are
    re-wrapped through the public ``MCPAgentTool(name_override=...)`` constructor;
    the original MCP name is still what goes over the wire to the server. Every tool also gets
    a per-call timeout (``tool_timeout`` seconds).
    """

    tool_timeout = None

    def list_tools_sync(self, *args, **kwargs):
        try:
            tools = super().list_tools_sync(*args, **kwargs)
        except ValidationError as e:
            refusal = _refusal_text(e)
            if refusal is None:
                raise
            raise ToolListRefused(f"The MCP server did not list its tools: {refusal}") from None
        timeout = timedelta(seconds=self.tool_timeout) if self.tool_timeout else None
        renamed = []
        for tool in tools:
            safe = _sanitize_tool_name(tool.tool_name)
            if safe != tool.tool_name or (timeout and tool.timeout is None):
                tool = MCPAgentTool(tool.mcp_tool, self, name_override=safe, timeout=tool.timeout or timeout)
            renamed.append(tool)
        return PaginatedList[MCPAgentTool](renamed, token=getattr(tools, "pagination_token", None))


def build_mcp_client(mcp_factory, startup_timeout, label=None, tool_timeout=DEFAULT_TOOL_TIMEOUT):
    """Construct an MCPClient with tool name sanitization and a per-call timeout (0 = none).

    The server-side MCP session ID is logged by the ``mcp.client.streamable_http``
    handler installed in :func:`create_mcp_client_factory`. ``label`` is accepted for
    callers written against the earlier signature and is not used.
    """
    client = _SanitizedMCPClient(mcp_factory, startup_timeout=startup_timeout)
    client.tool_timeout = tool_timeout
    _open_clients.add(client)
    return client


_open_clients = weakref.WeakSet()


def close_mcp_clients(clients=None, timeout=15):
    """Close MCP clients built here (all of them by default) so each session ends with its HTTP DELETE.

    Strands skips this clean-up once the interpreter is shutting down, so a process that simply
    exited left its session attached on the service, and the next connection to the same desktop was
    refused with "Another agentic client is already connected" until the service timed it out. Each
    client is stopped on a helper thread and given ``timeout`` seconds, so a hung connection cannot
    hold up the exit. Never raises.
    """
    for client in list(_open_clients if clients is None else clients):
        try:
            _open_clients.discard(client)
        except TypeError:           # not weak-referenceable: it was never registered
            pass
        worker = threading.Thread(target=_stop_quietly, args=(client,), daemon=True)
        worker.start()
        worker.join(timeout)


def _stop_quietly(client):
    try:
        client.stop(None, None, None)
    except Exception:
        pass


atexit.register(close_mcp_clients)


class _CancellationNoise(_logging.Filter):
    """Drops the console noise a deliberate cancel (Ctrl-C, a run limit) used to print.

    Cancelling an in-flight desktop action makes Strands log a traceback for its own
    "tool execution cancelled" exception, and the AWS proxy log that the service answered the
    ``notifications/cancelled`` message with HTTP 400. Both are expected and say nothing a user
    can act on; every other record from those loggers passes.
    """

    def filter(self, record):
        info = record.exc_info
        if info and info[0] is not None and info[0].__name__ == "_MCPCallCancelledError":
            return False
        return not (record.name.endswith("mcp1_compat") and "notifications/cancelled" in record.getMessage())


for _name in ("strands.tools.mcp.mcp_client", "mcp_proxy_for_aws.mcp1_compat"):
    _logging.getLogger(_name).addFilter(_CancellationNoise())

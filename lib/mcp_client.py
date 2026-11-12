# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP client factory, transport setup, and tool name sanitization."""

import atexit
import contextlib
import contextvars
import functools
import inspect
import json
import logging as _logging
import os
import re
import sys
import threading
import weakref
from datetime import timedelta

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCError, JSONRPCResponse
from pydantic import ValidationError
from strands.tools.mcp import MCPClient
from strands.tools.mcp import mcp_client as _strands_mcp_client
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.types import PaginatedList
from strands.types.exceptions import MCPClientInitializationError

from .domain_join_auth import DomainJoinAuth, build_meta
from .sigv4_signer import SigV4HttpAuth
from .computer_use_interface import interface_headers, requested_interface

MCP_PROTOCOL_STATELESS = "stateless"
MCP_PROTOCOL_STATEFUL = "stateful"
MCP_PROTOCOLS = (MCP_PROTOCOL_STATELESS, MCP_PROTOCOL_STATEFUL)
STATELESS_PROTOCOL_VERSION = "2026-07-28"

# Appended to the system prompt of a single-shot stateless agent. In stateless MCP closing the
# client does not end the session, so the agent has to call close_session itself.
STATELESS_CLOSE_PROMPT = (
    "When the task is done, or you cannot continue, call the close_session tool to end the "
    "desktop session."
)

CLOSE_SESSION_TOOL = "agentaccess___close_session"
CLOSE_TIMEOUT = timedelta(seconds=10)
SESSION_ARG = "mcpSessionId"
NEW_SESSION = "NEW"


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


def request_headers(args):
    """``session_headers`` plus the computer-use interface header (``lib/computer_use_interface.py``) when one is requested."""
    return {**session_headers(args), **interface_headers(args)}


# How long one HTTP exchange with the service may take, the wait for its reply included. The default
# (30 s) is shorter than the first tools/list on a stack with application settings persistence, which waits
# for the user's profile to load. Longer than DEFAULT_TOOL_TIMEOUT, so a slow desktop action is given up on
# by the per-call timeout, which leaves the connection usable.
HTTP_TIMEOUT = 300
_END_SESSION_TIMEOUT = 10


async def _report_http_error(response):
    """Answer a failed POST with a JSON-RPC error that names the HTTP status, the URL and what the service said.

    The guards in ``agent_factory`` and ``retry`` read that text: "HTTP 400 Bad Request from
    <endpoint>: <body>". mcp 2.x on its own answers a non-2xx without a JSON-RPC body with a generic "Server returned an error response". A JSON-RPC error
    body keeps its code and data (eg- -32022 for the version fallback, mcpSessionId in error.data); only
    its message gets the same prefix. 404 is left to mcp (session terminated), and so are notifications,
    which mcp 2.x drops quietly when they fail.
    """
    request = response.request
    if request.method != "POST" or response.status_code < 400 or response.status_code == 404:
        return
    try:
        message = json.loads(request.content)
    except ValueError:
        return
    if not isinstance(message, dict) or "method" not in message or not isinstance(message.get("id"), (int, str)):
        return
    try:
        body = (await response.aread()).decode("utf-8", errors="replace").strip()
    except Exception:  # a stream that cannot be read: report the status alone
        body = ""
    text = f"HTTP {response.status_code} {response.reason_phrase} from {response.url}"
    error = {"code": response.status_code, "message": f"{text}: {body}" if body else text}
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    inner = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(inner, dict) and isinstance(inner.get("code"), int):
        error = {**inner, "message": f"{text}: {inner.get('message', '')}"}
    response._content = json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": error}).encode("utf-8")
    response.status_code = 200
    response.headers["content-type"] = "application/json"


def _http_client(headers=None, timeout=None, auth=None):
    """The HTTP client for the MCP transport: signs with ``auth`` and reports failed requests (see above)."""
    client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
    # The endpoint does not redirect. A redirect would replay a Domain Join assertion with a signature
    # that no longer matches, so none is followed (mcp 2.x follows same-origin ones only, on its own).
    client.follow_redirects = False
    client.event_hooks.setdefault("response", []).append(_report_http_error)
    return client


def _is_initialize(request):
    try:
        message = json.loads(request.content)
    except ValueError:
        return False
    return isinstance(message, dict) and message.get("method") == "initialize"


@contextlib.asynccontextmanager
async def _end_session_on_failure(endpoint, connect, http_client_factory=_http_client, on_message=None):
    """Open the transport ``connect(http_client_factory)``; if it fails, end its MCP session with a DELETE.

    mcp sends the closing DELETE from inside the transport's task group, so when a request fails there
    (a read timeout, a dropped connection) the cancellation that tears the group down cancels the DELETE
    too. The service then kept the session and its hold on the desktop, and refused every later
    connection with CONNECTION_LIMIT_REACHED ("Another agentic client is already connected") until it
    timed the session out. The DELETE sent here leaves out the expire header: a failed attempt is
    usually retried, and the retry needs the desktop. Only a stateful session (from initialize) has
    an MCP session id to end this way.

    ``on_message`` sees every JSON-RPC message the server sends, so the client can record the
    mcpSessionId of a stateless session from errors too (error.data), which strands does not pass on.
    """
    clients, seen = [], {}

    async def remember(response):
        request = response.request
        if request.headers.get("mcp-protocol-version"):
            seen["version"] = request.headers["mcp-protocol-version"]
        if request.method == "DELETE":
            seen["deleted"] = True
        elif response.headers.get("mcp-session-id") and _is_initialize(request):
            seen["session"] = response.headers["mcp-session-id"]

    def recording_factory(headers=None, timeout=None, auth=None):
        client = http_client_factory(headers=headers, timeout=timeout, auth=auth)
        client.event_hooks.setdefault("response", []).insert(0, remember)   # before the status is rewritten
        clients.append(client)
        return client

    try:
        async with connect(recording_factory) as streams:
            async with _tee(streams, on_message) as streams:
                yield streams
    except BaseException:
        session_id = seen.get("session")
        if session_id and clients and not seen.get("deleted"):
            with anyio.move_on_after(_END_SESSION_TIMEOUT, shield=True):
                await _delete_session(clients[-1], endpoint, session_id, seen.get("version"))
        raise
    finally:
        with anyio.CancelScope(shield=True):
            for client in clients:      # mcp never closes a client it was given
                await client.aclose()


@contextlib.asynccontextmanager
async def _tee(streams, on_message):
    """``streams``, with every message the server sends also passed to ``on_message`` (if set).

    Needed since a JSON-RPC error carries the mcpSessionId only in error.data, which Strands drops.
    """
    if on_message is None:
        yield streams
        return
    read_stream, write_stream = streams[0], streams[1]
    send, receive = anyio.create_memory_object_stream(0)

    async def relay():
        async with send:
            async for item in read_stream:
                if isinstance(item, SessionMessage):
                    try:
                        on_message(item.message)
                    except Exception:  # best-effort bookkeeping must not kill the transport
                        pass
                await send.send(item)

    async with anyio.create_task_group() as tg:
        tg.start_soon(relay)
        try:
            yield receive, write_stream
        finally:
            tg.cancel_scope.cancel()


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


def _streamable_http(endpoint, headers, auth):
    """``connect`` for :func:`_end_session_on_failure`: the mcp 2.x streamable HTTP transport, signed with ``auth``."""
    @contextlib.asynccontextmanager
    async def connect(http_client_factory):
        client = http_client_factory(headers=headers, timeout=httpx2.Timeout(HTTP_TIMEOUT), auth=auth)
        async with streamable_http_client(endpoint, http_client=client) as streams:
            yield streams
    return connect


def _resolve_protocol(args):
    """The MCP protocol to use: ``--mcp-protocol`` (default stateless), stateful for ``--computer-use-interface``
    and for ``--expire-session-on-exit``."""
    protocol = getattr(args, 'mcp_protocol', None) or MCP_PROTOCOL_STATELESS
    if protocol not in MCP_PROTOCOLS:
        raise RuntimeError(f"--mcp-protocol must be one of {', '.join(MCP_PROTOCOLS)}.")
    if protocol == MCP_PROTOCOL_STATELESS and requested_interface(args):
        # The interface's typed tool declaration has no mcpSessionId argument, which stateless MCP needs.
        print("  MCP protocol: stateful (--computer-use-interface does not support stateless MCP yet)")
        protocol = MCP_PROTOCOL_STATEFUL
    if protocol == MCP_PROTOCOL_STATELESS and getattr(args, 'expire_session_on_exit', False):
        # The expire header acts on the session DELETE, and stateless MCP sends no DELETE.
        print("  MCP protocol: stateful (--expire-session-on-exit does not support stateless MCP yet)")
        protocol = MCP_PROTOCOL_STATEFUL
    return protocol


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

    mcp_protocol = _resolve_protocol(args)
    mcp_profile = getattr(args, 'mcp_profile', None)
    mcp_region = resolve_mcp_region(args)
    mcp_service = DEFAULT_MCP_SERVICE
    if not mcp_service:
        raise RuntimeError("AWS_SERVICE_NAME is required.")
    endpoint = resolve_mcp_endpoint(args)
    _log_server_session_id()
    auth = SigV4HttpAuth(mcp_service, mcp_region, profile=mcp_profile)

    # Check for Domain Join mode (SAML assertion)
    saml_assertion = getattr(args, 'saml_assertion', None)
    if saml_assertion:
        stack_arn = getattr(args, 'stack_arn', None)
        if not stack_arn:
            raise RuntimeError("--stack-arn is required for Domain Join mode.")

        print(f"  MCP transport: Domain Join via _meta ({endpoint}, signed for {mcp_service}/{mcp_region}, "
              f"{mcp_protocol})" + _EXPIRE_NOTE * bool(session_headers(args)))
        sys.stdout.flush()

        auth = DomainJoinAuth(auth, build_meta(saml_assertion, stack_arn))
        headers = request_headers(args)
    else:
        # Standard path: streaming URL as header
        streaming_url = getattr(args, 'streaming_url', None) or ""

        print(f"  MCP transport: remote ({endpoint}, signed for {mcp_service}/{mcp_region}, {mcp_protocol})"
              + _EXPIRE_NOTE * bool(session_headers(args)))
        sys.stdout.flush()
        headers = {
            "X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url,
            **request_headers(args),
        }

    connect = _streamable_http(endpoint, headers, auth)

    def factory(on_message=None):
        return _end_session_on_failure(endpoint, connect, on_message=on_message)

    factory.records_session_ids = True
    factory.mcp_protocol = mcp_protocol
    return factory


# Set while a stateful MCPClient starts. strands runs the connection in a background thread
# started with a copy of the caller's context, so the value reaches the negotiation below.
_FORCE_STATEFUL = contextvars.ContextVar("force_stateful_mcp", default=False)


async def _negotiate_session(session):
    """strands' negotiation (stateless when the server has 2026-07-28, else initialize), except a
    stateful client uses the initialize handshake directly."""
    if _FORCE_STATEFUL.get():
        await session.initialize()
        return session.instructions, session.server_capabilities
    return await _strands_negotiate_session(session)


# strands has no public way to choose the protocol, so this patches its negotiate_session and reads a few
# strands internals (checked below and in _SanitizedMCPClient). Fail at import, not quietly at runtime,
# when a strands bump renames them. Check these before bumping strands-agents.
_CALL_TOOL_PARAMS = ("tool_use_id", "name", "arguments")
if not callable(getattr(_strands_mcp_client, "negotiate_session", None)):
    raise ImportError("strands.tools.mcp.mcp_client.negotiate_session is missing: this strands-agents version "
                      "is not supported by lib/mcp_client.py (see the strands-agents pin in requirements.in)")
for _method in (MCPClient.call_tool_sync, MCPClient.call_tool_async):
    if tuple(inspect.signature(_method).parameters)[1:4] != _CALL_TOOL_PARAMS:
        raise ImportError(f"strands MCPClient.{_method.__name__} no longer takes {_CALL_TOOL_PARAMS}: this "
                          "strands-agents version is not supported by lib/mcp_client.py")

_strands_negotiate_session = _strands_mcp_client.negotiate_session
_strands_mcp_client.negotiate_session = _negotiate_session


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


def _tool_call(method, args, kwargs):
    """The tool name and mcpSessionId argument of a call_tool_sync/async call, bound by strands' signature."""
    bound = inspect.signature(method).bind_partial(None, *args, **kwargs).arguments
    arguments = bound.get("arguments")
    return bound.get("name"), arguments.get(SESSION_ARG) if isinstance(arguments, dict) else None


def _result_text(result):
    """The text of a tool result, for warnings."""
    if not isinstance(result, dict):
        return str(result)
    texts = [b.get("text", "") for b in result.get("content") or [] if isinstance(b, dict)]
    return " ".join(t for t in texts if t) or result.get("status", "unknown")


async def _close_over_new_transport(transport_callable, session_id):
    """close_session for stateless session ``session_id`` over a new short-lived connection.

    Used when the client that opened the session is no longer running. Returns a result in the
    call_tool_sync form ({"status": ..., "content": [{"text": ...}]}).
    """
    with anyio.fail_after(CLOSE_TIMEOUT.total_seconds()):
        async with transport_callable() as (read_stream, write_stream, *_):
            async with ClientSession(read_stream, write_stream) as session:
                await _strands_negotiate_session(session)
                result = await session.call_tool(CLOSE_SESSION_TOOL, {SESSION_ARG: session_id})
    texts = [{"text": c.text} for c in result.content if getattr(c, "text", None)]
    return {"status": "error" if result.is_error else "success", "content": texts}


class _SanitizedMCPClient(MCPClient):
    """MCPClient that sanitizes tool names for Bedrock compatibility.

    Bedrock Converse requires tool names to match [a-zA-Z0-9_-]+ (max 64).
    Forwarded MCP tools use dots, which must be replaced with dashes. Tools are
    re-wrapped through the public ``MCPAgentTool(name_override=...)`` constructor;
    the original MCP name is still what goes over the wire to the server. Every tool also gets
    a per-call timeout (``tool_timeout`` seconds).
    """

    tool_timeout = None

    def __init__(self, transport_callable, *args, mcp_protocol=MCP_PROTOCOL_STATELESS, **kwargs):
        if getattr(transport_callable, "records_session_ids", False) is True:
            transport_callable = functools.partial(transport_callable, on_message=self._record_message)
        super().__init__(transport_callable, *args, **kwargs)
        if not hasattr(self, "_transport_callable"):
            raise RuntimeError("strands MCPClient has no _transport_callable: this strands-agents version is not "
                               "supported by lib/mcp_client.py")
        self._mcp_protocol = mcp_protocol
        # Every mcpSessionId the server returned and that is not closed yet. A list, as a model can open
        # two sessions with parallel NEW calls.
        self._session_lock = threading.Lock()
        self._mcp_session_ids = []

    def start(self):
        token = _FORCE_STATEFUL.set(self._mcp_protocol == MCP_PROTOCOL_STATEFUL)
        try:
            result = super().start()
        finally:
            _FORCE_STATEFUL.reset(token)
        if not hasattr(self, "_background_thread_session"):
            raise RuntimeError("strands MCPClient has no _background_thread_session: this strands-agents version is "
                               "not supported by lib/mcp_client.py")
        version = getattr(self._background_thread_session, "protocol_version", None)
        if version and version != STATELESS_PROTOCOL_VERSION and self._mcp_protocol == MCP_PROTOCOL_STATELESS:
            # The endpoint has no 2026-07-28, so the client fell back to stateful: no close_session tool,
            # and the session ends with the DELETE on stop like any stateful session.
            self._mcp_protocol = MCP_PROTOCOL_STATEFUL
        return result

    @property
    def is_stateless(self):
        """True when this client runs stateless MCP, after any fallback to stateful at connect."""
        return self._mcp_protocol == MCP_PROTOCOL_STATELESS

    async def call_tool_async(self, *args, **kwargs):
        result = await super().call_tool_async(*args, **kwargs)
        self._after_call(*_tool_call(MCPClient.call_tool_async, args, kwargs), result)
        return result

    def call_tool_sync(self, *args, **kwargs):
        result = super().call_tool_sync(*args, **kwargs)
        self._after_call(*_tool_call(MCPClient.call_tool_sync, args, kwargs), result)
        return result

    def _open_session_ids(self):
        with self._session_lock:
            return list(self._mcp_session_ids)

    def _after_call(self, name, session_arg, result):
        """Track the session after a tool call.

        The server adds the mcpSessionId block to every result, close_session's too, so a close is not
        remembered. A session is forgotten only when close_session succeeded for it.
        """
        ok = isinstance(result, dict) and result.get("status") == "success"
        if name in (CLOSE_SESSION_TOOL, "close_session"):
            if ok:
                self._forget(session_arg)
            return
        self._remember_session_id(result)

    def _remember(self, session_id):
        if self._mcp_protocol != MCP_PROTOCOL_STATELESS:
            return
        if not isinstance(session_id, str) or not session_id or session_id == NEW_SESSION:
            return
        with self._session_lock:
            if session_id in self._mcp_session_ids:
                return
            self._mcp_session_ids.append(session_id)
        sys.stdout.write(f"  MCP session (stateless): {session_id}\n")
        sys.stdout.flush()

    def _forget(self, session_id):
        with self._session_lock:
            if session_id in self._mcp_session_ids:
                self._mcp_session_ids.remove(session_id)

    def _record_message(self, message):
        """Record the mcpSessionId from a raw JSON-RPC message: result.mcpSessionId, or error.data.mcpSessionId
        when the call failed on a session the server already created."""
        if isinstance(message, JSONRPCResponse):
            holder = message.result
        elif isinstance(message, JSONRPCError):
            holder = message.error.data
        else:
            return
        if isinstance(holder, dict):
            self._remember(holder.get(SESSION_ARG))

    def _remember_session_id(self, result):
        """Keep the mcpSessionId from the {"mcpSessionId": ...} text block the server adds to stateless results."""
        if self._mcp_protocol != MCP_PROTOCOL_STATELESS or not isinstance(result, dict):
            return
        for block in result.get("content") or []:
            text = block.get("text") if isinstance(block, dict) else None
            if not text or SESSION_ARG not in text:
                continue
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                self._remember(value.get(SESSION_ARG))

    def close_stateless_session(self):
        """Close every open stateless session from code, eg- on exit or error before the agent called close_session.

        A stateless client sends no DELETE when it stops. Closing an already closed session returns closed.
        """
        if self._mcp_protocol != MCP_PROTOCOL_STATELESS:
            return
        for session_id in self._open_session_ids():
            try:
                try:
                    # Short timeout: this also runs on Ctrl-C and at exit, which must not hang on a slow network.
                    result = self.call_tool_sync(f"close-{session_id}", CLOSE_SESSION_TOOL,
                                                 {SESSION_ARG: session_id}, read_timeout_seconds=CLOSE_TIMEOUT)
                except MCPClientInitializationError:
                    # The client is no longer running, eg- a dropped connection ended the mcp 2.x transport.
                    # The session is still open on the service and holds the desktop, so close it over a new
                    # short-lived connection; otherwise the next NEW call is refused until the service times it out.
                    result = anyio.run(_close_over_new_transport, self._transport_callable, session_id)
                if isinstance(result, dict) and result.get("status") == "success":
                    sys.stdout.write(f"  Closed stateless MCP session {session_id}\n")
                    self._forget(session_id)  # kept on failure, so the next close tries it again
                else:
                    sys.stdout.write(f"  Warning: could not close stateless MCP session {session_id}: "
                                     f"{_result_text(result)}\n")
            except Exception as e:  # best effort: the session also ends when the streaming URL expires
                sys.stdout.write(f"  Warning: could not close stateless MCP session {session_id}: {e}\n")
        sys.stdout.flush()

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


def mcp_protocol_of(mcp_factory):
    """The MCP protocol a factory from :func:`create_mcp_client_factory` was made for (stateless if unset)."""
    return getattr(mcp_factory, "mcp_protocol", None) or MCP_PROTOCOL_STATELESS


def build_mcp_client(mcp_factory, startup_timeout, label=None, tool_timeout=DEFAULT_TOOL_TIMEOUT):
    """Construct an MCPClient with tool name sanitization and a per-call timeout (0 = none).

    The client uses ``mcp_factory.mcp_protocol`` (stateless if not set). The server-side MCP session
    ID of a stateful session is logged by the ``mcp.client.streamable_http`` handler installed in
    :func:`create_mcp_client_factory`. ``label`` is accepted for callers written against the earlier
    signature and is not used.
    """
    client = _SanitizedMCPClient(mcp_factory, startup_timeout=startup_timeout,
                                 mcp_protocol=mcp_protocol_of(mcp_factory))
    client.tool_timeout = tool_timeout
    _open_clients.add(client)
    return client


_open_clients = weakref.WeakSet()


def close_mcp_clients(clients=None, timeout=15):
    """Close MCP clients built here (all of them by default) so each session ends.

    A stateful session ends with the HTTP DELETE the client sends when it stops. A stateless session
    is not ended by that, so its open sessions are first closed with close_session (10 s each).

    Strands skips this clean-up once the interpreter is shutting down, so a process that simply
    exited left its session attached on the service, and the next connection to the same desktop was
    refused with "Another agentic client is already connected" until the service timed it out. Each
    client is stopped on a helper thread and given ``timeout`` seconds, plus 10 s for each open
    stateless session it still has to close, so a hung connection cannot hold up the exit. Never raises.
    """
    for client in list(_open_clients if clients is None else clients):
        try:
            _open_clients.discard(client)
        except TypeError:           # not weak-referenceable: it was never registered
            pass
        open_ids = getattr(client, "_open_session_ids", None)
        try:
            pending = len(open_ids()) if callable(open_ids) else 0
        except Exception:
            pending = 0
        worker = threading.Thread(target=_stop_quietly, args=(client,), daemon=True)
        worker.start()
        worker.join(timeout + pending * CLOSE_TIMEOUT.total_seconds())
        try:
            if pending and not worker.is_alive() and open_ids():
                # A close failed: try it once more now, over a new connection. Not at exit, since a new
                # connection can no longer be opened once the interpreter is shutting down.
                worker = threading.Thread(target=_stop_quietly, args=(client,), daemon=True)
                worker.start()
                worker.join(timeout + len(open_ids()) * CLOSE_TIMEOUT.total_seconds())
        except Exception:
            pass


def close_stateless_sessions():
    """Close the open stateless sessions of every MCP client built here, and keep the clients running.

    For a conversation reset (eg- the terminal's /clear): the model forgets the mcpSessionId, so its next
    task opens a new session with NEW, and the old one would stay open and hold the desktop. Never raises.
    """
    for client in list(_open_clients):
        try:
            client.close_stateless_session()
        except Exception:
            pass


def _stop_quietly(client):
    try:
        close = getattr(client, "close_stateless_session", None)
        if close is not None:
            close()
    except Exception:
        pass
    try:
        client.stop(None, None, None)
    except Exception:
        pass


atexit.register(close_mcp_clients)


class _CancellationNoise(_logging.Filter):
    """Drops the console noise a deliberate cancel (Ctrl-C, a run limit) used to print.

    Cancelling an in-flight desktop action makes Strands log a traceback for its own
    "tool execution cancelled" exception. It is expected and says nothing a user can act on;
    every other record passes.
    """

    def filter(self, record):
        info = record.exc_info
        return not (info and info[0] is not None and info[0].__name__ == "_MCPCallCancelledError")


_logging.getLogger("strands.tools.mcp.mcp_client").addFilter(_CancellationNoise())

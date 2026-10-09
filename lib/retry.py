# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Retry logic and error handling for MCP agent connections."""

import sys
import time

import httpx
from mcp.shared.exceptions import McpError

from .agent_factory import (AUTH_SAML, DomainJoinRejected, RunInterrupted, RunLimitReached, agent_hooks,
                            agent_options, get_auth_mode, make_agent)
from .mcp_client import DEFAULT_TOOL_TIMEOUT, build_mcp_client, close_mcp_clients, resolve_mcp_endpoint
from .saml_assertion import redact

# Domain Join: a SAML assertion is short-lived (identity providers typically allow a few minutes)
# and its clock started when the provider issued it, before this process did. Retrying for
# longer than this is unlikely to help.
SAML_RETRY_WINDOW_SECONDS = 4 * 60
# HTTP statuses that no amount of waiting fixes for Domain Join: a bad or expired assertion,
# a stack ARN from another account, missing IAM permissions.
_SAML_NON_RETRYABLE_STATUSES = (400, 401, 403)
_MAX_SERVICE_MESSAGE = 500   # characters of a service error body worth printing


def _is_retryable_error(error_str):
    """Check if an error is a retryable MCP/connection error."""
    lower = error_str.lower()
    return any(pattern in lower for pattern in [
        "timed out", "initialization", "channel not connected",
        "connection to the mcp server was closed", "mcperror",
        "client_disconnected",
        "401 unauthorized", "iserror", "tools\nfield required",
        "did not list its tools",   # an error result for tools/list, e.g. while the desktop starts
        "length limit exceeded",
    ])


def _print_connection_limit():
    """Print guidance when another agentic client holds the desktop."""
    print("\n  Another agentic client is connected to this desktop: another agent, or an earlier run")
    print("  whose session was never ended (for example a process killed with kill -9). The desktop")
    print("  is free again once that client disconnects or the service ends its session; see")
    print("  \"CONNECTION_LIMIT_REACHED\" in the README's Troubleshooting section.")


def _is_client_disconnected(error_str):
    """Check if the error indicates the MCP client was disconnected."""
    return "client_disconnected" in error_str.lower()


def _print_client_disconnected(args=None):
    """Print guidance when a client_disconnected error is received."""
    saml = args is not None and get_auth_mode(args) == AUTH_SAML
    print("\n  ⚠️  Received 'client_disconnected' from the MCP server.")
    print("  This usually means the agent session was stopped — for example:")
    print("    • The user or orchestrator terminated the session")
    print("    • The SAML assertion expired or was revoked" if saml
          else "    • The streaming URL expired or was revoked")
    print("    • The AppStream session timed out")
    print("  If this was unexpected, retrying with a fresh SAML assertion may help." if saml
          else "  If this was unexpected, retrying with a fresh streaming URL may help.")


def find_http_rejection(error):
    """Return ``(status, message)`` for the HTTP error behind ``error``, or ``None``.

    Strands reports a failed connection as "the client initialization failed: unhandled
    errors in a TaskGroup"; the HTTP status and the service's own message are several
    ``__cause__`` / ``ExceptionGroup`` levels down, so walk the whole chain.
    """
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, DomainJoinRejected):
            return current.status, current.message
        if isinstance(current, McpError) and 400 <= current.error.code <= 599:
            # mcp-proxy-for-aws answers a failed POST with a JSON-RPC error whose code is the
            # HTTP status and whose message is "HTTP 400 Bad Request from <url>: <body>".
            return current.error.code, current.error.message
        if isinstance(current, httpx.HTTPStatusError):
            response = current.response
            try:
                body = response.text.strip()
            except Exception:  # streamed response that was never read
                body = ""
            message = f"HTTP {response.status_code} {response.reason_phrase} from {response.url}"
            return response.status_code, f"{message}: {body}" if body else message
        children = [current.__cause__]
        if not current.__suppress_context__:   # `raise X from None` hides the context on purpose
            children.append(current.__context__)
        children.extend(getattr(current, "exceptions", ()))
        pending.extend(reversed(children))     # pop() takes from the end: __cause__ is visited first
    return None


def root_cause(error):
    """The innermost exception behind ``error`` (following ``__cause__`` and ``ExceptionGroup``)."""
    current, seen = error, set()
    while id(current) not in seen:
        seen.add(id(current))
        group = getattr(current, "exceptions", None)
        if group:
            current = group[0]
        elif current.__cause__ is not None:
            current = current.__cause__
        else:
            break
    return current


def failure_text(error):
    """``str(error)``, plus the root cause when the wrapper (Strands' "unhandled errors in a
    TaskGroup") hides it, or the service's own message when it refused an HTTP request."""
    rejection = find_http_rejection(error)
    if rejection:
        return rejection[1]
    text = str(error)
    cause = root_cause(error)
    detail = str(cause).strip()
    if cause is not error and detail and detail not in text:
        text = f"{text}\n  Cause: {type(cause).__name__}: {detail}"
    return text


def _is_from_mcp_endpoint(message, args):
    """True if an HTTP error message names the Agent Access endpoint (not, say, a model API)."""
    return bool(getattr(args, "mcp_endpoint", None)) and resolve_mcp_endpoint(args).rstrip("/") in message


def _print_domain_join_rejection():
    """Print guidance for a Domain Join request the service refused outright."""
    print("\n  Domain Join was rejected. Please check:")
    print("    1. The SAML assertion is fresh. Assertions are short-lived: get a new one and")
    print("       start the agent right away.")
    print("    2. --stack-arn is a stack in the AWS account of the credentials that sign the")
    print("       request (--mcp-profile, or your default credentials).")
    print("    3. The assertion is the base64 SAML Response exactly as the identity provider")
    print("       issued it.")


def _print_connection_error(args):
    """Print connection troubleshooting guidance."""
    saml = get_auth_mode(args) == AUTH_SAML
    print("\n  The Agent Access MCP Server failed to connect. Please check:")
    print(f"    1. The endpoint URL is correct: {getattr(args, 'mcp_endpoint', 'not set')}")
    print("    2. Your AWS credentials have access to the Agent Access MCP Server")
    print("    3. The SAML assertion is still valid and --stack-arn is right" if saml
          else "    3. The streaming URL is valid and not expired")
    print(f"    4. Your AWS region ({args.region}) matches the fleet region")
    if getattr(args, 'mcp_profile', None):
        print(f"    5. The AWS profile '{args.mcp_profile}' is configured correctly")


def _print_bedrock_error(args):
    """Print Bedrock auth troubleshooting guidance."""
    print("\n  AWS Bedrock authentication failed. Please check:")
    print("    1. You are signed in to AWS (aws sso login --profile <your-profile>)")
    print(f"    2. Your credentials have Bedrock access in {args.region}")
    print("    3. If using env vars, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_SESSION_TOKEN are set")
    if getattr(args, 'llm_profile', None):
        print(f"    4. The LLM profile '{args.llm_profile}' is configured correctly")


def _should_retry_after_error(e, attempt, max_retries, args, started=None):
    """Report a failed connection attempt and say whether the caller should retry.

    A retryable error with attempts left sleeps with linear back-off and returns
    True. Anything else prints the error plus troubleshooting guidance and returns
    False. Shared by every retry loop in this module so they cannot drift apart.

    Domain Join (SAML) runs give up sooner: the service refusing the assertion outright
    (HTTP 400/401/403) cannot be fixed by retrying, and ``started`` (``time.monotonic()``
    of the first attempt) bounds the retrying to the assertion's short lifetime.
    """
    error = str(e)
    rejection = find_http_rejection(e)
    saml = get_auth_mode(args) == AUTH_SAML
    rejected = (saml and rejection is not None and rejection[0] in _SAML_NON_RETRYABLE_STATUSES
                and _is_from_mcp_endpoint(rejection[1], args))
    wait = 10 * attempt
    window_closed = (saml and started is not None
                     and time.monotonic() - started + wait >= SAML_RETRY_WINDOW_SECONDS)

    if _is_client_disconnected(error):
        _print_client_disconnected(args)
    if _is_retryable_error(error) and attempt < max_retries and not rejected and not window_closed:
        sys.stdout.write(f"\n  ⏳ Connection lost — the session may still be starting. Retrying in {wait}s...\n")
        sys.stdout.flush()
        time.sleep(wait)
        return True

    shown = redact(failure_text(e))
    print(f"\n\n✗ Error: {shown[:_MAX_SERVICE_MESSAGE] + '…' if len(shown) > _MAX_SERVICE_MESSAGE else shown}")
    if _is_client_disconnected(error):
        pass
    elif "connection_limit_reached" in error.lower():
        _print_connection_limit()
    elif rejected:
        _print_domain_join_rejection()
    elif window_closed and _is_retryable_error(error):
        print("\n  Stopped retrying: SAML assertions are short-lived, so this one has probably")
        print("  expired. Get a fresh assertion and run the agent again.")
        _print_connection_error(args)
    elif _is_retryable_error(error):
        _print_connection_error(args)
    elif "signature" in error.lower() and "thinking" in error.lower():
        print("\n  Bedrock rejected the model's earlier thinking blocks. This agent removes them from the")
        print("  history before each call, so this should not happen; please report it with the message")
        print("  above. Running with --no-screenshot-pruning avoids most edits to the history meanwhile.")
    elif any(s in error.lower() for s in ["bedrock", "credential", "unrecognizedclientexception",
                                           "accessdeniedexception", "expiredtokenexception"]):
        _print_bedrock_error(args)
    return False


def run_agent_with_retry(args, mcp_factory, model, system_prompt, task_prompt, agent_logger, conversation_manager=None):
    """Run an agent with MCP connection retry logic.

    Returns (success, error, result) tuple.
    """
    from .agent_common import setup_signal_handler, print_handler

    setup_signal_handler(agent_logger)

    max_retries = args.mcp_retries
    success = False
    error = None
    result = None
    started = time.monotonic()

    for attempt in range(1, max_retries + 1):
        sys.stdout.write(f"  Connecting to desktop (attempt {attempt}/{max_retries})...\n")
        sys.stdout.flush()

        connecting = True
        mcp_client = None
        try:
            mcp_client = build_mcp_client(mcp_factory, args.mcp_timeout,
                                          tool_timeout=getattr(args, 'tool_timeout', DEFAULT_TOOL_TIMEOUT))
            agent = make_agent(
                model, system_prompt, mcp_client,
                conversation_manager=conversation_manager,
                hooks=agent_hooks(args, agent_logger),
                callback_handler=print_handler,
                **agent_options(args),
            )
            connecting = False

            result = agent(task_prompt)
            success = True
            error = None   # an earlier attempt's failure is not this run's
            print("\n\n✓ Completed")
            break

        except (KeyboardInterrupt, RunInterrupted):
            error = "Interrupted"
            print("\n\n⚠️  Interrupted")
            break
        except RunLimitReached as e:          # retrying would only spend the same budget again
            error = str(e)
            print(f"\n\n✗ Stopped: {e}")
            break
        except Exception as e:
            error = redact(failure_text(e))
            if mcp_client is not None:
                close_mcp_clients([mcp_client])    # a leaked connection would block the retry
            # the assertion-lifetime window bounds (re)connecting, not a task that was already running
            if _should_retry_after_error(e, attempt, max_retries, args, started=started if connecting else None):
                continue
            break

    return success, error, result


def create_agent_with_retry(args, mcp_factory, model, system_prompt, agent_logger, conversation_manager=None,
                            callback_handler=None, extra_hooks=()):
    """Create an Agent with MCP connection retry (without running a task).

    ``callback_handler`` replaces the plain stdout printer and ``extra_hooks`` are added to the agent's
    hooks (the interactive terminal uses both). Returns (agent, error) tuple.
    """
    from .agent_common import setup_signal_handler, print_handler

    setup_signal_handler(agent_logger)

    max_retries = args.mcp_retries
    error = None
    started = time.monotonic()

    for attempt in range(1, max_retries + 1):
        sys.stdout.write(f"  Connecting to desktop (attempt {attempt}/{max_retries})...\n")
        sys.stdout.flush()

        mcp_client = None
        try:
            mcp_client = build_mcp_client(mcp_factory, args.mcp_timeout,
                                          tool_timeout=getattr(args, 'tool_timeout', DEFAULT_TOOL_TIMEOUT))
            agent = make_agent(
                model, system_prompt, mcp_client,
                conversation_manager=conversation_manager,
                hooks=agent_hooks(args, agent_logger, *extra_hooks),
                callback_handler=callback_handler or print_handler,
                **agent_options(args),
            )
            return agent, None

        except KeyboardInterrupt:
            return None, "Interrupted"
        except Exception as e:
            error = redact(failure_text(e))
            if mcp_client is not None:
                close_mcp_clients([mcp_client])    # a leaked connection would block the retry
            if _should_retry_after_error(e, attempt, max_retries, args, started=started):
                continue
            return None, error

    return None, error

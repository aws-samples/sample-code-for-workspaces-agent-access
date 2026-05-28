# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Retry logic and error handling for MCP agent connections."""

import sys
import time

from strands import Agent

from .mcp_client import build_mcp_client


def _is_retryable_error(error_str):
    """Check if an error is a retryable MCP/connection error."""
    lower = error_str.lower()
    return any(pattern in lower for pattern in [
        "timed out", "initialization", "channel not connected",
        "connection to the mcp server was closed", "mcperror",
        "client_disconnected",
        "401 unauthorized", "iserror", "tools\nfield required",
        "length limit exceeded",
    ])


def _is_client_disconnected(error_str):
    """Check if the error indicates the MCP client was disconnected."""
    return "client_disconnected" in error_str.lower()


def _print_client_disconnected():
    """Print guidance when a client_disconnected error is received."""
    print("\n  ⚠️  Received 'client_disconnected' from the MCP server.")
    print("  This usually means the agent session was stopped — for example:")
    print("    • The user or orchestrator terminated the session")
    print("    • The streaming URL expired or was revoked")
    print("    • The AppStream session timed out")
    print("  If this was unexpected, retrying with a fresh streaming URL may help.")


def _print_connection_error(args):
    """Print connection troubleshooting guidance."""
    print("\n  The Agent Access MCP Server failed to connect. Please check:")
    print(f"    1. The endpoint URL is correct: {getattr(args, 'mcp_endpoint', 'not set')}")
    print("    2. Your AWS credentials have access to the Agent Access MCP Server")
    print("    3. The streaming URL is valid and not expired")
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

    for attempt in range(1, max_retries + 1):
        sys.stdout.write(f"  Connecting to desktop (attempt {attempt}/{max_retries})...\n")
        sys.stdout.flush()

        try:
            mcp_client = build_mcp_client(mcp_factory, args.mcp_timeout)
            agent = Agent(
                model=model,
                tools=[mcp_client],
                system_prompt=system_prompt,
                conversation_manager=conversation_manager,
                hooks=[agent_logger],
                callback_handler=print_handler,
            )

            result = agent(task_prompt)
            success = True
            print("\n\n✓ Completed")
            break

        except KeyboardInterrupt:
            error = "Interrupted"
            print("\n\n⚠️  Interrupted")
            break
        except Exception as e:
            error = str(e)
            if _is_client_disconnected(error):
                _print_client_disconnected()
            if _is_retryable_error(error) and attempt < max_retries:
                wait = 10 * attempt
                sys.stdout.write(f"\n  ⏳ Connection lost — the session may still be starting. Retrying in {wait}s...\n")
                sys.stdout.flush()
                time.sleep(wait)
                continue

            print(f"\n\n✗ Error: {e}")
            if _is_client_disconnected(error):
                pass
            elif _is_retryable_error(error):
                _print_connection_error(args)
            elif any(s in error.lower() for s in ["bedrock", "credential", "unrecognizedclientexception",
                                           "accessdeniedexception", "expiredtokenexception"]):
                _print_bedrock_error(args)
            break

    return success, error, result


def create_agent_with_retry(args, mcp_factory, model, system_prompt, agent_logger, conversation_manager=None):
    """Create an Agent with MCP connection retry (without running a task).

    Returns (agent, error) tuple.
    """
    from .agent_common import setup_signal_handler, print_handler

    setup_signal_handler(agent_logger)

    max_retries = args.mcp_retries
    error = None

    for attempt in range(1, max_retries + 1):
        sys.stdout.write(f"  Connecting to desktop (attempt {attempt}/{max_retries})...\n")
        sys.stdout.flush()

        try:
            mcp_client = build_mcp_client(mcp_factory, args.mcp_timeout)
            agent = Agent(
                model=model,
                tools=[mcp_client],
                system_prompt=system_prompt,
                conversation_manager=conversation_manager,
                hooks=[agent_logger],
                callback_handler=print_handler,
            )
            return agent, None

        except KeyboardInterrupt:
            return None, "Interrupted"
        except Exception as e:
            error = str(e)
            if _is_client_disconnected(error):
                _print_client_disconnected()
            if _is_retryable_error(error) and attempt < max_retries:
                wait = 10 * attempt
                sys.stdout.write(f"\n  ⏳ Connection lost — the session may still be starting. Retrying in {wait}s...\n")
                sys.stdout.flush()
                time.sleep(wait)
                continue

            print(f"\n\n✗ Error: {e}")
            if _is_client_disconnected(error):
                pass
            elif _is_retryable_error(error):
                _print_connection_error(args)
            elif any(s in error.lower() for s in ["bedrock", "credential", "unrecognizedclientexception",
                                           "accessdeniedexception", "expiredtokenexception"]):
                _print_bedrock_error(args)
            return None, error

    return None, error

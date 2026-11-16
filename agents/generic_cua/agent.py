#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generic Computer-Use Agent - Interactive or single-shot tasks on a remote desktop."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from lib import agent_common
from lib import mcp_client
from lib import replay_session
from lib import streaming_url
from lib import terminal_ui
from lib.agent_factory import RunInterrupted, RunLimitReached
from lib.saml_assertion import parse_stack_arn


def checked_replay_delivery(parser, args):
    """Where --save-replay looks for recordings, or None without --save-replay.

    Checked before the task runs, so a wrong stack, region or profile is reported up front. Exits
    through ``parser.error`` when the stack can't be told or its bucket can't be read.
    """
    if not args.save_replay:
        return None
    delivery = _delivery_target(parser, args)
    try:
        replay_session.check_delivery(delivery)
    except replay_session.RecordingNotRetrieved as exc:
        parser.error(f"--save-replay: {exc}")
    return delivery


def _delivery_target(parser, args):
    """The stack the desktop runs on, in its own Region: the --stack-arn stack on Domain Join, else the
    stack the --streaming-url was created for. It is read with the --mcp-profile identity (the stack
    is in its account). Exits through ``parser.error`` when the stack can't be told."""
    try:
        if args.stack_arn:
            region, stack_name = parse_stack_arn(args.stack_arn)
        else:
            region, stack_name = streaming_url.stack_of(args.streaming_url)
    except ValueError as exc:
        parser.error(f"--save-replay: {exc}")
    return replay_session.DeliveryBucket(region=region, stack_name=stack_name, profile=args.mcp_profile)


def finalize_and_save_replay(agent_logger, delivery, success, error, result=None,
                             wait_seconds=replay_session.DEFAULT_WAIT_SECONDS):
    """End the MCP session and write the metrics; with a ``delivery`` bucket, then download the run's
    recordings, waiting up to ``wait_seconds`` in all for the sessions that ran a tool call.

    Returns the task's exit code: a recording that doesn't arrive is reported with the command to
    fetch it later, and doesn't change it.
    """
    exit_code = agent_common.finalize_and_exit(agent_logger, success, error, result)
    if delivery:
        replay_session.save_recordings(delivery, mcp_client.server_sessions(), wait_seconds)
    return exit_code


def main():
    parser = agent_common.create_base_parser('Generic Computer-Use Agent')
    parser.add_argument('--system-prompt', type=str, default=None,
                       help='Path to custom system prompt markdown file')
    parser.add_argument('--task-prompt', type=str, default=None,
                       help='Path to task prompt file; triggers single-shot mode')
    parser.add_argument('--skill', type=str, default=None,
                       help='Path to skill JSON file to append to system prompt')
    parser.add_argument('--plain', action='store_true', default=False,
                       help='Interactive sessions: plain text output instead of the terminal interface '
                            '(always plain when input or output is not a terminal)')
    parser.add_argument('--save-replay', action='store_true', default=False,
                       help='After the session ends, wait for its Replay Session recording and download it '
                            'to recordings/<session-id>/ (the stack needs Replay Session on: '
                            'scripts/setup_replay_session.sh)')
    args = parser.parse_args()
    agent_common.resolve_session_auth(parser, args)
    delivery = checked_replay_delivery(parser, args)

    agent_dir = os.path.dirname(os.path.abspath(__file__))

    # Bound CLI-supplied prompt/skill paths so an attacker can't read arbitrary
    # files and exfiltrate them into the LLM system prompt. The agent_dir and
    # the repo's top-level prompts/skills directories are the only legal roots.
    repo_root = os.path.dirname(os.path.dirname(agent_dir))
    allowed_roots = [
        agent_dir,
        os.path.join(repo_root, "agents"),
        os.path.join(repo_root, "skills"),
    ]

    def _validate(path, label):
        if not path:
            return
        real = os.path.realpath(path)
        if not any(
            real == os.path.realpath(r) or real.startswith(os.path.realpath(r) + os.sep)
            for r in allowed_roots
        ):
            parser.error(
                f"{label} {path!r} is outside the allowed prompt/skill roots "
                f"(must be under one of: {allowed_roots})"
            )

    _validate(args.system_prompt, "--system-prompt")
    _validate(args.task_prompt, "--task-prompt")
    _validate(args.skill, "--skill")

    agent_common.print_banner(
        "Generic Computer-Use Agent",
        "An interactive agent that performs tasks\n"
        "on a remote Windows desktop via DCV.",
        args.model_id, args,
    )
    if args.system_prompt:
        sys.stdout.write(f"  System Prompt: {args.system_prompt}\n")
    if args.skill:
        sys.stdout.write(f"  Skill: {args.skill}\n")

    # --skill is a full path here, not a filename under agent_dir/skills/.
    # Copy it into a temp location so setup_standard_agent's convention works, or
    # inline the skill content after setup. Simplest: read and append ourselves.
    setup = agent_common.setup_standard_agent(
        args, agent_dir,
        system_prompt_path=args.system_prompt,
        task_prompt_path=args.task_prompt,
    )

    if args.skill:
        import json
        try:
            with open(args.skill, 'r') as f:
                skill = json.load(f)
            setup["system_prompt"] += f"\n\n=== SKILL ===\n{json.dumps(skill, indent=2)}\n"
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"  Warning: Could not load skill from '{args.skill}': {e}")

    # An interactive session on a terminal gets the terminal interface (lib/terminal_ui.py).
    ui = None
    if setup["task_prompt"] is None and terminal_ui.wanted(args):
        ui = terminal_ui.TerminalUI(args.model_id, args.region, setup["agent_logger"],
                                    call_namer=setup["agent_logger"].call_namer)
        setup["agent_logger"].display_sink = ui.on_action

    # Create agent (with retry) — don't run task yet
    agent, error = agent_common.create_agent_with_retry(
        args, setup["mcp_factory"], setup["model"],
        setup["system_prompt"], setup["agent_logger"], setup["conv_manager"],
        callback_handler=ui.callback_handler if ui else None,
        extra_hooks=[ui] if ui else (),
    )
    if agent is None:
        # every session was a failed connection attempt: check for recordings once, without waiting
        return finalize_and_save_replay(setup["agent_logger"], delivery, False, error, wait_seconds=0)

    # Execute
    success = False
    result = None
    task_prompt = setup["task_prompt"]

    if task_prompt is not None:
        try:
            result = agent(task_prompt)
            success = True
            print("\n\n✓ Completed")
        except (KeyboardInterrupt, RunInterrupted):
            error = "Interrupted"
            print("\n\n⚠️  Interrupted")
        except Exception as e:
            error = str(e)
            print(f"\n\n✗ Error: {e}")
    elif ui is not None:
        success = True
        ui.run(agent, interrupted=(KeyboardInterrupt, RunInterrupted, RunLimitReached))
    else:
        # REPL mode, plain output
        success = True
        print("  Type a task and press Enter. Type 'exit' or 'quit' to stop.\n")
        while True:
            try:
                user_input = input("> ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ("exit", "quit"):
                    break
                try:
                    agent(user_input)
                except (KeyboardInterrupt, RunInterrupted):
                    print("\n\n⚠️  Task interrupted. Returning to prompt.\n")
                except Exception as e:
                    print(f"\n\n✗ Error: {e}\n")
            except (EOFError, KeyboardInterrupt):
                print()
                break

    return finalize_and_save_replay(setup["agent_logger"], delivery, success, error,
                                    result if task_prompt else None)


if __name__ == "__main__":
    sys.exit(main())

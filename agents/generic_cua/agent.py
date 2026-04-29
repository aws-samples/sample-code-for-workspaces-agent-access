#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generic Computer-Use Agent - Interactive or single-shot tasks on a remote desktop."""

import os
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from lib import agent_common


def main():
    parser = agent_common.create_base_parser('Generic Computer-Use Agent')
    parser.add_argument('--system-prompt', type=str, default=None,
                       help='Path to custom system prompt markdown file')
    parser.add_argument('--task-prompt', type=str, default=None,
                       help='Path to task prompt file; triggers single-shot mode')
    parser.add_argument('--skill', type=str, default=None,
                       help='Path to skill JSON file to append to system prompt')
    args = parser.parse_args()
    agent_common.resolve_streaming_url(parser, args)

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

    # Create agent (with retry) — don't run task yet
    agent, error = agent_common.create_agent_with_retry(
        args, setup["mcp_factory"], setup["model"],
        setup["system_prompt"], setup["agent_logger"], setup["conv_manager"],
    )
    if agent is None:
        return agent_common.finalize_and_exit(setup["agent_logger"], False, error)

    # Execute
    success = False
    result = None
    task_prompt = setup["task_prompt"]

    if task_prompt is not None:
        try:
            result = agent(task_prompt)
            success = True
            print("\n\n✓ Completed")
        except KeyboardInterrupt:
            error = "Interrupted"
            print("\n\n⚠️  Interrupted")
        except Exception as e:
            error = str(e)
            print(f"\n\n✗ Error: {e}")
    else:
        # REPL mode
        signal.signal(signal.SIGINT, signal.SIG_DFL)
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
                except KeyboardInterrupt:
                    print("\n\n⚠️  Task interrupted. Returning to prompt.\n")
                except Exception as e:
                    print(f"\n\n✗ Error: {e}\n")
            except (EOFError, KeyboardInterrupt):
                print()
                break

    return agent_common.finalize_and_exit(
        setup["agent_logger"], success, error,
        result if task_prompt else None,
    )


if __name__ == "__main__":
    sys.exit(main())

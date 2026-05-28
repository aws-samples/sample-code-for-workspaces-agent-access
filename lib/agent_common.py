# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Common agent infrastructure for WorkSpaces Agent Framework.

This module re-exports from sub-modules for backward compatibility:
- model.py: Bedrock model creation
- mcp_client.py: MCP transport, tool name sanitization
- retry.py: Connection retry logic, error handling
"""

import argparse
import json
import os
import signal
import sys

from strands import Agent

from .strands_logger import StrandsAgentLogger, parse_prompt_frontmatter
from .screenshot_pruning_manager import ScreenshotPruningConversationManager

# Re-exports from sub-modules
from .model import ALLOWED_REGIONS, _supports_converse_images, create_model
from .mcp_client import (
    DEFAULT_MCP_ENDPOINT,
    DEFAULT_MCP_REGION_OVERRIDE,
    DEFAULT_MCP_SERVICE,
    resolve_mcp_region,
    create_mcp_client_factory,
    build_mcp_client,
)
from .retry import (
    _is_retryable_error,
    _is_client_disconnected,
    run_agent_with_retry,
    create_agent_with_retry,
)


def load_prompt(path, allowed_roots=None):
    """Load a prompt file, stripping YAML frontmatter."""
    try:
        if allowed_roots is not None:
            real = os.path.realpath(path)
            roots = [os.path.realpath(r) for r in allowed_roots]
            if not any(
                real == r or real.startswith(r + os.sep) for r in roots
            ):
                raise ValueError(
                    f"path {path!r} is outside the allowed prompt roots"
                )
        with open(path, 'r') as f:
            content = f.read()
        if content.startswith('---'):
            end = content.find('---', 3)
            if end != -1:
                content = content[end + 3:].strip()
        return content
    except ValueError:
        raise
    except Exception as e:
        print(f"Warning: Could not load {path}: {e}")
        return ""


def create_base_parser(description):
    """Create an argument parser with the standard agent arguments."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--streaming-url',
                       help='AppStream streaming URL for the desktop session')
    parser.add_argument('--model-id', default='global.anthropic.claude-sonnet-4-6',
                       help='Bedrock model ID (default: global.anthropic.claude-sonnet-4-6)')
    parser.add_argument('--mcp-timeout', type=int, default=180,
                       help='MCP client startup timeout in seconds (default: 180)')
    parser.add_argument('--mcp-retries', type=int, default=3,
                       help='Number of MCP client connection retries (default: 3)')
    parser.add_argument('--region',
                       default=os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')),
                       help='AWS region for Bedrock (default: auto-detect from environment)')
    parser.add_argument('--no-screenshot-pruning', action='store_true', default=False,
                       help='Disable screenshot pruning from conversation context')
    parser.add_argument('--computer-use-tool', action='store_true', default=True,
                       help=argparse.SUPPRESS)
    parser.add_argument('--no-computer-use-tool', dest='computer_use_tool',
                       action='store_false',
                       help='Disable the Anthropic computer-use-2025-11-24 beta.')
    parser.add_argument('--mcp-endpoint', metavar='URL', default=DEFAULT_MCP_ENDPOINT,
                       help='MCP endpoint URL (default: from config.json)')
    parser.add_argument('--mcp-profile', metavar='PROFILE',
                       help='AWS profile for SigV4 signing to the MCP endpoint')
    parser.add_argument('--mcp-region', metavar='REGION',
                       help='AWS region for MCP SigV4 signing (defaults to runtime region)')
    parser.add_argument('--llm-profile', metavar='PROFILE',
                       help='AWS profile for Bedrock LLM calls (if different from default)')
    parser.add_argument('--bedrock-api-key', metavar='KEY',
                       help='Bedrock API key for bedrock-mantle (non-Anthropic models).')
    parser.add_argument('--fleet-name', metavar='NAME',
                       help='AppStream fleet name (for streaming URL generation)')
    parser.add_argument('--stack-name', metavar='NAME',
                       help='AppStream stack name (for streaming URL generation)')
    parser.add_argument('--account-id', metavar='ID',
                       help='AWS account ID where AppStream resources live')
    return parser


def resolve_streaming_url(parser, args):
    """Resolve the streaming URL from args."""
    if args.streaming_url:
        args.streaming_url = args.streaming_url.strip().replace('\\?', '?').replace('\\=', '=').replace('\\&', '&')
        return
    parser.error(
        "--streaming-url is required.\n\n"
        "  Generate one with:\n"
        "    aws appstream create-streaming-url \\\n"
        "      --stack-name <STACK> --fleet-name <FLEET> \\\n"
        "      --user-id testuser --validity 3600 \\\n"
        "      --query StreamingURL --output text"
    )


def create_logger(agent_dir, task_prompt, model_id):
    """Create and configure a StrandsAgentLogger."""
    logger = StrandsAgentLogger(
        log_dir=os.path.join(agent_dir, "logs"),
        metrics_dir=os.path.join(agent_dir, "metrics"),
        screenshots_dir=os.path.join(agent_dir, "screenshots"),
        quiet_display=True,
    )
    logger.set_task_info(task_prompt[:200], model_id)
    return logger


def setup_signal_handler(agent_logger):
    """Install Ctrl-C handler that finalizes metrics and exits."""
    def handler(sig, frame):
        sys.stdout.write("\n\n⚠️  Interrupted\n")
        sys.stdout.flush()
        agent_logger.finalize(False, "Interrupted")
        os._exit(1)
    signal.signal(signal.SIGINT, handler)


def print_handler(**kwargs):
    """Callback handler to stream agent output to stdout."""
    if "data" in kwargs:
        sys.stdout.write(kwargs["data"])
        sys.stdout.flush()


def print_banner(title, description, model_id, args):
    """Print the agent startup banner."""
    os.system('clear 2>/dev/null || cls 2>/dev/null || true')
    print(f"\n{title}\n")
    if description:
        print(f"{description}\n")
    print("Built using the Strands Agents SDK")
    print("and the MCP client.\n")
    sys.stdout.write(f"  API: Bedrock\n")
    sys.stdout.write(f"  Model: {model_id}\n")
    sys.stdout.write(f"  Region: {args.region}\n")
    if getattr(args, 'computer_use_tool', False):
        sys.stdout.write("  Computer Use beta: enabled (computer-use-2025-11-24)\n")
    sys.stdout.write("  " + "─" * 36 + "\n\n")
    sys.stdout.flush()


def finalize_and_exit(agent_logger, success, error, result=None):
    """Finalize metrics, print paths, and return exit code."""
    metrics_file = agent_logger.finalize(success, error, agent_result=result)
    print(f"📊 Metrics: {metrics_file}")
    print(f"📝 Logs: {agent_logger.log_file}")
    return 0 if success else 1


def setup_standard_agent(args, agent_dir, skill_filename=None, skill_label=None,
                         system_prompt_path=None, task_prompt_path=None):
    """Load prompts, skill, and build the logger/model/factory/conversation manager."""
    default_sys = os.path.join(agent_dir, "prompts/system_prompt.md")
    system_prompt = load_prompt(system_prompt_path or default_sys)

    task_prompt = None
    if task_prompt_path:
        task_prompt = load_prompt(task_prompt_path)

    if skill_filename:
        skill_path = os.path.join(agent_dir, "skills", skill_filename)
        try:
            with open(skill_path, 'r') as f:
                skill = json.load(f)
            label = skill_label or "SKILL"
            system_prompt += f"\n\n=== {label} ===\n{json.dumps(skill, indent=2)}\n"
        except Exception as e:
            print(f"  Warning: Could not load skill: {e}")

    agent_logger = create_logger(agent_dir, task_prompt or "", args.model_id)
    _, sys_ver = parse_prompt_frontmatter(system_prompt)
    task_ver = None
    if task_prompt:
        _, task_ver = parse_prompt_frontmatter(task_prompt)
    agent_logger.set_prompt_versions(sys_ver, task_ver)

    model = create_model(args)
    mcp_factory = create_mcp_client_factory(args)
    if not _supports_converse_images(args.model_id):
        conv_manager = None if args.no_screenshot_pruning else ScreenshotPruningConversationManager(max_messages=6)
    else:
        conv_manager = None if args.no_screenshot_pruning else ScreenshotPruningConversationManager()

    return {
        "system_prompt": system_prompt,
        "task_prompt": task_prompt,
        "agent_logger": agent_logger,
        "model": model,
        "mcp_factory": mcp_factory,
        "conv_manager": conv_manager,
    }


def run_standard_agent(agent_dir, description, banner_title, banner_body,
                       skill_filename=None, skill_label=None):
    """Run the standard agent pipeline: parse args → load prompts/skill → run → finalize."""
    parser = create_base_parser(description)
    args = parser.parse_args()
    resolve_streaming_url(parser, args)

    print_banner(banner_title, banner_body, args.model_id, args)

    setup = setup_standard_agent(
        args, agent_dir,
        skill_filename=skill_filename,
        skill_label=skill_label,
        task_prompt_path=os.path.join(agent_dir, "prompts/task_prompt.md"),
    )

    success, error, result = run_agent_with_retry(
        args, setup["mcp_factory"], setup["model"],
        setup["system_prompt"], setup["task_prompt"],
        setup["agent_logger"], setup["conv_manager"],
    )
    return finalize_and_exit(setup["agent_logger"], success, error, result)

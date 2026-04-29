# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Common agent infrastructure for WorkSpaces Agent Framework.

Extracts shared boilerplate: argument parsing, model configuration,
MCP client setup, retry logic, signal handling, and error messaging.
"""

import argparse
import json
import os
import signal
import sys
import time

from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from .strands_logger import StrandsAgentLogger, parse_prompt_frontmatter
from .screenshot_pruning_manager import ScreenshotPruningConversationManager


def load_prompt(path, allowed_roots=None):
    """Load a prompt file, stripping YAML frontmatter.

    If `allowed_roots` is provided, the file's real path must be contained
    within at least one of those directories. Prevents CLI flags from
    reading arbitrary files (e.g. /etc/passwd) that would then be
    concatenated into an LLM prompt and exfiltrated to Bedrock.

    Args:
        path: Path to the prompt file.
        allowed_roots: Iterable of absolute directory paths that bound what
            files may be loaded. If None, no containment check is run
            (used by trusted internal callers that pass a known agent path).
    """
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
        # Re-raise validation errors so callers see them.
        raise
    except Exception as e:
        print(f"Warning: Could not load {path}: {e}")
        return ""


def _load_config():
    """Load scripts/config.json, returning a dict.

    Searches both the package's scripts/ directory (when installed) and the
    current working directory (when run from a freshly unzipped drop).
    """
    import re
    for candidate in [
        os.path.join(os.path.dirname(__file__), '..', 'scripts', 'config.json'),
        os.path.join(os.getcwd(), 'scripts', 'config.json'),
    ]:
        path = os.path.normpath(candidate)
        if os.path.isfile(path):
            try:
                raw = open(path).read()
                raw = re.sub(r'(?m)^\s*//.*$', '', raw)  # Strip JSONC line comments
                return json.loads(raw)
            except Exception:
                pass
    return {}


_config = _load_config()

# MCP endpoint + signing service must be supplied by the operator via
# MCP_ENDPOINT/AWS_SERVICE_NAME env vars or scripts/config.json. No default
# is shipped to avoid leaking internal infrastructure and to prevent
# production deployments from silently hitting a non-prod endpoint.
#
# The signing region defaults to the caller's runtime region (see
# resolve_mcp_region below). config.mcp.region can force a specific region.
_mcp_cfg = _config.get("mcp", {}) if isinstance(_config.get("mcp"), dict) else {}
DEFAULT_MCP_ENDPOINT = (
    _mcp_cfg.get("endpoint")
    or _config.get("mcpEndpoint")
    or os.environ.get("MCP_ENDPOINT")
    or ""
)
DEFAULT_MCP_REGION_OVERRIDE = _mcp_cfg.get("region")  # None unless forced
DEFAULT_MCP_SERVICE = (
    _mcp_cfg.get("service")
    or os.environ.get("AWS_SERVICE_NAME")
    or ""
)


def resolve_mcp_region(args):
    """Pick the MCP signing region.

    Priority: CLI flag > config override > current runtime region (AWS_REGION
    / AWS_DEFAULT_REGION) > us-west-2 fallback.

    MCP is deployed per-region — an agent running in us-west-2 should sign
    for us-west-2 regardless of what region its Bedrock model lives in.
    """
    if getattr(args, 'mcp_region', None):
        return args.mcp_region
    if DEFAULT_MCP_REGION_OVERRIDE:
        return DEFAULT_MCP_REGION_OVERRIDE
    return os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-west-2'


def create_base_parser(description):
    """Create an argument parser with the standard agent arguments."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--streaming-url',
                       help='AppStream streaming URL for the desktop session')
    parser.add_argument('--model-id', default='us.anthropic.claude-sonnet-4-6',
                       help='Bedrock model ID (default: us.anthropic.claude-sonnet-4-6)')
    parser.add_argument('--computer-use-tool', action='store_true', default=False,
                       help='Enable computer-use-2025-11-24 tool configuration')
    parser.add_argument('--mcp-timeout', type=int, default=180,
                       help='MCP client startup timeout in seconds (default: 180)')
    parser.add_argument('--mcp-retries', type=int, default=3,
                       help='Number of MCP client connection retries (default: 3)')
    parser.add_argument('--region',
                       default=os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')),
                       help='AWS region for Bedrock (default: auto-detect from environment)')
    parser.add_argument('--no-screenshot-pruning', action='store_true', default=False,
                       help='Disable screenshot pruning from conversation context')

    # MCP endpoint — defaults from config.json, can be overridden
    parser.add_argument('--mcp-endpoint', metavar='URL', default=DEFAULT_MCP_ENDPOINT,
                       help=argparse.SUPPRESS)  # Hidden — uses config.json default
    parser.add_argument('--mcp-profile', metavar='PROFILE',
                       help='AWS profile for SigV4 signing to the MCP endpoint')
    parser.add_argument('--mcp-region', metavar='REGION',
                       help='AWS region for MCP SigV4 signing (defaults to runtime region)')
    parser.add_argument('--llm-profile', metavar='PROFILE',
                       help='AWS profile for Bedrock LLM calls (if different from default)')

    return parser


def resolve_streaming_url(parser, args):
    """Resolve the streaming URL from args. Sanitizes shell escapes.

    Modifies args in place.
    """
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


# Accepted models and regions. Keep the demo open enough that experiments
# work out of the box, but reject anything unexpected so a caller-supplied
# `model_id` can't steer the agent toward an unreviewed model or region.
ALLOWED_MODELS = frozenset({
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-5",
    "us.anthropic.claude-opus-4-6",
    "us.anthropic.claude-opus-4-7",
})
ALLOWED_REGIONS = frozenset({
    # Americas
    "us-east-1",       # US East (N. Virginia)
    "us-east-2",       # US East (Ohio)
    "us-west-2",       # US West (Oregon)
    "ca-central-1",    # Canada (Central)
    # Europe
    "eu-central-1",    # Europe (Frankfurt)
    "eu-west-1",       # Europe (Ireland)
    "eu-west-2",       # Europe (London)
    "eu-west-3",       # Europe (Paris)
    # Asia Pacific
    "ap-northeast-1",  # Asia Pacific (Tokyo)
    "ap-northeast-2",  # Asia Pacific (Seoul)
    "ap-south-1",      # Asia Pacific (Mumbai)
    "ap-southeast-1",  # Asia Pacific (Singapore)
    "ap-southeast-2",  # Asia Pacific (Sydney)
})


def create_model(args):
    """Create a BedrockModel from parsed args.

    Validates `model_id` and `region` against the allow-list. Unknown values
    are rejected so a caller-supplied `model_id` cannot route traffic to an
    unreviewed foundation model.
    """
    import boto3

    if args.model_id not in ALLOWED_MODELS:
        raise ValueError(
            f"model_id {args.model_id!r} not in allow-list. "
            f"Permitted: {sorted(ALLOWED_MODELS)}"
        )
    if args.region not in ALLOWED_REGIONS:
        raise ValueError(
            f"region {args.region!r} not in allow-list. "
            f"Permitted: {sorted(ALLOWED_REGIONS)}"
        )

    model_kwargs = {
        "model_id": args.model_id,
    }
    if args.computer_use_tool:
        model_kwargs["additional_model_request_fields"] = {
            "computer_use_tool_configuration": {
                "type": "computer-use-2025-11-24"
            }
        }

    # Use a separate profile for LLM if specified
    if getattr(args, 'llm_profile', None):
        model_kwargs["boto_session"] = boto3.Session(
            profile_name=args.llm_profile, region_name=args.region)
    else:
        model_kwargs["region_name"] = args.region

    return BedrockModel(**model_kwargs)


def _is_remote_mcp(args):
    """Check if the agent should use a remote MCP endpoint."""
    return bool(getattr(args, 'mcp_endpoint', None))


def create_mcp_client_factory(args, root_dir=None):
    """Create the MCP client transport factory for the Agent Access MCP Server.

    Uses the endpoint from config.json (or --mcp-endpoint override).
    Returns a callable suitable for passing to MCPClient().
    """
    if not _is_remote_mcp(args):
        raise RuntimeError(
            "No MCP endpoint configured.\n"
            "  Check scripts/config.json or set --mcp-endpoint."
        )

    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    endpoint = args.mcp_endpoint
    if not endpoint:
        raise RuntimeError(
            "MCP_ENDPOINT is required. Set the MCP_ENDPOINT environment "
            "variable or mcp.endpoint in scripts/config.json."
        )
    mcp_profile = getattr(args, 'mcp_profile', None)
    # MCP is deployed per-region. Sign for whichever region the caller is in.
    mcp_region = resolve_mcp_region(args)
    mcp_service = DEFAULT_MCP_SERVICE
    if not mcp_service:
        raise RuntimeError(
            "AWS_SERVICE_NAME is required. Set the AWS_SERVICE_NAME "
            "environment variable or mcp.service in scripts/config.json."
        )
    # Substitute {region} in the endpoint template with the signing region,
    # so a single config line like "https://agentaccess-mcp.{region}.api.aws/mcp"
    # resolves to the regional deployment the caller is pointed at.
    endpoint = endpoint.replace("{region}", mcp_region)
    streaming_url = args.streaming_url

    print(f"  MCP transport: remote ({endpoint}, signed for {mcp_service}/{mcp_region})")
    sys.stdout.flush()

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
    if args.computer_use_tool:
        sys.stdout.write(f"  Computer Use: enabled\n")
    sys.stdout.write("  " + "─" * 36 + "\n\n")
    sys.stdout.flush()


def _is_retryable_error(error_str):
    """Check if an error is a retryable MCP/connection error."""
    lower = error_str.lower()
    return any(pattern in lower for pattern in [
        "timed out", "initialization", "channel not connected",
        "connection to the mcp server was closed", "mcperror",
        "401 unauthorized", "iserror", "tools\nfield required",
    ])


def _print_connection_error(args):
    """Print connection troubleshooting guidance."""
    print("\n  The Agent Access MCP Server failed to connect. Please check:")
    print(f"    1. The endpoint URL is correct: {getattr(args, 'mcp_endpoint', 'not set')}")
    print("    2. Your AWS credentials have access to the Agent Access MCP Server")
    print("    3. The streaming URL is valid and not expired")
    if getattr(args, 'mcp_profile', None):
        print(f"    4. The AWS profile '{args.mcp_profile}' is configured correctly")


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

    Args:
        args: Parsed CLI arguments.
        mcp_factory: Callable returned by create_mcp_client_factory().
        model: BedrockModel instance.
        system_prompt: System prompt string.
        task_prompt: Task prompt string.
        agent_logger: StrandsAgentLogger instance.
        conversation_manager: Optional conversation manager.

    Returns (success, error, result) tuple.
    """
    setup_signal_handler(agent_logger)

    max_retries = args.mcp_retries
    success = False
    error = None
    result = None

    for attempt in range(1, max_retries + 1):
        sys.stdout.write(f"  Connecting to desktop (attempt {attempt}/{max_retries})...\n")
        sys.stdout.flush()

        mcp_client = MCPClient(mcp_factory, startup_timeout=args.mcp_timeout)
        try:
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
            if _is_retryable_error(error) and attempt < max_retries:
                wait = 10 * attempt
                sys.stdout.write(f"\n  ⏳ Connection lost — the session may still be starting. Retrying in {wait}s...\n")
                sys.stdout.flush()
                time.sleep(wait)
                continue

            print(f"\n\n✗ Error: {e}")
            if _is_retryable_error(error):
                _print_connection_error(args)
            elif any(s in error for s in ["bedrock", "credential", "UnrecognizedClientException",
                                           "AccessDeniedException", "ExpiredTokenException"]):
                _print_bedrock_error(args)
            break

    return success, error, result


def create_agent_with_retry(args, mcp_factory, model, system_prompt, agent_logger, conversation_manager=None):
    """Create an Agent with MCP connection retry (without running a task).

    Used by agents that need the Agent object for REPL or custom execution.
    Returns (agent, error) tuple. Agent is None if all retries failed.
    """
    setup_signal_handler(agent_logger)

    max_retries = args.mcp_retries
    error = None

    for attempt in range(1, max_retries + 1):
        sys.stdout.write(f"  Connecting to desktop (attempt {attempt}/{max_retries})...\n")
        sys.stdout.flush()

        mcp_client = MCPClient(mcp_factory, startup_timeout=args.mcp_timeout)
        try:
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
            if _is_retryable_error(error) and attempt < max_retries:
                wait = 10 * attempt
                sys.stdout.write(f"\n  ⏳ Connection lost — the session may still be starting. Retrying in {wait}s...\n")
                sys.stdout.flush()
                time.sleep(wait)
                continue

            print(f"\n\n✗ Error: {e}")
            if _is_retryable_error(error):
                _print_connection_error(args)
            elif any(s in error for s in ["bedrock", "credential", "UnrecognizedClientException",
                                           "AccessDeniedException", "ExpiredTokenException"]):
                _print_bedrock_error(args)
            return None, error

    return None, error


def finalize_and_exit(agent_logger, success, error, result=None):
    """Finalize metrics, print paths, and return exit code."""
    metrics_file = agent_logger.finalize(success, error, agent_result=result)
    print(f"📊 Metrics: {metrics_file}")
    print(f"📝 Logs: {agent_logger.log_file}")
    return 0 if success else 1


def setup_standard_agent(args, agent_dir, skill_filename=None, skill_label=None,
                         system_prompt_path=None, task_prompt_path=None):
    """Load prompts, skill, and build the logger/model/factory/conversation manager.

    Returns a dict with keys: ``system_prompt``, ``task_prompt``, ``agent_logger``,
    ``model``, ``mcp_factory``, ``conv_manager``.

    This is the shared prep work for any agent — use it when you need to customize
    what happens after setup (e.g. REPL loops) but want the standard initialization.

    Args:
        args: Parsed CLI arguments from ``create_base_parser``.
        agent_dir: Absolute path to the agent's directory.
        skill_filename: Optional skill JSON filename under ``<agent_dir>/skills/``.
        skill_label: Optional label for the skill section in the system prompt.
        system_prompt_path: Override for the system prompt path.
        task_prompt_path: Override for the task prompt path. If None, no task prompt
            is loaded (useful for REPL agents).
    """
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
    conv_manager = None if args.no_screenshot_pruning else ScreenshotPruningConversationManager()

    return {
        "system_prompt": system_prompt,
        "task_prompt": task_prompt,
        "agent_logger": agent_logger,
        "model": model,
        "mcp_factory": mcp_factory,
        "conv_manager": conv_manager,
    }


def run_standard_agent(
    agent_dir,
    description,
    banner_title,
    banner_body,
    skill_filename=None,
    skill_label=None,
):
    """Run the standard agent pipeline: parse args → load prompts/skill → run → finalize.

    Handles the full lifecycle for a single-shot agent with a fixed prompt and
    skill file. Use this for agents that don't need custom argument parsing or
    non-standard execution (for that, compose the lower-level helpers directly).

    Args:
        agent_dir: Absolute path to the agent's directory (use
            ``os.path.dirname(os.path.abspath(__file__))`` from the agent).
        description: argparse description string.
        banner_title: Title shown in the startup banner.
        banner_body: Multi-line description shown in the startup banner.
        skill_filename: Optional skill JSON filename under ``<agent_dir>/skills/``.
        skill_label: Optional label shown above the skill content in the prompt.
            Defaults to ``"SKILL"``.

    Returns:
        An exit code suitable for ``sys.exit()``.
    """
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

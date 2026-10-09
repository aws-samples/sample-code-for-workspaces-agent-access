# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Common agent infrastructure for WorkSpaces Agent Framework.

This module re-exports from sub-modules for backward compatibility:
- model.py: Bedrock model creation
- mcp_client.py: MCP transport, tool name sanitization
- agent_factory.py: the one place Agents are constructed, explicit auth modes
- retry.py: Connection retry logic, error handling
"""

import argparse
import json
import os
import sys

from .strands_logger import StrandsAgentLogger, parse_prompt_frontmatter
from .screenshot_pruning_manager import (
    DEFAULT_KEEP_SCREENSHOTS,
    DEFAULT_PRUNE_BATCH,
    DEFAULT_PRUNE_BATCH_CACHED,
    ScreenshotPruningConversationManager,
)

# Re-exports from sub-modules
from .computer_tool import DEFAULT_VERSION, VERSIONS
from .agent_factory import (
    AUTH_SAML,
    AUTH_STREAMING_URL,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_TURNS,
    RunInterrupted,
    RunLimitReached,
    get_auth_mode,
    make_agent,
    require_auth_mode,
    setup_signal_handler,
)
from .saml_assertion import (
    ENV_SAML_ASSERTION,
    pending_warnings,
    redact,
    register_argv_secrets,
    resolve_saml_args,
)
from .model import ALLOWED_REGIONS, _supports_converse_images, create_model
from .mcp_client import (
    _config,
    _is_remote_mcp,
    DEFAULT_MCP_ENDPOINT,
    DEFAULT_MCP_REGION_OVERRIDE,
    DEFAULT_MCP_SERVICE,
    resolve_mcp_region,
    create_mcp_client_factory,
    build_mcp_client,
    close_mcp_clients,
    DEFAULT_TOOL_TIMEOUT,
)
from .retry import (
    _is_retryable_error,
    _is_client_disconnected,
    run_agent_with_retry,
    create_agent_with_retry,
)


def _read_prompt(path, allowed_roots=None):
    """Return a prompt file's raw text (frontmatter included), or None if it cannot be read."""
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
            return f.read()
    except ValueError:
        raise
    except Exception as e:
        print(f"Warning: Could not load {path}: {e}")
        return None


def _strip_frontmatter(content):
    if content.startswith('---'):
        end = content.find('---', 3)
        if end != -1:
            content = content[end + 3:].strip()
    return content


def load_prompt(path, allowed_roots=None):
    """Load a prompt file, stripping YAML frontmatter."""
    content = _read_prompt(path, allowed_roots)
    return "" if content is None else _strip_frontmatter(content)


def load_prompt_with_meta(path, allowed_roots=None):
    """Load a prompt file; returns ``(content without frontmatter, frontmatter dict or None)``.

    The frontmatter (``version``, ``description``...) is what ends up in the run's metrics as
    ``prompt_versions``; it has to be read before it is stripped.
    """
    content = _read_prompt(path, allowed_roots)
    if content is None:
        return "", None
    return _strip_frontmatter(content), parse_prompt_frontmatter(content)[1]


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that cannot echo a credential and explains a blank stray argument.

    argparse reports the words it does not understand before any of our validation runs. A
    SAML assertion that lands in the wrong place (an unquoted ``$(cat wrapped.b64)`` is split
    into several words, or it is given to the wrong option) would be printed back in full, so
    long base64-looking words are registered as secrets first and scrubbed from every error.
    """

    def parse_known_args(self, args=None, namespace=None):
        register_argv_secrets(sys.argv[1:] if args is None else args)
        return super().parse_known_args(args, namespace)

    def parse_args(self, args=None, namespace=None):
        namespace, extras = self.parse_known_args(args, namespace)
        if extras:
            message = "unrecognized arguments: " + " ".join(w if w.strip() else repr(w) for w in extras)
            if any(not w.strip() for w in extras):
                message += ("\n  A blank argument usually means a space after the backslash that "
                            "continues a line (\\ then a space). Remove it, or put the whole command "
                            "on one line.")
            self.error(message)
        return namespace

    def error(self, message):
        super().error(redact(message))


def create_base_parser(description):
    """Create an argument parser with the standard agent arguments."""
    parser = _ArgumentParser(description=description)
    parser.add_argument('--streaming-url',
                       help='AppStream streaming URL for the desktop session '
                            '(not needed with a SAML assertion)')
    parser.add_argument('--model-id', default='global.anthropic.claude-sonnet-5-5',
                       help='Bedrock model ID (default: global.anthropic.claude-sonnet-5-5)')
    parser.add_argument('--mcp-timeout', type=int, default=180,
                       help='MCP client startup timeout in seconds (default: 180)')
    parser.add_argument('--mcp-retries', type=int, default=3,
                       help='Number of MCP client connection retries (default: 3)')
    parser.add_argument('--max-turns', type=int, default=DEFAULT_MAX_TURNS, metavar='N',
                       help=f'Stop a task after N model calls (default: no limit)')
    parser.add_argument('--max-seconds', type=int, default=DEFAULT_MAX_SECONDS, metavar='S',
                       help=f'Stop a task after S seconds (default: no limit)')
    parser.add_argument('--tool-timeout', type=int, default=DEFAULT_TOOL_TIMEOUT, metavar='S',
                       help=f'Give up on one desktop action after S seconds (default: {DEFAULT_TOOL_TIMEOUT})')
    parser.add_argument('--region',
                       default=os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')),
                       help='AWS region for Bedrock (default: auto-detect from environment)')
    parser.add_argument('--no-screenshot-pruning', action='store_true', default=False,
                       help='Disable screenshot pruning from conversation context')
    parser.add_argument('--keep-screenshots', type=int, default=DEFAULT_KEEP_SCREENSHOTS, metavar='N',
                       help='Screenshots kept in the conversation; older ones are replaced by a '
                            f'placeholder (default: {DEFAULT_KEEP_SCREENSHOTS})')
    parser.add_argument('--prune-batch', type=int, default=None, metavar='N',
                       help='Prune once N screenshots beyond --keep-screenshots have piled up, so the cached '
                            f'prompt stays valid in between (default: {DEFAULT_PRUNE_BATCH_CACHED} with prompt '
                            f'caching, {DEFAULT_PRUNE_BATCH} = before every model call without it)')
    parser.add_argument('--prompt-cache', action=argparse.BooleanOptionalAction, default=True,
                       help='Bedrock prompt caching for Claude models (default: on; --no-prompt-cache turns it off)')
    parser.add_argument('--max-tokens', type=int, default=None, metavar='N',
                       help='Maximum tokens per model response (default: the model default)')
    parser.add_argument('--native-computer-tool', action=argparse.BooleanOptionalAction, default=False,
                       help="Offer Claude Anthropic's computer_20251124 tool instead of the individual "
                            'desktop tools (Claude models only; implies the computer-use beta header)')
    parser.add_argument('--computer-tool-version', choices=sorted(VERSIONS), default=DEFAULT_VERSION,
                       help='With --native-computer-tool: the Anthropic computer tool version, 20251124 '
                            '(beta header, one computer tool; default) or 20260801 (the newer toolset, no header)')
    parser.add_argument('--effort', choices=['low', 'medium', 'high'], default=None,
                       help='Reasoning effort for Claude models that support it (sends adaptive thinking '
                            'and output_config.effort; default: the model default)')
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
    parser.add_argument('--saml-assertion-file', metavar='PATH',
                       help='File holding the base64 SAML assertion for Domain Join (AD-joined '
                            'fleets); replaces --streaming-url. '
                            f'${ENV_SAML_ASSERTION} is used when it is not given.')
    # Removed: an assertion on the command line is visible in the process list. Kept hidden so the
    # old spelling is refused with a pointer to the file, instead of being taken by argparse as an
    # abbreviation of --saml-assertion-file (with the assertion as the "path").
    parser.add_argument('--saml-assertion', dest='removed_inline_assertion', help=argparse.SUPPRESS)
    parser.set_defaults(saml_assertion=None)     # set from the file (or environment) by resolve_session_auth
    parser.add_argument('--stack-arn', metavar='ARN',
                       help='AppStream stack ARN (required with --saml-assertion-file)')
    parser.add_argument('--expire-session-on-exit', action='store_true', default=False,
                       help='When the agent disconnects, also expire the WorkSpaces Applications streaming '
                            'session (sends X-Amzn-AgentAccess-Expire-Streaming-Session-On-Delete: true). '
                            'The desktop and everything open on it is discarded and the fleet scales per its '
                            'policy; the default leaves the session running until its disconnect timeout')
    return parser


def resolve_session_auth(parser, args):
    """Validate how the desktop session is authenticated and normalise ``args`` for it.

    Either a streaming URL (``--streaming-url``) or, for a domain-joined fleet, a SAML
    assertion plus ``--stack-arn`` (``--saml-assertion-file`` or ``$AGENTACCESS_SAML_ASSERTION``)
    - never both. Exits through ``parser.error`` on
    misuse. Returns ``AUTH_SAML`` or ``AUTH_STREAMING_URL``.
    """
    if resolve_saml_args(parser, args):
        return AUTH_SAML
    streaming_url = (args.streaming_url or "").strip()
    if streaming_url:
        args.streaming_url = streaming_url.replace('\\?', '?').replace('\\=', '=').replace('\\&', '&')
        return AUTH_STREAMING_URL
    parser.error(
        "--streaming-url is required (or, for a domain-joined fleet, "
        "--saml-assertion-file together with --stack-arn).\n\n"
        "  Generate a streaming URL with:\n"
        "    aws appstream create-streaming-url \\\n"
        "      --stack-name <STACK> --fleet-name <FLEET> \\\n"
        "      --user-id testuser --validity 3600 \\\n"
        "      --query StreamingURL --output text"
    )


# Kept for agents written against the previous name.
resolve_streaming_url = resolve_session_auth


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
    if get_auth_mode(args) == AUTH_SAML:
        sys.stdout.write("  Auth: Domain Join (SAML assertion)\n")
    if getattr(args, 'native_computer_tool', False):
        version = VERSIONS.get(getattr(args, 'computer_tool_version', DEFAULT_VERSION), "?")
        sys.stdout.write(f"  Computer tool: {version} (native), screen 1280x720\n")
    sys.stdout.write("  " + "─" * 36 + "\n")
    if sys.stderr.isatty():  # the clear above erased what argument handling warned about
        for message in pending_warnings():
            sys.stdout.write(f"  ⚠️  {message}\n")
    sys.stdout.write("\n")
    sys.stdout.flush()


def finalize_and_exit(agent_logger, success, error, result=None):
    """Finalize metrics, print paths, and return exit code."""
    close_mcp_clients()      # end the desktop session cleanly: see close_mcp_clients
    metrics_file = agent_logger.finalize(success, error, agent_result=result)
    print(f"📊 Metrics: {metrics_file}")
    print(f"📝 Logs: {agent_logger.log_file}")
    return 0 if success else 1


NATIVE_COMPUTER_NOTE = """

=== NATIVE COMPUTER TOOL ===
In this session you control the desktop with one tool named `computer`, not with the individual desktop tools described above. Use the `computer` action that matches each of them:
- `screenshot`
- `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`: `coordinate` [x, y]; `text` holds modifier keys such as "ctrl" or "ctrl+shift"
- `left_click_drag`: `start_coordinate` and `coordinate` (the end point)
- `mouse_move`, `left_mouse_down`, `left_mouse_up`: `coordinate`
- `scroll`: `coordinate`, `scroll_direction` (up, down, left, right), `scroll_amount` in wheel notches (not ticks: ignore the tick numbers above)
- `type`: `text`; `key`: `text`, optional `repeat`; `hold_key`: `text`, `duration` 1 to 30 seconds
- `wait`: `duration` in seconds
`zoom` is not available, and `cursor_position` only reports the last position you set. The screen is 1280 x 720; coordinates are [x, y] pixels. Any other tools in your list (for example `forwarded___` tools) work as described above.
"""


NATIVE_TOOLSET_NOTE = """

=== NATIVE COMPUTER TOOLS ===
In this session you control the desktop with the computer tools that are in your tool list (`screenshot`, `left_click`, `type`, `key`, `scroll`, ...), not with the individual desktop tools described above. Each one matches a tool above:
- `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`: `coordinate` [x, y]; `text` holds modifier keys such as "ctrl" or "ctrl+shift"
- `left_click_drag`: `start_coordinate` and `coordinate` (the end point)
- `mouse_move`, `left_mouse_down`, `left_mouse_up`: `coordinate`
- `scroll`: `coordinate`, `scroll_direction` (up, down, left, right), `scroll_amount` in wheel notches (not ticks: ignore the tick numbers above)
- `type`: `text`; `key`: `text`, optional `repeat`; `hold_key`: `text`, `duration` 1 to 30 seconds
- `wait`: `duration` in seconds
The screen is 1280 x 720; coordinates are [x, y] pixels. Any other tools in your list (for example `forwarded___` tools) work as described above.
"""


def resolve_prune_batch(args):
    """``--prune-batch``, else 10 when prompt caching is on (measured: about 45% lower cost at the same success) and 1 without it."""
    batch = getattr(args, 'prune_batch', None)
    if batch is not None:
        return batch
    return DEFAULT_PRUNE_BATCH_CACHED if getattr(args, 'prompt_cache', True) else DEFAULT_PRUNE_BATCH


def setup_standard_agent(args, agent_dir, skill_filename=None, skill_label=None,
                         system_prompt_path=None, task_prompt_path=None):
    """Load prompts, skill, and build the logger/model/factory/conversation manager."""
    default_sys = os.path.join(agent_dir, "prompts/system_prompt.md")
    system_prompt, sys_ver = load_prompt_with_meta(system_prompt_path or default_sys)

    task_prompt = None
    task_ver = None
    if task_prompt_path:
        task_prompt, task_ver = load_prompt_with_meta(task_prompt_path)

    if skill_filename:
        skill_path = os.path.join(agent_dir, "skills", skill_filename)
        try:
            with open(skill_path, 'r') as f:
                skill = json.load(f)
            label = skill_label or "SKILL"
            system_prompt += f"\n\n=== {label} ===\n{json.dumps(skill, indent=2)}\n"
        except Exception as e:
            print(f"  Warning: Could not load skill: {e}")

    native = bool(getattr(args, 'native_computer_tool', False))
    version = getattr(args, 'computer_tool_version', DEFAULT_VERSION)
    if native:
        system_prompt += NATIVE_TOOLSET_NOTE if version == "20260801" else NATIVE_COMPUTER_NOTE

    agent_logger = create_logger(agent_dir, task_prompt or "", args.model_id)
    agent_logger.set_prompt_versions(sys_ver, task_ver)
    agent_logger.metrics["computer_tool"] = VERSIONS[version] if native else "function_tools"
    agent_logger.metrics["effort"] = getattr(args, 'effort', None)

    model = create_model(args)
    mcp_factory = create_mcp_client_factory(args)
    conv_manager = None
    if not args.no_screenshot_pruning:
        conv_manager = ScreenshotPruningConversationManager(
            keep_last_n=getattr(args, 'keep_screenshots', DEFAULT_KEEP_SCREENSHOTS),
            batch=resolve_prune_batch(args),
            # the bedrock-mantle path has a request size limit: keep its history window tight
            max_messages=0 if _supports_converse_images(args.model_id) else 6,
        )

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
    resolve_session_auth(parser, args)

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

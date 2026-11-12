# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Computer-use interfaces: the service runs the computer-use tools a model was trained on.

By default the model is offered the Agent Access desktop tools (``agentaccess___left_click``,
``agentaccess___type_text``, ...). With ``--computer-use-interface`` every MCP request also carries
the ``X-Amzn-AgentAccess-Computer-Use-Interface-Id`` header, and the service lists that interface's
tools in their place: the **interface tools**. For ``ANTHROPIC_COMPUTER_20251124`` that is one tool
named ``computer``; for ``ANTHROPIC_COMPUTER_TOOLSET_20260801`` it is one tool per action
(``screenshot``, ``left_click``, ``type``, ...). The service converts each interface tool call into the
Agent Access desktop action it stands for, so this client does no conversion.

Here "Agent Access desktop tools" means the screenshot, mouse and keyboard tools. The service also
lists its other Agent Access tools, such as ``agentaccess___wait`` (pause) and
``agentaccess___execute_sequence``, and no interface replaces them.

For the two Anthropic interfaces, Claude is offered its typed tool declaration (``lib/computer_tool.py``,
:func:`typed_tool_version`), and each call goes to the service's interface tool of the same name. The
other interfaces are offered as the function tools the service lists.

The default mode's client-side ``WaitTool`` (``lib/wait_tool.py``) is not added, with one exception:
Claude's ``ANTHROPIC_COMPUTER_TOOLSET_20260801`` declaration offers a ``wait`` member tool that the
service does not list, so a client-side ``wait`` tool pauses for it.

An account the service does not enable computer-use interfaces for gets the Agent Access desktop
tools instead. The agent then stops with an error (:func:`require_applied`), because the model would
be offered tools that nothing runs.
"""

from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from .computer_tool import COMPUTER_TOOL_VERSION, SERVICE_PREFIX, TOOLSET_VERSION, as_service_call

COMPUTER_USE_INTERFACE_HEADER = "X-Amzn-AgentAccess-Computer-Use-Interface-Id"

# Values the service accepts in the header. They are case-sensitive.
COMPUTER_USE_INTERFACES = (
    "ANTHROPIC_COMPUTER_20250124",
    "ANTHROPIC_COMPUTER_20251124",
    "ANTHROPIC_COMPUTER_TOOLSET_20260801",
    "OPENAI_COMPUTER_V1",
    "GOOGLE_COMPUTER_USE_3X_DESKTOP_V1",
)

# Interfaces Claude is offered as its typed tool, with the lib/computer_tool.py version that declares it.
_TYPED_TOOL_VERSIONS = {
    "ANTHROPIC_COMPUTER_20251124": COMPUTER_TOOL_VERSION,
    "ANTHROPIC_COMPUTER_TOOLSET_20260801": TOOLSET_VERSION,
}

# Every interface replaces this Agent Access desktop tool, so seeing it listed means the service did
# not apply the interface.
_ALWAYS_REPLACED_DESKTOP_TOOL = SERVICE_PREFIX + "type_text"

# The typed-tool note, by computer tool version: which tools the model controls the desktop with, and
# how it pauses. With one `computer` tool the service rejects its `wait` action, so the model pauses
# with agentaccess___wait. The toolset has a `wait` member tool, which this agent runs itself
# (lib/agent_factory.py _client_side_tools).
_TYPED_TOOL_SENTENCES = {
    COMPUTER_TOOL_VERSION: "In this session you control the desktop with the `computer` tool in your tool list, not with "
                "the individual desktop tools described above; use its actions as its definition describes them. "
                "The service carries out each call. It cannot wait, zoom or report the cursor position: to "
                "pause, call agentaccess___wait; to see the screen, take a screenshot.",
    TOOLSET_VERSION: "In this session you control the desktop with the computer tools in your tool list, one tool "
                "per action (`screenshot`, `left_click`, `type`, `key`, `scroll`, `wait`, ...), not with the "
                "individual desktop tools described above; use them as their definitions describe them. The "
                "service carries out each call except `wait`, which pauses for the given duration without "
                "calling the service. Zoom and the cursor position "
                "are not available: to see the screen, take a screenshot.",
}

TYPED_TOOL_NOTE = """

=== COMPUTER USE INTERFACE ===
{tools_sentences} The screen is 1280 x 720; coordinates are [x, y] pixels. Any other tools in your list (for example `forwarded___` tools) work as described above.
"""

FUNCTION_TOOLS_NOTE = """

=== COMPUTER USE INTERFACE ===
This session uses the {interface} computer-use interface. Use the desktop tools exactly as their tool definitions describe them; desktop tools named elsewhere in this prompt are not available.
"""


# How the run log, metrics and terminal name an OpenAI or Google interface tool call: the Agent Access
# action it stands for, so screenshots are counted and saved and typed text is redacted like a
# type_text call. Only names are mapped here; the service converts the arguments. OpenAI has one
# `computer` tool and names the action in `type`; Google has one tool per action.
# `click` is named by its `button` (_OPENAI_CLICK_BUTTONS), so it is not in this table.
_OPENAI_ACTIONS = {
    "screenshot": "screenshot", "double_click": "double_click",
    "drag": "left_click_drag", "move": "move_pointer", "scroll": "scroll",
    "keypress": "key", "type": "type_text", "wait": "wait",
}
_OPENAI_CLICK_BUTTONS = {"left": "left_click", "right": "right_click", "middle": "middle_click"}
_GOOGLE_TOOLS = {
    "take_screenshot": "screenshot", "click": "left_click", "double_click": "double_click",
    "triple_click": "triple_click", "middle_click": "middle_click", "right_click": "right_click",
    "mouse_down": "left_mouse_down", "mouse_up": "left_mouse_up", "move": "move_pointer",
    "type": "type_text", "press_key": "key", "hotkey": "key", "scroll": "scroll",
    "drag_and_drop": "left_click_drag",
}
# Calls in Claude's format (`computer` with `action`, or toolset member tools) keep
# computer_tool.as_service_call; these interfaces use the names above instead.
_LISTED_CALL_INTERFACES = frozenset({"OPENAI_COMPUTER_V1", "GOOGLE_COMPUTER_USE_3X_DESKTOP_V1"})


def call_namer(interface: Optional[str]) -> Callable[[str, Any], Tuple[str, Dict[str, Any]]]:
    """The function that names a tool call ``(short name, input)`` for the run log, metrics and terminal."""
    return as_listed_call if interface in _LISTED_CALL_INTERFACES else as_service_call


def as_listed_call(name: str, tool_input: Any) -> Tuple[str, Dict[str, Any]]:
    """``(short name, input)`` for an OpenAI or Google interface tool call.

    The name is the Agent Access action the call stands for (``take_screenshot`` -> ``screenshot``,
    ``press_key`` -> ``key``); the input is the call's own, with a list of keys joined into one
    string so it is redacted like a ``key`` call. A call this table does not know is named as listed
    and logged without its input, so nothing typed can leak.
    """
    args = dict(tool_input) if isinstance(tool_input, dict) else {}
    short = name.rsplit("___", 1)[-1]
    if short == "computer":
        action = args.get("type")
        mapped = (_OPENAI_CLICK_BUTTONS.get(args.get("button", "left"))
                  if action == "click" else _OPENAI_ACTIONS.get(action))
    else:
        mapped = _GOOGLE_TOOLS.get(short)
    if mapped is None:
        return short, {}
    for key in ("keys", "key"):
        if isinstance(args.get(key), list):
            args[key] = "+".join(str(part) for part in args[key])
    return mapped, args


class InterfaceNotApplied(ValueError):
    """The service listed the Agent Access desktop tools, not the requested interface tools."""


def requested_interface(args) -> Optional[str]:
    """The ``--computer-use-interface`` value, or None when no interface is requested."""
    return getattr(args, "computer_use_interface", None) or None


def interface_headers(args) -> dict:
    """The header that requests the interface, or no headers when none is requested."""
    interface = requested_interface(args)
    return {COMPUTER_USE_INTERFACE_HEADER: interface} if interface else {}


def typed_tool_version(interface: Optional[str]) -> Optional[str]:
    """The ``lib/computer_tool.py`` version of the typed tool Claude is offered for ``interface``, or
    None when the interface uses the function tools the service lists (or none is requested)."""
    return _TYPED_TOOL_VERSIONS.get(interface)


def needs_client_side_wait(interface: Optional[str]) -> bool:
    """True when Claude's declaration for ``interface`` offers a ``wait`` tool the service does not list:
    the toolset's ``wait`` member (``ANTHROPIC_COMPUTER_TOOLSET_20260801``)."""
    return typed_tool_version(interface) == TOOLSET_VERSION


def prompt_note(args) -> str:
    """Text to add to the system prompt so it matches the interface tools the service lists."""
    interface = requested_interface(args)
    if not interface:
        return ""
    version = typed_tool_version(interface)
    if version:
        return TYPED_TOOL_NOTE.format(tools_sentences=_TYPED_TOOL_SENTENCES[version])
    return FUNCTION_TOOLS_NOTE.format(interface=interface)


def interface_applied(tool_names: Iterable[str]) -> bool:
    """True unless ``tools/list`` shows the Agent Access desktop tools, which every interface replaces."""
    return _ALWAYS_REPLACED_DESKTOP_TOOL not in tool_names


def require_applied(interface: str, tool_names: Iterable[str]) -> None:
    """Raise :class:`InterfaceNotApplied` when the service listed the Agent Access desktop tools.

    This needs the full ``tools/list`` of a connected desktop. It only catches an interface the
    service declined: a list without desktop tools for another reason (no tools listed yet) passes.
    """
    if not interface_applied(tool_names):
        raise InterfaceNotApplied(
            f"The service listed the Agent Access desktop tools instead of the {interface} interface tools "
            "(computer-use interfaces may not be enabled for this account). Run without "
            "--computer-use-interface to use the Agent Access desktop tools.")

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Claude's typed computer tools, for the Anthropic computer-use interfaces.

With ``--computer-use-interface ANTHROPIC_COMPUTER_20251124`` or ``ANTHROPIC_COMPUTER_TOOLSET_20260801``
Claude is offered its own typed tool declaration (:data:`TOOL_DEFINITION`, :data:`TOOLSET_DEFINITION`),
and the service runs each call (``lib/computer_use_interface.py``). This module holds those
declarations, which tool specs they replace (:func:`is_typed_tool_spec`), and how a call is named in the
run log and metrics (:func:`as_service_call`): as the Agent Access desktop action it stands for
(:func:`service_call_for`).

The service's screenshots are always 1280 x 720, so coordinates map one to one. Service tool reference:
https://docs.aws.amazon.com/appstream2/latest/developerguide/agent-access-mcp-server-tools.html
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

COMPUTER_TOOL_NAME = "computer"
COMPUTER_TOOL_TYPE = "computer_20251124"
COMPUTER_USE_BETA = "computer-use-2025-11-24"
SCREEN_WIDTH, SCREEN_HEIGHT = 1280, 720

# What goes in the model request: the typed tool, declared at the service's screen size.
TOOL_DEFINITION = {
    "type": COMPUTER_TOOL_TYPE,
    "name": COMPUTER_TOOL_NAME,
    "display_width_px": SCREEN_WIDTH,
    "display_height_px": SCREEN_HEIGHT,
}

# The newer toolset: no beta header, one entry that gives the model a tool per action ("left_click",
# "type", ...) instead of one ``computer`` tool with an ``action`` field. The two the service cannot do
# are switched off in the definition.
TOOLSET_TYPE = "computer_toolset_20260801"
TOOLSET_DEFINITION = {
    "type": TOOLSET_TYPE,
    "configs": {"zoom": {"enabled": False}, "cursor_position": {"enabled": False}},
}
TOOLSET_MEMBERS = (
    "screenshot", "left_click", "right_click", "middle_click", "double_click", "triple_click", "left_click_drag",
    "mouse_move", "left_mouse_down", "left_mouse_up", "scroll", "type", "key", "hold_key", "wait")
_TOOLSET_NAMES = frozenset(TOOLSET_MEMBERS) | {"zoom", "cursor_position"}
COMPUTER_TOOL_VERSION = "20251124"
TOOLSET_VERSION = "20260801"
VERSIONS = {COMPUTER_TOOL_VERSION: COMPUTER_TOOL_TYPE, TOOLSET_VERSION: TOOLSET_TYPE}

SERVICE_PREFIX = "agentaccess___"

# Claude actions named for the Agent Access tool they stand for; the others share the tool's name.
_ACTION_TOOL_NAMES = {"mouse_move": "move_pointer", "type": "type_text"}
_POINT_ACTIONS = {"left_click", "double_click", "triple_click", "right_click", "middle_click",
                  "left_mouse_down", "left_mouse_up", "mouse_move", "scroll"}
_MODIFIER_ACTIONS = _POINT_ACTIONS - {"mouse_move"}      # `text` holds the keys held during the action
_NAMED_ACTIONS = _POINT_ACTIONS | {"screenshot", "left_click_drag", "type", "key", "hold_key", "wait"}


class ComputerActionError(ValueError):
    """A ``computer`` tool input that does not map to an Agent Access call."""


@dataclass
class ServiceCall:
    """One Agent Access tool call (``name`` without the ``agentaccess___`` prefix)."""
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


def _number(value, what) -> int:
    if isinstance(value, bool):
        raise ComputerActionError(f"{what} must be a number")
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise ComputerActionError(f"{what} must be a number, got {value!r}") from None


def _parse_point(value, what) -> Tuple[int, int]:
    """``[x, y]`` as integers, also when a model wrote it as text (``"[640, 360]"``, ``"640, 360"``,
    ``["640", "360"]``). Bounds are not checked."""
    if isinstance(value, str):
        value = [part for part in value.strip("[]() ").replace(" ", "").split(",") if part]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ComputerActionError(f"{what} must be [x, y]")
    return _number(value[0], f"{what} x"), _number(value[1], f"{what} y")


def is_text_point(value) -> bool:
    """True for a point a model wrote as text: a string, or a list holding a string."""
    return isinstance(value, str) or (isinstance(value, list) and any(isinstance(part, str) for part in value))


def point_from_text(value) -> Optional[List[int]]:
    """A point a model wrote as text (:func:`is_text_point`) as ``[x, y]`` integers, or None when it
    is not two numbers. Bounds are not checked: the service checks them."""
    try:
        return list(_parse_point(value, "point"))
    except ComputerActionError:
        return None


def _xy(tool_input, key, action) -> Tuple[int, int]:
    if tool_input.get(key) is None:
        raise ComputerActionError(f"{action} needs a {key} [x, y]")
    return _parse_point(tool_input[key], key)


def service_call_for(tool_input) -> ServiceCall:
    """The Agent Access call one Claude ``computer`` tool input stands for, to name it in the run log.

    Only names and arguments are mapped, without limits: the service converts and checks the call
    itself. Raises :class:`ComputerActionError` when there is no action, the action has no Agent Access
    counterpart, or a coordinate it needs is missing or is not two numbers.
    """
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("action"), str):
        raise ComputerActionError("the computer tool needs an 'action'")
    action = tool_input["action"].strip().lower()
    if action not in _NAMED_ACTIONS:
        raise ComputerActionError(f"no Agent Access action for {tool_input['action']!r}")

    arguments: Dict[str, Any] = {}
    if action in _POINT_ACTIONS:
        arguments["x"], arguments["y"] = _xy(tool_input, "coordinate", action)
    if action == "left_click_drag":
        arguments["start_x"], arguments["start_y"] = _xy(tool_input, "start_coordinate", action)
        arguments["end_x"], arguments["end_y"] = _xy(tool_input, "coordinate", action)
    if action == "scroll":
        arguments.update({key: tool_input[key] for key in ("scroll_direction", "scroll_amount") if key in tool_input})
    if action in _MODIFIER_ACTIONS and tool_input.get("text"):
        arguments["modifiers"] = tool_input["text"]
    if action == "type":
        arguments["text"] = tool_input.get("text")
    if action in ("key", "hold_key"):
        arguments["keys"] = tool_input.get("text")
    if action == "hold_key" and "duration" in tool_input:
        arguments["duration"] = tool_input["duration"]
    if action == "wait":
        arguments["seconds"] = tool_input.get("duration")
    return ServiceCall(_ACTION_TOOL_NAMES.get(action, action), arguments)


def is_anthropic_computer_call(name: str) -> bool:
    """True for a call to Claude's typed computer tool, in either version: ``computer`` or a toolset member."""
    return name == COMPUTER_TOOL_NAME or name in _TOOLSET_NAMES


def is_typed_tool_spec(name: str, version: str) -> bool:
    """True for a tool spec the typed tool declaration replaces: ``computer`` itself, and the
    toolset's member tools when that version is used."""
    return is_anthropic_computer_call(name) and (name == COMPUTER_TOOL_NAME or version == TOOLSET_VERSION)


def as_service_call(name: str, tool_input) -> Tuple[str, Dict[str, Any]]:
    """``(short tool name, input)`` as the run log and metrics record it.

    A ``computer`` call, or a toolset member call, is reported as the Agent Access call it stands for
    (``left_click`` with ``x``/``y``, not ``computer``), so metrics compare across interfaces. An input
    that names no desktop action is reported as ``computer:<action>``.
    """
    short = name.rsplit("___", 1)[-1]
    if name == "wait" and isinstance(tool_input, dict) and "seconds" in tool_input:
        return "wait", {"seconds": tool_input["seconds"]}  # this agent's own wait (seconds); the toolset's takes duration
    if "___" not in name and name in _TOOLSET_NAMES:      # a member of the toolset: "left_click", "type"...
        tool_input = {"action": name, **(tool_input if isinstance(tool_input, dict) else {})}
        short = COMPUTER_TOOL_NAME
    if short != COMPUTER_TOOL_NAME:
        return short, tool_input if isinstance(tool_input, dict) else {}
    try:
        call = service_call_for(tool_input)
    except ComputerActionError:
        action = tool_input.get("action") if isinstance(tool_input, dict) else None
        return f"computer:{action}", {}
    except Exception:  # a log line must never break a run
        return COMPUTER_TOOL_NAME, {}
    return call.name, dict(call.arguments)

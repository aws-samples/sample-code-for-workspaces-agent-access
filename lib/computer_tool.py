# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Anthropic's ``computer_20251124`` tool on top of the Agent Access desktop tools.

In the default mode the model sees the service's desktop tools (``left_click``, ``type_text``...)
as ordinary function tools. With ``--native-computer-tool`` it sees Anthropic's own ``computer``
tool instead, which Claude is trained on, and this module turns each ``computer`` action into
the matching Agent Access call:

* :func:`translate` is pure: a ``computer`` tool input in, a list of steps out (service calls, a
  client-side wait, or a text answer), or a :class:`ComputerActionError` saying why it cannot be done.
* :class:`ComputerTool` is the Strands tool that runs those steps against the MCP client.

The service's screenshots are always 1280 x 720, so coordinates map one to one and no screenshot
needs rescaling. Service tool reference:
https://docs.aws.amazon.com/appstream2/latest/developerguide/agent-access-mcp-server.html#agent-access-mcp-server-tools
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool

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
VERSIONS = {"20251124": COMPUTER_TOOL_TYPE, "20260801": TOOLSET_TYPE}
DEFAULT_VERSION = "20251124"

SERVICE_PREFIX = "agentaccess___"
# The Agent Access tools the ``computer`` tool replaces; the model is not shown them in this mode.
DESKTOP_TOOLS = frozenset({
    "screenshot", "left_click", "double_click", "triple_click", "right_click", "middle_click",
    "left_click_drag", "left_mouse_down", "left_mouse_up", "move_pointer", "scroll", "type_text",
    "key", "hold_key", "wait",
})

TICKS_PER_NOTCH = 120          # the service measures scroll_amount in ticks
MAX_SCROLL_NOTCHES = 100
MAX_TYPE_CHARS = 10_000
HOLD_KEY_SECONDS = (1, 30)
MAX_KEY_REPEAT = 100
MAX_WAIT_SECONDS = 100

_CLICKS = {"left_click", "double_click", "triple_click", "right_click", "middle_click"}
_MODIFIER_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "option": "alt", "shift": "shift",
    "super": "super", "win": "super", "windows": "super", "meta": "super", "cmd": "super", "command": "super",
}
_DIRECTIONS = {"up": "Up", "down": "Down", "left": "Left", "right": "Right"}


class ComputerActionError(ValueError):
    """A ``computer`` tool input the desktop cannot carry out; the message goes back to the model."""


@dataclass
class ServiceCall:
    """One Agent Access tool call (``name`` without the ``agentaccess___`` prefix)."""
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ClientWait:
    """Pause on this side (the service has no ``wait`` tool yet)."""
    seconds: float


@dataclass
class ClientText:
    """Answer without touching the desktop."""
    text: str


Step = Any   # ServiceCall | ClientWait | ClientText


class Pointer:
    """Where the mouse last was, as far as this agent knows (the service cannot report it)."""

    def __init__(self):
        self.position: Optional[Tuple[int, int]] = None


def _number(value, what) -> int:
    if isinstance(value, bool):
        raise ComputerActionError(f"{what} must be a number")
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise ComputerActionError(f"{what} must be a number, got {value!r}") from None


def _point(value, what) -> Tuple[int, int]:
    if isinstance(value, str):     # "[640, 360]" or "640, 360"
        value = [part for part in value.strip("[]() ").replace(" ", "").split(",") if part]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ComputerActionError(f"{what} must be [x, y]")
    x, y = _number(value[0], f"{what} x"), _number(value[1], f"{what} y")
    if not (0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT):
        raise ComputerActionError(
            f"{what} ({x}, {y}) is outside the {SCREEN_WIDTH}x{SCREEN_HEIGHT} screen "
            f"(x 0-{SCREEN_WIDTH - 1}, y 0-{SCREEN_HEIGHT - 1})")
    return x, y


def _modifiers(text) -> Optional[str]:
    """The ``text`` input of a click or scroll (keys held during it) as the service's ``modifiers``."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return None
    if not isinstance(text, str):
        raise ComputerActionError("text (modifier keys) must be a string such as 'ctrl' or 'ctrl+shift'")
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    unknown = [p for p in parts if p not in _MODIFIER_ALIASES]
    if unknown or not parts:
        raise ComputerActionError(
            f"unsupported modifier {unknown or text!r}: use ctrl, alt, shift or super, joined by '+'")
    return "+".join(dict.fromkeys(_MODIFIER_ALIASES[p] for p in parts))


def _target(tool_input, pointer, action) -> Tuple[int, int]:
    """The ``coordinate`` of an action, else the pointer's last position."""
    if tool_input.get("coordinate") is not None:
        return _point(tool_input["coordinate"], "coordinate")
    if pointer is not None and pointer.position is not None:
        return pointer.position
    raise ComputerActionError(f"{action} needs a coordinate [x, y]")


def _with_modifiers(arguments, tool_input):
    modifiers = _modifiers(tool_input.get("text"))
    if modifiers:
        arguments["modifiers"] = modifiers
    return arguments


def _text_input(tool_input, action) -> str:
    text = tool_input.get("text")
    if not isinstance(text, str) or not text:
        raise ComputerActionError(f"{action} needs non-empty text")
    return text


def translate(tool_input, pointer: Optional[Pointer] = None) -> List[Step]:
    """Turn one ``computer`` tool input into the steps that carry it out.

    ``pointer`` supplies the position for actions given without a ``coordinate`` and is updated
    by the caller after the steps succeed (see :func:`pointer_after`). Raises
    :class:`ComputerActionError` with a message for the model when the action cannot be done.
    """
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("action"), str):
        raise ComputerActionError("the computer tool needs an 'action'")
    action = tool_input["action"].strip().lower()

    if action == "screenshot":
        return [ServiceCall("screenshot")]
    if action in _CLICKS:
        x, y = _target(tool_input, pointer, action)
        return [ServiceCall(action, _with_modifiers({"x": x, "y": y}, tool_input))]
    if action == "mouse_move":
        x, y = _point(tool_input.get("coordinate"), "coordinate")
        return [ServiceCall("move_pointer", {"x": x, "y": y})]
    if action in ("left_mouse_down", "left_mouse_up"):
        x, y = _target(tool_input, pointer, action)
        return [ServiceCall(action, _with_modifiers({"x": x, "y": y}, tool_input))]
    if action == "left_click_drag":
        if tool_input.get("start_coordinate") is not None:
            start = _point(tool_input["start_coordinate"], "start_coordinate")
        elif pointer is not None and pointer.position is not None:
            start = pointer.position
        else:
            raise ComputerActionError("left_click_drag needs a start_coordinate [x, y]")
        end = _point(tool_input.get("coordinate"), "coordinate")
        return [ServiceCall("left_click_drag", {"start_x": start[0], "start_y": start[1], "end_x": end[0], "end_y": end[1]})]
    if action == "scroll":
        x, y = _target(tool_input, pointer, action)
        direction = _DIRECTIONS.get(str(tool_input.get("scroll_direction", "")).strip().lower())
        if direction is None:
            raise ComputerActionError("scroll_direction must be up, down, left or right")
        notches = _number(tool_input.get("scroll_amount", 1), "scroll_amount")
        if not 1 <= notches <= MAX_SCROLL_NOTCHES:
            raise ComputerActionError(f"scroll_amount must be between 1 and {MAX_SCROLL_NOTCHES} wheel notches")
        return [ServiceCall("scroll", _with_modifiers(
            {"x": x, "y": y, "scroll_direction": direction, "scroll_amount": notches * TICKS_PER_NOTCH}, tool_input))]
    if action == "type":
        text = _text_input(tool_input, "type")
        if len(text) > MAX_TYPE_CHARS:
            raise ComputerActionError(f"type is limited to {MAX_TYPE_CHARS} characters; split the text")
        return [ServiceCall("type_text", {"text": text})]
    if action == "key":
        keys = _text_input(tool_input, "key")
        repeat = _number(tool_input.get("repeat", 1), "repeat")
        if not 1 <= repeat <= MAX_KEY_REPEAT:
            raise ComputerActionError(f"repeat must be between 1 and {MAX_KEY_REPEAT}")
        return [ServiceCall("key", {"keys": keys}) for _ in range(repeat)]
    if action == "hold_key":
        keys = _text_input(tool_input, "hold_key")
        seconds = _number(tool_input.get("duration"), "duration")
        low, high = HOLD_KEY_SECONDS
        if not low <= seconds <= high:
            raise ComputerActionError(f"hold_key duration must be between {low} and {high} seconds")
        return [ServiceCall("hold_key", {"keys": keys, "duration": seconds})]
    if action == "wait":
        seconds = _number(tool_input.get("duration"), "duration")
        if not 0 <= seconds <= MAX_WAIT_SECONDS:
            raise ComputerActionError(f"wait duration must be between 0 and {MAX_WAIT_SECONDS} seconds")
        return [ClientWait(seconds)]
    if action == "cursor_position":
        if pointer is not None and pointer.position is not None:
            return [ClientText("X=%d, Y=%d" % pointer.position)]
        raise ComputerActionError("the pointer position is not known yet: move the mouse or click first")
    if action == "zoom":
        raise ComputerActionError("zoom is not available; take a screenshot instead")
    raise ComputerActionError(f"unknown action {tool_input['action']!r}")


def pointer_after(step: Step) -> Optional[Tuple[int, int]]:
    """Where the pointer ends up after a service call, or None if the step does not move it."""
    if not isinstance(step, ServiceCall):
        return None
    args = step.arguments
    if "end_x" in args:
        return args["end_x"], args["end_y"]
    if "x" in args and "y" in args:
        return args["x"], args["y"]
    return None


def is_desktop_spec(name: str, version: str = DEFAULT_VERSION) -> bool:
    """True for a tool spec the model must not see in native mode: ``computer`` itself (it is
    declared as the typed tool), the toolset's member tools when that version is used, and the
    Agent Access desktop tools they replace."""
    if name == COMPUTER_TOOL_NAME:
        return True
    if version == "20260801" and name in _TOOLSET_NAMES:
        return True
    prefix, _, short = name.partition("___")
    return prefix == SERVICE_PREFIX.rstrip("_") and short in DESKTOP_TOOLS


def as_service_call(name: str, tool_input) -> Tuple[str, Dict[str, Any]]:
    """``(short tool name, input)`` as the run log and metrics record it.

    A ``computer`` call is reported as the service call it becomes, so a run's metrics look the same
    with and without the native tool (``left_click`` with ``x``/``y``, not ``computer``). An input
    that cannot be translated is reported as ``computer:<action>``.
    """
    short = name.rsplit("___", 1)[-1]
    if name == "wait" and isinstance(tool_input, dict) and "seconds" in tool_input:
        return "wait", {"seconds": tool_input["seconds"]}  # the local wait tool (lib/wait_tool.py), not the toolset's
    if "___" not in name and name in _TOOLSET_NAMES:      # a member of the toolset: "left_click", "type"...
        tool_input = {"action": name, **(tool_input if isinstance(tool_input, dict) else {})}
        short = COMPUTER_TOOL_NAME
    if short != COMPUTER_TOOL_NAME:
        return short, tool_input if isinstance(tool_input, dict) else {}
    try:
        steps = translate(tool_input)
    except ComputerActionError:
        action = tool_input.get("action") if isinstance(tool_input, dict) else None
        return f"computer:{action}", {}
    except Exception:  # a log line must never break a run
        return COMPUTER_TOOL_NAME, {}
    first = steps[0]
    if isinstance(first, ServiceCall):
        return first.name, dict(first.arguments)
    if isinstance(first, ClientWait):
        return "wait", {"seconds": first.seconds}
    return "cursor_position", {}


def _result(tool_use_id, status, text):
    return {"toolUseId": tool_use_id, "status": status, "content": [{"text": text}]}


class ComputerTool(AgentTool):
    """The ``computer`` tool: runs :func:`translate`'s steps against the Agent Access MCP client.

    With ``member`` set it is one tool of the 20260801 toolset instead (``left_click``, ``type``...):
    the same translation, with the tool's name as the action.
    """

    def __init__(self, mcp_client, timeout=None, member=None, pointer=None):
        super().__init__()
        self._mcp = mcp_client
        self._timeout = timeout
        self._member = member
        self.pointer = pointer or Pointer()

    @property
    def tool_name(self) -> str:
        return self._member or COMPUTER_TOOL_NAME

    @property
    def tool_spec(self):
        # Never sent to the model (the typed definition replaces it): Strands wants a spec to register.
        return {
            "name": self.tool_name,
            "description": "Control the desktop with the mouse and keyboard (Anthropic computer tool).",
            "inputSchema": {"json": {"type": "object", "properties": {"action": {"type": "string"}},
                                     "required": [] if self._member else ["action"]}},
        }

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use, invocation_state, **kwargs):
        tool_use_id = tool_use["toolUseId"]
        cancel = getattr(invocation_state.get("agent"), "_cancel_signal", None)
        tool_input = tool_use.get("input")
        if self._member:
            tool_input = {"action": self._member, **(tool_input if isinstance(tool_input, dict) else {})}
        try:
            steps = translate(tool_input, self.pointer)
        except ComputerActionError as e:
            yield ToolResultEvent(_result(tool_use_id, "error", str(e)))
            return

        result = _result(tool_use_id, "success", "OK")
        for step in steps:
            if isinstance(step, ClientWait):
                await _cancellable_sleep(step.seconds, cancel)
                result = _result(tool_use_id, "success", f"Waited {step.seconds:g} seconds")
            elif isinstance(step, ClientText):
                result = _result(tool_use_id, "success", step.text)
            else:
                result = await self._mcp.call_tool_async(
                    tool_use_id=tool_use_id, name=SERVICE_PREFIX + step.name, arguments=step.arguments,
                    read_timeout_seconds=self._timeout, cancel_signal=cancel)
                if result.get("status") == "error":
                    break
            moved = pointer_after(step)
            if moved is not None:
                self.pointer.position = moved
        yield ToolResultEvent(result)


def build_computer_tools(mcp_client, timeout=None, version=DEFAULT_VERSION):
    """The Strands tools that carry out the model's computer actions for the chosen tool ``version``."""
    if version == "20260801":
        pointer = Pointer()
        return [ComputerTool(mcp_client, timeout, member=name, pointer=pointer) for name in TOOLSET_MEMBERS]
    if version != DEFAULT_VERSION:
        raise ValueError(f"unknown computer tool version {version!r}; choose one of {sorted(VERSIONS)}")
    return [ComputerTool(mcp_client, timeout)]


async def _cancellable_sleep(seconds, cancel_signal):
    """Sleep ``seconds``, returning early if the run is cancelled (Ctrl-C, a limit)."""
    remaining = float(seconds)
    while remaining > 0 and not (cancel_signal is not None and cancel_signal.is_set()):
        step = min(0.2, remaining)
        await asyncio.sleep(step)
        remaining -= step

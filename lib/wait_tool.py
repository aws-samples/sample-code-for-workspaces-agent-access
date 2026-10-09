# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A ``wait`` tool run on this side, until the Agent Access service offers its own.

The prompts tell the model to ``wait`` while something loads, and the service has no such tool yet
(replace it when the service does). :class:`WaitTool` pauses here, without calling the
service, and gives up early when the run is cancelled (Ctrl-C, a run limit).
"""

from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool

from .computer_tool import MAX_WAIT_SECONDS, _cancellable_sleep, _result

WAIT_TOOL_NAME = "wait"


class WaitTool(AgentTool):
    """``wait(seconds)``: pause while something loads."""

    @property
    def tool_name(self) -> str:
        return WAIT_TOOL_NAME

    @property
    def tool_spec(self):
        return {
            "name": WAIT_TOOL_NAME,
            "description": "Pause while something loads, then look at the screen again.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"seconds": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_SECONDS,
                                           "description": "Seconds to wait"}},
                "required": ["seconds"],
            }},
        }

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use, invocation_state, **kwargs):
        tool_use_id = tool_use["toolUseId"]
        tool_input = tool_use.get("input")
        seconds = tool_input.get("seconds") if isinstance(tool_input, dict) else None
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= MAX_WAIT_SECONDS:
            yield ToolResultEvent(_result(
                tool_use_id, "error", f"seconds must be a number between 1 and {MAX_WAIT_SECONDS}"))
            return
        cancel = getattr(invocation_state.get("agent"), "_cancel_signal", None)
        await _cancellable_sleep(seconds, cancel)
        yield ToolResultEvent(_result(tool_use_id, "success", f"Waited {seconds:g} seconds"))

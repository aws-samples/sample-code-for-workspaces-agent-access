# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A ``wait`` tool run on this side, where the service lists none.

The prompts tell the model to ``wait`` while something loads. In the default function-tool mode the
service has no such tool yet (replace it when the service does), and with
``ANTHROPIC_COMPUTER_TOOLSET_20260801`` Claude's toolset declaration offers a ``wait`` member tool
that the service does not list. :class:`WaitTool` pauses here, without calling the service, and gives
up early when the run is cancelled (Ctrl-C, a run limit).
"""

import asyncio
from typing import Optional

from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool

WAIT_TOOL_NAME = "wait"
MAX_WAIT_SECONDS = 100
SECONDS_ARGUMENT = "seconds"    # the default mode's own wait tool
DURATION_ARGUMENT = "duration"  # the toolset's wait member


class WaitTool(AgentTool):
    """``wait(seconds)``: pause while something loads.

    ``argument`` names the input that holds the seconds: ``seconds`` for this agent's own wait tool,
    ``duration`` for the toolset's ``wait`` member (whose spec the typed declaration replaces).
    """

    def __init__(self, argument: str = SECONDS_ARGUMENT):
        super().__init__()
        self._argument = argument

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
                "properties": {self._argument: {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_SECONDS,
                                                "description": "Seconds to wait"}},
                "required": [self._argument],
            }},
        }

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use, invocation_state, **kwargs):
        tool_use_id = tool_use["toolUseId"]
        tool_input = tool_use.get("input")
        seconds = _seconds(tool_input.get(self._argument) if isinstance(tool_input, dict) else None)
        if seconds is None or not 0 < seconds <= MAX_WAIT_SECONDS:
            yield ToolResultEvent(_result(
                tool_use_id, "error", f"{self._argument} must be a number between 1 and {MAX_WAIT_SECONDS}"))
            return
        cancel = getattr(invocation_state.get("agent"), "_cancel_signal", None)
        await _cancellable_sleep(seconds, cancel)
        yield ToolResultEvent(_result(tool_use_id, "success", f"Waited {seconds:g} seconds"))


def _seconds(value) -> Optional[float]:
    """``value`` as a number of seconds, also when a model wrote it as text (``"2"``), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _result(tool_use_id, status, text):
    return {"toolUseId": tool_use_id, "status": status, "content": [{"text": text}]}


async def _cancellable_sleep(seconds, cancel_signal):
    """Sleep ``seconds``, returning early if the run is cancelled (Ctrl-C, a limit)."""
    remaining = float(seconds)
    while remaining > 0 and not (cancel_signal is not None and cancel_signal.is_set()):
        step = min(0.2, remaining)
        await asyncio.sleep(step)
        remaining -= step

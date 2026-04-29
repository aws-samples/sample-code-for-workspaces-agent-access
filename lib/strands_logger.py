# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Strands Agent Logger - HookProvider for logging tool calls, model calls, and screenshots.

This logger integrates with the Strands Agents SDK via the hooks system.
It implements HookProvider and registers callbacks for AfterToolCallEvent
and AfterModelCallEvent.
"""

import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


# Tools whose input parameters may contain secrets (passwords, PII). We
# never persist their plaintext to metrics or logs — replace with a
# length-and-hash marker so operators can correlate without exposing the
# actual value.
_SENSITIVE_INPUT_TOOLS = frozenset({"type_text", "key"})


# Allow-list of MCP tool short-names this agent may invoke. Any other tool
# (`open_url`, `download_file`, `upload_file`, etc.) is blocked at the
# BeforeToolCallEvent stage until an approval flow exists. The normalized
# "short name" strips the `<server>___` MCP prefix.
#
# This covers the Claude Computer Use desktop-automation tool set: pointer
# movement, click variants (including triple_click for text selection),
# scroll/drag, keyboard input, and screenshot. Network/file-transfer tools
# are intentionally excluded.
_ALLOWED_TOOL_SHORTNAMES = frozenset({
    # Pointer + click
    "screenshot",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "move_pointer",
    "left_mouse_down",
    "left_mouse_up",
    "cursor_position",
    # Drag / scroll
    "scroll",
    "drag",
    # Keyboard
    "type_text",
    "key",
    "hold_key",
    # Timing
    "wait",
})


def _redact_tool_input(short_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of tool_input with sensitive string values redacted.

    For tools in `_SENSITIVE_INPUT_TOOLS`, string values are replaced with
    `<redacted len=N sha256:XXXXXXXX>`. Non-string values (ints, coords)
    pass through untouched. Binary payloads (`data`) are always stripped.
    """
    if short_name not in _SENSITIVE_INPUT_TOOLS:
        return {k: v for k, v in tool_input.items() if k != "data"}
    redacted: Dict[str, Any] = {}
    for k, v in tool_input.items():
        if k == "data":
            continue
        if isinstance(v, str) and v:
            h = hashlib.sha256(v.encode("utf-8")).hexdigest()[:8]
            redacted[k] = f"<redacted len={len(v)} sha256:{h}>"
        else:
            redacted[k] = v
    return redacted


def parse_prompt_frontmatter(prompt_content: str) -> tuple[str, Optional[Dict[str, str]]]:
    """
    Parse YAML frontmatter from prompt content.
    Returns (content_without_frontmatter, frontmatter_dict)
    """
    frontmatter_pattern = r'^---\s*\n(.*?)\n---\s*\n'
    match = re.match(frontmatter_pattern, prompt_content, re.DOTALL)

    if not match:
        return prompt_content, None

    frontmatter_text = match.group(1)
    content = prompt_content[match.end():]

    try:
        import yaml
        parsed = yaml.safe_load(frontmatter_text) or {}
        # Coerce to str→str dict for consumer compatibility
        frontmatter = {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except ImportError:
        # PyYAML not available — fall back to a simple key:value parser
        frontmatter = {}
        for line in frontmatter_text.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                frontmatter[key.strip()] = value.strip().strip('"\'')

    return content, frontmatter

from strands.hooks.events import AfterModelCallEvent, AfterToolCallEvent, BeforeToolCallEvent


class StrandsAgentLogger:
    """Strands HookProvider that logs tool calls, model calls, and screenshots.

    Produces the same metrics JSON schema, log format, and screenshot naming
    as MetricsLogger so that scripts/analyze_metrics.py works with either.

    Usage:
        logger = StrandsAgentLogger(agent_dir)
        agent = Agent(model=model, tools=[...], hooks=[logger])
        result = agent(prompt)
        logger.finalize(success=True)
    """

    def __init__(self, log_dir="logs", metrics_dir="metrics", screenshots_dir="screenshots", quiet_display=False):
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.start_time = time.time()
        self.screenshot_counter = 0
        self.iterations = 0
        self.quiet_display = quiet_display

        # Two-line display tracking
        self.current_thinking = ""
        self.current_action = ""
        self.display_initialized = False

        # Create directories
        Path(metrics_dir).mkdir(parents=True, exist_ok=True)
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        Path(screenshots_dir).mkdir(parents=True, exist_ok=True)

        # File paths
        self.metrics_file = f"{metrics_dir}/metrics_{self.session_id}.json"
        self.log_file = f"{log_dir}/agent_{self.session_id}.log"
        self.screenshot_dir = screenshots_dir

        # Metrics — same schema as MetricsLogger
        self.metrics = {
            "session_id": self.session_id,
            "start_time": datetime.now().isoformat(),
            "model_id": None,
            "task_description": None,
            "prompt_versions": {
                "system_prompt": None,
                "task_prompt": None
            },
            "tool_calls": [],
            "model_calls": [],
            "actions_log": [],
            "total_tokens": {
                "input": 0,
                "output": 0,
                "total": 0
            },
            "iterations": 0,
            "success": False,
            "error": None,
            "duration_seconds": 0,
            "end_time": None
        }

        # File logger — same format as MetricsLogger
        self.file_logger = logging.getLogger(f'WorkSpaceAgent_{self.session_id}')
        self.file_logger.setLevel(logging.DEBUG)
        self.file_logger.propagate = False

        file_handler = logging.FileHandler(self.log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        self.file_logger.addHandler(file_handler)
        self.file_logger.debug(f"Session started: {self.session_id}")

    # --- Strands HookProvider interface ---

    def register_hooks(self, registry, **kwargs):
        """Register hook callbacks with the Strands agent."""
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)
        registry.add_callback(AfterModelCallEvent, self._on_after_model_call)

    def _on_before_tool_call(self, event: BeforeToolCallEvent):
        """Hook: enforce tool allow-list; fix coordinate-param string coercion.

        Raises PermissionError if the model tries to invoke a tool outside
        `_ALLOWED_TOOL_SHORTNAMES`. Strands surfaces this as a tool failure
        that the model can observe and react to, which is the desired
        behavior — the tool simply doesn't execute.
        """
        tool_name = event.tool_use.get("name", "")
        short_name = tool_name.rsplit("___", 1)[-1]
        if short_name not in _ALLOWED_TOOL_SHORTNAMES:
            self.file_logger.warning(
                f"Blocked tool call: {tool_name!r} (short={short_name!r}) "
                f"not in allow-list {sorted(_ALLOWED_TOOL_SHORTNAMES)}"
            )
            raise PermissionError(
                f"Tool {short_name!r} is not in the allow-list. "
                f"Permitted: {sorted(_ALLOWED_TOOL_SHORTNAMES)}"
            )

        tool_input = event.tool_use.get("input", {})
        changed = False
        for key in ("x", "y", "scroll_amount"):
            val = tool_input.get(key)
            if val is None:
                continue
            if isinstance(val, str):
                # Handle "875, 27" style — split into x and y
                if "," in val:
                    parts = [p.strip() for p in val.split(",") if p.strip()]
                    try:
                        if key == "x" and len(parts) >= 2:
                            tool_input["x"] = int(parts[0])
                            tool_input["y"] = int(parts[1])
                        elif parts:
                            tool_input[key] = int(parts[0])
                        changed = True
                    except (ValueError, IndexError):
                        pass
                else:
                    try:
                        tool_input[key] = int(float(val))
                        changed = True
                    except (ValueError, TypeError):
                        pass
        if changed:
            # Don't log raw params for sensitive tools even here.
            self.file_logger.debug(
                f"Fixed tool params: {json.dumps(_redact_tool_input(short_name, tool_input))}"
            )

    def _on_after_tool_call(self, event: AfterToolCallEvent):
        """Hook: called after each tool invocation."""
        tool_name = event.tool_use.get("name", "unknown")
        tool_input = event.tool_use.get("input", {})
        error_str = str(event.exception) if event.exception else None
        success = event.exception is None

        short_name = tool_name.rsplit("___", 1)[-1]

        # Console display
        if short_name == "screenshot":
            self.screenshot_counter += 1
            self.show_action(f"📸 Screenshot #{self.screenshot_counter}")
            self._save_screenshot(event.result)
        elif short_name == "left_click":
            self.show_action(f"🖱️ Click ({tool_input.get('x')},{tool_input.get('y')})")
        elif short_name == "double_click":
            self.show_action(f"🖱️ DblClk ({tool_input.get('x')},{tool_input.get('y')})")
        elif short_name == "triple_click":
            self.show_action(f"🖱️ TplClk ({tool_input.get('x')},{tool_input.get('y')})")
        elif short_name == "right_click":
            self.show_action(f"🖱️ RClick ({tool_input.get('x')},{tool_input.get('y')})")
        elif short_name == "middle_click":
            self.show_action(f"🖱️ MClick ({tool_input.get('x')},{tool_input.get('y')})")
        elif short_name in ("left_mouse_down", "left_mouse_up"):
            action = "Down" if short_name == "left_mouse_down" else "Up  "
            self.show_action(f"🖱️ {action} ({tool_input.get('x')},{tool_input.get('y')})")
        elif short_name == "cursor_position":
            self.show_action("🖱️ CursorPos")
        elif short_name == "type_text":
            # Never preview the typed text — it might be a password.
            text_len = len(str(tool_input.get('text', '')))
            self.show_action(f"⌨️ type_text ({text_len} chars)")
        elif short_name == "key":
            # Key combinations can be sensitive too; show only the count.
            keys = tool_input.get('keys') or []
            self.show_action(f"⌨️ key ({len(keys) if isinstance(keys, list) else 1} keys)")
        elif short_name == "hold_key":
            self.show_action(f"⌨️ hold_key ({tool_input.get('duration', '?')}s)")
        elif short_name == "scroll":
            self.show_action(f"🖱️ Scroll {tool_input.get('scroll_direction')}")
        elif short_name == "drag":
            self.show_action(
                f"🖱️ Drag ({tool_input.get('start_x')},{tool_input.get('start_y')}) → "
                f"({tool_input.get('end_x')},{tool_input.get('end_y')})"
            )
        elif short_name == "move_pointer":
            self.show_action(f"🖱️ Move ({tool_input.get('x')},{tool_input.get('y')})")
        elif short_name == "wait":
            self.show_action(f"⏳ wait ({tool_input.get('duration', '?')}s)")
        else:
            self.show_action(f"🔧 {tool_name}")

        redacted_input = _redact_tool_input(short_name, tool_input)

        # File log — redacted params so passwords don't leak to the debug log.
        self.file_logger.debug(
            f"Tool Call: dcv.{tool_name} | Duration: 0.000s | "
            f"Success: {success} | Params: {json.dumps(redacted_input)[:200]}"
        )
        if error_str:
            self.file_logger.error(f"Tool Error: {error_str}")
        if short_name != "screenshot":
            status = "✓" if success else "✗"
            self.file_logger.info(f"{status} {tool_name}")

        # Metrics — redacted params so metrics.json is safe to share.
        self.metrics["tool_calls"].append({
            "timestamp": datetime.now().isoformat(),
            "tool_name": "dcv",
            "action": short_name,
            "params": redacted_input,
            "duration_seconds": 0,
            "success": success,
            "error": error_str
        })

    def _on_after_model_call(self, event: AfterModelCallEvent):
        """Hook: called after each model invocation."""
        self.iterations += 1
        self.metrics["iterations"] = self.iterations

        stop_reason = "unknown"
        tools_used = []

        if event.stop_response:
            stop_reason = str(event.stop_response.stop_reason)
            msg = event.stop_response.message

            # Extract tool names from message content
            content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, 'content', [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name")
                        if name:
                            tools_used.append(name)
        elif event.exception:
            stop_reason = f"error: {event.exception}"
            self.file_logger.error(f"Model error: {event.exception}")

        self.metrics["model_calls"].append({
            "timestamp": datetime.now().isoformat(),
            "stop_reason": stop_reason,
            "tools_used": tools_used
        })

        self.file_logger.debug(f"Model Call: stop_reason={stop_reason} tools={tools_used}")

    # --- Screenshot saving ---

    def _save_screenshot(self, result):
        """Save screenshot image data from a tool result."""
        try:
            content = []
            if isinstance(result, dict):
                content = result.get("content", [])
            elif hasattr(result, 'content'):
                content = result.content

            for item in content:
                raw_bytes = None

                if isinstance(item, dict):
                    # Format 1: {"type": "image", "data": "base64..."} (custom agent)
                    if item.get("type") == "image" and "data" in item:
                        raw_bytes = base64.b64decode(item["data"])

                    # Format 2: {"image": {"format": "png", "source": {"bytes": b'...'}}} (MCP/Strands)
                    elif "image" in item:
                        img = item["image"]
                        if isinstance(img, dict):
                            source = img.get("source", {})
                            if isinstance(source, dict) and "bytes" in source:
                                raw_bytes = source["bytes"]
                            elif "data" in img:
                                raw_bytes = base64.b64decode(img["data"])

                if raw_bytes:
                    screenshot_id = f"{self.session_id}_screenshot_{self.screenshot_counter:03d}"
                    path = os.path.join(self.screenshot_dir, f"{screenshot_id}.png")
                    with open(path, 'wb') as f:
                        f.write(raw_bytes)
                    self.file_logger.debug(f"💾 Screenshot saved: {path} (ID: {screenshot_id})")
                    return

            self.file_logger.warning("No image data found in screenshot result")
        except Exception as e:
            self.file_logger.warning(f"Could not save screenshot: {e}")

    # --- Display (matches MetricsLogger two-line display) ---

    def show_thinking(self, text):
        self.file_logger.debug(f"DISPLAY: Thinking: '{text}'")
        text = text.replace('\n', ' ').replace('\r', ' ')
        if len(text) > 40:
            text = text[:37] + "..."
        self.current_thinking = text
        self._update_display()

    def show_action(self, action_text):
        self.file_logger.debug(f"DISPLAY: Action: '{action_text}'")
        action_text = action_text.replace('\n', ' ').replace('\r', ' ')
        if len(action_text) > 40:
            action_text = action_text[:37] + "..."
        self.current_action = action_text
        self._update_display()

    def _update_display(self):
        if self.quiet_display:
            return
        if not self.display_initialized:
            sys.stdout.write(f"{self.current_thinking}\n{self.current_action}\n")
            self.display_initialized = True
        else:
            sys.stdout.write(f"\033[2A\033[2K{self.current_thinking}\n\033[2K{self.current_action}\n")
        sys.stdout.flush()

    # --- Config setters (match MetricsLogger interface) ---

    def set_task_info(self, task_description, model_id):
        self.metrics["task_description"] = task_description[:200]
        self.metrics["model_id"] = model_id
        self.file_logger.debug(f"Model: {model_id}")

    def set_prompt_versions(self, system_prompt_version=None, task_prompt_version=None):
        if system_prompt_version:
            self.metrics["prompt_versions"]["system_prompt"] = system_prompt_version
        if task_prompt_version:
            self.metrics["prompt_versions"]["task_prompt"] = task_prompt_version

    # --- Finalize (matches MetricsLogger.finalize output) ---

    def finalize(self, success, error=None, agent_result=None):
        """Write final metrics to disk. Pass agent_result to capture token usage."""
        self.metrics["success"] = success
        self.metrics["error"] = error
        self.metrics["duration_seconds"] = round(time.time() - self.start_time, 2)
        self.metrics["end_time"] = datetime.now().isoformat()

        # Pull token usage from Strands AgentResult.metrics.accumulated_usage
        if agent_result and hasattr(agent_result, 'metrics'):
            usage = getattr(agent_result.metrics, 'accumulated_usage', {})
            self.metrics["total_tokens"] = {
                "input": usage.get("inputTokens", 0),
                "output": usage.get("outputTokens", 0),
                "total": usage.get("totalTokens", 0)
            }

        tc = self.metrics["tool_calls"]
        cc = self.metrics["model_calls"]
        total_tokens = self.metrics["total_tokens"]["total"]
        self.metrics["summary"] = {
            "total_tool_calls": len(tc),
            "total_model_calls": len(cc),
            "total_actions": len(self.metrics["actions_log"]),
            "avg_tool_duration": round(
                sum(t["duration_seconds"] for t in tc) / len(tc) if tc else 0, 3
            ),
            "tokens_per_second": round(
                total_tokens / self.metrics["duration_seconds"]
                if self.metrics["duration_seconds"] > 0 else 0, 2
            ),
            "tool_success_rate": round(
                sum(1 for t in tc if t["success"]) / len(tc) * 100 if tc else 0, 2
            )
        }

        with open(self.metrics_file, 'w') as f:
            json.dump(self.metrics, f, indent=2)

        self.file_logger.debug(f"Session completed: {success}")
        self.file_logger.debug(f"Duration: {self.metrics['duration_seconds']}s")
        self.file_logger.debug(f"Total tokens: {total_tokens}")

        status = "✓ Success" if success else "✗ Failed"
        self.file_logger.info(f"\n{status} - {self.metrics['duration_seconds']}s")
        self.file_logger.info(f"📊 Metrics: {self.metrics_file}")
        self.file_logger.info(f"📝 Logs: {self.log_file}")

        return self.metrics_file

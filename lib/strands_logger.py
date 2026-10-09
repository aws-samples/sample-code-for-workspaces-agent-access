# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Strands Agent Logger - HookProvider for logging tool calls, model calls, and screenshots.

This logger integrates with the Strands Agents SDK via the hooks system.
It implements HookProvider and registers callbacks for BeforeToolCallEvent,
AfterToolCallEvent, BeforeModelCallEvent and AfterModelCallEvent.
"""

import base64
import functools
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


@functools.lru_cache(maxsize=1)
def _string_yaml_loader():
    """A YAML loader that leaves numbers and dates as the text they were written as.

    ``version: 1.10`` must stay "1.10" (not become the float 1.1) when it is recorded as a
    prompt version.
    """
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    keep_as_text = {"tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:timestamp"}
    Loader.yaml_implicit_resolvers = {
        first: [(tag, pattern) for tag, pattern in rules if tag not in keep_as_text]
        for first, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    return Loader


def _parse_simple_frontmatter(text: str) -> Dict[str, str]:
    """``key: value`` lines only: the fallback for frontmatter PyYAML cannot read."""
    frontmatter = {}
    for line in text.split('\n'):
        if ':' in line:
            key, value = line.split(':', 1)
            frontmatter[key.strip()] = value.strip().strip('"\'')
    return frontmatter


def parse_prompt_frontmatter(prompt_content: str) -> tuple[str, Optional[Dict[str, str]]]:
    """
    Parse YAML frontmatter from prompt content.
    Returns (content_without_frontmatter, frontmatter_dict)

    The frontmatter only labels a run's prompts in its metrics, so it must never stop a run: text
    PyYAML cannot parse (say ``description: Draw: a dog``) falls back to a plain ``key: value`` reader.
    """
    frontmatter_pattern = r'^---\s*\n(.*?)\n---\s*\n'
    match = re.match(frontmatter_pattern, prompt_content, re.DOTALL)

    if not match:
        return prompt_content, None

    frontmatter_text = match.group(1)
    content = prompt_content[match.end():]

    try:
        import yaml
        parsed = yaml.load(frontmatter_text, Loader=_string_yaml_loader()) or {}
        # Coerce to str→str dict for consumer compatibility
        frontmatter = (
            {str(k): str(v) for k, v in parsed.items() if v is not None} if isinstance(parsed, dict) else {}
        )
    except Exception:  # PyYAML missing, or the frontmatter is not valid YAML
        frontmatter = _parse_simple_frontmatter(frontmatter_text)

    return content, frontmatter

from strands.hooks.events import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
)

from .computer_tool import as_service_call
from .saml_assertion import redact

_MAX_ERROR_CHARS = 500   # how much of a tool's error text goes into the metrics file
METRICS_SCHEMA_VERSION = 2   # 1 = before per-call tokens, durations and failure accounting


def _never_raise(method):
    """A hook callback must not break the run it observes: note the problem in the log and carry on."""
    @functools.wraps(method)
    def wrapper(self, event):
        try:
            return method(self, event)
        except Exception as exc:
            try:
                self.file_logger.error(f"{method.__name__} failed: {type(exc).__name__}: {exc}")
            except Exception:
                pass
    return wrapper


def _result_text(result) -> str:
    """The text blocks of a Strands tool result, joined."""
    if not isinstance(result, dict):
        return ""
    blocks = result.get("content", [])
    return " ".join(
        b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)
    ).strip()


def _tool_use_name(block) -> Optional[str]:
    """Name of the tool a message content block asks for (Strands ``toolUse``; also the raw ``tool_use`` form)."""
    if not isinstance(block, dict):
        return None
    name = tool_input = None
    if isinstance(block.get("toolUse"), dict):
        name, tool_input = block["toolUse"].get("name"), block["toolUse"].get("input")
    elif block.get("type") == "tool_use":
        name, tool_input = block.get("name"), block.get("input")
    # the same short name tool_calls[].action uses, so the two can be matched: agentaccess___click -> click
    # (a native ``computer`` call is named for the service call it becomes)
    return as_service_call(name, tool_input)[0] if isinstance(name, str) else None


def _input_dict(tool_use) -> Dict[str, Any]:
    """A tool call's input as a dict (a model can send null or a list for a tool without parameters)."""
    tool_input = tool_use.get("input") if isinstance(tool_use, dict) else None
    return tool_input if isinstance(tool_input, dict) else {}


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
        # Called with (action text, succeeded, error) after every tool call; the interactive
        # terminal (lib/terminal_ui.py) prints its action lines from it.
        self.display_sink = None
        self._model_call_started = None
        self._tool_started = {}
        self._invocation_started = None
        self._active_seconds = 0.0

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
            "schema_version": METRICS_SCHEMA_VERSION,
            "session_id": self.session_id,
            "start_time": datetime.now().isoformat(),
            "model_id": None,
            "task_description": None,
            "prompt_versions": {
                "system_prompt": None,
                "task_prompt": None
            },
            "attempts": 0,
            "tool_calls": [],
            "model_calls": [],
            "total_tokens": {
                "input": 0,
                "output": 0,
                "total": 0,
                "cache_read": 0,
                "cache_write": 0
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
        # One Agent per attempt: a run that reconnects and replays its task registers the same logger again.
        self.metrics["attempts"] += 1
        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)
        registry.add_callback(AfterInvocationEvent, self._on_after_invocation)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)
        registry.add_callback(AfterModelCallEvent, self._on_after_model_call)

    @_never_raise
    def _on_before_invocation(self, event):
        """Hook: an agent(...) call starts (the clock for the time actually spent working)."""
        self._invocation_started = time.monotonic()

    @_never_raise
    def _on_after_invocation(self, event):
        """Hook: an agent(...) call ended. A REPL spends most of its wall-clock time waiting at the prompt."""
        if self._invocation_started is not None:
            self._active_seconds += time.monotonic() - self._invocation_started
            self._invocation_started = None

    @_never_raise
    def _on_before_tool_call(self, event: BeforeToolCallEvent):
        """Hook: fix coordinate-param string coercion.

        Some models emit coordinate values as strings ("875") or combined
        ("875, 27"). Coerce them to integers so the MCP server accepts them.
        """
        tool_name = event.tool_use.get("name", "")
        short_name, tool_input = as_service_call(tool_name, _input_dict(event.tool_use))
        self._tool_started[event.tool_use.get("toolUseId")] = time.monotonic()
        changed = False
        for key in ("x", "y", "start_x", "start_y", "end_x", "end_y", "scroll_amount"):
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

    @_never_raise
    def _on_after_tool_call(self, event: AfterToolCallEvent):
        """Hook: called after each tool invocation."""
        tool_name = event.tool_use.get("name", "unknown")
        error_str = self._tool_error(event)
        success = error_str is None
        duration = self._tool_duration(event)

        short_name, tool_input = as_service_call(tool_name, _input_dict(event.tool_use))

        # Console display
        if short_name == "screenshot":
            if success:  # a failed screenshot (say "dcv session not ready") produced no image
                self.screenshot_counter += 1
                self.show_action(f"📸 Screenshot #{self.screenshot_counter}")
                self._save_screenshot(event.result)
            else:
                self.show_action("📸 Screenshot failed")
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
            self.show_action(f"⌨️ hold_key ({tool_input.get('duration', tool_input.get('seconds', '?'))}s)")
        elif short_name == "scroll":
            self.show_action(f"🖱️ Scroll {tool_input.get('scroll_direction')} {tool_input.get('scroll_amount', '')}".rstrip())
        elif short_name == "left_click_drag":
            # The service takes start_x/start_y/end_x/end_y; Anthropic's computer tool uses
            # `start_coordinate` + `coordinate` (the end point).
            start = tool_input.get('start_coordinate') or [tool_input.get('start_x'), tool_input.get('start_y')]
            end = tool_input.get('coordinate') or [tool_input.get('end_x'), tool_input.get('end_y')]
            start_s = f"({start[0]},{start[1]})" if len(start) == 2 and None not in start else "(?)"
            end_s = f"({end[0]},{end[1]})" if len(end) == 2 and None not in end else "(?)"
            self.show_action(f"🖱️ Drag {start_s} → {end_s}")
        elif short_name == "move_pointer":
            self.show_action(f"🖱️ Move ({tool_input.get('x')},{tool_input.get('y')})")
        elif short_name == "wait":
            self.show_action(f"⏳ wait ({tool_input.get('seconds', tool_input.get('duration', '?'))}s)")
        elif short_name == "launch_application":
            self.show_action(f"🚀 Launch {tool_input.get('id', '?')}")
        elif short_name == "get_session_info":
            self.show_action("ℹ️ Session info")
        elif short_name == "toggle_app_switcher":
            self.show_action("🗂️ App switcher")
        else:
            self.show_action(f"🔧 {tool_name}")
        if self.display_sink is not None:
            self.display_sink(self.current_action, success, error_str)

        redacted_input = _redact_tool_input(short_name, tool_input)

        # File log — redacted params so passwords don't leak to the debug log.
        self.file_logger.debug(
            f"Tool Call: dcv.{tool_name} | Duration: {duration:.3f}s | "
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
            "duration_seconds": duration,
            "success": success,
            "error": error_str
        })

    def _tool_duration(self, event) -> float:
        """Seconds the tool ran: Strands' figure, else timed here (older Strands; 0 if it never ran)."""
        started = self._tool_started.pop(event.tool_use.get("toolUseId"), None)
        duration = getattr(event, "duration", None)
        if duration is None and not hasattr(event, "duration") and started is not None:
            duration = time.monotonic() - started
        return round(duration or 0.0, 3)

    @staticmethod
    def _tool_error(event) -> Optional[str]:
        """Why a tool call failed, or None if it worked.

        A tool fails by raising (``event.exception``), by being cancelled before it ran, or - the
        usual case for an MCP tool, which Strands wraps so that it never raises - by returning a
        result whose ``status`` is ``"error"``. Only the first was counted before, so every
        failed desktop action was logged as a success.
        """
        if event.exception is not None:
            text = str(event.exception) or type(event.exception).__name__
        elif getattr(event, "cancel_message", None):
            text = f"cancelled: {event.cancel_message}"
        elif isinstance(event.result, dict) and event.result.get("status") == "error":
            text = _result_text(event.result) or "tool returned status=error"
        else:
            return None
        return redact(text)[:_MAX_ERROR_CHARS]

    @_never_raise
    def _on_before_model_call(self, event: BeforeModelCallEvent):
        """Hook: remember when the model call started."""
        self._model_call_started = time.monotonic()

    @_never_raise
    def _on_after_model_call(self, event: AfterModelCallEvent):
        """Hook: called after each model invocation."""
        started, self._model_call_started = self._model_call_started, None
        duration = round(time.monotonic() - started, 3) if started is not None else 0.0

        stop_reason = "unknown"
        tools_used = []
        usage = {}
        error = None

        if event.stop_response:
            stop_reason = str(event.stop_response.stop_reason)
            msg = event.stop_response.message

            content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, 'content', [])
            if isinstance(content, list):
                tools_used = [name for name in map(_tool_use_name, content) if name]

            # Strands attaches this call's usage to the message before the hook runs.
            metadata = msg.get("metadata") if isinstance(msg, dict) else None
            usage = (metadata or {}).get("usage") or {}
        elif event.exception:
            stop_reason = "error"
            error = redact(str(event.exception) or type(event.exception).__name__)[:_MAX_ERROR_CHARS]
            self.file_logger.error(f"Model error: {error}")

        # An attempt that raised, or whose response a hook threw away to retry, is not a model
        # call the run made progress with: keep the record, flagged, but leave it out of the counts.
        failed = event.exception is not None
        retried = bool(getattr(event, "retry", False))
        if not failed and not retried:
            self.iterations += 1
            self.metrics["iterations"] = self.iterations

        input_tokens = int(usage.get("inputTokens") or 0)
        output_tokens = int(usage.get("outputTokens") or 0)
        total_tokens = int(usage.get("totalTokens") or (input_tokens + output_tokens))
        cache_read = int(usage.get("cacheReadInputTokens") or 0)
        cache_write = int(usage.get("cacheWriteInputTokens") or 0)

        totals = self.metrics["total_tokens"]   # tokens of a discarded response were still billed
        totals["input"] += input_tokens
        totals["output"] += output_tokens
        totals["total"] += total_tokens
        totals["cache_read"] += cache_read
        totals["cache_write"] += cache_write

        call = {
            "timestamp": datetime.now().isoformat(),
            "stop_reason": stop_reason,
            "tools_used": tools_used,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "duration_seconds": duration
        }
        if failed:
            call["failed"] = True
            call["error"] = error
        if retried:
            call["retried"] = True
        self.metrics["model_calls"].append(call)

        self.file_logger.debug(
            f"Model Call: stop_reason={stop_reason} tools={tools_used} "
            f"tokens={input_tokens}+{output_tokens} cache={cache_read}/{cache_write} duration={duration:.3f}s"
            + (" FAILED" if failed else " RETRIED" if retried else "")
        )

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

        # Token totals were summed per model call. Fall back to the Strands AgentResult when the
        # model calls carried no usage (an older Strands, or a model that does not report it).
        totals = self.metrics["total_tokens"]
        if not totals["total"] and agent_result is not None and hasattr(agent_result, 'metrics'):
            usage = getattr(agent_result.metrics, 'accumulated_usage', None) or {}
            totals.update({
                "input": usage.get("inputTokens", 0),
                "output": usage.get("outputTokens", 0),
                "total": usage.get("totalTokens", 0),
                "cache_read": usage.get("cacheReadInputTokens", 0),
                "cache_write": usage.get("cacheWriteInputTokens", 0),
            })

        tc = self.metrics["tool_calls"]
        cc = [c for c in self.metrics["model_calls"] if not c.get("failed") and not c.get("retried")]
        total_tokens = totals["total"]
        active = round(self._active_seconds, 2)
        self.metrics["active_seconds"] = active
        self.metrics["summary"] = {
            "total_tool_calls": len(tc),
            "failed_tool_calls": sum(1 for t in tc if not t["success"]),
            "total_screenshots": self.screenshot_counter,
            "total_model_calls": len(cc),
            "failed_model_calls": sum(1 for c in self.metrics["model_calls"] if c.get("failed")),
            "avg_tool_duration": round(
                sum(t["duration_seconds"] for t in tc) / len(tc) if tc else 0, 3
            ),
            "avg_model_duration": round(
                sum(c["duration_seconds"] for c in cc) / len(cc) if cc else 0, 3
            ),
            # per second of work, not of wall-clock: a REPL waits at its prompt between tasks
            "tokens_per_second": round(
                total_tokens / (active or self.metrics["duration_seconds"])
                if (active or self.metrics["duration_seconds"]) > 0 else 0, 2
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

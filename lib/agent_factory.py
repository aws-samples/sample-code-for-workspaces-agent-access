# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Single construction path for the MCP client + Strands Agent, and explicit auth modes.

Why this exists
---------------
``lib/retry.py`` (twice) and the AgentCore handler
each built ``MCPClient`` + ``Agent`` by hand and had already drifted apart (tool-name
sanitising, logger hooks, ...). Anything that must be identical everywhere - tool
executor, limits, caching - goes through :func:`make_agent`.

It also makes the authentication mode explicit. Agent Access accepts either a streaming
URL or, for domain-joined fleets, a SAML assertion plus the stack ARN. Entry points
declare which modes they support and :func:`require_auth_mode` rejects the rest loudly
instead of silently ignoring the flags.
"""

import re
import signal
import sys
import threading
from datetime import timedelta

from strands import Agent
from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookOrder,
)
from strands.tools.executors import SequentialToolExecutor

from .computer_tool import COMPUTER_TOOL_NAME, DEFAULT_VERSION, build_computer_tools, as_service_call
from .mcp_client import resolve_mcp_endpoint
from .wait_tool import WaitTool

# Optional bounds on one task (one ``agent(...)`` call), off by default: ``--max-turns N`` /
# ``--max-seconds S`` turn them on, 0 (or None) means no bound.
DEFAULT_MAX_TURNS = 0
DEFAULT_MAX_SECONDS = 0

AUTH_STREAMING_URL = "streaming_url"
AUTH_SAML = "saml"


def get_auth_mode(args):
    """Return ``AUTH_SAML`` when a SAML assertion (inline or file) was supplied, else ``AUTH_STREAMING_URL``.

    An empty value still counts as asking for Domain Join, so an empty assertion is reported
    instead of silently ignored.
    """
    if (getattr(args, "saml_assertion", None) is not None
            or getattr(args, "saml_assertion_file", None) is not None):
        return AUTH_SAML
    return AUTH_STREAMING_URL


def require_auth_mode(args, supported, entry_point):
    """Raise ``ValueError`` if ``args`` select an auth mode ``entry_point`` cannot use.

    ``--stack-arn`` only has meaning together with ``--saml-assertion``, so a stray
    ``--stack-arn`` is rejected too by entry points that do not support SAML.

    Args:
        args: parsed CLI namespace (or any object with the same attributes).
        supported: iterable of ``AUTH_*`` modes the entry point handles.
        entry_point: name used in the error message.
    """
    supported = tuple(supported)
    mode = get_auth_mode(args)
    if mode not in supported:
        raise ValueError(
            f"{entry_point} does not support Domain Join "
            f"(--saml-assertion / --saml-assertion-file); it only works with streaming URLs."
        )
    if AUTH_SAML not in supported and getattr(args, "stack_arn", None) is not None:
        raise ValueError(
            f"{entry_point} does not support Domain Join; --stack-arn is only used "
            f"together with --saml-assertion."
        )
    return mode


def make_agent(model, system_prompt, mcp_client, *, max_turns=DEFAULT_MAX_TURNS,
               max_seconds=DEFAULT_MAX_SECONDS, native_computer=False, tool_timeout=None,
               computer_version=DEFAULT_VERSION, **agent_kwargs):
    """Construct the Agent every entry point uses.

    ``agent_kwargs`` are passed straight to :class:`strands.Agent` (for example
    ``conversation_manager``, ``hooks``, ``callback_handler``), so callers keep their
    existing behaviour. Defaults that must apply everywhere belong here:

    * desktop actions run one at a time (there is one mouse and one keyboard, and the model
      often asks for several actions in a single turn), and the rest of a turn is skipped once
      an action has failed;
    * earlier thinking blocks are removed from the history before each model call (see
      :class:`DropReasoning`);
    * a task ends, with an exception, when the MCP connection has died, when the user presses
      Ctrl-C, or (if set) when ``max_turns`` model calls or ``max_seconds`` seconds have passed.

    ``mcp_client`` may also be a list of tools. Unless ``native_computer`` is set, the agent also gets a ``wait`` tool that pauses here (``lib/wait_tool.py``). ``native_computer`` adds the ``computer`` tool
    (see ``lib.computer_tool``); the model has to be the one from ``create_model`` with
    ``--native-computer-tool`` for the model to be offered it. ``tool_timeout`` (a timedelta) bounds
    each of its desktop actions.
    """
    hooks = list(agent_kwargs.pop("hooks", None) or [])
    hooks += [DropReasoning(), StopAfterFailedAction(), ConnectionLostGuard(),
              RunBudgetGuard(max_turns, max_seconds), InterruptGuard()]
    agent_kwargs.setdefault("tool_executor", SequentialToolExecutor())
    tools = list(mcp_client) if isinstance(mcp_client, (list, tuple)) else [mcp_client]
    if native_computer:
        tools.extend(build_computer_tools(tools[0], timeout=tool_timeout, version=computer_version))
    else:
        tools.append(WaitTool())      # the service has no wait tool yet; replace this when it ships
    return Agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        hooks=hooks,
        **agent_kwargs,
    )


def agent_options(args):
    """Everything ``make_agent`` takes from the command line: the limits, the native computer tool, the tool timeout."""
    seconds = getattr(args, "tool_timeout", None)
    return {**agent_limits(args),
            "native_computer": bool(getattr(args, "native_computer_tool", False)),
            "computer_version": getattr(args, "computer_tool_version", DEFAULT_VERSION),
            "tool_timeout": timedelta(seconds=seconds) if seconds else None}


def agent_limits(args):
    """The ``make_agent`` keyword arguments the ``--max-turns`` / ``--max-seconds`` flags select (0 = none)."""
    turns = getattr(args, "max_turns", DEFAULT_MAX_TURNS)
    seconds = getattr(args, "max_seconds", DEFAULT_MAX_SECONDS)
    return {"max_turns": turns or None, "max_seconds": seconds or None}


class DomainJoinRejected(Exception):
    """The service refused the Domain Join credentials (HTTP 400/401/403) during a run."""

    def __init__(self, status, message):
        super().__init__(
            f"{message}\n  The SAML assertion was refused (it has probably expired). "
            "Get a fresh one and start the agent again."
        )
        self.status = status
        self.message = message
        self.conversation_note = (
            f"The task was stopped: the service refused the SAML assertion (HTTP {status}), so no "
            "further desktop actions were possible.")


def _error_text(result):
    """The text of a Strands tool result that reports an error, else ``None``."""
    if not isinstance(result, dict) or result.get("status") != "error":
        return None
    return " ".join(b.get("text", "") for b in result.get("content", []) if isinstance(b, dict))


class _CancelAndRaise:
    """Base for hooks that end a run when a tool result says it cannot usefully go on.

    Raising from a tool hook does not stop a run: Strands catches it and hands the model one
    more tool error. So a guard decides in ``AfterToolCallEvent``, asks the agent to cancel -
    Strands then stops at the next safe point, with the conversation still valid and the agent
    still reusable - and raises once the invocation has ended, where the exception reaches the
    caller of ``agent(...)``.
    """

    def __init__(self):
        self._stop = None

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeInvocationEvent, self._reset)
        registry.add_callback(AfterToolCallEvent, self._on_tool)
        # Last: an exception out of this callback skips the ones after it, and the run logger's
        # AfterInvocation callback (time actually spent working) must still get its turn.
        registry.add_callback(AfterInvocationEvent, self._raise_pending, order=HookOrder.SDK_LAST)

    def _on_tool(self, event):
        """Nothing to look at by default (a subclass may watch something else)."""

    def _stop_run(self, agent, exception):
        if self._stop is None:
            self._stop = exception
            agent.cancel()

    def _reset(self, event):
        self._stop = None

    def _raise_pending(self, event):
        stop, self._stop = self._stop, None
        if stop is None:
            return
        # A cancelled run ends on the user message holding the last tool results. The next task
        # would follow it with another user message, and Bedrock's Converse API wants the roles to
        # alternate - so close the turn with a short assistant note, which also tells the model
        # why the previous task stopped.
        messages = event.agent.messages
        if messages and messages[-1].get("role") == "user":
            messages.append({"role": "assistant", "content": [{"text": stop.conversation_note}]})
        raise stop


class DomainJoinGuard(_CancelAndRaise):
    """Stops a Domain Join run as soon as the service refuses the assertion.

    Connect-time refusals already raise. Once the session is up, Strands turns a refused
    ``tools/call`` into an ordinary error result for the model, which then retries it or
    carries on, and the run ends "Completed". The usual cause is the assertion expiring
    part-way through a long task or an idle REPL: the service refuses requests that carry an
    expired assertion.

    Only errors naming the Agent Access endpoint are matched, so an HTTP 403 reported by a
    forwarded tool (say a ``fetch`` of some web page) does not stop the run.
    """

    def __init__(self, endpoint):
        super().__init__()
        self._pattern = re.compile(r"HTTP (400|401|403) [^:\n]*? from " + re.escape(endpoint.rstrip("/")))

    def _on_tool(self, event):
        text = _error_text(event.result)
        match = self._pattern.search(text) if text else None
        if match:
            self._stop_run(event.agent, DomainJoinRejected(int(match.group(1)), text[match.start():]))


class StreamingUrlRefused(Exception):
    """The service refused the streaming URL (expired or invalid) during a run."""

    conversation_note = ("The task was stopped: the service refused the streaming URL, so no further desktop "
                         "actions were possible.")

    def __init__(self, message):
        super().__init__(
            f"{message}\n  The service refused the streaming URL, so this session cannot take any more "
            "desktop actions. Create a new streaming URL (with a --validity that covers the whole session, "
            "for example 3600) and start the agent again.")
        self.message = message


class StreamingUrlGuard(_CancelAndRaise):
    """Stops a run as soon as the service refuses the streaming URL.

    The service answers every later request the same way (``HTTP 400 ... Invalid streaming URL:
    streaming URL has expired``), but Strands hands each refusal to the model as an ordinary tool
    error: it retried screenshots and other tools until it gave up, and the task ended "Completed".
    Only refusals from the Agent Access endpoint are matched.
    """

    def __init__(self, endpoint):
        super().__init__()
        self._pattern = re.compile(r"HTTP 400 [^:\n]*? from " + re.escape(endpoint.rstrip("/"))
                                   + r"[^\n]*?Invalid streaming URL[^\n]*")

    def _on_tool(self, event):
        text = _error_text(event.result)
        match = self._pattern.search(text) if text else None
        if match:
            self._stop_run(event.agent, StreamingUrlRefused(match.group(0)))


class McpConnectionLost(RuntimeError):
    """The MCP session is gone: every desktop action now fails the same way."""

    conversation_note = "The task was stopped: the connection to the desktop was lost, so no further actions were possible."


class ConnectionLostGuard(_CancelAndRaise):
    """Ends a run once the MCP connection has demonstrably died.

    When the session is disabled or the connection drops for good, every tool call fails with
    "the client session is not running" (or "connection to the MCP server was closed"), and the
    model keeps trying. After ``limit`` such failures in a row the run is stopped.
    """

    _SYMPTOMS = ("client session is not running", "connection to the mcp server was closed")

    def __init__(self, limit=5):
        super().__init__()
        self._limit = limit
        self._failures = 0

    def _on_tool(self, event):
        text = _error_text(event.result)
        if text is None or not any(symptom in text.lower() for symptom in self._SYMPTOMS):
            self._failures = 0
            return
        self._failures += 1
        if self._failures >= self._limit:
            failures, self._failures = self._failures, 0
            self._stop_run(event.agent, McpConnectionLost(
                f"MCP connection permanently lost ({failures} consecutive failures). "
                "The session may have been disabled."))


def strip_reasoning(messages):
    """Remove the model's thinking blocks (``reasoningContent``) from ``messages``, in place.

    Returns how many were removed. A message the removal would leave empty is kept as it is.
    """
    removed = 0
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        kept = [block for block in content if not (isinstance(block, dict) and "reasoningContent" in block)]
        if len(kept) != len(content) and kept:
            removed += len(content) - len(kept)
            message["content"] = kept
    return removed


class DropReasoning:
    """Keeps the model's thinking blocks out of the history it is sent back.

    Claude 5.x signs each thinking block against the conversation before it, and rejects the request
    ("Invalid `signature` in `thinking` block ... bound to a different conversation") if anything
    earlier has changed since: a screenshot replaced by a placeholder, a rewritten tool input, trimmed
    history. This agent edits its history all the time, so the thinking blocks are removed before
    every model call. The model keeps what it did and saw, not its earlier private reasoning. They sit
    after the last cache point of the request that produced them, so removing them does not
    invalidate the prompt cache.
    """

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeModelCallEvent, self._before_model_call)

    def _before_model_call(self, event):
        strip_reasoning(event.agent.messages)


class StopAfterFailedAction:
    """Skips the rest of a model turn's tool calls once one of them has failed.

    The model plans a turn's actions (click, type, press Enter...) assuming each works. When one
    fails the later ones act on a screen the model did not expect, so they are answered with "not
    executed" and the model looks again.
    """

    def __init__(self):
        self._failed = None

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeModelCallEvent, self._new_turn)
        registry.add_callback(BeforeToolCallEvent, self._skip_if_failed)
        registry.add_callback(AfterToolCallEvent, self._note_failure)

    def _new_turn(self, event):
        self._failed = None

    def _skip_if_failed(self, event):
        if self._failed is not None:
            event.cancel_tool = (
                f"Not executed: an earlier action in this turn ({self._failed}) failed. "
                "Take a screenshot to see the current state, then decide what to do.")

    def _note_failure(self, event):
        if self._failed is None and _error_text(event.result) is not None:
            self._failed = as_service_call(event.tool_use.get("name", "tool"), event.tool_use.get("input"))[0]


class RunLimitReached(RuntimeError):
    """A task used up its model calls or its time (``--max-turns`` / ``--max-seconds``)."""

    def __init__(self, message):
        super().__init__(message)
        self.conversation_note = f"The task was stopped: {message}"


class RunInterrupted(Exception):
    """The user pressed Ctrl-C while a task was running."""

    conversation_note = "The task was stopped by the user."

    def __init__(self):
        super().__init__("Interrupted")


class RunBudgetGuard(_CancelAndRaise):
    """Ends a task that has made ``max_turns`` model calls or run for ``max_seconds``.

    The limits stop a run that is going nowhere (a model stuck repeating itself, a desktop that
    never answers) from spending unbounded time and tokens. They are checked per ``agent(...)``
    call, so each task typed into an interactive session gets a fresh budget. The clock runs on
    a timer thread, so it also fires while the model or a desktop action is still in progress.
    """

    def __init__(self, max_turns=DEFAULT_MAX_TURNS, max_seconds=DEFAULT_MAX_SECONDS):
        super().__init__()
        self._max_turns = max_turns
        self._max_seconds = max_seconds
        self._turns = 0
        self._timer = None
        self._lock = threading.Lock()

    def register_hooks(self, registry, **kwargs):
        super().register_hooks(registry, **kwargs)
        registry.add_callback(AfterModelCallEvent, self._count_turn)

    def _reset(self, event):
        super()._reset(event)
        self._turns = 0
        self._cancel_timer()
        if self._max_seconds:
            timer = threading.Timer(self._max_seconds, self._out_of_time, args=(event.agent,))
            timer.daemon = True
            with self._lock:
                self._timer = timer
            timer.start()

    def _cancel_timer(self):
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _out_of_time(self, agent):
        with self._lock:
            if self._timer is None:          # the task ended just before the timer fired
                return
        self._stop_run(agent, RunLimitReached(
            f"it ran for {self._max_seconds} seconds (--max-seconds). "
            "Raise the limit, or 0 to remove it, for longer tasks."))

    def _count_turn(self, event):
        self._turns += 1

    def _on_tool(self, event):
        # Stopping needs a safe point. After a tool call is the one where the model has just asked
        # for more work: a model call that ends the task (no tool call) is let through.
        if self._max_turns and self._turns >= self._max_turns:
            self._stop_run(event.agent, RunLimitReached(
                f"it used {self._turns} model calls (--max-turns). "
                "Raise the limit, or 0 to remove it, for longer tasks."))

    def _raise_pending(self, event):
        self._cancel_timer()
        super()._raise_pending(event)


_running = set()                 # InterruptGuards of tasks that are running
_running_lock = threading.Lock()


class InterruptGuard(_CancelAndRaise):
    """Lets Ctrl-C end the running task cleanly (see :func:`setup_signal_handler`)."""

    def __init__(self):
        super().__init__()
        self._agent = None

    def _reset(self, event):
        super()._reset(event)
        self._agent = event.agent
        with _running_lock:
            _running.add(self)

    def _raise_pending(self, event):
        with _running_lock:
            _running.discard(self)
        super()._raise_pending(event)

    def request_stop(self):
        """Ask the task to stop. False if it was already asked to (or is not running)."""
        agent = self._agent
        if agent is None or self._stop is not None:
            return False
        self._stop_run(agent, RunInterrupted())
        return True


def setup_signal_handler(agent_logger=None):
    """Make Ctrl-C stop a running task cleanly instead of killing the process.

    The first press cancels the task: the current desktop action finishes, then ``agent(...)``
    raises :class:`RunInterrupted` and the caller writes its metrics and exits, or (an interactive
    session) returns to its prompt. A second press, or a press while no task is running, raises
    ``KeyboardInterrupt`` as usual. ``SIGTERM`` (``kill``, a container stop) is handled the same way, so
    the process exits through its exit handlers and ends its desktop session instead of leaving it
    attached to the service. Only the main thread can install a signal handler.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum, frame):
        with _running_lock:
            guards = list(_running)
        if any([guard.request_stop() for guard in guards]):
            sys.stdout.write("\n⚠️  Stopping after the current action... (press Ctrl-C again to quit now)\n")
            sys.stdout.flush()
            return
        if signum == signal.SIGTERM:
            raise SystemExit(128 + signal.SIGTERM)    # unwinds through the exit handlers: see close_mcp_clients
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def agent_hooks(args, *hooks):
    """``hooks`` as a list, plus the guard for how the session is authenticated: :class:`DomainJoinGuard`
    for Domain Join, :class:`StreamingUrlGuard` for a streaming URL."""
    hooks = list(hooks)
    endpoint = resolve_mcp_endpoint(args)
    if get_auth_mode(args) == AUTH_SAML:
        hooks.append(DomainJoinGuard(endpoint))
    else:
        hooks.append(StreamingUrlGuard(endpoint))
    return hooks

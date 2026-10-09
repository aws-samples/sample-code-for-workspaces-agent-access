# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The interactive terminal for ``generic_cua``: your messages and the agent's output kept apart.

Inline, like Claude Code (no full-screen takeover, so scrollback and copy work as usual):

* the input sits between a rule and a hint line, with history (Up/Down), Esc+Enter for a new
  line, and Ctrl-C to clear it (twice on an empty line to quit);
* a sent message stays in the transcript in a box of its own;
* the agent's replies are rendered as Markdown after a ``●``, and every desktop action it takes is
  one line under it (failures in red, typed text never shown: the run logger formats the lines);
* while a task runs, a status line shows what the agent is doing and for how long;
* each task ends with one summary line (time, model calls, actions, tokens), and so does the session.

It is only used when both stdin and stdout are terminals; piped input (for example a script that
feeds tasks on stdin) and ``--plain`` keep the plain output.
"""

import os
import sys
import threading
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent

from .computer_tool import as_service_call

EXIT_WORDS = ("exit", "quit", "/exit", "/quit")
HELP = """\
**Commands**

- `/help`: this list
- `/status`: model, region and what this session has used so far
- `/clear`: start a new conversation (the desktop is not touched)
- `/exit` or `/quit` (or `exit`, `quit`, Ctrl-D): end the session

**Keys**

- Enter sends; Esc then Enter adds a new line
- Up / Down: earlier messages
- Ctrl-C: stop the running task after its current action; at the prompt it clears the line,
  and a second Ctrl-C on an empty line quits
"""


def wanted(args):
    """True when the interactive terminal should be used instead of plain output."""
    return (not getattr(args, "plain", False) and sys.stdin.isatty() and sys.stdout.isatty()
            and os.environ.get("TERM") != "dumb")


class _Status:
    """The live line under the transcript while a task runs: spinner, what is happening, elapsed time."""

    def __init__(self, ui):
        self._ui = ui
        self._spinner = Spinner("dots", style="cyan")

    def __rich__(self):
        ui = self._ui
        with ui._lock:
            text, label, started = ui._text, ui._label, ui._task_started
        elapsed = int(time.monotonic() - started) if started else 0
        self._spinner.update(text=Text(f" {label}… {elapsed}s · Ctrl-C to stop", style="dim"))
        if text.strip():
            return Group(_reply(text), self._spinner)
        return self._spinner


def _reply(text):
    """The agent's text after a bullet, rendered as Markdown."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text("●", style="bold"), Markdown(text.strip()))
    return grid


class TerminalUI:
    """Renders one interactive session. Pass :meth:`callback_handler` as the agent's callback handler,
    the instance itself as a hook provider, and :meth:`on_action` as the run logger's ``display_sink``."""

    def __init__(self, model_id, region, agent_logger=None, console=None):
        self.console = console or Console(highlight=False)
        self._model_id = model_id
        self._region = region
        self._logger = agent_logger
        self._lock = threading.Lock()
        self._text = ""
        self._label = "Thinking"
        self._task_started = None
        self._live = None
        self._session = None
        self._tasks = 0

    # --- what the agent does (called from the agent's worker thread) ---------------------------

    def callback_handler(self, **kwargs):
        data = kwargs.get("data")
        if data:
            with self._lock:
                self._text += data
                self._label = "Writing"
        if kwargs.get("reasoningText"):
            with self._lock:
                self._label = "Thinking"
        start = (kwargs.get("event") or {}).get("contentBlockStart", {}).get("start", {})
        if "toolUse" in start:
            self._flush_text()
            with self._lock:
                self._label = "Deciding on an action"

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeModelCallEvent, self._before_model_call)
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _before_model_call(self, event):
        self._flush_text()
        with self._lock:
            self._label = "Thinking"

    def _before_tool_call(self, event):
        self._flush_text()
        name, _ = as_service_call(event.tool_use.get("name", "tool"), event.tool_use.get("input"))
        with self._lock:
            self._label = f"Running {name}"

    def on_action(self, text, succeeded, error=None):
        """One desktop action has finished (the run logger's ``display_sink``)."""
        icon, _, rest = text.partition(" ")
        if rest and not icon.isascii():
            text = rest              # the logger's emoji: terminals disagree on their width, which garbles lines
        line = Text("  ⎿ ", style="dim")
        if succeeded:
            line.append(text, style="dim")
        else:
            line.append(f"{text} failed", style="red")
            if error:
                line.append(f": {' '.join(str(error).split())[:120]}", style="red dim")
        self.console.print(line)

    def _flush_text(self):
        with self._lock:
            text, self._text = self._text, ""
        if text.strip():
            self.console.print(_reply(text))

    # --- one task ---------------------------------------------------------------------------------

    def begin_task(self, task):
        self._tasks += 1
        self.console.print(Panel(Text(task), box=box.ROUNDED, border_style="cyan", expand=False,
                                 padding=(0, 1)))
        self._counts = self._usage()
        with self._lock:
            self._text, self._label, self._task_started = "", "Thinking", time.monotonic()
        self._live = Live(_Status(self), console=self.console, refresh_per_second=8, transient=True,
                          redirect_stdout=True, redirect_stderr=True)
        self._live.start()

    def end_task(self, outcome="done", message=None):
        """``outcome`` is ``done``, ``stopped`` (Ctrl-C, a run limit) or ``error``."""
        self._flush_text()
        if self._live is not None:
            self._live.stop()
            self._live = None
        with self._lock:
            started, self._task_started = self._task_started, None
        seconds = time.monotonic() - started if started else 0
        usage = [now - before for now, before in zip(self._usage(), self._counts)]
        summary = " · ".join([_duration(seconds), _usage_text(*usage)])
        if outcome == "done":
            self.console.print(Text(f"✓ Done · {summary}", style="green"))
        elif outcome == "stopped":
            self.console.print(Text(f"⚠ {message or 'Stopped'} · {summary}", style="yellow"))
        else:
            self.console.print(Panel(Text(message or "Error"), title="Error", title_align="left",
                                     border_style="red", box=box.ROUNDED, expand=False))
            self.console.print(Text(summary, style="dim"))
        self.console.print()

    def _usage(self):
        """(model calls, actions, failed actions, input tokens, output tokens) so far, from the run logger."""
        if self._logger is None:
            return (0, 0, 0, 0, 0)
        metrics = self._logger.metrics
        models, tools = metrics.get("model_calls", []), metrics.get("tool_calls", [])
        return (len(models), len(tools), sum(1 for t in tools if not t.get("success", True)),
                sum((m.get("input_tokens") or 0) + (m.get("cache_read_tokens") or 0)
                    + (m.get("cache_write_tokens") or 0) for m in models),
                sum(m.get("output_tokens") or 0 for m in models))

    # --- input ----------------------------------------------------------------------------------

    def read_task(self):
        """The next message, or ``None`` to end the session."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings

        if self._session is None:
            keys = KeyBindings()

            @keys.add("escape", "enter")
            def _(event):
                event.current_buffer.insert_text("\n")

            # in memory only: tasks can name accounts or contain values a user would not want on disk
            self._session = PromptSession(
                history=InMemoryHistory(), key_bindings=keys, erase_when_done=True,
                prompt_continuation="  ",
                bottom_toolbar=HTML(f"<b>{self._model_id}</b> · /help for commands · Esc+Enter for a new line"),
            )
        armed = False
        while True:
            self.console.print(Rule(style="bright_black"))
            try:
                text = self._session.prompt(HTML("<ansicyan><b>❯</b></ansicyan> ")).strip()
            except KeyboardInterrupt:
                if armed:
                    return None
                armed = True
                self.console.print(Text("Press Ctrl-C again to quit, or type a task.", style="dim"))
                continue
            except EOFError:
                return None
            if text:
                return text
            armed = False

    # --- the loop ---------------------------------------------------------------------------------

    def run(self, agent, interrupted=(KeyboardInterrupt,)):
        """Read tasks and run them on ``agent`` until the user quits, then print what the session used.
        ``interrupted`` are the exceptions that mean the task was stopped (Ctrl-C, a run limit) rather
        than failed."""
        self.console.print(Text("Type a task and press Enter. /help lists the commands.\n", style="dim"))
        started = time.monotonic()
        try:
            self._loop(agent, interrupted)
        finally:
            self.console.print(Text(
                f"Session ended · {_duration(time.monotonic() - started)} · {self._tasks} task"
                f"{'s' * (self._tasks != 1)} · {_usage_text(*self._usage())}", style="bold"))

    def _loop(self, agent, interrupted):
        while True:
            task = self.read_task()
            if task is None or task.lower() in EXIT_WORDS:
                return
            if task.startswith("/"):
                self._command(task, agent)
                continue
            self.begin_task(task)
            try:
                agent(task)
            except interrupted as e:
                self.end_task("stopped", str(e) if str(e) not in ("", "Interrupted") else "Stopped")
            except Exception as e:      # the session stays usable: say what happened, then ask again
                self.end_task("error", f"{type(e).__name__}: {e}")
            else:
                self.end_task("done")

    def _command(self, text, agent):
        name = text.split()[0].lower()
        if name == "/help":
            self.console.print(Markdown(HELP))
        elif name == "/clear":
            agent.messages.clear()
            self.console.print(Text("Started a new conversation. The desktop is as it was.", style="dim"))
        elif name == "/status":
            self.console.print(Text(
                f"Model {self._model_id} · Bedrock region {self._region} · {self._tasks} task"
                f"{'s' * (self._tasks != 1)} · {_usage_text(*self._usage())}", style="dim"))
        else:
            self.console.print(Text(f"Unknown command {name}. /help lists the commands.", style="yellow"))
        self.console.print()


def _usage_text(calls, actions, failed, tokens_in, tokens_out):
    parts = [f"{calls} model call{'s' * (calls != 1)}",
             f"{actions} action{'s' * (actions != 1)}" + (f" ({failed} failed)" if failed else "")]
    if tokens_in or tokens_out:
        parts.append(f"{_k(tokens_in)} in / {_k(tokens_out)} out tokens")
    return " · ".join(parts)


def _duration(seconds):
    """45s, 12m 04s, 1h 03m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

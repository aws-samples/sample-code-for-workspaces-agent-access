# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Screenshot Pruning Conversation Manager.

Replaces old screenshots in the conversation with a short text placeholder before every
model call, so a long run does not re-send every screen it has ever seen. Screenshots are
already saved to disk by StrandsAgentLogger, so no data is lost.

Prompt caching matches an exact prefix of the request, so replacing a screenshot changes the
request from that message onward. ``batch=N`` lets the window grow by N screenshots and prunes
back to ``keep_last_n`` in one go, so the history stays byte-identical for the calls in between.
Whether that beats pruning before every call (``batch=1``) depends on how big the stable prefix
(tools + system prompt) is next to the screenshots; an AWS write-up measured a large saving with
``keep_last_n=1`` and ``batch=1``. It is a setting to measure, not to assume.
"""

from typing import Any

from strands.agent.conversation_manager.conversation_manager import ConversationManager
from strands.hooks import BeforeModelCallEvent

DEFAULT_KEEP_SCREENSHOTS = 3
DEFAULT_PRUNE_BATCH = 1
DEFAULT_PRUNE_BATCH_CACHED = 10      # the agent's default with prompt caching on (about 45% lower cost at the same success in a live comparison)

PLACEHOLDER = "[screenshot — saved to disk, removed from context]"


def _is_image(block):
    return isinstance(block, dict) and ("image" in block or block.get("type") in ("image", "image_url"))


def screenshot_locations(messages):
    """Where every screenshot is, oldest first.

    A location is ``(message, block)`` for an image sitting in a message, or
    ``(message, block, inner)`` for one inside a tool result. Handles the Converse format and the
    OpenAI-compatible one Strands builds for bedrock-mantle models.
    """
    locations = []
    for msg_idx, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in ("user", "tool"):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block_idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if _is_image(block):
                locations.append((msg_idx, block_idx))
            inner = block.get("toolResult", {}).get("content") if "toolResult" in block else None
            if isinstance(inner, list):
                locations.extend((msg_idx, block_idx, i) for i, b in enumerate(inner) if _is_image(b))
    return locations


def prune_screenshots(messages, keep_last_n=DEFAULT_KEEP_SCREENSHOTS, batch=DEFAULT_PRUNE_BATCH):
    """Replace all but the newest ``keep_last_n`` screenshots with a placeholder, in place.

    Nothing happens until there are ``keep_last_n + batch`` screenshots, so with ``batch=1`` every
    call prunes down to ``keep_last_n``, and with a larger batch the pruning (and the cache
    invalidation that comes with it) happens once per ``batch`` new screenshots. Only image blocks
    change: no message or tool result is ever removed, so tool-use/tool-result pairs stay intact.
    Returns how many screenshots were replaced.
    """
    keep_last_n = max(1, keep_last_n)
    locations = screenshot_locations(messages)
    if len(locations) < keep_last_n + max(1, batch):
        return 0
    old = locations[:-keep_last_n]
    for location in old:
        msg_idx, block_idx = location[0], location[1]
        if len(location) == 2:
            messages[msg_idx]["content"][block_idx] = {"text": PLACEHOLDER}
        else:
            messages[msg_idx]["content"][block_idx]["toolResult"]["content"][location[2]] = {"text": PLACEHOLDER}
    return len(old)


def _has_tool_result(message):
    return any(isinstance(b, dict) and "toolResult" in b for b in message.get("content", []) or [])


def trim_history(messages, max_messages, minimum=0):
    """Drop the oldest messages so at most ``max_messages`` remain (at least ``minimum`` if a safe start is further).

    The new first message is always a user message that answers nothing: cutting between a tool
    call and its result would leave the result without its call, which Bedrock rejects. Returns how
    many messages were dropped.
    """
    if max_messages <= 0 or len(messages) <= max_messages:
        return 0
    start = len(messages) - max_messages
    while start < len(messages) and (messages[start].get("role") != "user" or _has_tool_result(messages[start])):
        start += 1
    if start >= len(messages):      # no safe place to cut inside the window: keep everything
        return 0
    del messages[:start]
    return start


class ScreenshotPruningConversationManager(ConversationManager):
    """Prunes old screenshot images from the conversation before each model call.

    Usage:
        agent = Agent(
            model=model,
            tools=[mcp_client],
            system_prompt=system_prompt,
            conversation_manager=ScreenshotPruningConversationManager(keep_last_n=3),
        )
    """

    PLACEHOLDER = PLACEHOLDER

    def __init__(self, keep_last_n: int = DEFAULT_KEEP_SCREENSHOTS, max_messages: int = 0,
                 batch: int = DEFAULT_PRUNE_BATCH):
        """Initialize the manager.

        Args:
            keep_last_n: Screenshots kept in context (the newest ones).
            max_messages: Also keep at most this many messages (0 = no limit), cutting only where
                no tool call is separated from its result. bedrock-mantle has a strict request
                size limit (~20MB) and each screenshot is 1-2MB in its wire format, so that path
                uses a tight window.
            batch: Prune when there are ``keep_last_n + batch`` screenshots. 1 prunes before every
                model call (see the module docstring for the prompt-caching trade-off).
        """
        super().__init__()
        self.keep_last_n = max(1, keep_last_n)
        self.max_messages = max_messages
        self.batch = max(1, batch)

    def register_hooks(self, registry, **kwargs: Any) -> None:
        super().register_hooks(registry, **kwargs)
        registry.add_callback(BeforeModelCallEvent, self._before_model_call)

    def _before_model_call(self, event: BeforeModelCallEvent) -> None:
        self.apply_management(event.agent)

    def apply_management(self, agent: "Agent", **kwargs: Any) -> None:
        """Trim the history to ``max_messages``, then prune old screenshots. Edits ``agent.messages`` in place."""
        self.removed_message_count += trim_history(agent.messages, self.max_messages)
        prune_screenshots(agent.messages, self.keep_last_n, self.batch)

    def reduce_context(self, agent: "Agent", e: Exception | None = None, **kwargs: Any) -> None:
        """Handle a context window overflow by trimming hard.

        Keeps only the last few messages (starting where no tool call is cut from its result),
        prunes every screenshot but the newest, and tells the model the history was reduced so it
        looks at the screen instead of restarting the task.
        """
        self.removed_message_count += trim_history(agent.messages, 4)
        prune_screenshots(agent.messages, 1, 1)

        note = {"text": (
            "[CONTEXT TRIMMED — conversation history was reduced to stay within payload limits. "
            "Take a screenshot to see the current desktop state, then continue from where you left "
            "off. Do NOT restart the task from the beginning.]")}
        if agent.messages and agent.messages[0].get("role") == "user":
            agent.messages[0]["content"].insert(0, note)      # roles must alternate: no second user message
        else:
            agent.messages.insert(0, {"role": "user", "content": [note]})

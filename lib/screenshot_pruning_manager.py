# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Screenshot Pruning Conversation Manager.

A ConversationManager that strips old screenshot image data from the conversation
history before each model call, keeping only the most recent screenshot. This
dramatically reduces token usage for computer-use agents that take many screenshots.

Screenshots are already saved to disk by StrandsAgentLogger, so no data is lost.
"""

from typing import Any

from strands.agent.conversation_manager.conversation_manager import ConversationManager


class ScreenshotPruningConversationManager(ConversationManager):
    """Prunes old screenshot images from conversation history.

    Before each model invocation, walks the message history and replaces all
    screenshot image content blocks except the most recent one with a lightweight
    text placeholder. This keeps the LLM focused on the current screen state
    while preserving the conversational flow.

    Usage:
        from lib import ScreenshotPruningConversationManager

        agent = Agent(
            model=model,
            tools=[mcp_client],
            system_prompt=system_prompt,
            conversation_manager=ScreenshotPruningConversationManager(),
        )
    """

    PLACEHOLDER = "[screenshot — saved to disk, removed from context]"

    def __init__(self, keep_last_n: int = 1):
        """Initialize the manager.

        Args:
            keep_last_n: Number of most recent screenshots to keep in context.
                         Defaults to 1 (only the latest screenshot is sent to the model).
        """
        super().__init__()
        self.keep_last_n = max(1, keep_last_n)

    def apply_management(self, agent: "Agent", **kwargs: Any) -> None:
        """Strip old screenshot images from the conversation history.

        Walks agent.messages to find all image content blocks that came from
        screenshot tool results. Keeps the most recent `keep_last_n` screenshots
        intact and replaces older ones with a text placeholder.

        The messages list is modified in-place.
        """
        # Collect (message_index, content_block_index) for every screenshot image
        screenshot_locations = []

        for msg_idx, message in enumerate(agent.messages):
            role = message.get("role") if isinstance(message, dict) else None
            if role != "user":
                continue

            content = message.get("content", [])
            if not isinstance(content, list):
                continue

            for block_idx, block in enumerate(content):
                if not isinstance(block, dict):
                    continue

                # Tool results contain content blocks. Screenshot tool results
                # have image blocks with format like:
                #   {"image": {"format": "png", "source": {"bytes": b'...'}}}
                # or nested inside toolResult.content
                if "image" in block:
                    screenshot_locations.append((msg_idx, block_idx))
                elif block.get("type") == "image":
                    screenshot_locations.append((msg_idx, block_idx))

                # Also check inside toolResult content blocks
                if "toolResult" in block:
                    tool_result = block["toolResult"]
                    tr_content = tool_result.get("content", [])
                    if isinstance(tr_content, list):
                        for tr_idx, tr_block in enumerate(tr_content):
                            if not isinstance(tr_block, dict):
                                continue
                            if "image" in tr_block or tr_block.get("type") == "image":
                                screenshot_locations.append((msg_idx, block_idx, tr_idx))

        if len(screenshot_locations) <= self.keep_last_n:
            return  # Nothing to prune

        # Keep the last N, prune the rest
        to_prune = screenshot_locations[:-self.keep_last_n]

        for location in to_prune:
            if len(location) == 2:
                msg_idx, block_idx = location
                agent.messages[msg_idx]["content"][block_idx] = {
                    "text": self.PLACEHOLDER
                }
            elif len(location) == 3:
                msg_idx, block_idx, tr_idx = location
                tool_result = agent.messages[msg_idx]["content"][block_idx]["toolResult"]
                tool_result["content"][tr_idx] = {
                    "text": self.PLACEHOLDER
                }

    def reduce_context(self, agent: "Agent", e: Exception | None = None, **kwargs: Any) -> None:
        """Handle context window overflow by aggressively pruning all but the latest screenshot.

        This is called when the model's context window is exceeded. We prune all
        screenshots except the very latest one, regardless of keep_last_n.
        """
        # Force keep only 1 for emergency reduction
        original = self.keep_last_n
        self.keep_last_n = 1
        self.apply_management(agent, **kwargs)
        self.keep_last_n = original

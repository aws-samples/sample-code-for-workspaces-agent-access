---
version: "1.0.1"
description: "Example task prompt — open Notepad and type a note with tool sequencing"
last_updated: "2026-11-12"
---

# Task: Open Notepad and Type a Note with Tool Sequencing

Use `agentaccess___execute_sequence` to send several desktop actions in one call. A sequence that succeeds returns one screenshot, taken after its last action. Check it before you send the next sequence. A sequence that fails returns text only, so take a screenshot before you go on.

## Steps

1. Take a screenshot to see the desktop. Do this before the first sequence.
2. Open Notepad from the Run dialog with one `agentaccess___execute_sequence` call: press `super+r`, wait 1 second, type `notepad`, wait 1 second, press `Return`, then wait 3 seconds.
3. Look at the screenshot the sequence returned. If Notepad is not open and in front, say what you see and open it another way.
4. Type this note with a second `agentaccess___execute_sequence` call: one `type_text` action per line, with a `Return` key action between them.
   ```
   Typed by an agent with one execute_sequence call.
   Each line is a type_text action, and the line break is a key action.
   ```
5. Check that the returned screenshot shows both lines.

## Writing the actions

- Each action names the tool it runs in `type` and carries that tool's arguments, for example `{"type": "agentaccess___type_text", "text": "notepad"}` or `{"type": "agentaccess___key", "keys": "Return"}`.
- To pause inside a sequence, use `{"type": "agentaccess___wait", "duration": 1}`. `duration` is in seconds, at most 5. This agent's own `wait` tool, which takes `seconds`, runs on the agent's machine and cannot be an action.
- A screenshot cannot be an action: the sequence takes one after its last action.

## Verification

Report how many actions each sequence ran and what the last screenshot shows. Leave the note unsaved and Notepad open. Ending the session when you are done does not close Notepad.

---
version: "1.0.0"
description: "Task prompt for the Replay Session demo - short Notepad task to record"
last_updated: "2026-10-22"
---

# Task: Do a Short, Recorded Desktop Task

This stack records your session with **Replay Session**. Do the steps below in
order; each one becomes part of the recording that is reviewed afterwards.

---

## Step 1: Identify the session

Call `get_session_info` and report the `mcpSessionId` it returns. The recording
is delivered to a folder named after this ID.

---

## Step 2: Write a note in Notepad

1. Open Notepad: `key("super+r")`, `type_text("notepad")`, `key("Return")`.
2. Take **one** `screenshot` to confirm Notepad is open and focused.
3. Type this text with `type_text`:
   `Replay Session demo: this note was written by an AI agent.`
4. Save it: `key("ctrl+s")`, then type the file name
   `C:\Users\Public\Documents\replay_session_demo.txt` and press `key("Return")`.
   If a "replace existing file" prompt appears, choose **Yes**.

---

## Step 3: Confirm on the desktop

Take **one** `screenshot` and confirm the title bar shows
`replay_session_demo.txt`. If a dialog blocks you, `key("Escape")` and report
what you saw — this step is confirmation, not the core of the task.

---

## Final Report

Summarize:
- the `mcpSessionId` from Step 1,
- the actions you took, in order,
- whether the file was saved and confirmed on screen.

Keep it concise.

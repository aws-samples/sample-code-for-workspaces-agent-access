---
version: "1.0.0"
description: "Task prompt for the MCP tool forwarding demo - exercise forwarded filesystem + fetch tools"
last_updated: "2026-07-13"
---

# Task: Exercise Forwarded MCP Tools (filesystem + fetch)

This fleet has **MCP tool forwarding** enabled. Your tool list therefore contains
forwarded tools (prefixed with `forwarded___`) from two servers running on the
Windows host: a **filesystem** server and a **fetch** server.

Your goal is to prove those forwarded tools work end to end and then confirm the
result on the desktop. Do the steps in order. Do **not** open apps to read or
write files — use the forwarded filesystem tools directly.

The filesystem server is sandboxed to `C:\Users\Public\Documents`. Keep all
paths inside it. The `setup_mcp_forwarding.sh` script seeds `hello.txt` and
`test.txt` there.

---

## Step 1: Discover the forwarded tools

Look at your available tools and identify the ones prefixed with `forwarded___`.
State which forwarded tools you found and which server (filesystem or fetch)
each belongs to. If there are **no** `forwarded___` tools, stop and report that
MCP tool forwarding is not active on this fleet (the stack needs
`FORWARD_MCP_TOOLS: ENABLED` and the image must include the MCP servers).

---

## Step 2: List and read seeded files (forwarded filesystem)

1. Call the forwarded **list-directory** tool on `C:\Users\Public\Documents`.
   Report the entries. You should see `hello.txt` and `test.txt`.
2. Call the forwarded **read-file** tool on
   `C:\Users\Public\Documents\hello.txt`. Report its exact contents.

---

## Step 3: Fetch web content (forwarded fetch)

1. Call the forwarded **fetch** tool on `https://example.com` with a small
   `max_length` (e.g. 500).
2. Report the first line or heading of the returned text (e.g. the
   "Example Domain" title).

---

## Step 4: Write a report file (forwarded filesystem)

Combine what you gathered and write a new file with the forwarded
**write-file** tool:

- Path: `C:\Users\Public\Documents\mcp_forwarding_report.txt`
- Content: a short plain-text report containing
  1. the directory listing from Step 2,
  2. the contents of `hello.txt`,
  3. the title/first line fetched from `example.com` in Step 3.

Then call the forwarded **read-file** tool on
`C:\Users\Public\Documents\mcp_forwarding_report.txt` and confirm it was
written correctly.

---

## Step 5: Visual confirmation on the desktop (desktop tools)

Show that the forwarded write is visible to the desktop itself:

1. Open the file in Notepad: `key("super+r")`, `type_text("notepad C:\\Users\\Public\\Documents\\mcp_forwarding_report.txt")`, `key("Return")`.
2. Take **one** `screenshot` to confirm the report is on screen.
3. If a dialog blocks you, `key("Escape")` and continue — this step is
   best-effort confirmation, not the core of the task.

---

## Final Report

Summarize:
- which `forwarded___` tools you called and what each returned,
- confirmation that the report file was written and read back correctly,
- whether the visual confirmation succeeded.

Keep it concise.

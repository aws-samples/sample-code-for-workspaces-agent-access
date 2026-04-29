---
name: workspace-skill-creator
description: >
  Create skills for automating Windows desktop applications on WorkSpaces via computer use tools.
  Use this skill whenever someone wants to create a new application skill, improve an existing
  desktop automation skill, or document how to interact with a specific Windows application
  (e.g., Excel, Notepad, Paint, File Explorer, browser, any installed desktop app).
  Also use when the user mentions "skill", "application guide", "app instructions",
  or wants to teach the agent how to use a new program.
---

# Workspace Application Skill Creator

A skill for creating and improving skills that automate Windows desktop applications
via screenshot-based computer use tools (click, type, drag, screenshot, etc.).

## What This Skill Produces

A JSON skill file in OpenAPI-inspired format that teaches a computer use agent how to
interact with a specific Windows application. The output includes:

- Application launch and setup instructions
- Toolbar/menu layout maps with spatial descriptions
- Tool-by-tool interaction patterns (click sequences, drag patterns, keyboard shortcuts)
- Coordinate planning strategies for pixel-accurate actions
- Error recovery patterns specific to the application
- Action batching guidance to minimize unnecessary screenshots

## When to Use

- User wants to automate a new desktop application
- User wants to improve an existing application skill
- User says "create a skill for [app name]"
- User wants to document how to interact with a Windows program
- User has a workflow in an app they want to capture as reusable instructions

## Process

### 1. Capture Intent

Start by understanding what application and what tasks within it:

- Which Windows application? (exact name, how to launch it)
- What tasks should the agent be able to do? (draw, edit, fill forms, navigate menus)
- What's the application's UI layout? (ribbon, toolbar, sidebar, canvas, etc.)
- Are there known gotchas? (dialogs that pop up, tools that behave unexpectedly)

If the user has already been working with the app in conversation (e.g., they just
finished a session and want to capture what they learned), extract the interaction
patterns, tool sequences, and corrections from the conversation history first.

### 2. Research the Application

Before writing, gather information about the target application:

- Standard UI layout and toolbar organization
- Common keyboard shortcuts
- Known interaction patterns (click vs drag vs double-click for different operations)
- File format support and save/export workflows
- Common error states and how to recover from them

If the user has screenshots or can describe the UI, use that as the primary source.
The skill should reflect the actual UI the agent will encounter, not generic documentation.

### 3. Write the Skill JSON

Generate a skill file following the schema in `references/skill-schema.md`.

The skill uses an OpenAPI-inspired JSON structure with these key sections:

#### Header
```json
{
  "openapi": "3.0.0",
  "info": {
    "title": "<App Name> - Computer Use Agent Skill",
    "version": "1.0.0",
    "description": "Instructions for a computer use agent to interact with <App Name> on Windows."
  }
}
```

#### Paths (one per operation category)

Each path represents a category of operations (launch, tool selection, drawing, saving, etc.).
Use `x-agent-instructions` to embed the actual guidance:

```json
"/launch": {
  "post": {
    "summary": "Launch <App Name>",
    "operationId": "launchApp",
    "x-agent-instructions": {
      "steps": ["Step 1", "Step 2"],
      "visualCues": { "windowTitle": "...", "mainArea": "..." },
      "verification": "Take a screenshot to confirm the app opened"
    }
  }
}
```

#### General Instructions

The `x-agent-general-instructions` section at the root level covers cross-cutting concerns:

- `applicationOverview` — name, type, purpose
- `windowLayout` — spatial description of UI regions
- `coordinatePlanning` — how to estimate pixel positions from screenshots
- `actionBatching` — when to batch actions vs take screenshots
- `errorRecovery` — common failure modes and recovery strategies
- `interactionPatterns` — click, drag, type patterns for this app
- `keyboardShortcuts` — app-specific shortcuts

### 4. Key Principles for Desktop Skills

These principles come from how computer use models actually work — they analyze
screenshots pixel-by-pixel and count distances from reference points. Skills that
account for this produce dramatically better results.

#### Spatial Descriptions Over Abstract Labels

Bad: "Click the save button"
Good: "Click the floppy disk icon in the top-left toolbar area, approximately 30px from the left edge"

The agent navigates by pixel coordinates derived from screenshots. Spatial context
(top-left, ribbon area, bottom status bar) helps it locate elements faster than
abstract names alone.

#### Coordinate Planning

Include guidance on how to plan coordinates before acting:
- Identify reference points (canvas corners, toolbar edges, status bar)
- Estimate target positions relative to those references
- For drag operations, calculate both start AND end coordinates before beginning
- Note the canvas origin point from the first screenshot

#### Action Batching

Computer use tools have latency — each screenshot round-trip costs time and tokens.
Skills should guide the agent to:
- Batch 3-5 related actions before taking a verification screenshot
- Only screenshot when verifying something unexpected or after completing a major step
- Plan sequences ahead of time rather than checking after every click

#### Error Recovery

Every application has failure modes. Document them explicitly:
- What does the error look like? (dialog box, unexpected color, wrong tool selected)
- What's the immediate recovery? (Ctrl+Z, close dialog, reselect tool)
- When should the agent take a screenshot to diagnose? (after unexpected behavior)

#### Tool State Awareness

Desktop apps have stateful tools — the selected tool, active color, brush size all
persist between actions. Skills should remind the agent to:
- Verify the correct tool is selected before drawing/editing
- Set both foreground and background colors when relevant
- Check tool options (size, fill style) before using a shape tool

### 5. Review and Iterate

After generating the skill:

1. Validate the JSON is syntactically correct
2. Check that every operation has clear steps, not just descriptions
3. Verify spatial descriptions match the actual UI layout
4. Ensure error recovery covers the most common failure modes
5. Test with a simple task to see if the agent can follow the instructions

If the user provides feedback from a test run, focus improvements on:
- Steps where the agent got confused or clicked the wrong thing
- Missing tool state setup (forgot to select color before drawing)
- Unnecessary screenshots that could be batched
- Missing error recovery for failures that occurred

## Output Format

Save the skill as `<app-name>-skill.json` in the agent's `skills/` directory.

## Example

See `agents/paint_demo/skills/ms-paint-skill.json` for a complete example of a
well-structured desktop application skill targeting MS Paint.

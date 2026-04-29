# Workspace Application Skill Schema

## Top-Level Structure

```json
{
  "openapi": "3.0.0",
  "info": {
    "title": "<App Name> - Computer Use Agent Skill",
    "version": "<semver>",
    "description": "Instructions for a computer use agent to interact with <App Name> on Windows."
  },
  "servers": [
    {
      "url": "desktop://localhost",
      "description": "Windows Desktop Application"
    }
  ],
  "paths": { ... },
  "x-agent-general-instructions": { ... }
}
```

## Paths

Each path represents an operation category. Use REST-style naming:

| Path Pattern | Purpose |
|---|---|
| `/launch` | How to open the application |
| `/tools/select` | Toolbar layout and tool selection |
| `/tools/<tool-name>` | Specific tool usage (pencil, fill, eraser, etc.) |
| `/colors/select` | Color picker interaction |
| `/file/save` | Save/export workflows |
| `/file/open` | Open file workflows |
| `/canvas/operations` | Canvas-level operations (undo, redo, resize) |
| `/menu/<menu-name>` | Menu navigation patterns |

## Path Entry Structure

```json
{
  "/tools/<name>": {
    "post": {
      "summary": "Short description of what this does",
      "operationId": "camelCaseIdentifier",
      "x-agent-instructions": {
        "steps": [
          "Step-by-step instructions the agent should follow"
        ],
        "tips": [
          "Additional guidance, gotchas, and best practices"
        ],
        "verification": "When/how to verify this action succeeded"
      }
    }
  }
}
```

## x-agent-general-instructions Fields

### Required

- `applicationOverview` — `{ name, type, purpose }`
- `windowLayout` — Spatial description of major UI regions
- `interactionPatterns` — How to click, drag, type in this app
- `keyboardShortcuts` — App-specific keyboard shortcuts

### Recommended

- `coordinatePlanning` — Strategy for estimating pixel positions
- `actionBatching` — Guidelines for batching actions vs screenshotting
- `errorRecovery` — Common failures and recovery patterns

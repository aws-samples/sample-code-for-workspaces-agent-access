# Generic Computer-Use Agent

An interactive agent that performs arbitrary tasks on a remote Windows desktop via DCV, built with the Strands Agents SDK and the WorkSpaces MCP client.

## Modes

- **REPL mode** (default): Interactive loop where you type tasks and the agent executes them.
- **Single-shot mode**: Pass `--task-prompt` to execute one task and exit.

## Usage

```bash
# REPL mode — interactive session
python3 agents/generic_cua/agent.py --streaming-url "<STREAMING_URL>"

# Single-shot mode — execute a task file and exit
python3 agents/generic_cua/agent.py --streaming-url "<STREAMING_URL>" \
  --task-prompt prompts/task_prompt.md

# With a custom system prompt and skill file
python3 agents/generic_cua/agent.py --streaming-url "<STREAMING_URL>" \
  --system-prompt my_prompt.md \
  --skill skills/ms-paint-skill.json

# With a specific model and region
python3 agents/generic_cua/agent.py --streaming-url "<STREAMING_URL>" \
  --model-id global.anthropic.claude-sonnet-4-6 \
  --region us-east-1
```

## CLI Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `--streaming-url` | Yes | — | AppStream streaming URL for the desktop session |
| `--system-prompt` | No | `prompts/system_prompt.md` | Path to a custom system prompt markdown file |
| `--task-prompt` | No | None | Path to a task prompt file; triggers single-shot mode |
| `--skill` | No | None | Path to a skill JSON file to append to the system prompt |
| `--model-id` | No | `global.anthropic.claude-sonnet-4-6` | Bedrock model ID |
| `--region` | No | `us-west-2` | AWS region for Bedrock |
| `--no-computer-use-tool` | No | off | Disable Claude's computer-use training optimizations |
| `--mcp-timeout` | No | `180` | MCP client startup timeout in seconds |
| `--mcp-retries` | No | `3` | Number of MCP client connection retries |

## Skill Files

A skill file is a JSON document that gives the agent application-specific knowledge (UI layout, tool locations, workflows). When provided via `--skill`, its contents are appended to the system prompt under an `=== SKILL ===` section:

```
{system prompt content}

=== SKILL ===
{skill JSON, pretty-printed}
```

See `skills/computer-use-skill.json` for an example. Any valid JSON file works — the agent receives it as additional context for reasoning about the target application.

## Structure

```
generic_cua/
├── agent.py              # Agent orchestrator
├── prompts/
│   ├── system_prompt.md  # Default generic desktop control prompt
│   └── task_prompt.md    # Example task prompt (Notepad)
├── skills/
│   └── computer-use-skill.json  # Example skill file (Notepad)
├── logs/                 # Runtime logs
├── metrics/              # Performance metrics
└── screenshots/          # Screenshots captured during execution
```

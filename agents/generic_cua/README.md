# Generic Computer-Use Agent

An interactive agent that performs arbitrary tasks on a remote Windows desktop via DCV, built with the Strands Agents SDK and the WorkSpaces MCP client.

## Modes

- **REPL mode** (default): Interactive loop where you type tasks and the agent executes them.
- **Single-shot mode**: Pass `--task-prompt` to execute one task and exit.

### The interactive terminal

On a terminal, REPL mode keeps your messages and the agent's output apart:

```
──────────────────────────────────────────────────
╭──────────────────────────────╮
│ Open Notepad and type a line │
╰──────────────────────────────╯
● I'll open the Run dialog and start Notepad.
  ⎿ Click (412,388)
  ⎿ type_text (21 chars)
  ⎿ key (1 keys) failed: backend unavailable
● Notepad is open and shows the line.
✓ Done · 37s · 9 model calls · 14 actions (1 failed) · 61.2k in / 1.4k out tokens
```

- Each message you send stays in its own box. The agent's replies follow a `●` and are rendered as Markdown, and each desktop action is one line under them (failures in red; typed text shows only its length).
- While a task runs, a status line shows what the agent is doing and for how long.
- Input: Enter sends, Esc then Enter adds a new line, Up/Down recall earlier messages (kept in memory only). Ctrl-C stops a running task after its current action; at the prompt it clears the line, and a second Ctrl-C on an empty line quits. Ctrl-D also quits.
- Commands: `/help`, `/status` (model, region, what the session has used), `/clear` (new conversation; the desktop is not touched), `/exit` or `/quit`.
- Ending the session (`/exit`, `exit`, `quit`, Ctrl-D, or Ctrl-C twice) prints one line with what it used: total time, tasks, model calls, actions and input/output tokens.
- `--plain` keeps the plain output. Input or output that is not a terminal (tasks piped in, output redirected to a file) is always plain.

## Usage

Run from the repository root.

```bash
# REPL mode — interactive session
python3 agents/generic_cua/agent.py --streaming-url "<STREAMING_URL>"

# Single-shot mode — execute a task file and exit
python3 agents/generic_cua/agent.py --streaming-url "<STREAMING_URL>" \
  --task-prompt agents/generic_cua/prompts/task_prompt.md

# With a custom system prompt and skill file
python3 agents/generic_cua/agent.py --streaming-url "<STREAMING_URL>" \
  --system-prompt agents/generic_cua/prompts/system_prompt.md \
  --skill agents/generic_cua/skills/computer-use-skill.json

# With a specific model and region
python3 agents/generic_cua/agent.py --streaming-url "<STREAMING_URL>" \
  --model-id global.anthropic.claude-sonnet-5-5 \
  --region us-east-1

# Domain-joined (AD) fleet — a SAML assertion and the stack ARN replace the streaming URL
chmod 600 assertion.b64
python3 agents/generic_cua/agent.py --saml-assertion-file assertion.b64 \
  --stack-arn "arn:aws:appstream:us-east-1:123456789012:stack/MyDJStack"
```

Each command above is one logical line: when you continue a line with `\`, the backslash must be the last character on it. A space after it passes a blank argument to the agent (`unrecognized arguments:`) and the shell runs the next line as a separate command (`command not found: --saml-assertion-file`). See "Domain Join" in the top-level README for the details.

## CLI Parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `--streaming-url` | Yes, unless using Domain Join | — | AppStream streaming URL for the desktop session |
| `--saml-assertion-file` | Domain Join | None | File holding the base64 SAML assertion (replaces `--streaming-url`; `AGENTACCESS_SAML_ASSERTION` is read when it is not given) |
| `--stack-arn` | Domain Join | None | AppStream stack ARN, required with the SAML assertion |
| `--system-prompt` | No | `prompts/system_prompt.md` | Path to a custom system prompt markdown file |
| `--task-prompt` | No | None | Path to a task prompt file; triggers single-shot mode |
| `--skill` | No | None | Path to a skill JSON file to append to the system prompt |
| `--plain` | No | off | Plain text output in REPL mode instead of the interactive terminal |
| `--model-id` | No | `global.anthropic.claude-sonnet-5-5` | Bedrock model ID |
| `--region` | No | `AWS_REGION`, else `us-east-1` | AWS region for Bedrock |
| `--effort` | No | model default | `low`, `medium` or `high`: reasoning effort for Claude models that support it |
| `--native-computer-tool` | No | off | Use Claude's native `computer` tool instead of the individual desktop tools (Claude models only) |
| `--computer-tool-version` | No | `20251124` | With `--native-computer-tool`: `20251124` or `20260801` (the newer toolset) |
| `--mcp-region` | No | `AWS_REGION` | AWS region for MCP SigV4 signing; must match the fleet's region, which can differ from `--region` |
| `--mcp-endpoint` | No | from `scripts/config.json` | Agent Access MCP endpoint (`{region}` is replaced with the MCP region) |
| `--mcp-profile` | No | default credentials | AWS profile for SigV4 signing to the MCP endpoint |
| `--llm-profile` | No | default credentials | AWS profile for Bedrock calls |
| `--bedrock-api-key` | No | `AWS_BEARER_TOKEN_BEDROCK`, else minted | Bedrock API key for non-Claude models (bedrock-mantle) |
| `--mcp-timeout` | No | `180` | MCP client startup timeout in seconds |
| `--mcp-retries` | No | `3` | Number of MCP client connection retries |
| `--no-screenshot-pruning` | No | off | Keep every screenshot in the conversation |
| `--keep-screenshots` | No | `3` | Screenshots kept in the conversation; older ones become a text placeholder |
| `--prune-batch` | No | `10` (cached) / `1` | Prune once N screenshots beyond `--keep-screenshots` have piled up (`1` = before every model call); 10 is the default with prompt caching (measured: about 45% lower cost at the same success) |
| `--prompt-cache` / `--no-prompt-cache` | No | on | Bedrock prompt caching (Claude models) |
| `--max-tokens` | No | model default | Maximum tokens per model response |
| `--max-turns` | No | off | Stop a task after N model calls; each REPL task gets a fresh budget |
| `--max-seconds` | No | off | Stop a task after S seconds |
| `--expire-session-on-exit` | No | off | Expire the streaming session when the agent disconnects (fresh desktop next run; discards what is open) |
| `--tool-timeout` | No | `120` | Give up on one desktop action after S seconds |

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

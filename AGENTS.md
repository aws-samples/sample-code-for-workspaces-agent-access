# Amazon WorkSpaces Applications Agent Access — Developer Guide

This guide helps AI development environments (Kiro, Claude Code, VS Code, etc.) understand how to build with and use the Agent Access MCP Server.

## What is Agent Access?

Agent Access lets AI agents interact with Windows desktop applications running on Amazon WorkSpaces Applications (AppStream 2.0). Agents connect via the Model Context Protocol (MCP) and can take screenshots, click, type, scroll, and perform keyboard shortcuts — automating any desktop workflow.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  AI Agent   │────▶│  MCP Transport   │────▶│  Agent Access    │────▶│  Windows Desktop │
│  (IDE/SDK)  │◀────│  (SigV4 signed)  │◀────│  MCP Server      │◀────│  (AppStream)     │
└─────────────┘     └──────────────────┘     └──────────────────┘     └─────────────────┘
```

### Connection Methods

| Method | Auth | Use Case |
|--------|------|----------|
| Direct (SigV4) | `mcp-proxy-for-aws` signs each request | SDK integrations, custom agents |
| AgentCore Gateway | Gateway signs on your behalf | AgentCore Harness, managed deployments |
| AgentCore Harness | Fully managed orchestration | Zero-code agents via console/CLI |

### Endpoint

```
https://agentaccess-mcp.{region}.api.aws/mcp
```

Available regions:

| Region | Endpoint |
|--------|----------|
| US East (N. Virginia) | `agentaccess-mcp.us-east-1.api.aws` |
| US East (Ohio) | `agentaccess-mcp.us-east-2.api.aws` |
| US West (Oregon) | `agentaccess-mcp.us-west-2.api.aws` |
| Canada (Central) | `agentaccess-mcp.ca-central-1.api.aws` |
| Europe (Frankfurt) | `agentaccess-mcp.eu-central-1.api.aws` |
| Europe (Ireland) | `agentaccess-mcp.eu-west-1.api.aws` |
| Europe (London) | `agentaccess-mcp.eu-west-2.api.aws` |
| Europe (Paris) | `agentaccess-mcp.eu-west-3.api.aws` |
| Asia Pacific (Tokyo) | `agentaccess-mcp.ap-northeast-1.api.aws` |
| Asia Pacific (Seoul) | `agentaccess-mcp.ap-northeast-2.api.aws` |
| Asia Pacific (Mumbai) | `agentaccess-mcp.ap-south-1.api.aws` |
| Asia Pacific (Singapore) | `agentaccess-mcp.ap-southeast-1.api.aws` |
| Asia Pacific (Sydney) | `agentaccess-mcp.ap-southeast-2.api.aws` |

### Authentication

Requests must be SigV4-signed with service name `agentaccess-mcp`. Required IAM action: `agentaccess-mcp:*`.

### Streaming Session

Each MCP session is bound to a desktop via a streaming URL (from `appstream:CreateStreamingURL`) passed as a header:

```
X-Amzn-AgentAccess-Streaming-Session-Url: <streaming-url>
```

When connecting through an AgentCore Gateway with `IamCredentialProvider`, the MCP server auto-provisions sessions — no streaming URL management needed.

## Available MCP Tools

The MCP server exposes these tools for desktop interaction:

### `screenshot`
Capture the current screen state. Returns a PNG image.

```json
{"name": "screenshot", "arguments": {}}
```

### `left_click(x, y)`
Click at screen coordinates.

```json
{"name": "left_click", "arguments": {"x": 500, "y": 300}}
```

### `double_click(x, y)`
Double-click at coordinates.

```json
{"name": "double_click", "arguments": {"x": 500, "y": 300}}
```

### `triple_click(x, y)`
Triple-click (select line/paragraph).

```json
{"name": "triple_click", "arguments": {"x": 500, "y": 300}}
```

### `type_text(text)`
Type a string of text.

```json
{"name": "type_text", "arguments": {"text": "Hello World"}}
```

### `key(keys)`
Press key combinations. Supports modifiers (`ctrl`, `alt`, `shift`, `super`) and special keys (`Return`, `Escape`, `Tab`, `F1`-`F12`).

```json
{"name": "key", "arguments": {"keys": "ctrl+s"}}
```

Common combinations:
- `ctrl+c` / `ctrl+v` — copy/paste
- `ctrl+a` — select all
- `ctrl+z` — undo
- `alt+F4` — close window
- `super` — open Start Menu
- `super+r` — open Run dialog
- `alt+Tab` — switch windows
- `Return` — press Enter
- `Escape` — dismiss dialog

### `scroll(x, y, direction, amount)`
Scroll at coordinates.

```json
{"name": "scroll", "arguments": {"x": 500, "y": 400, "direction": "down", "amount": 3}}
```

### `wait(seconds)`
Pause execution (useful for waiting for applications to load).

```json
{"name": "wait", "arguments": {"seconds": 5}}
```

## Integration Patterns

### Pattern 1: Direct MCP Connection (Python)

```python
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

mcp_client = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint="https://agentaccess-mcp.us-east-1.api.aws/mcp",
    aws_service="agentaccess-mcp",
    aws_region="us-east-1",
    headers={"X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url},
))

model = BedrockModel(model_id="global.anthropic.claude-sonnet-4-6")
agent = Agent(model=model, tools=[mcp_client])
agent("Open Notepad and type 'Hello World'")
```

### Pattern 2: AgentCore Harness (Zero Code)

```bash
# Deploy Gateway + Lambda Proxy + Harness
./scripts/deploy_agentcore_harness.sh --region us-east-1

# Use via Console Playground or agentcore dev
```

**Architecture:** The harness uses a Lambda proxy target because the AgentCore Gateway cannot connect directly to the Agent Access MCP server (the MCP server requires a streaming URL for any connection, including health checks). The Lambda handles streaming URL lifecycle, session initialization, and tool call forwarding.

```
Harness → Gateway → Lambda Proxy → (SigV4) → Agent Access MCP Server → Desktop
```

**Key implementation details:**
- The Lambda creates/caches streaming URLs from AppStream (`CreateStreamingURL`)
- On first invocation, it initializes the MCP session and polls `tools/list` until DCV tools are ready (up to 60s)
- Tool names are prefixed with `agentaccess___` by the MCP server — the Lambda strips this on input and adds it on output
- The Lambda timeout is 180s to accommodate DCV session warmup on cold starts

**Known limitations:**
- First tool call after a cold start takes 10-30s while the desktop session connects

### Pattern 3: IDE MCP Server Configuration

Any IDE that supports MCP servers via stdio can connect using `mcp-proxy-for-aws`:

```bash
pip install mcp-proxy-for-aws
```

Add an MCP server to your IDE's config with:

```json
{
  "type": "stdio",
  "command": "mcp-proxy-for-aws",
  "args": [
    "https://agentaccess-mcp.us-east-1.api.aws/mcp",
    "--service", "agentaccess-mcp",
    "--region", "us-east-1"
  ]
}
```

Where to put this depends on your IDE:
- **Kiro**: `.kiro/settings/mcp.json` → under `mcpServers.<name>`
- **Claude Code**: `.claude/settings.json` → under `mcpServers.<name>`
- **VS Code**: `.vscode/mcp.json` → under `servers.<name>`

Optional flags:
- `--profile <name>` — use a specific AWS profile
- `--region <region>` — match your fleet's region
- `--metadata "aws.agentaccess/streamingSessionUrl=<URL>"` — pass an explicit streaming URL

## Best Practices for Agent Prompts

1. **Minimize screenshots** — they're expensive (large image payloads). Take one, perform 3-5 actions, then screenshot to verify.
2. **Use exact tool names** — `left_click`, not `click`. `key("ctrl+a")`, not `ctrl_a`.
3. **Handle dialogs** — applications may show update prompts, recovery dialogs, or setup wizards. Use `key("Escape")` or `key("alt+F4")` to dismiss.
4. **Use Run dialog for launching apps** — `key("super+r")` → `type_text("notepad")` → `key("Return")` is more reliable than Start Menu search.
5. **Batch actions** — don't screenshot after every single action. Group related actions together.
6. **Don't repeat failures** — if an approach fails twice, try a completely different method.

## Session Lifecycle

1. **Session starts** when the first MCP `initialize` request is received with a streaming URL (or auto-provisioned via Gateway).
2. **Session is active** while the MCP connection is open. Tools can be called repeatedly.
3. **Session ends** when the MCP connection closes or the streaming URL expires (default: 1 hour, max: 16 hours).
4. **Desktop state persists** within a session — applications stay open, files remain on disk.

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | SigV4 signing failed | Check AWS credentials |
| `403 Forbidden` | Missing IAM permissions | Add `agentaccess-mcp:*` to policy |
| `DCV proxy not initialized` | No streaming URL provided | Pass streaming URL header or use Gateway |
| `dcv session not ready` | Desktop still booting | Retry — agent retries automatically for up to 10 minutes |
| `backend unavailable` | Transient service issue | Retry (auto-retried 10 times) |

## Dependencies

```
pip install strands-agents mcp-proxy-for-aws boto3
```


## Known Issues & Workarounds

### Tool Name Prefixing

The MCP server prefixes all tool names with `agentaccess___` (e.g., `agentaccess___screenshot`, `agentaccess___left_click`). When using the Lambda proxy, tool names are automatically mapped. When connecting directly, use the unprefixed names — the `mcp-proxy-for-aws` transport handles the prefix transparently.

### DCV Session Warmup

After creating a streaming URL, the DCV desktop session takes 5-30 seconds to connect. During this time, `tools/call` returns `"Unknown tool"` errors. The Lambda proxy handles this by polling `tools/list` until tools appear. For custom agents, implement retry logic or use `lib/agent_common.py` which retries MCP connection automatically.

## Contributing

### Local checks before a PR

CI (`.github/workflows/ci.yml`) runs a fast, dependency-free gate on every push
and pull request to `main`:

1. **Byte-compiles** all Python under `agents/`, `lib/`, `scripts/`,
   `mcp_servers/`, and the repo root (syntax / Python-version check on 3.10 and 3.12).
2. **Validates** every `agents/**/skills/*.json` file parses as JSON.

Run the exact same checks locally before opening a PR:

```bash
./scripts/ci_local.sh
```

Exit status `0` means both checks pass — the same contract as CI. The script
needs only Python (no `venv`, AWS credentials, or runtime dependencies). Override
the interpreter with `PYTHON_CMD=python3.10 ./scripts/ci_local.sh` to match a
specific CI matrix version.

> This gate intentionally does **not** install dependencies or run the agents —
> it catches syntax errors and malformed skill files, not runtime behavior.
> Full behavioral testing requires a live forwarding/desktop fleet.

### Adding a new demo agent

A demo agent is a thin `agent.py` that calls `agent_common.run_standard_agent`,
plus a `prompts/` directory (`system_prompt.md`, `task_prompt.md`) and an
optional `skills/<name>.json`. Copy an existing agent (e.g. `agents/paint_demo`)
as a starting point, then run `./scripts/ci_local.sh` to confirm it compiles and
its skill JSON is valid. See `agents/mcp_forwarding_demo` for an example that
drives forwarded MCP tools.

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

### Connection Method

Agents connect directly to the MCP endpoint over Streamable HTTP, with `mcp-proxy-for-aws` SigV4-signing each request. Every sample in this repo uses this path.

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

### Domain-joined fleets (SAML)

A domain-joined fleet has no streaming URL. The client sends a SAML assertion and the stack ARN in the JSON-RPC `_meta` of every request instead, and must add them **before** signing: the SigV4 signature covers the body. `mcp-proxy-for-aws`'s `metadata=` option adds `_meta` after signing (and skips requests without `params`), so this repo does it in `lib/domain_join_auth.py`, an `httpx.Auth` that wraps the proxy's signer. The `_meta` keys are `aws.agentaccess/workspacesApplicationsSamlAssertion` and `aws.agentaccess/workspacesApplicationsStackArn`; a live domain-joined fleet accepted them. A request that carries an expired assertion is refused (HTTP 400, `SAML assertion has expired`), and this sample re-sends the assertion it started with, so it stops working when that assertion expires. The public developer guide's example uses other key names (not tried here) and recommends `metadata=`, which cannot work with SigV4 because it edits the body after signing. See "Domain Join" in the README.

## Available MCP Tools

The MCP server exposes these tools for desktop interaction:

### `screenshot(include_cursor)`
Capture the current screen state. Returns a PNG image. The image dimensions define the coordinate space for all mouse tools; in practice screenshots are always 1280 × 720.

```json
{"name": "screenshot", "arguments": {}}
```

`include_cursor` is optional and defaults to `false`.

### Mouse tools

`left_click`, `double_click`, `triple_click`, `right_click`, `middle_click` take `x` and `y` (required) and `modifiers` (optional, for example `"ctrl"` or `"ctrl+shift"`).

```json
{"name": "left_click", "arguments": {"x": 500, "y": 300}}
{"name": "left_click", "arguments": {"x": 500, "y": 300, "modifiers": "ctrl"}}
```

`left_click_drag(start_x, start_y, end_x, end_y)` drags from the start point to the end point.

```json
{"name": "left_click_drag", "arguments": {"start_x": 100, "start_y": 200, "end_x": 400, "end_y": 200}}
```

`left_mouse_down(x, y, modifiers)` and `left_mouse_up(x, y, modifiers)` press and release the left button at the given coordinates. `move_pointer(x, y)` moves the pointer.

### `scroll(x, y, scroll_direction, scroll_amount, modifiers)`
Scroll the mouse wheel at the coordinates. `scroll_direction` is `Up`, `Down`, `Left` or `Right`. `scroll_amount` is in **ticks**, where 120 ticks equal one wheel notch (so 360 is three notches). `modifiers` is optional.

```json
{"name": "scroll", "arguments": {"x": 500, "y": 400, "scroll_direction": "Down", "scroll_amount": 360}}
```

### Keyboard tools

`type_text(text)` types a string (up to 10,000 characters).

```json
{"name": "type_text", "arguments": {"text": "Hello World"}}
```

`key(keys)` presses a key or combination joined by `+` (for example `a`, `ctrl+c`, `ctrl+shift+s`). Modifiers are `ctrl`, `alt`, `shift`, `super`; special keys include `Return`, `Escape`, `Tab`, `F1`-`F12`.

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

`hold_key(keys, duration)` holds a key or combination for 1 to 30 seconds.

```json
{"name": "hold_key", "arguments": {"keys": "shift", "duration": 2}}
```

### `launch_application(id)`
Launch an application from the image's application catalog. The valid `id` values depend on the image
and are listed in the tool's description in `tools/list` (for example `chrome` and `Code`). Faster and
more reliable than finding the app in the Start menu, when the app is in the catalog.

```json
{"name": "launch_application", "arguments": {"id": "chrome"}}
```

### `get_session_info()`
Return read-only metadata about the current session. Takes no arguments.

```json
{"name": "get_session_info", "arguments": {}}
```

### `toggle_app_switcher()`
Open or close the app-switcher overlay. Takes no arguments. It only toggles the overlay: to bring an
application to the foreground, follow up with a `left_click` on its thumbnail, using coordinates read
from a screenshot. No tool reports which application is in the foreground, so take a screenshot and
look.

```json
{"name": "toggle_app_switcher", "arguments": {}}
```

> Only on fleets whose **Stream view** is **Application**, which shows only the windows of the
> applications that were opened. With **Desktop** stream view the operating system's desktop and
> taskbar are there instead, and the tool is not listed.

### `wait(seconds)`
Pause execution (useful for waiting for applications to load). The service does not offer this tool yet; this repo's agents give the model a local `wait` that pauses on the client (`lib/wait_tool.py`) and will use the service's own once it ships. The parameter name is `seconds` until then, so check `tools/list`.

```json
{"name": "wait", "arguments": {"seconds": 5}}
```

The service's own tool reference is at <https://docs.aws.amazon.com/appstream2/latest/developerguide/agent-access-mcp-server.html#agent-access-mcp-server-tools>. In `POLLING` connect mode only `connection_status` is listed until the desktop is connected.

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

model = BedrockModel(model_id="global.anthropic.claude-sonnet-5-5")
agent = Agent(model=model, tools=[mcp_client])
agent("Open Notepad and type 'Hello World'")
```

### Pattern 2: IDE MCP Server Configuration

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
4. **Launch apps directly** — `launch_application(id)` when the app is in the image's catalog (the IDs are in the tool's description); otherwise the Run dialog, `key("super+r")` → `type_text("notepad")` → `key("Return")`, is more reliable than Start Menu search.
5. **Batch actions** — don't screenshot after every single action. Group related actions together.
6. **Don't repeat failures** — if an approach fails twice, try a completely different method.
7. **Verify before declaring done** — take a final screenshot and check the result against the task.
8. **Expect one action at a time** — this repo's agents run a turn's actions in order and skip the rest of the turn after a failure, so put dependent steps in one turn and re-plan from a screenshot when something fails.

## Session Lifecycle

1. **Session starts** when the first MCP `initialize` request is received with a streaming URL (or, for domain-joined fleets, a SAML assertion and stack ARN in `_meta`).
2. **Session is active** while the MCP connection is open. Tools can be called repeatedly.
3. **Session ends** when the MCP connection closes or the streaming URL expires (`aws appstream create-streaming-url --validity`: the API default is 60 seconds and the maximum 604800; the examples here use 3600). A domain-joined session as this sample runs it stops working when its SAML assertion expires: the service refuses requests that carry an expired one.
4. **Desktop state persists** within a session — applications stay open, files remain on disk.

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | SigV4 signing failed | Check AWS credentials |
| `403 Forbidden` | Missing IAM permissions | Add `agentaccess-mcp:*` to policy |
| `DCV proxy not initialized` | No streaming URL provided | Pass the streaming URL header |
| `dcv session not ready` | Desktop still booting | Retry — agent retries automatically for up to 10 minutes |
| `backend unavailable` | Transient service issue | Retry (auto-retried 10 times) |
| `CONNECTION_LIMIT_REACHED` / `Another agentic client is already connected` | An MCP session that was never ended still holds the desktop (another agent, a killed process) | Wait for that session to time out, or expire the desktop session. The agent ends its own session on exit and after a failed connection attempt, and waits up to 300 s for a reply (the first `tools/list` on a stack with application settings persistence waits for the user profile to load) |
| `HTTP 400 ... Invalid streaming URL` (for example `streaming URL has expired`) | The service refused the streaming URL (expired or invalid) | Create a new URL and start again (`create-streaming-url --validity` defaults to 60 s; the examples use 3600). The agent stops the task at the first refusal |
| `HTTP 400` / `401` / `403` on a Domain Join run | The service refused the SAML assertion or the stack ARN (for example an expired assertion, or credentials from another account) | Read the service's message, get a fresh assertion, check `--stack-arn` and `--mcp-profile`. The agent stops instead of retrying |

## Dependencies

```
pip install strands-agents mcp-proxy-for-aws boto3
```


## Known Issues & Workarounds

### Tool Name Prefixing

The MCP server prefixes all tool names with `agentaccess___` (e.g., `agentaccess___screenshot`, `agentaccess___left_click`), and `tools/list` returns them that way. An agent built on this repo's `lib/` sees the prefixed names; the prompts and logs refer to the short names (`screenshot`, `left_click`). Forwarded tools are `forwarded___{server}___{tool}`.

### DCV Session Warmup

After creating a streaming URL, the DCV desktop session takes 5-30 seconds to connect. During this time, `tools/call` returns `"Unknown tool"` errors. Implement retry logic in your agent, or use `lib/agent_common.py`, which retries the MCP connection automatically.

## Contributing

### Local checks before a PR

CI (`.github/workflows/ci.yml`) runs a job on every push and pull request to
`main`.

**`compile-and-validate`** — a fast, dependency-free gate:

1. **Byte-compiles** all Python under `agents/`, `lib/`, `scripts/`,
   `mcp_servers/`, and the repo root (syntax / Python-version check on 3.10 and 3.12).
2. **Validates** every `agents/**/skills/*.json` file parses as JSON.

Run the same checks locally before opening a PR:

```bash
./scripts/ci_local.sh                                   # compile + skill JSON (no dependencies)
```

Exit status `0` means every requested check passed — the same contract as CI. The
default run needs only Python (no `venv`, AWS credentials, or runtime dependencies).
Override the interpreter with `PYTHON_CMD=python3.10 ./scripts/ci_local.sh` to match
a specific CI matrix version.

> Neither job drives a real desktop. Full behavioral testing requires a live
> forwarding/desktop fleet.

### Adding a new demo agent

A demo agent is a thin `agent.py` that calls `agent_common.run_standard_agent`,
plus a `prompts/` directory (`system_prompt.md`, `task_prompt.md`) and an
optional `skills/<name>.json`. Copy an existing agent (e.g. `agents/application_validation`)
as a starting point, then run `./scripts/ci_local.sh` to confirm it compiles and
its skill JSON is valid. See `agents/mcp_forwarding_demo` for an example that
drives forwarded MCP tools.

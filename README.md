# Sample Code for Amazon WorkSpaces Applications with agent access

Build autonomous agents that automate desktop workflows on [Amazon WorkSpaces Applications with agent access](https://docs.aws.amazon.com/appstream2/latest/developerguide/agent-access.html). Agents interact with any combination of applications — filling forms, transferring data between apps, navigating multi-step processes — using the Strands Agents SDK and Claude with screenshot, mouse and keyboard tools.

Amazon WorkSpaces Applications enables agents to connect to streaming sessions and interact with desktop applications through [a managed Model Context Protocol (MCP) service](https://docs.aws.amazon.com/appstream2/latest/developerguide/agent-access-mcp-server.html).

## Prerequisites
The Quick Start helps you get setup with:
- **AWS account** with permission to create Amazon WorkSpaces Applications fleets/stacks and invoke Amazon Bedrock
- **AWS CLI v2** — [install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- **Python 3.10+** — [install guide](https://www.python.org/downloads/)
- **Valid AWS credentials** configured (run `aws sts get-caller-identity` to verify)
- **bash** on Windows — the deploy step runs via [Git for Windows](https://git-scm.com/download/win) (`winget install -e --id Git.Git`) or WSL (`wsl --install`)

> **Note:** Creating a fleet requires the `AmazonAppStreamServiceAccess` service role in your account. WorkSpaces Applications creates it for you when you choose **Get started** in the [WorkSpaces Applications console](https://console.aws.amazon.com/appstream2/home). To check whether your account already has it, or to create it yourself, see [Checking for the AmazonAppStreamServiceAccess service role](https://docs.aws.amazon.com/appstream2/latest/developerguide/controlling-access-checking-for-iam-service-access.html).

## Quick Start

Clone the repo and run the setup script from the repository root. Agents log their output and store screenshots in the relative agent folders (`agents/generic_cua/screenshots` and `agents/generic_cua/logs`).

### macOS / Linux

```bash
git clone https://github.com/aws-samples/sample-code-for-workspaces-agent-access.git
cd sample-code-for-workspaces-agent-access
./scripts/setup.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/aws-samples/sample-code-for-workspaces-agent-access.git
cd sample-code-for-workspaces-agent-access
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.sh` / `setup.ps1` installs dependencies, deploys WorkSpaces resources (VPC, Fleet, Stack with AgentAccessConfig), waits for the fleet to reach RUNNING state, generates a streaming URL, and runs the demo agent.

### Run agents again after setup

Use the helper to mint a fresh streaming URL (default validity: 1 hour):

```bash
source venv/bin/activate
STREAMING_URL=$(scripts/streaming_url.sh)
python3 agents/application_validation/agent.py --streaming-url "$STREAMING_URL"
```

## Demo Agents

```bash
source venv/bin/activate

# App validation — tests desktop applications
python3 agents/application_validation/agent.py --streaming-url "$STREAMING_URL"

# Interactive — REPL for arbitrary desktop tasks
python3 agents/generic_cua/agent.py --streaming-url "$STREAMING_URL"

# MCP tool forwarding — uses forwarded filesystem + fetch tools (requires a forwarding fleet)
python3 agents/mcp_forwarding_demo/agent.py --streaming-url "$STREAMING_URL"
```

### MCP tool forwarding

Fleets can expose additional tools beyond desktop interaction via MCP tool forwarding. When enabled, `tools/list` returns both desktop tools (`screenshot`, `left_click`, etc.) and forwarded tools (prefixed with `forwarded___`). These forwarded tools call external APIs or services configured on the fleet — your agent uses them like any other MCP tool.

To set up a fleet with custom MCP servers (requires ~30 min for AMI build + import):

```bash
./scripts/setup_mcp_forwarding.sh --region us-east-1
```

This builds a Windows Server 2025 image with Python + FastMCP and two example servers (`filesystem`, `fetch`), imports it to WorkSpaces, and creates a fleet with `FORWARD_MCP_TOOLS` enabled. See `mcp_servers/` for the server source code.

> **Validate first:** before building a custom image, check that your MCP servers will forward correctly with the compatibility tester in [`utils/mcpforwardingtester/`](utils/mcpforwardingtester/). It catches the common failures (a server that lists no tools, a config saved with a BOM, or `env`/`cwd` keys that get silently ignored) before the ~30-minute image build.

Once the forwarding fleet is running, `agents/mcp_forwarding_demo/` drives the forwarded tools end to end — it lists and reads seeded files, fetches a web page, writes a report file, and confirms the result on the desktop:

```bash
STREAMING_URL=$(aws appstream create-streaming-url \
  --stack-name MCPForwardingStack --fleet-name MCPForwarding \
  --user-id test --validity 3600 \
  --query StreamingURL --output text)
python3 agents/mcp_forwarding_demo/agent.py --streaming-url "$STREAMING_URL"
```

### Domain Join (AD-joined fleets)

For fleets joined to an Active Directory domain, agents authenticate with a SAML assertion and the stack ARN instead of a streaming URL. Both are sent in the MCP `_meta` field of every request, and the request is SigV4-signed after they are added. Keep the assertion in a file only you can read:

```bash
chmod 600 assertion.b64
python3 agents/generic_cua/agent.py \
    --saml-assertion-file assertion.b64 \
    --stack-arn "arn:aws:appstream:us-east-1:123456789012:stack/MyDJStack"
```

Prerequisites:
- WorkSpaces Applications fleet joined to an AD domain with Certificate-Based Authentication (CBA) enabled
- IAM SAML provider registered with your IdP certificate
- IAM role trusting the SAML provider
- Base64-encoded SAML assertion from your IdP (Okta, Entra ID, Ping, etc.), exactly as the IdP issued it
- AWS credentials that sign the MCP requests (`--mcp-profile`, or your default credentials), expected to be from the account that owns the stack

Things to know:
- **Start the agent right after you get the assertion.** SAML assertions are short-lived (typically minutes) and the agent sends the same one with every request. A request that carries an expired assertion is refused (HTTP 400, `SAML assertion has expired`), and this sample re-sends the assertion it started with, so it stops working when that assertion expires (an idle REPL included). When the service refuses the assertion, while connecting or mid-run, the agent prints the service's message and stops instead of retrying.
- `--streaming-url` is not used; the MCP server provisions a desktop session bound to the AD user identity in the assertion. The two are mutually exclusive.
- The assertion is read from `--saml-assertion-file` (or, if that is not given, the `AGENTACCESS_SAML_ASSERTION` environment variable), never from the command line, where other local users could see it in the process list. It is not printed or logged.
- Supported by `generic_cua`, `application_validation` and `mcp_forwarding_demo`. The other samples (`quickstart.py`, the AgentCore sample) take a streaming URL only.
- A live domain-joined fleet accepted the `_meta` keys this sample sends (`aws.agentaccess/workspacesApplicationsSamlAssertion` and `aws.agentaccess/workspacesApplicationsStackArn`). The AWS developer guide's example uses other names (`saml_response` / `stack_arn`), which were not tried here.

## Create Your Own Agent
There's a library provided in `lib/agent_common.py` that you can use to create your own agent. We've also provided an agent creator with prompts to describe your workflow:
```bash
python3 agents/agent_creator/agent.py
```

The agent creator interviews you about your workflow, then generates skill files, prompts, and an `agent.py`. Iterate:

```bash
python3 agents/<your_workflow>/agent.py --streaming-url "$STREAMING_URL"
python3 agents/agent_creator/agent.py --update agents/<your_workflow>
```

## The native computer tool

By default the model is offered the Agent Access desktop tools (`left_click`, `type_text`, ...) as ordinary function tools. `--native-computer-tool` offers it Anthropic's own `computer` tool instead (version `computer_20251124`), which Claude is trained on. The agent translates each `computer` action into the matching Agent Access call (`lib/computer_tool.py`): clicks keep their modifier keys, scrolls go from wheel notches to the service's ticks (120 per notch), `wait` is done locally, and `zoom` and `cursor_position` are not offered by the service (the latter answers with the last position the agent moved the mouse to). Coordinates outside the 1280 x 720 screen are refused with a message before anything reaches the desktop. Run metrics record the call as the service tool it became, and `computer_tool` in the metrics file says which mode a run used. Whether the native tool does better than the function tools has not been measured; the default stays on the function tools until it has. See `TESTING-3.md` section H.

## Run metrics

Every run of `generic_cua`, `application_validation` and `mcp_forwarding_demo` that finishes writes `agents/<agent>/metrics/metrics_<timestamp>.json` (its log and screenshots sit beside it; a run killed before it finishes leaves a log but no metrics file): each model and tool call with its duration, failed tool calls with their error text, tokens including prompt-cache reads and writes, and the versions of the prompts that have frontmatter. Counts are per model call, not per turn: a turn in which the model uses a tool takes at least two model calls, so per-turn token figures run higher than per-call ones. The other samples (`agent_creator`, `quickstart.py`, the AgentCore sample) write none. To compare two batches of runs, for example before and after a change:

```bash
python3 scripts/analyze_metrics.py --dir agents/application_validation/metrics --since 20261001_120000 --table
```

This prints one line per run (seconds, model calls, tool calls, failures, screenshots, tokens) and the means. Leave out `--table` for the full report.

## Running and stopping tasks

Desktop actions run one at a time, in the order the model asks for them. If one fails, the rest of that model turn is skipped (the model is told, and looks at the screen again). `--max-turns` and `--max-seconds` are off unless set; they bound each task (an interactive `generic_cua` session gives every task you type a fresh budget), and a task that reaches one stops with a message naming the flag, and is not retried. A single desktop action that gets no answer is given up on after `--tool-timeout`; the service may keep executing it, so later actions can queue behind it. Keep `--tool-timeout` longer than the longest action a task needs (`hold_key` allows up to 30 seconds).

Press Ctrl-C once to stop the running task: the current action finishes, the metrics are written, and an interactive session returns to its prompt. Press it again to quit at once. With `--max-seconds` set, a model call that never answers is not covered; it ends when the Bedrock client's own timeout does.

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--streaming-url URL` | *(required unless a SAML assertion is given)* | WorkSpaces Applications streaming URL for the desktop session |
| `--model-id ID` | `global.anthropic.claude-sonnet-5-5` | Bedrock model ID (`global.anthropic.claude-sonnet-4-6` also works) |
| `--native-computer-tool` | off | Offer Claude Anthropic's `computer_20251124` tool (declared at the service's 1280x720 screen) instead of the individual desktop tools; each `computer` action is carried out with the matching Agent Access tool. Claude models only; sends the `computer-use-2025-11-24` beta header (the default mode sends none). |
| `--computer-tool-version` | `20251124` | With `--native-computer-tool`: `20251124` (the `computer_20251124` tool, beta header) or `20260801` (the newer `computer_toolset_20260801`: one tool per action, no beta header, Sonnet 5 / Opus 5 models and later) |
| `--effort low\|medium\|high` | model default | Reasoning effort for Claude models that support it (sends adaptive thinking and `output_config.effort`) |
| `--mcp-timeout SECS` | `180` | MCP client startup timeout |
| `--mcp-retries N` | `3` | Number of MCP connection retries |
| `--max-turns N` | off | Stop a task after N model calls |
| `--max-seconds S` | off | Stop a task after S seconds |
| `--expire-session-on-exit` | off | When the agent disconnects, also expire the WorkSpaces Applications streaming session (sends `X-Amzn-AgentAccess-Expire-Streaming-Session-On-Delete: true`). The desktop and everything open on it is discarded and the fleet scales per its policy; by default the session keeps running until its disconnect timeout. |
| `--tool-timeout S` | `120` | Give up on one desktop action after S seconds (keep it above the longest `hold_key`/wait a task uses) |
| `--region REGION` | auto-detect | AWS region for Bedrock calls |
| `--mcp-region REGION` | `AWS_REGION` (or `mcp.region` in `scripts/config.json`) | AWS region for MCP SigV4 signing and the endpoint's `{region}`. Must match the fleet's region, which can differ from `--region` (Bedrock) |
| `--no-screenshot-pruning` | off | Keep all screenshots in conversation context |
| `--keep-screenshots N` | `3` | Screenshots kept in context; older ones become a text placeholder (the files stay on disk) |
| `--prune-batch N` | `10` with prompt caching, `1` without | Prune once N screenshots beyond `--keep-screenshots` have piled up; `1` prunes before every model call. In a 540-run comparison on a live desktop, 10 cut the cost per run by about 45% at the same success |
| `--prompt-cache` / `--no-prompt-cache` | on | Bedrock prompt caching (Claude models); `--no-prompt-cache` turns it off |
| `--max-tokens N` | model default | Maximum tokens per model response |
| `--mcp-profile PROFILE` | default | AWS profile for SigV4 signing to the MCP endpoint |
| `--llm-profile PROFILE` | default | AWS profile for Bedrock LLM calls |
| `--mcp-endpoint URL` | `mcp.endpoint` in `scripts/config.json` | Agent Access MCP endpoint; `{region}` is replaced with the MCP region |
| `--bedrock-api-key KEY` | `AWS_BEARER_TOKEN_BEDROCK`, else a short-term key minted from your credentials | Bedrock API key for non-Claude models (bedrock-mantle); not used for Claude |
| `--saml-assertion-file PATH` | | File holding the base64 SAML assertion for Domain Join (replaces `--streaming-url`). `AGENTACCESS_SAML_ASSERTION` is read when this is not given |
| `--stack-arn ARN` | | WorkSpaces Applications stack ARN (required with the assertion) |

`generic_cua` also takes `--system-prompt`, `--task-prompt`, `--skill` and `--plain`; see [its README](agents/generic_cua/README.md).

## Project Structure

```
sample-code-for-workspaces-agent-access/
├── quickstart.py                # Minimal self-contained example (~60 lines)
├── AGENTS.md                    # Developer guide (tools, architecture, IDE setup)
├── agents/
│   ├── agent_creator/          # Interactive agent builder
│   ├── application_validation/ # Single-app validation
│   ├── generic_cua/            # Interactive REPL agent
│   └── mcp_forwarding_demo/    # Forwarded MCP tools (filesystem + fetch) demo
├── lib/
│   ├── agent_common.py         # Shared infrastructure (re-exports from sub-modules)
│   ├── agent_factory.py        # The one place desktop agents are built; explicit auth modes
│   ├── model.py                # Bedrock model creation + multi-model support
│   ├── mcp_client.py           # MCP transport, endpoint resolution, tool names
│   ├── domain_join_auth.py     # Domain Join: adds the SAML assertion to _meta before SigV4 signing
│   ├── saml_assertion.py       # Domain Join inputs: assertion file, validation, log redaction
│   ├── retry.py                # Connection retry logic, error classification
│   ├── screenshot_pruning_manager.py  # Token-saving screenshot manager
│   ├── computer_tool.py        # Anthropic computer tool on top of the Agent Access tools (--native-computer-tool)
│   ├── wait_tool.py            # Local wait tool until the service offers one
│   ├── terminal_ui.py          # generic_cua's interactive terminal
│   └── strands_logger.py       # Metrics and logging
├── scripts/
│   ├── config.json             # Fleet, stack, VPC, MCP endpoint config
│   ├── setup.sh                # One-step setup (macOS / Linux)
│   ├── setup.ps1               # One-step setup (Windows)
│   ├── streaming_url.sh        # Mint a fresh WorkSpaces Applications streaming URL
│   ├── deploy.sh               # Deploy VPC + Fleet + Stack
│   ├── cleanup.sh              # Tear down all resources
│   ├── deploy_agentcore.sh     # Deploy agent to Bedrock AgentCore Runtime
│   ├── install.sh              # Install Python dependencies (macOS / Linux)
│   ├── install.ps1             # Install Python dependencies (Windows)
│   ├── ci_local.sh             # Run CI checks locally (compile + validate skill JSON)
├── skills/
│   └── workspace-skill-creator/  # Skill for creating new app skills
├── utils/
│   └── mcpforwardingtester/      # Validate MCP servers forward correctly before building an image
└── requirements.txt
```

## Troubleshooting

**"The Agent Access MCP Server failed to connect"**
- Check that the streaming URL hasn't expired (default: 1 hour)
- Verify your AWS credentials: `aws sts get-caller-identity`

**"timed out" / "Channel not connected"**
- The desktop session may still be initializing. The agent retries automatically (3 attempts with increasing wait times).
- If all retries fail, generate a fresh streaming URL and try again.

**"Another agentic client is already connected" / `CONNECTION_LIMIT_REACHED`**
- Only one agent can be attached to a desktop session. A session that an agent left attached, for example one whose process was killed with `kill -9` (older versions of this sample also left it attached after every normal exit), refuses the next connection with this message. This sample now ends its MCP session on exit, on Ctrl-C and on `SIGTERM`.
- A connection attempt that failed part-way also left its session attached, so every retry and every later run was refused although the WorkSpaces Applications session showed as connected. This happened on stacks with application settings persistence: the first `tools/list` waits for the user's profile to load, and that took longer than the 30-second HTTP timeout this sample used to have. The agent now waits up to 300 seconds and ends the MCP session of a failed attempt, so the retry can connect.
- To recover, wait for the session to time out, or expire it (`aws appstream expire-session --session-id <ID>`, ids from `aws appstream describe-sessions`) and start a new one; a fresh streaming URL for the same user then gets a new desktop.

**`unrecognized arguments:` followed by `command not found: --saml-assertion-file` (or another flag)**
- A space came after the `\` that continues a line, so the shell passed a blank argument to the agent and ran the next line as its own command. Remove the trailing space, or put the whole command on one line.

**"400 Bad Request" from MCP endpoint**
- The fleet's region and the MCP signing region must match. If your fleet is in `us-east-1` but you're signing requests for `us-west-2` (or vice versa), the service rejects them as cross-region.
- Check `AWS_REGION` matches the fleet region, or pass `--mcp-region <fleet-region>` explicitly.

**"400 Bad Request ... Invalid streaming URL: streaming URL has expired"**
- The service refused the streaming URL (expired or invalid). Every later request is refused the same way, so the agent stops the task at the first refusal. Create a new URL and start the agent again; `create-streaming-url --validity` defaults to 60 seconds, and the examples here use 3600.

**"401 Unauthorized" from MCP endpoint**
- Your AWS credentials can't sign requests. Run `aws sts get-caller-identity` to verify.
- If using profiles: `--mcp-profile <profile>` for MCP, `--llm-profile <profile>` for Bedrock.

**"403 Forbidden" from MCP endpoint**
- Check that your IAM credentials have the required permissions for the Agent Access MCP Server.
- Domain Join: the credentials that sign the requests are expected to belong to the account that owns the `--stack-arn` stack.

**Domain Join: the service reports that the SAML assertion has expired**
- The assertion is short-lived. Get a new one and start the agent right away; retrying an expired assertion cannot work, so the agent stops at the first refusal.

**"AccessDeniedException" from Bedrock**
- Your credentials don't have `bedrock:InvokeModel` permission.
- Check that the model ID is available in your region.

**Agent runs but doesn't interact with the desktop**
- Confirm the Stack was created with AgentAccessConfig (COMPUTER_INPUT, COMPUTER_VISION all ENABLED).
- Recreate the stack if needed — see `scripts/deploy.sh`.

**Fleet fails to start or "AccessDeniedException" from AppStream**
- Your account may be missing the `AmazonAppStreamServiceAccess` service role, which WorkSpaces Applications needs to create fleets (see the note under [Prerequisites](#prerequisites)).
- Check if it exists: `aws iam get-role --role-name AmazonAppStreamServiceAccess`
- If missing, choose **Get started** in the [WorkSpaces Applications console](https://console.aws.amazon.com/appstream2/home), which creates it, or see [Checking for the AmazonAppStreamServiceAccess service role](https://docs.aws.amazon.com/appstream2/latest/developerguide/controlling-access-checking-for-iam-service-access.html).

**Screenshot pruning**
- By default, screenshots older than the newest three are replaced by a text placeholder before each model call, to reduce token usage. Use `--no-screenshot-pruning` to keep them all (useful for debugging).
- Claude 5.x signs each thinking block against the conversation before it and rejects a request whose earlier content has changed (`Invalid signature in thinking block`). The agent edits its history (pruned screenshots, trimmed messages), so it removes the model's thinking blocks from the history before every model call; the model keeps what it did and saw, not its earlier reasoning.
- Prompt caching (on by default for Claude models; `--no-prompt-cache` turns it off) reads the unchanged start of each request (tools, system prompt, earlier turns) at about a tenth of the normal input price; writing new content to the cache costs about 1.25x once. Pruning a screenshot changes the request from that message onward, and `--prune-batch` controls how often that happens. An AWS write-up of a similar agent (`--keep-screenshots 1`, pruning before every call) measured about 88% lower cost and 36% lower latency with caching, In a 540-run comparison on a live desktop (20 read-only tasks, 3 runs each), pruning every 10 screenshots instead of every call cut the cost by about 45% at the same success, and keeping only 1 screenshot lowered success (93% against 98%) and raised the cost. The `cache_read` and `cache_write` totals in the run metrics show what a run got. The cache expires after about 5 minutes without a hit, so the first model call after a long pause in an interactive session pays the write price again.

## Cleanup

Remove all deployed AWS resources:

```bash
./scripts/cleanup.sh
```

This tears down the stack, fleet, VPC, subnets, NAT gateway, and security groups in reverse order.

## Appendix: Deploy to Bedrock AgentCore Runtime

Deploy an agent to Bedrock AgentCore Runtime for managed hosting:

```bash
./scripts/deploy_agentcore.sh
./scripts/deploy_agentcore.sh --agent application_validation --name MyValidationAgent
```

Prerequisites: Node.js 20+, `agentcore` CLI (`npm install -g @aws/agentcore`), `uv` (Python package manager).

Invoke:

```bash
cd .agentcore-build/WorkspacesAgentDemo
agentcore invoke '{"streaming_url": "<URL>"}' --stream
```

View logs:

```bash
# Local CLI logs
agentcore logs

# CloudWatch logs (runtime)
aws logs tail "/aws/bedrock-agentcore/runtimes/WorkspacesAgentDemo" --region us-east-1 --follow
```

Cleanup:

```bash
./scripts/deploy_agentcore.sh --cleanup
```

> **Note:** The AgentCore execution role must have access to the MCP Server endpoint. The deploy script automatically attaches the required IAM policy.

**Security: single-principal deployment only**

The AgentCore handler accepts `streaming_url` directly from the invocation payload. Anyone with permission to invoke the runtime can drive any WorkSpaces Applications session the execution role can reach — there is no cross-invoker isolation in this demo.

**Do not expose the runtime to more than one principal.** Recommended configurations:

- Single trusted caller (human operator or orchestration service) with `bedrock-agentcore:InvokeAgentRuntime` scoped to that principal.
- No resource-based policies that grant broad cross-account invoke access.

For production multi-tenant deployments, add a signed-grant flow: the caller passes an opaque `session_id`, the handler resolves it against a DynamoDB table that records the issuing principal, and rejects cross-principal lookups.

## Appendix: Integrate Into Your Own Agent

If you already have a WorkSpaces Applications fleet deployed and want to add agent access to your own codebase, see [`quickstart.py`](quickstart.py) — a self-contained ~60-line example with no dependencies on this repo's `lib/`:

```bash
pip install strands-agents mcp-proxy-for-aws boto3
STREAMING_URL=$(scripts/streaming_url.sh)
python3 quickstart.py "$STREAMING_URL"
```

The full framework in `agents/` and `lib/` adds retry logic, screenshot pruning, metrics, and multi-model support — but `quickstart.py` is all you need to connect an existing agent to a remote desktop.

## References

- [Amazon WorkSpaces Applications — Administration Guide](https://docs.aws.amazon.com/appstream2/latest/developerguide/) — fleets, stacks, image builders, streaming URLs
- [Amazon WorkSpaces Applications — Getting Started](https://docs.aws.amazon.com/appstream2/latest/developerguide/getting-started.html) — end-to-end setup with sample applications
- [Amazon WorkSpaces Applications — API Reference](https://docs.aws.amazon.com/appstream2/latest/APIReference/Welcome.html) — SDK and CLI operations
- [Amazon Bedrock — Claude models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html) — model IDs, inference parameters, regional availability
- [Strands Agents SDK](https://strandsagents.com/) — the agent framework this sample builds on

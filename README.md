# Sample Code for Amazon WorkSpaces Applications with agent access

Build autonomous agents that automate desktop workflows on [Amazon WorkSpaces Applications with agent access](https://docs.aws.amazon.com/appstream2/latest/developerguide/agent-access.html). Agents interact with any combination of applications — filling forms, transferring data between apps, navigating multi-step processes — using the Strands Agents SDK with Claude Computer Use.

Amazon WorkSpaces Applications enables agents to connect to streaming sessions and interact with desktop applications through [a managed Model Context Protocol (MCP) service](https://docs.aws.amazon.com/appstream2/latest/developerguide/agent-access-mcp-server.html).

## Prerequisites
The Quick Start helps you get setup with:
- **AWS account** with permission to create Amazon WorkSpaces Applications fleets/stacks and invoke Amazon Bedrock
- **AWS CLI v2** — [install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- **Python 3.10+** — [install guide](https://www.python.org/downloads/)
- **Valid AWS credentials** configured (run `aws sts get-caller-identity` to verify)
- **bash** on Windows — the deploy step runs via [Git for Windows](https://git-scm.com/download/win) (`winget install -e --id Git.Git`) or WSL (`wsl --install`)

## Quick Start

Clone the repo and run the setup script from the repository root. Agents log their output and store screenshots in the relative agent folders (`agents/pdf_extractor_demo/screenshots` and `agents/pdf_extractor_demo/logs`).

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
python3 agents/pdf_extractor_demo/agent.py --streaming-url "$STREAMING_URL"
```

## Demo Agents

```bash
source venv/bin/activate

# PDF extractor — uses Firefox, OpenOffice Writer, File Explorer
python3 agents/pdf_extractor_demo/agent.py --streaming-url "$STREAMING_URL"

# Paint — draws a stick figure dog in MS Paint
python3 agents/paint_demo/agent.py --streaming-url "$STREAMING_URL"

# App validation — tests desktop applications
python3 agents/application_validation/agent.py --streaming-url "$STREAMING_URL"

# Interactive — REPL for arbitrary desktop tasks
python3 agents/generic_cua/agent.py --streaming-url "$STREAMING_URL"

# Multi-agent — parallel validation (reads stack/fleet from config by default)
python3 agents/multi_agent_validation/agent.py

# MCP redirection — uses forwarded filesystem + fetch tools (requires a redirection fleet)
python3 agents/mcp_redirection_demo/agent.py --streaming-url "$STREAMING_URL"
```

### MCP Redirection (forwarded tools)

Fleets can expose additional tools beyond desktop interaction via MCP Redirection. When enabled, `tools/list` returns both desktop tools (`screenshot`, `left_click`, etc.) and forwarded tools (prefixed with `forwarded___`). These forwarded tools call external APIs or services configured on the fleet — your agent uses them like any other MCP tool.

To set up a fleet with custom MCP servers (requires ~30 min for AMI build + import):

```bash
./scripts/setup_mcp_redirection.sh --region us-east-1
```

This builds a Windows Server 2025 image with Python + FastMCP and two example servers (`filesystem`, `fetch`), imports it to WorkSpaces, and creates a fleet with `FORWARD_MCP_TOOLS` enabled. See `mcp_servers/` for the server source code.

> **Validate first:** before building a custom image, check that your MCP servers will forward correctly with the compatibility tester in [`utils/mcpforwardingtester/`](utils/mcpforwardingtester/). It catches the common failures (a server that lists no tools, a config saved with a BOM, or `env`/`cwd` keys that get silently ignored) before the ~30-minute image build.

Once the redirection fleet is running, `agents/mcp_redirection_demo/` drives the forwarded tools end to end — it lists and reads seeded files, fetches a web page, writes a report file, and confirms the result on the desktop:

```bash
STREAMING_URL=$(aws appstream create-streaming-url \
  --stack-name MCPRedirectStack --fleet-name MCPRedirect \
  --user-id test --validity 3600 \
  --query StreamingURL --output text)
python3 agents/mcp_redirection_demo/agent.py --streaming-url "$STREAMING_URL"
```

### Domain Join (AD-joined fleets)

For fleets joined to an Active Directory domain, agents authenticate via SAML assertion instead of a streaming URL. The assertion is passed in the MCP `_meta` field on the initialize request:

```bash
python3 agents/pdf_extractor_demo/agent.py \
    --saml-assertion "$(cat assertion.b64)" \
    --stack-arn "arn:aws:appstream:us-east-1:123456789012:stack/MyDJStack"
```

Prerequisites:
- WorkSpaces Applications fleet joined to an AD domain with Certificate-Based Authentication (CBA) enabled
- IAM SAML provider registered with your IdP certificate
- IAM role trusting the SAML provider
- Base64-encoded SAML assertion from your IdP (Okta, Entra ID, Ping, etc.)

The `--streaming-url` flag is not needed — the MCP server provisions a desktop session bound to the AD user identity in the assertion.

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

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--streaming-url URL` | *(required)* | WorkSpaces Applications streaming URL for the desktop session |
| `--model-id ID` | `global.anthropic.claude-sonnet-4-6` | Bedrock model ID |
| `--no-computer-use-tool` | off | Disable Claude's computer-use training optimizations |
| `--mcp-timeout SECS` | `180` | MCP client startup timeout |
| `--mcp-retries N` | `3` | Number of MCP connection retries |
| `--region REGION` | auto-detect | AWS region for Bedrock calls |
| `--mcp-region REGION` | matches `--region` | AWS region for MCP SigV4 signing. Must match the fleet region. |
| `--no-screenshot-pruning` | off | Keep all screenshots in conversation context |
| `--mcp-profile PROFILE` | default | AWS profile for SigV4 signing to the MCP endpoint |
| `--llm-profile PROFILE` | default | AWS profile for Bedrock LLM calls |
| `--saml-assertion B64` | | Base64-encoded SAML assertion for Domain Join (replaces `--streaming-url`) |
| `--stack-arn ARN` | | WorkSpaces Applications stack ARN (required with `--saml-assertion`) |

## Project Structure

```
sample-code-for-workspaces-agent-access/
├── quickstart.py                # Minimal self-contained example (~60 lines)
├── AGENTS.md                    # Developer guide (tools, architecture, IDE setup)
├── agents/
│   ├── agent_creator/          # Interactive agent builder
│   ├── application_validation/ # Single-app validation
│   ├── generic_cua/            # Interactive REPL agent
│   ├── mcp_redirection_demo/   # Forwarded MCP tools (filesystem + fetch) demo
│   ├── multi_agent_validation/ # Parallel multi-session validation
│   ├── paint_demo/             # MS Paint drawing demo
│   └── pdf_extractor_demo/     # PDF → Writer extraction demo
├── lib/
│   ├── agent_common.py         # Shared infrastructure (re-exports from sub-modules)
│   ├── model.py                # Bedrock model creation + multi-model support
│   ├── mcp_client.py           # MCP transport, endpoint resolution, tool names
│   ├── retry.py                # Connection retry logic, error classification
│   ├── screenshot_pruning_manager.py  # Token-saving screenshot manager
│   └── strands_logger.py       # Metrics and logging
├── scripts/
│   ├── config.json             # Fleet, stack, VPC, MCP endpoint config
│   ├── setup.sh                # One-step setup (macOS / Linux)
│   ├── setup.ps1               # One-step setup (Windows)
│   ├── streaming_url.sh        # Mint a fresh WorkSpaces Applications streaming URL
│   ├── deploy.sh               # Deploy VPC + Fleet + Stack
│   ├── cleanup.sh              # Tear down all resources
│   ├── deploy_agentcore.sh     # Deploy agent to Bedrock AgentCore Runtime
│   ├── deploy_agentcore_harness.sh  # Deploy via AgentCore Harness + Gateway + Memory (preview)
│   ├── install.sh              # Install Python dependencies (macOS / Linux)
│   ├── install.ps1             # Install Python dependencies (Windows)
│   ├── ci_local.sh             # Run CI checks locally (compile + validate skill JSON)
│   ├── package.sh              # Create distribution zip
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

**"400 Bad Request" from MCP endpoint**
- The fleet's region and the MCP signing region must match. If your fleet is in `us-east-1` but you're signing requests for `us-west-2` (or vice versa), the service rejects them as cross-region.
- Check `AWS_REGION` matches the fleet region, or pass `--mcp-region <fleet-region>` explicitly.

**"401 Unauthorized" from MCP endpoint**
- Your AWS credentials can't sign requests. Run `aws sts get-caller-identity` to verify.
- If using profiles: `--mcp-profile <profile>` for MCP, `--llm-profile <profile>` for Bedrock.

**"403 Forbidden" from MCP endpoint**
- Check that your IAM credentials have the required permissions for the Agent Access MCP Server.

**"AccessDeniedException" from Bedrock**
- Your credentials don't have `bedrock:InvokeModel` permission.
- Check that the model ID is available in your region.

**Agent runs but doesn't interact with the desktop**
- Confirm the Stack was created with AgentAccessConfig (COMPUTER_INPUT, COMPUTER_VISION all ENABLED).
- Recreate the stack if needed — see `scripts/deploy.sh`.

**Fleet fails to start or "AccessDeniedException" from AppStream**
- Your account may be missing the AppStream service role. AppStream requires a service-linked role to manage resources on your behalf.
- Check if it exists: `aws iam get-role --role-name AWSServiceRoleForAppStream`
- If missing, create it by visiting the [AppStream 2.0 console](https://console.aws.amazon.com/appstream2) or see [Checking for the IAM service access](https://docs.aws.amazon.com/appstream2/latest/developerguide/controlling-access-checking-for-iam-service-access.html).

**Screenshot pruning**
- By default, old screenshots are removed from conversation context to reduce token usage. Use `--no-screenshot-pruning` to keep all screenshots (useful for debugging).

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
./scripts/deploy_agentcore.sh --agent paint_demo --name MyPaintAgent
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

## Appendix: Deploy with AgentCore Harness (Preview)

The [AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html) provides managed agent orchestration — no custom agent code required. The deploy script creates an AgentCore Gateway that SigV4-signs requests to the MCP endpoint, then deploys a harness with [persistent memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html) (semantic + summarization) that uses it.

```bash
# Deploy (creates Gateway + Harness)
./scripts/deploy_agentcore_harness.sh
```

Prerequisites: Node.js 20+, `agentcore` CLI preview channel (`npm install -g @aws/agentcore@preview`), Python 3.10+ with `boto3`.

### Run the agent

Use the [AgentCore Harness Playground](https://us-east-1.console.aws.amazon.com/bedrock-agentcore/harnesses/playground) in the AWS Console — select your harness and start chatting. No local setup needed.

Or run locally:

```bash
# Navigate to the project
cd .agentcore-build/WSAgentHarness
agentcore dev
```

### Cleanup

```bash
./scripts/deploy_agentcore_harness.sh --cleanup
```

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

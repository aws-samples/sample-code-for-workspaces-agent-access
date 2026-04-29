# WorkSpaces Agent Demo

Build autonomous agents that automate desktop workflows on Amazon WorkSpaces. Agents interact with any combination of Windows applications — filling forms, transferring data between apps, navigating multi-step processes — using the Strands Agents SDK with Claude Computer Use.

## Quick Start

Clone the repo (or unzip a drop) on any environment with AWS CLI v2, Python 3.11+, and valid AWS credentials — your laptop, a cloud desktop, or a dev EC2 instance all work.

### macOS / Linux

```bash
cd workspaces-agent-demo
./scripts/setup.sh
```

### Windows (PowerShell)

```powershell
cd workspaces-agent-demo
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

The Windows setup needs a bash interpreter for the deploy step. Install [Git for Windows](https://git-scm.com/download/win) (`winget install -e --id Git.Git`) or enable WSL (`wsl --install`) and you're set.

This installs dependencies, deploys WorkSpaces resources (VPC, Fleet, Stack with AgentAccessConfig), waits for the fleet to start, generates a streaming URL, and runs the demo agent.

To run agents again after setup:

```bash
source venv/bin/activate

# Read stack + fleet names from config
STACK=$(python3 -c "import json,re; raw=re.sub(r'(?m)^\s*//.*$','',open('scripts/config.json').read()); print(json.loads(raw)['stack']['name'])")
FLEET=$(python3 -c "import json,re; raw=re.sub(r'(?m)^\s*//.*$','',open('scripts/config.json').read()); print(json.loads(raw)['fleet']['name'])")

# Generate a streaming URL (valid for 1 hour)
STREAMING_URL=$(aws appstream create-streaming-url \
  --stack-name "$STACK" --fleet-name "$FLEET" \
  --user-id testuser --validity 3600 \
  --query StreamingURL --output text)

# Run the agent
python3 agents/pdf_extractor_demo/agent.py --streaming-url "$STREAMING_URL"
```

## Demo Agents

```bash
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
```

## Create Your Own Agent

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
| `--streaming-url URL` | *(required)* | AppStream streaming URL for the desktop session |
| `--model-id ID` | `us.anthropic.claude-sonnet-4-6` | Bedrock model ID |
| `--computer-use-tool` | off | Enable `computer-use-2025-11-24` tool configuration |
| `--mcp-timeout SECS` | `180` | MCP client startup timeout |
| `--mcp-retries N` | `3` | Number of MCP connection retries |
| `--region REGION` | auto-detect | AWS region for Bedrock calls |
| `--no-screenshot-pruning` | off | Keep all screenshots in conversation context |
| `--mcp-profile PROFILE` | default | AWS profile for SigV4 signing to the MCP endpoint |
| `--llm-profile PROFILE` | default | AWS profile for Bedrock LLM calls |

## Project Structure

```
workspaces-agent-demo/
├── agents/
│   ├── agent_creator/          # Interactive agent builder
│   ├── application_validation/ # Single-app validation
│   ├── generic_cua/            # Interactive REPL agent
│   ├── multi_agent_validation/ # Parallel multi-session validation
│   ├── paint_demo/             # MS Paint drawing demo
│   └── pdf_extractor_demo/     # PDF → Writer extraction demo
├── lib/
│   ├── agent_common.py         # Shared infrastructure (parser, retry, MCP)
│   ├── screenshot_pruning_manager.py  # Token-saving screenshot manager
│   └── strands_logger.py       # Metrics and logging
├── scripts/
│   ├── config.json             # Fleet, stack, VPC, MCP endpoint config
│   ├── setup.sh                # One-step setup (macOS / Linux)
│   ├── setup.ps1               # One-step setup (Windows)
│   ├── deploy.sh               # Deploy VPC + Fleet + Stack
│   ├── cleanup.sh              # Tear down all resources
│   ├── deploy_agentcore.sh     # Deploy agent to Bedrock AgentCore Runtime
│   ├── install.sh              # Install Python dependencies (macOS / Linux)
│   ├── install.ps1             # Install Python dependencies (Windows)
│   ├── package.sh              # Create distribution zip
│   └── migrate_to_mcp.sh       # Migrate from local client to MCP server
├── skills/
│   └── workspace-skill-creator/  # Skill for creating new app skills
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
cd .agentcore-build/workspaces-agent-demo
agentcore invoke '{"streaming_url": "<URL>"}' --stream
```

View logs:

```bash
# Local CLI logs
agentcore logs

# CloudWatch logs (runtime)
aws logs tail "/aws/bedrock-agentcore/runtimes/workspaces-agent-demo" --region us-east-1 --follow
```

Cleanup:

```bash
./scripts/deploy_agentcore.sh --cleanup
```

> **Note:** The AgentCore execution role must have access to the MCP Server endpoint. The deploy script automatically attaches the required IAM policy.

### Security: single-principal deployment only

The AgentCore handler accepts `streaming_url` directly from the invocation payload. Anyone with permission to invoke the runtime can drive any AppStream session the execution role can reach — there is no cross-invoker isolation in this demo.

**Do not expose the runtime to more than one principal.** Recommended configurations:

- Single trusted caller (human operator or orchestration service) with `bedrock-agentcore:InvokeAgentRuntime` scoped to that principal.
- No resource-based policies that grant broad cross-account invoke access.

For production multi-tenant deployments, add a signed-grant flow: the caller passes an opaque `session_id`, the handler resolves it against a DynamoDB table that records the issuing principal, and rejects cross-principal lookups.

#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# Deploy a WorkSpaces agent to Amazon Bedrock AgentCore Runtime
#
# Creates an AgentCore project, injects the agent code, and deploys.
# Requires: Node.js 20+, Python 3.10+, AWS CDK, agentcore CLI
#
# Usage:
#   ./scripts/deploy_agentcore.sh
#   ./scripts/deploy_agentcore.sh --agent pdf_extractor_demo --name MyPdfAgent
#   ./scripts/deploy_agentcore.sh --cleanup
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
AGENT_NAME="pdf_extractor_demo"
AC_PROJECT_NAME="WorkspacesAgentDemo"
CLEANUP=false

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail() { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)   AGENT_NAME="$2"; shift 2 ;;
    --name)    AC_PROJECT_NAME="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    --cleanup) CLEANUP=true; shift ;;
    --help)
      echo "Usage: $0 [OPTIONS]"
      echo "  --agent NAME    Agent to deploy (default: pdf_extractor_demo)"
      echo "  --name NAME     AgentCore project name (default: WorkspacesAgentDemo)"
      echo "  --region REGION AWS region (default: auto-detect)"
      echo "  --cleanup       Remove deployed resources"
      exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
done

BUILD_DIR="$PROJECT_ROOT/.agentcore-build/$AC_PROJECT_NAME"

# Read MCP endpoint + (optional) signing-region override from config.json.
# The signing region defaults to the runtime region (AWS_REGION at container
# startup); only set MCP_REGION in the env file if config.json forces one
# explicitly.
eval $(python3 - "$PROJECT_ROOT" <<'PY'
import json, os, re, sys

project_root = sys.argv[1]

endpoint = os.environ.get("MCP_ENDPOINT", "")
service = os.environ.get("AWS_SERVICE_NAME", "")
region_override = ""
for p in [os.path.join(project_root, "scripts", "config.json"),
          os.path.join(os.getcwd(), "scripts", "config.json")]:
    if os.path.isfile(p):
        raw = re.sub(r"(?m)^\s*//.*$", "", open(p).read())
        data = json.loads(raw)
        mcp = data.get("mcp", {}) or {}
        endpoint = endpoint or mcp.get("endpoint") or data.get("mcpEndpoint") or ""
        service = service or mcp.get("service") or ""
        region_override = mcp.get("region") or ""
        break

if not endpoint:
    sys.stderr.write("ERROR: MCP_ENDPOINT is required. Set the environment variable or mcp.endpoint in scripts/config.json.\n")
    sys.exit(1)
if not service:
    sys.stderr.write("ERROR: AWS_SERVICE_NAME is required. Set the environment variable or mcp.service in scripts/config.json.\n")
    sys.exit(1)

print(f"MCP_ENDPOINT={endpoint!r}")
print(f"MCP_SERVICE={service!r}")
print(f"MCP_REGION_OVERRIDE={region_override!r}")
PY
)

# ── Cleanup mode ──────────────────────────────────────────────
if [ "$CLEANUP" = true ]; then
  info "Cleaning up AgentCore deployment: $AC_PROJECT_NAME"
  if [ -d "$BUILD_DIR" ]; then
    cd "$BUILD_DIR"
    agentcore remove all -y 2>/dev/null || true
    agentcore deploy -y 2>/dev/null || true
    cd "$PROJECT_ROOT"
    rm -rf "$BUILD_DIR"
    ok "Cleaned up"
  else
    info "No build directory found at $BUILD_DIR"
  fi
  exit 0
fi

# ── Preflight checks ─────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Deploy to Amazon Bedrock AgentCore Runtime${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Agent:   $AGENT_NAME"
echo "  Project: $AC_PROJECT_NAME"
echo "  Region:  $REGION"
echo ""

for cmd in node npm python3; do
  if ! command -v "$cmd" &>/dev/null; then
    fail "'$cmd' not found. Install it first."
  fi
done

# Check Node.js version (agentcore CLI requires 20+)
NODE_MAJOR=$(node -v | sed 's/v//' | cut -d. -f1)
if [ "$NODE_MAJOR" -lt 20 ]; then
  info "Node.js $NODE_MAJOR found — agentcore CLI requires 20+. Upgrading..."
  if command -v mise &>/dev/null; then
    mise install node@20 && mise use node@20
  elif command -v nvm &>/dev/null; then
    nvm install 20 && nvm use 20
  else
    fail "Node.js 20+ required (found v$NODE_MAJOR). Install via mise, nvm, or https://nodejs.org"
  fi
  ok "Node.js $(node -v) ready"
fi

if ! command -v agentcore &>/dev/null; then
  info "Installing AgentCore CLI..."
  npm install -g @aws/agentcore
fi

if ! command -v cdk &>/dev/null; then
  info "Installing AWS CDK..."
  npm install -g aws-cdk
fi

if ! command -v uv &>/dev/null; then
  info "Installing uv (Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

ok "Prerequisites found"

AGENT_DIR="$PROJECT_ROOT/agents/$AGENT_NAME"
if [ ! -d "$AGENT_DIR" ]; then
  fail "Agent not found: $AGENT_DIR"
fi

# ── Step 1: Create AgentCore project ──────────────────────────
info "Step 1: Creating AgentCore project..."

mkdir -p "$(dirname "$BUILD_DIR")"

if [ -d "$BUILD_DIR/agentcore" ]; then
  # Check if our deps are already in pyproject.toml
  if grep -q "mcp-proxy-for-aws" "$BUILD_DIR/app/$AC_PROJECT_NAME/pyproject.toml" 2>/dev/null; then
    info "Project already exists with deps — reusing"
  else
    info "Project exists but missing deps — recreating"
    rm -rf "$BUILD_DIR"
  fi
fi

if [ ! -d "$BUILD_DIR/agentcore" ]; then
  cd "$(dirname "$BUILD_DIR")"
  agentcore create \
    --name "$AC_PROJECT_NAME" \
    --framework Strands \
    --protocol HTTP \
    --build Container \
    --model-provider Bedrock \
    --memory none
  ok "Project scaffolded"
fi

cd "$BUILD_DIR"

# ── Step 2: Inject agent code ─────────────────────────────────
info "Step 2: Injecting agent code..."

APP_DIR="$BUILD_DIR/app/$AC_PROJECT_NAME"
mkdir -p "$APP_DIR"

# Copy lib
cp -r "$PROJECT_ROOT/lib" "$APP_DIR/lib"

# Copy the agent's prompts and skills
mkdir -p "$APP_DIR/agents/$AGENT_NAME"
cp -r "$AGENT_DIR/prompts" "$APP_DIR/agents/$AGENT_NAME/"
if [ -d "$AGENT_DIR/skills" ]; then
  cp -r "$AGENT_DIR/skills" "$APP_DIR/agents/$AGENT_NAME/"
fi

# Copy config.json for MCP endpoint default
mkdir -p "$APP_DIR/scripts"
cp "$PROJECT_ROOT/scripts/config.json" "$APP_DIR/scripts/"

# Write the main.py entrypoint
cat > "$APP_DIR/main.py" << 'MAIN_EOF'
"""
AgentCore Runtime entrypoint for WorkSpaces agents.

Handles HTTP requests from AgentCore Runtime, creates an agent,
and returns the result.
"""

import json
import os
import sys
import time
import logging
import traceback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

AGENT_NAME = os.environ.get("AGENT_NAME", "pdf_extractor_demo")
MCP_ENDPOINT = os.environ.get("MCP_ENDPOINT", "")
if not MCP_ENDPOINT:
    logger.error("MCP_ENDPOINT env var is required")
AWS_SERVICE_NAME = os.environ.get("AWS_SERVICE_NAME", "")
if not AWS_SERVICE_NAME:
    logger.error("AWS_SERVICE_NAME env var is required")
# MCP is deployed per-region. Sign for the runtime's own region by default
# (AWS_REGION is set by AgentCore Runtime). Set MCP_REGION env var only to
# override.
MCP_REGION = os.environ.get("MCP_REGION") or os.environ.get("AWS_REGION", "us-east-1")

# ── SSRF allow-list ──────────────────────────────────────────────
# Caller-supplied overrides (mcp_endpoint, mcp_region, region) are validated
# against a host/region allow-list before being forwarded. Reject anything
# that isn't https + in the allow-list. Env-var-driven so the allow-list
# can be updated without code changes.
#
# ALLOWED_MCP_HOSTS: comma-separated hostnames (no scheme / path).
# ALLOWED_MCP_REGIONS / ALLOWED_BEDROCK_REGIONS: comma-separated AWS regions.
from urllib.parse import urlparse as _urlparse

def _parse_csv(env_var, fallback=None):
    raw = os.environ.get(env_var, "")
    if not raw and fallback is not None:
        return frozenset(fallback)
    return frozenset(x.strip() for x in raw.split(",") if x.strip())

# In-scope for public preview. Americas + Europe + Asia Pacific.
_PUBLIC_PREVIEW_REGIONS = [
    # Americas
    "us-east-1", "us-east-2", "us-west-2", "ca-central-1",
    # Europe
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3",
    # Asia Pacific
    "ap-northeast-1", "ap-northeast-2", "ap-south-1",
    "ap-southeast-1", "ap-southeast-2",
]
ALLOWED_MCP_REGIONS = _parse_csv("ALLOWED_MCP_REGIONS", _PUBLIC_PREVIEW_REGIONS)
ALLOWED_BEDROCK_REGIONS = _parse_csv("ALLOWED_BEDROCK_REGIONS", _PUBLIC_PREVIEW_REGIONS)

# Build the default host allow-list by expanding any {region} template in
# MCP_ENDPOINT across every allowed MCP region. If the endpoint has no
# {region} token, the literal hostname is used as-is.
def _expand_endpoint_hosts(endpoint, regions):
    if not endpoint:
        return []
    if "{region}" in endpoint:
        return [
            _urlparse(endpoint.replace("{region}", r)).hostname
            for r in regions
            if _urlparse(endpoint.replace("{region}", r)).hostname
        ]
    host = _urlparse(endpoint).hostname
    return [host] if host else []

ALLOWED_MCP_HOSTS = _parse_csv(
    "ALLOWED_MCP_HOSTS",
    _expand_endpoint_hosts(MCP_ENDPOINT, ALLOWED_MCP_REGIONS),
)


def _validated_endpoint(url, default):
    """Resolve url-or-default, enforce https + allow-list. Raises ValueError."""
    candidate = url or default
    if not candidate:
        raise ValueError("mcp_endpoint is required")
    parsed = _urlparse(candidate)
    if parsed.scheme != "https":
        raise ValueError(f"mcp_endpoint must use https; got scheme {parsed.scheme!r}")
    if parsed.hostname not in ALLOWED_MCP_HOSTS:
        raise ValueError(f"mcp_endpoint host {parsed.hostname!r} not in allow-list")
    return candidate


def _validated_region(region, default, allowed):
    """Resolve region-or-default, enforce allow-list. Raises ValueError."""
    candidate = region or default
    if candidate not in allowed:
        raise ValueError(f"region {candidate!r} not in allow-list")
    return candidate

from bedrock_agentcore import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


def _build_agent(body):
    """Build and return a configured Agent for a single invocation.

    Raises an exception with a descriptive message if setup fails so the
    caller can surface it in the response.
    """
    from lib import agent_common, ScreenshotPruningConversationManager
    from strands import Agent
    from strands.models.bedrock import BedrockModel
    from strands.tools.mcp import MCPClient
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    import glob

    streaming_url = body.get("streaming_url")
    try:
        mcp_region = _validated_region(
            body.get("mcp_region"), MCP_REGION, ALLOWED_MCP_REGIONS
        )
        # Substitute {region} in the endpoint template before allow-list
        # validation so the templated hostname can match a specific entry.
        raw_endpoint = body.get("mcp_endpoint") or MCP_ENDPOINT
        rendered_endpoint = raw_endpoint.replace("{region}", mcp_region) if raw_endpoint else raw_endpoint
        mcp_endpoint = _validated_endpoint(rendered_endpoint, rendered_endpoint)
        llm_region = _validated_region(
            body.get("region"),
            os.environ.get("AWS_REGION", "us-east-1"),
            ALLOWED_BEDROCK_REGIONS,
        )
    except ValueError as e:
        raise ValueError(f"invalid endpoint/region: {e}") from e
    task_prompt_override = body.get("task_prompt") or body.get("prompt")
    model_id = body.get("model_id", "global.anthropic.claude-sonnet-4-6")

    if not streaming_url:
        raise ValueError(
            "No streaming_url provided. Pass {\"streaming_url\": \"<URL>\"} in the payload."
        )

    # Sanitize the streaming URL (shells can escape special chars)
    streaming_url = streaming_url.strip().replace("\\?", "?").replace("\\=", "=").replace("\\&", "&")

    logger.info(f"MCP endpoint: {mcp_endpoint}  (signing region: {mcp_region})")
    logger.info(f"Bedrock region: {llm_region}  Model: {model_id}")

    # Load agent prompts and skills
    agent_dir = os.path.join(APP_DIR, "agents", AGENT_NAME)
    system_prompt = agent_common.load_prompt(os.path.join(agent_dir, "prompts/system_prompt.md"))
    task_prompt = task_prompt_override or agent_common.load_prompt(os.path.join(agent_dir, "prompts/task_prompt.md"))

    for sf in glob.glob(os.path.join(agent_dir, "skills/*.json")):
        try:
            with open(sf) as f:
                skill = json.load(f)
            system_prompt += f"\n\n=== SKILL ===\n{json.dumps(skill, indent=2)}\n"
        except Exception:
            pass

    model = BedrockModel(model_id=model_id, region_name=llm_region)

    def mcp_factory():
        return aws_iam_streamablehttp_client(
            endpoint=mcp_endpoint,
            aws_service=AWS_SERVICE_NAME,
            aws_region=mcp_region,
            headers={
                "X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url,
            },
        )

    # Retry MCP client startup — session may still be initializing
    max_retries = 3
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            mcp_client = MCPClient(mcp_factory, startup_timeout=120)
            agent = Agent(
                model=model,
                tools=[mcp_client],
                system_prompt=system_prompt,
                conversation_manager=ScreenshotPruningConversationManager(),
            )
            return agent, task_prompt
        except Exception as e:
            last_err = e
            cause = e.__cause__ or e.__context__ or e
            logger.warning(
                f"MCP connect attempt {attempt}/{max_retries} failed: "
                f"{type(cause).__name__}: {cause}"
            )
            if attempt < max_retries:
                time.sleep(10 * attempt)

    raise RuntimeError(
        f"Failed to connect to MCP after {max_retries} attempts. "
        f"Last error: {type(last_err).__name__}: {last_err}"
    ) from last_err


@app.entrypoint
async def handler(payload):
    """AgentCore entrypoint for WorkSpaces agents.

    Declared ``async`` because AgentCore Runtime invokes entrypoints inside a
    running asyncio event loop. MCPClient.start() spawns a background thread
    and synchronously waits on a future — doing that from an already-running
    loop raises WouldBlock. We therefore offload the blocking build to a
    worker thread via asyncio.to_thread.
    """
    import asyncio

    body = payload if isinstance(payload, dict) else json.loads(payload)

    # agentcore invoke wraps the input as {"prompt": "..."} — unwrap if needed
    if "prompt" in body and isinstance(body["prompt"], str):
        try:
            inner = json.loads(body["prompt"])
            if isinstance(inner, dict):
                body = inner
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        # _build_agent is fully blocking: it starts the MCP background thread
        # and waits for the init future. Run it off the event loop.
        agent, task_prompt = await asyncio.to_thread(_build_agent, body)
    except ValueError as e:
        # User input errors get a 400-style response
        return {"status": "error", "error": str(e), "agent": AGENT_NAME}
    except Exception as e:
        logger.exception("Failed to build agent")
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc(limit=10),
            "agent": AGENT_NAME,
        }

    try:
        # Strands Agent() invocation is also sync (it internally uses
        # run_until_complete). Offload it too.
        result = await asyncio.to_thread(agent, task_prompt)
        output = str(result) if result else ""
        return {"status": "success", "result": output, "agent": AGENT_NAME}
    except Exception as e:
        logger.exception("Agent execution failed")
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc(limit=10),
            "agent": AGENT_NAME,
        }

if __name__ == "__main__":
    app.run()
MAIN_EOF

ok "Agent code injected"

# ── Step 3: Update dependencies ───────────────────────────────
info "Step 3: Updating dependencies..."

# Container deps pinned to the same versions as requirements.txt at the
# package root. Bump both places together. See requirements.in for the
# source of truth and regenerate the hashed lockfile with pip-compile.
if [ -f "$APP_DIR/pyproject.toml" ]; then
  cd "$APP_DIR"
  for dep in \
    "mcp-proxy-for-aws==1.4.0" \
    "strands-agents==1.36.0" \
    "mcp==1.27.0" \
    "boto3==1.42.93" \
  ; do
    uv add --quiet "$dep" 2>/dev/null || true
  done
  cd "$BUILD_DIR"
  ok "Dependencies installed"
else
  warn "pyproject.toml not found — add mcp-proxy-for-aws manually"
fi

# ── Step 4: Update environment variables ──────────────────────
info "Step 4: Configuring environment..."

# MCP_REGION is only set when config.json forces an explicit override.
# Otherwise the handler falls back to AWS_REGION at runtime so it signs
# for the region where the AgentCore container is running.
ENV_FILE="$BUILD_DIR/agentcore/.env.local"
{
  echo "AGENT_NAME=$AGENT_NAME"
  echo "MCP_ENDPOINT=$MCP_ENDPOINT"
  if [ -n "$MCP_REGION_OVERRIDE" ]; then
    echo "MCP_REGION=$MCP_REGION_OVERRIDE"
  fi
} >> "$ENV_FILE"

info "  MCP endpoint: $MCP_ENDPOINT"
if [ -n "$MCP_REGION_OVERRIDE" ]; then
  info "  MCP signing region: $MCP_REGION_OVERRIDE (from config override)"
else
  info "  MCP signing region: runtime region (AWS_REGION)"
fi
ok "Environment configured"

# Update aws-targets.json with account and region (list format only — AgentCore default)
TARGETS_FILE="$BUILD_DIR/agentcore/aws-targets.json"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION" 2>/dev/null || echo "")

python3 - "$TARGETS_FILE" "$ACCOUNT_ID" "$REGION" <<'PY'
import json, os, sys

targets_file, account, region = sys.argv[1], sys.argv[2], sys.argv[3]
target = {"name": "default", "account": account, "region": region}

if os.path.isfile(targets_file):
    existing = json.load(open(targets_file))
    if isinstance(existing, list):
        existing = [t for t in existing if t.get("name") != "default"] + [target]
    else:
        existing = [target]
else:
    existing = [target]

json.dump(existing, open(targets_file, "w"), indent=2)
print(f"  Account: {account}, Region: {region}")
PY

# ── Step 5: Deploy ────────────────────────────────────────────
info "Step 5: Deploying to AgentCore Runtime..."
echo ""

agentcore deploy -y

echo ""
ok "Deployed to AgentCore Runtime"

# ── Step 6: Add MCP endpoint access to execution role ─────────
info "Step 6: Configuring MCP endpoint access..."

EXEC_ROLE_ARN=""
EXEC_ROLE=""

# Preferred: ask bedrock-agentcore-control for the runtime's exact execution role.
# This avoids guessing based on role name patterns.
#
# AgentCore doubles the project name: a project called "WorkspacesAgentDemo" creates
# a runtime named "WorkspacesAgentDemo_WorkspacesAgentDemo". Match by contains() so we
# tolerate that and any future naming conventions.
RUNTIME_ID=$(aws bedrock-agentcore-control list-agent-runtimes \
  --region "$REGION" \
  --query "agentRuntimes[?contains(agentRuntimeName, '${AC_PROJECT_NAME}')].agentRuntimeId | [0]" \
  --output text 2>/dev/null || echo "")

if [ -n "$RUNTIME_ID" ] && [ "$RUNTIME_ID" != "None" ]; then
  info "Found runtime: $RUNTIME_ID"

  # get-agent-runtime returns the exact roleArn used by the container
  EXEC_ROLE_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
    --region "$REGION" \
    --agent-runtime-id "$RUNTIME_ID" \
    --query 'roleArn' --output text 2>/dev/null || echo "")

  if [ -n "$EXEC_ROLE_ARN" ] && [ "$EXEC_ROLE_ARN" != "None" ]; then
    EXEC_ROLE="${EXEC_ROLE_ARN##*/}"
    info "Execution role: $EXEC_ROLE_ARN"
  fi
fi

# Fallback: match by name pattern if the control-plane query didn't work.
# Real AgentCore role names look like "AgentCore-<Project>-ApplicationAgent..."
# so don't filter on "ExecutionRole" — it's not in the name.
if [ -z "$EXEC_ROLE" ]; then
  warn "Control-plane lookup failed, falling back to IAM list-roles"
  for PATTERN in "AgentCore-${AC_PROJECT_NAME}" "BedrockAgentCore" "${AC_PROJECT_NAME}"; do
    EXEC_ROLE=$(aws iam list-roles \
      --query "Roles[?contains(RoleName, '$PATTERN')].RoleName | [0]" \
      --output text 2>/dev/null || echo "")
    if [ -n "$EXEC_ROLE" ] && [ "$EXEC_ROLE" != "None" ]; then
      EXEC_ROLE_ARN=$(aws iam get-role --role-name "$EXEC_ROLE" \
        --query 'Role.Arn' --output text 2>/dev/null || echo "")
      info "Found execution role by name pattern '$PATTERN': $EXEC_ROLE_ARN"
      break
    fi
  done
fi

if [ -n "$EXEC_ROLE" ] && [ "$EXEC_ROLE" != "None" ]; then
  SERVICE_ACTION_PREFIX="${SERVICE_ACTION_PREFIX:-}"
  if [ -z "$SERVICE_ACTION_PREFIX" ]; then
    fail "SERVICE_ACTION_PREFIX env var is required (IAM action prefix for the MCP service)."
  fi

  # Scope Bedrock / AppStream / logs actions to the specific resources this
  # runtime needs. Operators should export these before running the deploy:
  #   ALLOWED_MODEL_ARNS  = "arn:aws:bedrock:...::foundation-model/global.anthropic.claude-sonnet-4-6,..."
  #   STACK_ARN           = "arn:aws:appstream:us-east-1:123:stack/WorkspacesAgentDemo"
  #   FLEET_ARN           = "arn:aws:appstream:us-east-1:123:fleet/WorkspacesAgentDemo"
  # Defaults below are permissive to keep the demo working out of the box;
  # harden them before any production use. We allow both the global.* and us.*
  # inference-profile prefixes plus the underlying foundation-model ARNs so
  # Bedrock can resolve the profile to a target model in any routed region.
  ALLOWED_MODEL_ARNS="${ALLOWED_MODEL_ARNS:-arn:aws:bedrock:*::foundation-model/anthropic.claude-*,arn:aws:bedrock:*:*:inference-profile/global.anthropic.claude-*,arn:aws:bedrock:*:*:inference-profile/us.anthropic.claude-*}"
  STACK_ARN="${STACK_ARN:-arn:aws:appstream:*:*:stack/*}"
  FLEET_ARN="${FLEET_ARN:-arn:aws:appstream:*:*:fleet/*}"
  LOG_GROUP_ARN="${LOG_GROUP_ARN:-arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/runtimes/*:*}"

  # Build the policy document with least-privilege scoping. MCP action is
  # narrowed to Invoke with a SourceAccount condition so another account's
  # principal that somehow assumed this role still can't use it cross-account.
  POLICY_NAME="${SERVICE_ACTION_PREFIX}-access"
  POLICY_DOC=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeMcp",
      "Effect": "Allow",
      "Action": "${SERVICE_ACTION_PREFIX}:Invoke",
      "Resource": "*",
      "Condition": {
        "StringEquals": {"aws:SourceAccount": "${ACCOUNT_ID}"}
      }
    },
    {
      "Sid": "InvokeBedrockModel",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [$(echo "\"${ALLOWED_MODEL_ARNS}\"" | sed 's/,/","/g')]
    },
    {
      "Sid": "CreateStreamingUrlForScopedStack",
      "Effect": "Allow",
      "Action": "appstream:CreateStreamingURL",
      "Resource": ["${STACK_ARN}", "${FLEET_ARN}"]
    },
    {
      "Sid": "DescribeOwnedFleets",
      "Effect": "Allow",
      "Action": "appstream:DescribeFleets",
      "Resource": "*",
      "Condition": {
        "StringEquals": {"aws:ResourceTag/Project": "WorkSpacesAgentDemo"}
      }
    },
    {
      "Sid": "WriteOwnLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams"
      ],
      "Resource": "${LOG_GROUP_ARN}"
    }
  ]
}
EOF
)
  if aws iam put-role-policy \
    --role-name "$EXEC_ROLE" \
    --policy-name "$POLICY_NAME" \
    --policy-document "$POLICY_DOC" 2>&1; then
    ok "MCP endpoint access attached to $EXEC_ROLE (scoped)"
  else
    warn "Could not attach policy — check that you have iam:PutRolePolicy on $EXEC_ROLE"
  fi

  # Verify the policy actually landed
  if aws iam get-role-policy --role-name "$EXEC_ROLE" --policy-name "$POLICY_NAME" \
    --query "PolicyDocument.Statement[?Action=='${SERVICE_ACTION_PREFIX}:*'] | [0].Action" \
    --output text 2>/dev/null | grep -q "${SERVICE_ACTION_PREFIX}"; then
    ok "Verified: ${SERVICE_ACTION_PREFIX}:* is on the role"
  else
    warn "Could not verify the policy landed — check manually with:"
    echo "    aws iam get-role-policy --role-name $EXEC_ROLE --policy-name $POLICY_NAME"
  fi

  echo ""
  echo "  Role: $EXEC_ROLE"
  echo "  ARN:  $EXEC_ROLE_ARN"

  # IAM policy propagation can take 10-15 seconds — the next invoke may 403
  # if you try immediately. Warn the user.
  info "Note: IAM policy changes can take 10-15 seconds to propagate."
else
  warn "Could not find execution role — you'll need to attach MCP access manually"
  echo ""
  echo "  Find the role:"
  echo "    aws bedrock-agentcore-control get-agent-runtime \\"
  echo "      --region $REGION \\"
  echo "      --agent-runtime-id <RUNTIME_ID> \\"
  echo "      --query roleArn --output text"
  echo ""
  echo "  List runtimes to find the ID:"
  echo "    aws bedrock-agentcore-control list-agent-runtimes --region $REGION"
fi

# ── Step 7: Show CloudWatch log group ─────────────────────────
info "Step 7: CloudWatch Logs..."

# AgentCore log group follows the pattern: /aws/bedrock-agentcore/runtimes/<ProjectName>_<ProjectName>-<hash>-DEFAULT
# Try to find the exact log group
CW_LOG_GROUP=$(aws logs describe-log-groups \
  --region "$REGION" \
  --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/${AC_PROJECT_NAME}" \
  --query 'logGroups[0].logGroupName' --output text 2>/dev/null || echo "")

if [ -n "$CW_LOG_GROUP" ] && [ "$CW_LOG_GROUP" != "None" ]; then
  ok "Log group: $CW_LOG_GROUP"
else
  CW_LOG_GROUP="/aws/bedrock-agentcore/runtimes/${AC_PROJECT_NAME}"
  info "Log group (expected): ${CW_LOG_GROUP}_*"
fi

CW_URL="https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#logsV2:log-groups/log-group/\$252Faws\$252Fbedrock-agentcore\$252Fruntimes\$252F${AC_PROJECT_NAME}"

# ── Step 8: Show status and invoke instructions ───────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  AgentCore Deployment Complete${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Project: $AC_PROJECT_NAME"
echo "  Agent:   $AGENT_NAME"
echo "  Region:  $REGION"
if [ -n "$EXEC_ROLE_ARN" ] && [ "$EXEC_ROLE_ARN" != "None" ]; then
  echo ""
  echo "  Execution Role ARN:"
  echo "    $EXEC_ROLE_ARN"
fi
echo ""

agentcore status 2>/dev/null || true

echo ""
echo -e "${GREEN}Invoke your agent:${NC}"
echo ""
echo "  cd $BUILD_DIR"
echo ""
echo "  # With a streaming URL"
echo "  agentcore invoke '{\"streaming_url\": \"<URL>\"}' --stream"
echo ""
echo "  # With a custom task"
echo "  agentcore invoke '{\"streaming_url\": \"<URL>\", \"task_prompt\": \"Open Firefox and take a screenshot\"}' --stream"
echo ""
echo -e "${GREEN}View logs:${NC}"
echo ""
echo "  # CLI logs (local)"
echo "  agentcore logs"
echo ""
echo "  # CloudWatch logs (runtime)"
echo "  aws logs tail '$CW_LOG_GROUP' --region $REGION --follow"
echo ""
echo "  # CloudWatch console"
echo "  $CW_URL"
echo ""
echo -e "${GREEN}Manage:${NC}"
echo ""
echo "  # Return to project root"
echo "  cd $PROJECT_ROOT"
echo ""
echo "  # Clean up"
echo "  ./scripts/deploy_agentcore.sh --cleanup"
echo ""

#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# Deploy a WorkSpaces agent using AgentCore Harness + Gateway
#
# Creates an AgentCore Gateway (with SigV4 signing to the MCP
# endpoint), then deploys a Harness with persistent memory
# (semantic + summarization) that uses the Gateway as a tool.
# Zero custom agent code — the harness orchestrates, the Gateway
# handles auth, and memory persists across sessions.
#
# Prerequisites:
#   - Node.js 20+, agentcore CLI preview (@aws/agentcore@preview)
#   - AWS credentials with bedrock-agentcore + iam permissions
#   - A deployed WorkSpaces Applications fleet + stack
#
# Usage:
#   ./scripts/deploy_agentcore_harness.sh
#   ./scripts/deploy_agentcore_harness.sh --region us-east-1
#   ./scripts/deploy_agentcore_harness.sh --cleanup
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROJECT_NAME="WSAgentHarness"
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
    --name)    PROJECT_NAME="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    --cleanup) CLEANUP=true; shift ;;
    --help)
      echo "Usage: $0 [OPTIONS]"
      echo "  --name NAME      Project name (default: WSAgentHarness)"
      echo "  --region REGION  AWS region (default: us-east-1)"
      echo "  --cleanup        Remove deployed resources"
      exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "$ACCOUNT_ID" ]; then
  fail "Could not determine AWS account. Check your credentials."
fi

BUILD_DIR="$PROJECT_ROOT/.agentcore-build/$PROJECT_NAME"
GATEWAY_NAME="${PROJECT_NAME}-Gateway"
HARNESS_NAME="${PROJECT_NAME}"
ROLE_NAME="${PROJECT_NAME}-GatewayRole"
MCP_ENDPOINT="https://agentaccess-mcp.${REGION}.api.aws/mcp"

# ── Cleanup mode ──────────────────────────────────────────────
if [ "$CLEANUP" = true ]; then
  info "Cleaning up harness + gateway..."

  LAMBDA_NAME="${PROJECT_NAME}-McpProxy"
  LAMBDA_ROLE_NAME="${PROJECT_NAME}-McpProxyRole"

  # Delete harness via agentcore CLI
  if [ -d "$BUILD_DIR" ]; then
    cd "$BUILD_DIR"
    agentcore remove all -y 2>/dev/null || true
    agentcore deploy -y 2>/dev/null || true
    cd "$PROJECT_ROOT"
    rm -rf "$BUILD_DIR"
    ok "Harness project removed"
  fi

  # Delete gateway
  GW_ID=$(python3 -c "
import boto3, sys
try:
    client = boto3.client('bedrock-agentcore-control', region_name='$REGION')
    resp = client.list_gateways()
    for gw in resp.get('items', []):
        if gw.get('name') == '$GATEWAY_NAME':
            print(gw.get('gatewayId', ''))
            sys.exit(0)
except Exception:
    pass
print('')
" 2>/dev/null)
GW_ID="${GW_ID:-}"
  if [ -n "$GW_ID" ]; then
    # Delete targets first
    python3 -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='$REGION')
targets = client.list_gateway_targets(gatewayIdentifier='$GW_ID')
for t in targets.get('items', []):
    client.delete_gateway_target(gatewayIdentifier='$GW_ID', targetId=t['targetId'])
    print(f\"  Deleted target {t['targetId']}\")
" 2>/dev/null || true
    python3 -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='$REGION')
client.delete_gateway(gatewayIdentifier='$GW_ID')
" 2>/dev/null || true
    ok "Gateway deleted: $GW_ID"
  fi

  # Delete Lambda proxy
  aws lambda delete-function --function-name "$LAMBDA_NAME" --region "$REGION" 2>/dev/null || true
  aws iam delete-role-policy --role-name "$LAMBDA_ROLE_NAME" --policy-name "McpProxyPolicy" 2>/dev/null || true
  aws iam detach-role-policy --role-name "$LAMBDA_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true
  aws iam delete-role --role-name "$LAMBDA_ROLE_NAME" 2>/dev/null || true
  ok "Lambda proxy deleted: $LAMBDA_NAME"

  # Delete Gateway role
  aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "GatewayAccess" 2>/dev/null || true
  aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "LambdaInvokePolicy" 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || true
  ok "IAM role deleted: $ROLE_NAME"

  exit 0
fi

# ── Preflight ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Deploy AgentCore Harness + Gateway${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Account:      $ACCOUNT_ID"
echo "  Region:       $REGION"
echo "  MCP:          $MCP_ENDPOINT"
echo "  Gateway:      $GATEWAY_NAME"
echo "  Harness:      $HARNESS_NAME"
echo "  Memory:       ${HARNESS_NAME}Memory (semantic + summarization)"
echo ""

for cmd in node aws; do
  command -v "$cmd" &>/dev/null || fail "'$cmd' not found"
done

if ! command -v agentcore &>/dev/null; then
  info "Installing AgentCore CLI (preview)..."
  npm install -g @aws/agentcore@preview
fi

# ── Step 1: Create Gateway IAM role ──────────────────────────
info "Step 1: Creating Gateway IAM role..."

TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF
)

ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" \
  --query 'Role.Arn' --output text 2>/dev/null || echo "")

if [ -z "$ROLE_ARN" ] || [ "$ROLE_ARN" = "None" ]; then
  ROLE_ARN=$(aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --description "Role for AgentCore Gateway to sign MCP requests" \
    --query 'Role.Arn' --output text)

  # Grant the Gateway role permission to call the MCP service
  POLICY_DOC=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeMcp",
      "Effect": "Allow",
      "Action": "agentaccess-mcp:*",
      "Resource": "*",
      "Condition": {
        "StringEquals": {"aws:PrincipalAccount": "$ACCOUNT_ID"}
      }
    },
    {
      "Sid": "AgentCoreAccess",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:*",
      "Resource": "*",
      "Condition": {
        "StringEquals": {"aws:ResourceAccount": "$ACCOUNT_ID"}
      }
    }
  ]
}
EOF
)
  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "GatewayAccess" \
    --policy-document "$POLICY_DOC"

  ok "Created role: $ROLE_ARN"
  info "Waiting 10s for IAM propagation..."
  sleep 10
else
  ok "Role exists: $ROLE_ARN"
fi

# ── Step 2: Create Gateway ───────────────────────────────────
info "Step 2: Creating AgentCore Gateway..."

GW_ID=$(python3 -c "
import boto3, sys
try:
    client = boto3.client('bedrock-agentcore-control', region_name='$REGION')
    resp = client.list_gateways()
    for gw in resp.get('items', []):
        if gw.get('name') == '$GATEWAY_NAME':
            print(gw.get('gatewayId', ''))
            sys.exit(0)
except Exception:
    pass
print('')
" 2>/dev/null)
GW_ID="${GW_ID:-}"

if [ -z "$GW_ID" ]; then
  GW_ID=$(python3 -c "
import boto3, json, sys
client = boto3.client('bedrock-agentcore-control', region_name='$REGION')
try:
    resp = client.create_gateway(
        name='$GATEWAY_NAME',
        protocolType='MCP',
        roleArn='$ROLE_ARN',
        authorizerType='NONE',
        protocolConfiguration={'mcp': {'supportedVersions': ['2025-03-26', '2025-06-18']}}
    )
    print(resp.get('gatewayId', resp.get('gatewayIdentifier', '')))
except client.exceptions.ConflictException:
    # Gateway already exists — find its ID
    resp = client.list_gateways()
    for gw in resp.get('items', []):
        if gw.get('name') == '$GATEWAY_NAME':
            print(gw.get('gatewayId', ''))
            sys.exit(0)
    print('')
" 2>&1) || true

  if [ -z "$GW_ID" ] || echo "$GW_ID" | grep -q "Traceback"; then
    fail "Failed to create gateway: $GW_ID"
  fi

  # Wait for gateway to be ready
  info "Waiting for gateway to be ready..."
  for i in $(seq 1 30); do
    STATUS=$(aws bedrock-agentcore-control get-gateway --region "$REGION" \
      --gateway-identifier "$GW_ID" \
      --query 'status' --output text 2>/dev/null || echo "UNKNOWN")
    if [ "$STATUS" = "READY" ] || [ "$STATUS" = "ACTIVE" ]; then
      break
    fi
    sleep 5
  done

  ok "Gateway created: $GW_ID (status: $STATUS)"
else
  ok "Gateway exists: $GW_ID"
fi

# Get Gateway ARN
GW_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:gateway/${GW_ID}"

# ── Step 3: Create Lambda Proxy Target ───────────────────────
info "Step 3: Creating Lambda proxy for MCP tool calls..."

LAMBDA_NAME="${PROJECT_NAME}-McpProxy"
LAMBDA_ROLE_NAME="${PROJECT_NAME}-McpProxyRole"

# Create Lambda execution role
LAMBDA_ROLE_ARN=$(aws iam get-role --role-name "$LAMBDA_ROLE_NAME" \
  --query 'Role.Arn' --output text 2>/dev/null || echo "")

if [ -z "$LAMBDA_ROLE_ARN" ] || [ "$LAMBDA_ROLE_ARN" = "None" ]; then
  LAMBDA_ROLE_ARN=$(aws iam create-role \
    --role-name "$LAMBDA_ROLE_NAME" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }' \
    --query 'Role.Arn' --output text)

  aws iam put-role-policy \
    --role-name "$LAMBDA_ROLE_NAME" \
    --policy-name "McpProxyPolicy" \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {
          \"Effect\": \"Allow\",
          \"Action\": [\"appstream:CreateStreamingURL\", \"appstream:DescribeFleets\"],
          \"Resource\": \"*\"
        },
        {
          \"Effect\": \"Allow\",
          \"Action\": \"agentaccess-mcp:*\",
          \"Resource\": \"*\"
        },
        {
          \"Effect\": \"Allow\",
          \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\"],
          \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:*\"
        }
      ]
    }"

  aws iam attach-role-policy \
    --role-name "$LAMBDA_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

  info "Waiting 10s for Lambda role propagation..."
  sleep 10
  ok "Created Lambda role: $LAMBDA_ROLE_ARN"
else
  ok "Lambda role exists: $LAMBDA_ROLE_ARN"
fi

# Load fleet/stack names from config
FLEET_NAME=$(python3 -c "import json; print(json.load(open('$SCRIPT_DIR/config.json'))['fleet']['name'])" 2>/dev/null || echo "WorkspacesAgentDemo")
STACK_NAME=$(python3 -c "import json; print(json.load(open('$SCRIPT_DIR/config.json'))['stack']['name'])" 2>/dev/null || echo "Workspaces-Apps-AgentDemo")

# Package Lambda (include urllib3 dependency)
LAMBDA_DIR="$SCRIPT_DIR/gateway_lambda_proxy"
LAMBDA_ZIP="/tmp/${LAMBDA_NAME}.zip"
LAMBDA_PKG="/tmp/${LAMBDA_NAME}_pkg"
rm -rf "$LAMBDA_PKG" "$LAMBDA_ZIP"
pip install urllib3 -t "$LAMBDA_PKG" --quiet 2>/dev/null
cp "$LAMBDA_DIR/lambda_function.py" "$LAMBDA_PKG/"
(cd "$LAMBDA_PKG" && zip -qr "$LAMBDA_ZIP" .)
rm -rf "$LAMBDA_PKG"

# Create or update Lambda function
LAMBDA_ARN=$(aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" \
  --query 'Configuration.FunctionArn' --output text 2>/dev/null || echo "")

if [ -z "$LAMBDA_ARN" ] || [ "$LAMBDA_ARN" = "None" ]; then
  LAMBDA_ARN=$(aws lambda create-function \
    --function-name "$LAMBDA_NAME" \
    --region "$REGION" \
    --runtime python3.12 \
    --handler lambda_function.handler \
    --role "$LAMBDA_ROLE_ARN" \
    --zip-file "fileb://$LAMBDA_ZIP" \
    --timeout 180 \
    --memory-size 512 \
    --environment "Variables={FLEET_NAME=$FLEET_NAME,STACK_NAME=$STACK_NAME,MCP_ENDPOINT=$MCP_ENDPOINT}" \
    --query 'FunctionArn' --output text)
  ok "Created Lambda: $LAMBDA_ARN"
else
  aws lambda update-function-code \
    --function-name "$LAMBDA_NAME" \
    --region "$REGION" \
    --zip-file "fileb://$LAMBDA_ZIP" > /dev/null
  aws lambda update-function-configuration \
    --function-name "$LAMBDA_NAME" \
    --region "$REGION" \
    --timeout 180 \
    --memory-size 512 \
    --environment "Variables={FLEET_NAME=$FLEET_NAME,STACK_NAME=$STACK_NAME,MCP_ENDPOINT=$MCP_ENDPOINT}" > /dev/null 2>&1 || true
  ok "Updated Lambda: $LAMBDA_ARN"
fi

rm -f "$LAMBDA_ZIP"

# Grant Gateway permission to invoke the Lambda
aws lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --region "$REGION" \
  --statement-id "AllowGatewayInvoke" \
  --action "lambda:InvokeFunction" \
  --principal "bedrock-agentcore.amazonaws.com" \
  --source-arn "$GW_ARN" 2>/dev/null || true

# Add Lambda invoke to Gateway role
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "LambdaInvokePolicy" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": \"lambda:InvokeFunction\",
      \"Resource\": \"$LAMBDA_ARN\"
    }]
  }"

info "Waiting 10s for IAM propagation..."
sleep 10

ok "Lambda proxy deployed: $LAMBDA_ARN"

# ── Step 4: Create Gateway Lambda Target ─────────────────────
info "Step 4: Creating Gateway Target (Lambda proxy)..."

# Tool definitions for the desktop interaction tools
python3 - "$REGION" "$GW_ID" "$LAMBDA_ARN" <<'PY'
import boto3, json, sys

region, gw_id, lambda_arn = sys.argv[1], sys.argv[2], sys.argv[3]
client = boto3.client('bedrock-agentcore-control', region_name=region)

tools = [
    {"name": "screenshot", "description": "Capture a screenshot of the desktop. Returns a PNG image.", "inputSchema": {"type": "object", "properties": {"include_cursor": {"type": "boolean", "description": "Include cursor in screenshot"}}}},
    {"name": "left_click", "description": "Left click at screen coordinates", "inputSchema": {"type": "object", "properties": {"x": {"type": "integer", "description": "X coordinate"}, "y": {"type": "integer", "description": "Y coordinate"}}, "required": ["x", "y"]}},
    {"name": "double_click", "description": "Double click at coordinates", "inputSchema": {"type": "object", "properties": {"x": {"type": "integer", "description": "X coordinate"}, "y": {"type": "integer", "description": "Y coordinate"}}, "required": ["x", "y"]}},
    {"name": "right_click", "description": "Right click at coordinates", "inputSchema": {"type": "object", "properties": {"x": {"type": "integer", "description": "X coordinate"}, "y": {"type": "integer", "description": "Y coordinate"}}, "required": ["x", "y"]}},
    {"name": "type_text", "description": "Type text by simulating keyboard events", "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "Text to type"}}, "required": ["text"]}},
    {"name": "key", "description": "Press a key or key combination. Examples: ctrl+c, Return, alt+F4, super+r", "inputSchema": {"type": "object", "properties": {"keys": {"type": "string", "description": "Key or combination"}}, "required": ["keys"]}},
    {"name": "scroll", "description": "Scroll at coordinates", "inputSchema": {"type": "object", "properties": {"x": {"type": "integer", "description": "X"}, "y": {"type": "integer", "description": "Y"}, "scroll_direction": {"type": "string", "description": "Up, Down, Left, or Right"}, "scroll_amount": {"type": "integer", "description": "Ticks (120=one notch)"}}, "required": ["x", "y", "scroll_direction", "scroll_amount"]}},
    {"name": "left_click_drag", "description": "Drag from start to end coordinates", "inputSchema": {"type": "object", "properties": {"start_x": {"type": "integer", "description": "Start X"}, "start_y": {"type": "integer", "description": "Start Y"}, "end_x": {"type": "integer", "description": "End X"}, "end_y": {"type": "integer", "description": "End Y"}}, "required": ["start_x", "start_y", "end_x", "end_y"]}},
    {"name": "triple_click", "description": "Triple click at coordinates", "inputSchema": {"type": "object", "properties": {"x": {"type": "integer", "description": "X"}, "y": {"type": "integer", "description": "Y"}}, "required": ["x", "y"]}},
    {"name": "move_pointer", "description": "Move pointer to coordinates", "inputSchema": {"type": "object", "properties": {"x": {"type": "integer", "description": "X"}, "y": {"type": "integer", "description": "Y"}}, "required": ["x", "y"]}},
    {"name": "hold_key", "description": "Hold key for duration in seconds", "inputSchema": {"type": "object", "properties": {"keys": {"type": "string", "description": "Key"}, "duration": {"type": "integer", "description": "Seconds (1-30)"}}, "required": ["keys", "duration"]}}
]

try:
    response = client._make_api_call('CreateGatewayTarget', {
        'gatewayIdentifier': gw_id,
        'name': 'agent-access',
        'targetConfiguration': {
            'mcp': {
                'lambda': {
                    'lambdaArn': lambda_arn,
                    'toolSchema': {
                        'inlinePayload': tools
                    }
                }
            }
        },
        'credentialProviderConfigurations': [{
            'credentialProviderType': 'GATEWAY_IAM_ROLE'
        }]
    })
    print(f"  Target ID: {response.get('targetId', 'unknown')}")
    print(f"  Status: {response.get('status', 'unknown')}")
except Exception as e:
    if 'ConflictException' in str(type(e).__name__) or 'already exists' in str(e):
        print("  Target already exists — skipping")
    else:
        raise
PY

ok "Gateway target configured: agent-access → Lambda proxy → MCP"

# ── Step 5: Deploy Harness ───────────────────────────────────
info "Step 5: Deploying harness..."

mkdir -p "$(dirname "$BUILD_DIR")"

if [ ! -d "$BUILD_DIR/agentcore" ]; then
  cd "$(dirname "$BUILD_DIR")"
  agentcore create --name "$PROJECT_NAME" --no-agent
fi

cd "$BUILD_DIR"

# Add harness if not present
if ! grep -q "\"harnesses\"" agentcore/agentcore.json 2>/dev/null || \
   python3 -c "import json; c=json.load(open('agentcore/agentcore.json')); exit(0 if not c.get('harnesses') else 1)" 2>/dev/null; then
  agentcore add harness \
    --name "$HARNESS_NAME" \
    --model-id global.anthropic.claude-sonnet-4-6 \
    --system-prompt "You are controlling a Windows desktop via MCP tools. You can operate any application — launching programs, navigating menus, filling forms, managing files, and performing any task a human user would do with a mouse and keyboard.

## Core Principles

1. **Observe before acting** — Always take a screenshot first when you begin a task or arrive at an unfamiliar state. You need to see the desktop before you can interact with it accurately.
2. **Verify after uncertain actions** — After opening a menu, launching an app, or submitting a form, take a screenshot to confirm the result before continuing.
3. **Batch predictable actions** — Group related actions (click field → type text → press Tab) without screenshots between them. Only screenshot after completing a logical batch.
4. **Aim for center** — When clicking UI elements, target their visual center, not edges.

## Launching Applications

Use the Run dialog for reliability: key(super+r) → type_text(appname) → key(Return). This is more reliable than Start Menu search.

## Error Recovery

- **Unexpected dialog**: Read it, dismiss with Escape or click Close/Cancel.
- **Wrong window in front**: key(alt+Tab) or click the taskbar icon.
- **Click missed target**: Take a screenshot, re-identify coordinates, retry.
- **App not responding**: Wait a few seconds, try Escape, then alt+F4 as last resort.
- **General recovery**: Escape → alt+Tab → screenshot → resume from last confirmed step.

## Key Shortcuts

Ctrl+Z=Undo, Ctrl+S=Save, Ctrl+C=Copy, Ctrl+V=Paste, Ctrl+A=Select All, Escape=Dismiss, Alt+Tab=Switch Window, Alt+F4=Close Window, super+r=Run Dialog" 2>/dev/null || true
fi

# Add gateway tool to harness
agentcore add tool --harness "$HARNESS_NAME" --type agentcore_gateway \
  --name agent-access --gateway-arn "$GW_ARN" 2>/dev/null || true

# Add memory and restrict to Gateway tools only
agentcore add memory \
  --name "${HARNESS_NAME}Memory" \
  --strategies SEMANTIC,SUMMARIZATION \
  --expiry 30 2>/dev/null || true

python3 -c "
import json, os
# Restrict to Gateway tools and link memory in harness.json
hj = 'app/$HARNESS_NAME/harness.json'
if os.path.isfile(hj):
    with open(hj) as f:
        h = json.load(f)
    h['allowedTools'] = ['@agent-access']
    h['memory'] = {'name': '${HARNESS_NAME}Memory'}
    with open(hj, 'w') as f:
        json.dump(h, f, indent=2)
" 2>/dev/null || true

# Set target
python3 -c "
import json
t = [{'name': 'default', 'account': '$ACCOUNT_ID', 'region': '$REGION'}]
json.dump(t, open('agentcore/aws-targets.json', 'w'), indent=2)
"

agentcore deploy -y

ok "Harness deployed"

# ── Step 6: Grant harness role Gateway access ────────────────
info "Step 6: Granting harness role Gateway access..."

HARNESS_ROLE="${PROJECT_NAME}_${HARNESS_NAME}"
HARNESS_GW_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeGateway",
      "Resource": "$GW_ARN"
    },
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "*"
    }
  ]
}
EOF
)

aws iam put-role-policy \
  --role-name "$HARNESS_ROLE" \
  --policy-name "HarnessGatewayAccess" \
  --policy-document "$HARNESS_GW_POLICY" 2>/dev/null && \
  ok "Granted InvokeGateway to $HARNESS_ROLE" || \
  warn "Could not attach policy to $HARNESS_ROLE — attach manually"

# ── Done ─────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Deployment Complete${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Gateway:      $GW_ARN"
echo "  Harness:      $HARNESS_NAME"
echo "  Memory:       ${HARNESS_NAME}Memory (semantic + summarization)"
echo "  Lambda:       $LAMBDA_ARN"
echo "  MCP:          $MCP_ENDPOINT"
echo ""
echo -e "${GREEN}Run your agent:${NC}"
echo ""
echo "  # Option 1: AWS Console playground (no local setup needed)"
echo "  https://${REGION}.console.aws.amazon.com/bedrock-agentcore/harnesses/playground"
echo ""
echo "  # Option 2: Local dev server"
echo "  cd $BUILD_DIR"
echo "  agentcore dev"
echo ""
echo -e "${GREEN}Cleanup:${NC}"
echo ""
echo "  ./scripts/deploy_agentcore_harness.sh --cleanup"
echo ""

#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# WorkSpaces Agent Demo — One-Step Setup
#
# Run from any environment with Python 3.11+ and valid AWS credentials
# (e.g. your own laptop, an EC2 dev instance, or a cloud desktop).
# This script will install the AWS CLI for you if it isn't already
# present and walk you through signing in if your credentials aren't
# configured.
#
#   bash scripts/setup.sh
#
# Steps:
#   1. Ensures AWS CLI v2 is installed + credentials are valid
#   2. Installs Python 3.11 (skipped if already present)
#   3. Installs agent dependencies into a local venv
#   4. Deploys AWS resources (VPC, Fleet, Stack)
#   5. Waits for the fleet to reach RUNNING state
#   6. Generates a streaming URL
#   7. Runs the demo agent
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

FLEET_NAME=$(python3 -c "
import json,re,os
path = os.path.join('$SCRIPT_DIR', 'config.json')
if os.path.isfile(path):
    raw = re.sub(r'(?m)^\s*//.*','',open(path).read())
    print(json.loads(raw).get('fleet', {}).get('name', 'WorkspacesAgentDemo'))
else:
    print('WorkspacesAgentDemo')
" 2>/dev/null || echo "WorkspacesAgentDemo")
FLEET_WAIT_TIMEOUT=900
POLL_INTERVAL=15
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail() { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

separator() {
  echo ""
  echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
  echo -e "${BOLD}  $*${NC}"
  echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
  echo ""
}

# Ensure all scripts are executable (zip doesn't preserve permissions)
chmod +x "$SCRIPT_DIR"/*.sh 2>/dev/null || true

# ── Step 1: AWS CLI + credentials ─────────────────────────────
separator "Step 1/7: Checking AWS CLI and credentials"

if command -v aws &>/dev/null; then
  ok "AWS CLI found: $(aws --version 2>&1 | head -1)"
else
  warn "AWS CLI v2 is not installed."
  echo ""
  echo "  Install it with the commands below, then re-run this setup script."
  echo ""
  case "$(uname -s)" in
    Linux)
      case "$(uname -m)" in
        x86_64|amd64) AWS_ARCH="x86_64" ;;
        aarch64|arm64) AWS_ARCH="aarch64" ;;
        *) AWS_ARCH="x86_64" ;;
      esac
      echo -e "${CYAN}  # Linux ($AWS_ARCH)${NC}"
      echo "  curl \"https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip\" -o /tmp/awscliv2.zip"
      echo "  unzip -q /tmp/awscliv2.zip -d /tmp"
      echo "  sudo /tmp/aws/install"
      ;;
    Darwin)
      echo -e "${CYAN}  # macOS${NC}"
      echo "  curl \"https://awscli.amazonaws.com/AWSCLIV2.pkg\" -o /tmp/AWSCLIV2.pkg"
      echo "  sudo installer -pkg /tmp/AWSCLIV2.pkg -target /"
      ;;
    *)
      echo -e "${CYAN}  # See https://aws.amazon.com/cli/ for your platform.${NC}"
      ;;
  esac
  echo ""
  fail "AWS CLI not installed. Install it with the commands above, then re-run: bash scripts/setup.sh"
fi

# Verify credentials are configured and usable. sts:GetCallerIdentity is the
# cheapest call that proves the full SigV4 path works.
CALLER_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)

if [ -z "$CALLER_ACCOUNT" ]; then
  warn "AWS credentials are missing or expired."
  echo ""
  echo "  Sign in with one of the following, then re-run this setup script."
  echo ""
  echo -e "${CYAN}  # AWS SSO / IAM Identity Center (recommended)${NC}"
  echo "  aws configure sso          # first time only"
  echo "  aws sso login              # every session"
  echo ""
  echo -e "${CYAN}  # Long-lived access keys${NC}"
  echo "  aws configure"
  echo ""
  echo -e "${CYAN}  # Already have a named profile configured?${NC}"
  echo "  export AWS_PROFILE=<your-profile>"
  echo ""
  echo -e "${CYAN}  # Verify sign-in worked${NC}"
  echo "  aws sts get-caller-identity"
  echo ""
  fail "Not signed in. Sign in with the commands above, then re-run: bash scripts/setup.sh"
fi

CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo "")
ok "Signed in as: $CALLER_ARN"
ok "Account: $CALLER_ACCOUNT | Region: $REGION | Fleet: $FLEET_NAME"

# ── Step 2: Python ────────────────────────────────────────────
separator "Step 2/7: Installing Python 3.11"

if command -v python3.11 &>/dev/null; then
  ok "Python 3.11 is already installed."
else
  info "Installing python3.11 and python3.11-pip..."
  sudo yum install -y python3.11 python3.11-pip > /dev/null 2>&1
  ok "Python 3.11 installed."
fi

# ── Step 3: Agent dependencies ────────────────────────────────
separator "Step 3/7: Installing agent dependencies"

info "Running install.sh..."
"$SCRIPT_DIR/install.sh"
source "$PROJECT_ROOT/venv/bin/activate"
ok "Agent dependencies installed."

# ── Step 4: Deploy WorkSpaces resources ───────────────────────
separator "Step 4/7: Deploying AWS resources (VPC, Fleet, Stack)"

info "Running deploy.sh — this may take several minutes..."
"$SCRIPT_DIR/deploy.sh"
ok "AWS resources deployed."

# ── Step 5: Wait for fleet ────────────────────────────────────
separator "Step 5/7: Waiting for fleet to reach RUNNING state"

info "Fleet: $FLEET_NAME | Region: $REGION"
info "This can take a few minutes..."

ELAPSED=0
while true; do
  FLEET_STATE=$(aws appstream describe-fleets \
    --region "$REGION" \
    --names "$FLEET_NAME" \
    --query 'Fleets[0].State' \
    --output text 2>/dev/null || echo "UNKNOWN")

  if [ "$FLEET_STATE" = "RUNNING" ]; then
    ok "Fleet '$FLEET_NAME' is RUNNING."
    break
  fi

  if [ "$ELAPSED" -ge "$FLEET_WAIT_TIMEOUT" ]; then
    fail "Fleet did not reach RUNNING state within $(( FLEET_WAIT_TIMEOUT / 60 )) minutes (current: $FLEET_STATE)."
  fi

  info "Fleet state: $FLEET_STATE — waiting ${POLL_INTERVAL}s... (${ELAPSED}s / ${FLEET_WAIT_TIMEOUT}s)"
  sleep "$POLL_INTERVAL"
  ELAPSED=$(( ELAPSED + POLL_INTERVAL ))
done

# ── Step 6: Generate streaming URL ────────────────────────────
separator "Step 6/7: Generating streaming URL"

CONFIG="$SCRIPT_DIR/config.json"
STACK_NAME=$(python3 -c "
import json,re
raw=open('$CONFIG').read()
raw=re.sub(r'(?m)^\s*//.*','',raw)
d=json.loads(raw)
print(d['stack']['name'])" 2>/dev/null || echo "Workspaces-Apps-AgentDemo")

info "Creating streaming URL..."
STREAMING_URL=$(aws appstream create-streaming-url \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --fleet-name "$FLEET_NAME" \
  --user-id testuser \
  --validity 3600 \
  --query 'StreamingURL' --output text 2>/dev/null || echo "")

if [ -z "$STREAMING_URL" ]; then
  warn "Could not create streaming URL. The fleet may still be starting."
  warn "Generate one manually after the fleet is running:"
  echo "  aws appstream create-streaming-url --stack-name $STACK_NAME --fleet-name $FLEET_NAME --user-id testuser --validity 3600"
else
  echo ""
  echo -e "  Streaming URL: ${CYAN}${STREAMING_URL}${NC}"
  echo ""
fi

# ── Step 7: Run demo agent ────────────────────────────────────
separator "Step 7/7: Running demo agent"

source "$PROJECT_ROOT/venv/bin/activate"

info "Starting pdf_extractor_demo..."
info "Press Ctrl+C to stop."
echo ""

if [ -n "${STREAMING_URL:-}" ]; then
  python3 "$PROJECT_ROOT/agents/pdf_extractor_demo/agent.py" \
    --streaming-url "$STREAMING_URL"
else
  echo ""
  echo -e "${RED}No streaming URL available.${NC} Generate one and run manually:"
  echo ""
  echo "  aws appstream create-streaming-url \\"
  echo "    --stack-name $STACK_NAME --fleet-name $FLEET_NAME \\"
  echo "    --user-id testuser --validity 3600 \\"
  echo "    --query StreamingURL --output text"
  echo ""
  echo "  python3 agents/pdf_extractor_demo/agent.py --streaming-url '<URL>'"
  echo ""
fi

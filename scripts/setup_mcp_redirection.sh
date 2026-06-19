#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# MCP Redirection Fleet Setup Script
# Builds a Windows Server 2025 AMI with MCP servers, imports to AppStream,
# and creates a fleet + stack with FORWARD_MCP_TOOLS enabled.
#
# Prerequisites:
#   - AWS credentials for the target account
#   - IAM roles: MCP-ImageBuild-Profile, AppStreamImageImportRole
#
# Usage:
#   ./scripts/setup_mcp_redirection.sh [OPTIONS]
#
# Options:
#   --mcp-endpoint URL         Agent Access MCP endpoint (default: prod)
#   --region REGION            AWS region (default: us-east-1)

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
MCP_ENDPOINT=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --mcp-endpoint) MCP_ENDPOINT="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

MCP_ENDPOINT="${MCP_ENDPOINT:-https://agentaccess-mcp.${REGION}.api.aws/mcp}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_NAME="mcp-redirection-$(date +%Y%m%d-%H%M)"
FLEET_NAME="MCPRedirect"
STACK_NAME="MCPRedirectStack"

echo "=== MCP Redirection Setup ==="
echo "Account: $ACCOUNT_ID"
echo "Region: $REGION"
echo "MCP endpoint: $MCP_ENDPOINT"
echo ""

# ─── Step 1: Find latest Windows Server 2025 AMI ───
echo "Step 1: Finding latest Windows Server 2025 AMI..."
SOURCE_AMI=$(aws ec2 describe-images --owners amazon --region "$REGION" \
  --filters "Name=name,Values=TPM-Windows_Server-2025-English-Full-Base-*" \
            "Name=architecture,Values=x86_64" "Name=state,Values=available" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text)
echo "  Source AMI: $SOURCE_AMI"

# ─── Step 2: Ensure IAM roles exist ───
echo "Step 2: Checking IAM roles..."
if ! aws iam get-instance-profile --instance-profile-name MCP-ImageBuild-Profile --region "$REGION" &>/dev/null; then
  echo "  Creating MCP-ImageBuild-Profile..."
  aws iam create-role --role-name MCP-ImageBuild-Role \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --region "$REGION" --output text --query 'Role.Arn'
  aws iam attach-role-policy --role-name MCP-ImageBuild-Role \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore --region "$REGION"
  aws iam create-instance-profile --instance-profile-name MCP-ImageBuild-Profile --region "$REGION"
  aws iam add-role-to-instance-profile --instance-profile-name MCP-ImageBuild-Profile \
    --role-name MCP-ImageBuild-Role --region "$REGION"
  sleep 10
fi

if ! aws iam get-role --role-name AppStreamImageImportRole &>/dev/null; then
  echo "  Creating AppStreamImageImportRole..."
  aws iam create-role --role-name AppStreamImageImportRole --path /service-role/ \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"appstream.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --region "$REGION"
  aws iam put-role-policy --role-name AppStreamImageImportRole --policy-name EC2Access \
    --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ec2:ModifyImageAttribute","ec2:DescribeImages"],"Resource":"*"}]}' \
    --region "$REGION"
  sleep 10
fi
echo "  IAM roles ready."

# ─── Step 3: Launch EC2 instance ───
echo "Step 3: Launching EC2 instance..."
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id "$SOURCE_AMI" --instance-type m5.large \
  --iam-instance-profile Name=MCP-ImageBuild-Profile --count 1 \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=MCP-ImageBuild}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "  Instance: $INSTANCE_ID"

# ─── Step 4: Wait for SSM ───
echo "Step 4: Waiting for SSM agent..."
for i in $(seq 1 20); do
  sleep 15
  COUNT=$(aws ssm describe-instance-information --region "$REGION" \
    --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
    --query 'length(InstanceInformationList)' --output text)
  if [ "$COUNT" -gt "0" ]; then echo "  SSM ready."; break; fi
done

# ─── Step 5: Install Python + FastMCP ───
echo "Step 5: Installing Python + FastMCP..."
CMD_ID=$(aws ssm send-command --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["$ErrorActionPreference = \"Stop\"","Invoke-WebRequest -Uri \"https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe\" -OutFile \"$env:TEMP\\python-installer.exe\"","Start-Process -Wait -FilePath \"$env:TEMP\\python-installer.exe\" -ArgumentList \"InstallAllUsers=1\",\"PrependPath=1\",\"/quiet\"","Remove-Item \"$env:TEMP\\python-installer.exe\"","& \"C:\\Program Files\\Python312\\python.exe\" -m pip install fastmcp","& \"C:\\Program Files\\Python312\\python.exe\" --version"]' \
  --query 'Command.CommandId' --output text)
for i in $(seq 1 30); do
  sleep 15
  STATUS=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" --query 'Status' --output text 2>/dev/null)
  if [ "$STATUS" = "Success" ]; then echo "  Python + FastMCP installed."; break; fi
  if [ "$STATUS" = "Failed" ]; then echo "  FAILED!"; exit 1; fi
done

# ─── Step 6: Write MCP server files + manifest ───
echo "Step 6: Writing MCP server files..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Base64 encode server files
# Base64 encode server files
FS_B64=$(base64 -w0 "$SCRIPT_DIR/../mcp_servers/filesystem_server.py")
FETCH_B64=$(base64 -w0 "$SCRIPT_DIR/../mcp_servers/fetch_server.py")
MANIFEST='{"mcpServers":{"filesystem":{"command":"C:\\\\Program Files\\\\Python312\\\\python.exe","args":["C:\\\\McpServers\\\\filesystem_server.py"]},"fetch":{"command":"C:\\\\Program Files\\\\Python312\\\\python.exe","args":["C:\\\\McpServers\\\\fetch_server.py"]}}}'
MANIFEST_B64=$(echo -n "$MANIFEST" | base64 -w0)

CMD_ID=$(aws ssm send-command --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunPowerShellScript" \
  --parameters commands="[\"New-Item -ItemType Directory -Force -Path C:\\\\McpServers\",\"New-Item -ItemType Directory -Force -Path C:\\\\ProgramData\\\\NICE\\\\dcv\",\"New-Item -ItemType Directory -Force -Path C:\\\\Users\\\\Public\\\\Documents\",\"[System.IO.File]::WriteAllBytes('C:\\\\McpServers\\\\filesystem_server.py', [System.Convert]::FromBase64String('$FS_B64'))\",\"[System.IO.File]::WriteAllBytes('C:\\\\McpServers\\\\fetch_server.py', [System.Convert]::FromBase64String('$FETCH_B64'))\",\"[System.IO.File]::WriteAllBytes('C:\\\\ProgramData\\\\NICE\\\\dcv\\\\mcp_server_redirection_config.json', [System.Convert]::FromBase64String('$MANIFEST_B64'))\",\"Set-Content -Path 'C:\\\\Users\\\\Public\\\\Documents\\\\hello.txt' -Value 'Hello from MCP filesystem server'\",\"Set-Content -Path 'C:\\\\Users\\\\Public\\\\Documents\\\\test.txt' -Value 'Test file for MCP forwarding'\"]" \
  --query 'Command.CommandId' --output text)
for i in $(seq 1 10); do
  sleep 10
  STATUS=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" --query 'Status' --output text 2>/dev/null)
  if [ "$STATUS" = "Success" ]; then echo "  Files written."; break; fi
  if [ "$STATUS" = "Failed" ]; then echo "  FAILED!"; exit 1; fi
done

# ─── Step 7: Stop instance + create AMI ───
echo "Step 7: Creating AMI..."
aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" --output text > /dev/null
aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" --region "$REGION"
AMI_ID=$(aws ec2 create-image --instance-id "$INSTANCE_ID" \
  --name "$IMAGE_NAME-$(date +%Y%m%d-%H%M)" --region "$REGION" --query 'ImageId' --output text)
echo "  AMI: $AMI_ID"
echo "  Waiting for AMI (this takes ~5 min)..."
aws ec2 wait image-available --image-ids "$AMI_ID" --region "$REGION"
echo "  AMI available."

# ─── Step 8: Terminate build instance ───
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION" --output text > /dev/null
echo "  Build instance terminated."

# ─── Step 9: Import to AppStream ───
echo "Step 9: Importing to AppStream with ALWAYS_LATEST..."
aws appstream create-imported-image \
  --name "$IMAGE_NAME" \
  --source-ami-id "$AMI_ID" \
  --iam-role-arn "arn:aws:iam::${ACCOUNT_ID}:role/service-role/AppStreamImageImportRole" \
  --agent-software-version ALWAYS_LATEST \
  --region "$REGION"
echo ""
echo "  Waiting for image AVAILABLE (this takes ~15-20 min)..."
while true; do
  STATE=$(aws appstream describe-images --names "$IMAGE_NAME" --region "$REGION" \
    --query 'Images[0].State' --output text 2>/dev/null || echo "UNKNOWN")
  echo "  Image state: $STATE"
  if [ "$STATE" = "AVAILABLE" ]; then break; fi
  if [ "$STATE" = "FAILED" ]; then echo "  FAILED!"; exit 1; fi
  sleep 60
done

# ─── Step 10: Create fleet + stack ───
echo "Step 10: Creating fleet and stack..."

# Create or update fleet
FLEET_EXISTS=$(aws appstream describe-fleets --names "$FLEET_NAME" --region "$REGION" --query 'Fleets[0].Name' --output text 2>/dev/null || echo "")
if [ -z "$FLEET_EXISTS" ] || [ "$FLEET_EXISTS" = "None" ]; then
  aws appstream create-fleet --name "$FLEET_NAME" --region "$REGION" \
    --instance-type "GeneralPurpose.t3.large" \
    --image-name "$IMAGE_NAME" \
    --fleet-type "ON_DEMAND" \
    --compute-capacity DesiredInstances=1 \
    --stream-view "DESKTOP" > /dev/null
  echo "  Created fleet: $FLEET_NAME"
else
  aws appstream update-fleet --name "$FLEET_NAME" --region "$REGION" \
    --image-name "$IMAGE_NAME" > /dev/null 2>&1 || true
  echo "  Updated fleet: $FLEET_NAME (image: $IMAGE_NAME)"
fi

aws appstream start-fleet --name "$FLEET_NAME" --region "$REGION" 2>/dev/null || true

# Create stack with AgentAccessConfig (FORWARD_MCP_TOOLS enabled)
STACK_EXISTS=$(aws appstream describe-stacks --names "$STACK_NAME" --region "$REGION" --query 'Stacks[0].Name' --output text 2>/dev/null || echo "")
if [ -z "$STACK_EXISTS" ] || [ "$STACK_EXISTS" = "None" ]; then
  SIGV4_PYTHON="${SCRIPT_DIR}/../venv/bin/python"
  [ ! -x "$SIGV4_PYTHON" ] && SIGV4_PYTHON="python3"
  "$SIGV4_PYTHON" -c "
import json, urllib.request
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session
endpoint = 'https://appstream2.${REGION}.amazonaws.com'
body = json.dumps({
    'Name': '$STACK_NAME',
    'AgentAccessConfig': {
        'Settings': [
            {'AgentAction': 'COMPUTER_VISION', 'Permission': 'ENABLED'},
            {'AgentAction': 'COMPUTER_INPUT', 'Permission': 'ENABLED'},
            {'AgentAction': 'FORWARD_MCP_TOOLS', 'Permission': 'ENABLED'}
        ],
        'ScreenResolution': 'W_1280xH_720',
        'ScreenImageFormat': 'PNG',
        'UserControlMode': 'VIEW_STOP'
    }
})
headers = {'Content-Type': 'application/x-amz-json-1.1', 'X-Amz-Target': 'PhotonAdminProxyService.CreateStack'}
creds = Session().get_credentials().get_frozen_credentials()
req = AWSRequest(method='POST', url=endpoint, data=body, headers=headers)
SigV4Auth(creds, 'appstream', '$REGION').add_auth(req)
r = urllib.request.urlopen(urllib.request.Request(req.url, data=body.encode(), headers=dict(req.headers), method='POST'))
print('  Created stack: $STACK_NAME')
" || echo "  Stack creation failed (may already exist)"
else
  echo "  Stack exists: $STACK_NAME"
fi

# Associate fleet with stack
aws appstream associate-fleet --fleet-name "$FLEET_NAME" --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null || true

# Wait for fleet to start
echo "  Waiting for fleet to reach RUNNING state..."
for i in $(seq 1 40); do
  STATE=$(aws appstream describe-fleets --names "$FLEET_NAME" --region "$REGION" --query 'Fleets[0].State' --output text 2>/dev/null)
  if [ "$STATE" = "RUNNING" ]; then echo "  Fleet is RUNNING."; break; fi
  sleep 15
done

echo ""
echo "=== SETUP COMPLETE ==="
echo ""
echo "  Fleet:  $FLEET_NAME (image: $IMAGE_NAME)"
echo "  Stack:  $STACK_NAME"
echo "  MCP:    $MCP_ENDPOINT"
echo ""
echo "  Generate streaming URL:"
echo "    aws appstream create-streaming-url --stack-name $STACK_NAME --fleet-name $FLEET_NAME --user-id test --validity 3600 --region $REGION --query StreamingURL --output text"
echo ""
echo "  Run an agent:"
echo "    python3 agents/generic_cua/agent.py --streaming-url \"\$STREAMING_URL\""

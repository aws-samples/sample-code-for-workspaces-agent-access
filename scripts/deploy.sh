#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# WorkSpaces Applications — CLI Deploy (no CDK required)
#
# Reads config.json and provisions the same resources as the CDK
# stacks using AWS CLI only. No Node.js, no Python venv needed.
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

# Under MSYS/Cygwin (Git Bash), bash paths like /d/Users/... are not
# understood by native Windows Python. Convert to a Windows path so
# tools we invoke can actually open the file.
if command -v cygpath &>/dev/null; then
  CONFIG="$(cygpath -w "$CONFIG")"
fi

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: config.json not found at $CONFIG" >&2
  exit 1
fi

if ! command -v aws &>/dev/null; then
  echo "ERROR: AWS CLI v2 is not installed." >&2
  exit 1
fi

# Require AWS CLI v2. v1 is unsupported (dropped support for several APIs
# this script depends on, plus differs on some output formatting).
AWS_VERSION_LINE=$(aws --version 2>&1 | head -1)
if [[ "$AWS_VERSION_LINE" != aws-cli/2.* ]]; then
  echo "ERROR: Found '$AWS_VERSION_LINE' — AWS CLI v2 is required." >&2
  echo "       See https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null && ! command -v jq &>/dev/null; then
  echo "ERROR: Need python3, python, or jq to parse config.json." >&2
  exit 1
fi

# Pick a usable Python: prefer python3, fall back to python (common on Windows
# where the python.org installer only provides `python.exe`). We also skip
# anything under WindowsApps since those are the Microsoft Store stubs that
# print "Python was not found" to stderr instead of running.
_find_python() {
  for c in python3 python; do
    local path
    path=$(command -v "$c" 2>/dev/null || true)
    [ -z "$path" ] && continue
    case "$path" in
      *WindowsApps*|*windowsapps*) continue ;;
    esac
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' &>/dev/null; then
      echo "$c"
      return 0
    fi
  done
  return 1
}
PYTHON_BIN=$(_find_python || true)
if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: No working Python 3.8+ found on PATH." >&2
  echo "  If you are on Windows, install Python from https://www.python.org/" >&2
  echo "  or disable the Microsoft Store 'python' alias under Settings >" >&2
  echo "  Manage App Execution Aliases." >&2
  exit 1
fi

# ── Helper: read config values ────────────────────────────────
# We pass CONFIG through sys.argv (not interpolated into the Python source) so
# paths with backslashes or special chars don't break the Python string literal.
cfg() {
  "$PYTHON_BIN" -c "
import json,re,sys
raw=open(sys.argv[1]).read()
raw=re.sub(r'(?m)^\s*//.*$','',raw)
raw=re.sub(r',\s*//[^\n]*','',raw)
d=json.loads(raw)
v=eval('d$1')
if v is None:
    print('')
elif isinstance(v, bool):
    print(str(v))
else:
    print(v)
" "$CONFIG" 2>/dev/null || echo ""
}

# ── Read config ───────────────────────────────────────────────
USE_EXISTING=$(cfg '["vpc"]["useExisting"]')

# Region priority: AWS_REGION (from environment) > config.json > us-east-1
ENV_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"

if [ "$USE_EXISTING" = "True" ]; then
  CFG_REGION=$(cfg '["vpc"]["existing"]["region"]')
  REGION="${ENV_REGION:-${CFG_REGION:-us-east-1}}"
  VPC_ID=$(cfg '["vpc"]["existing"]["vpcId"]')
  SUBNET_ID=$(cfg '["vpc"]["existing"]["subnetId"]')
  SG_IDS=$("$PYTHON_BIN" -c "
import json, sys
cfg=json.load(open(sys.argv[1]))
sgs=cfg['vpc']['existing'].get('securityGroupIds',[])
print(sgs[0] if sgs else '')
" "$CONFIG")
else
  CFG_REGION=$(cfg '["vpc"]["new"]["region"]')
  REGION="${ENV_REGION:-${CFG_REGION:-us-east-1}}"
  VPC_NAME=$(cfg '["vpc"]["new"]["name"]')
  CIDR=$(cfg '["vpc"]["new"]["cidr"]')
fi

echo "Using region: $REGION"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")

# Fleet config
FLEET_NAME=$(cfg '["fleet"].get("name","WorkspacesAgentDemo")')
FLEET_DISPLAY=$(cfg '["fleet"]["displayName"]')
FLEET_DESC=$(cfg '["fleet"]["description"]')
FLEET_INSTANCE=$(cfg '["fleet"]["instanceType"]')
FLEET_TYPE=$(cfg '["fleet"].get("fleetType","ALWAYS_ON")')
FLEET_IMAGE=$(cfg '["fleet"]["imageName"]')
FLEET_MAX_DURATION=$(cfg '["fleet"].get("maxUserDurationInSeconds",57600)')
FLEET_DISCONNECT=$(cfg '["fleet"].get("disconnectTimeoutInSeconds",900)')
FLEET_IDLE=$(cfg '["fleet"].get("idleDisconnectTimeoutInSeconds",900)')
FLEET_DESIRED=$(cfg '["fleet"]["computeCapacity"]["desiredInstances"]')
FLEET_INTERNET=$(cfg '["fleet"].get("enableDefaultInternetAccess",False)')
FLEET_IAM=$(cfg '["fleet"].get("iamRoleArn","")')

# Stack config
STACK_NAME=$(cfg '["stack"]["name"]')
STACK_DISPLAY=$(cfg '["stack"].get("displayName","")')
STACK_DESC=$(cfg '["stack"].get("description","")')

# Image builder config
IB_CREATE=$(cfg '["imageBuilder"].get("create",False)')

echo "Account : $ACCOUNT_ID"
echo "Region  : $REGION"
echo ""

# ── VPC ───────────────────────────────────────────────────────
if [ "$USE_EXISTING" != "True" ]; then
  echo "=== Creating VPC ==="

  # Check if VPC already exists by name
  EXISTING_VPC=$(aws ec2 describe-vpcs \
    --region "$REGION" \
    --filters "Name=tag:Name,Values=$VPC_NAME" \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo "None")

  if [ "$EXISTING_VPC" != "None" ] && [ -n "$EXISTING_VPC" ]; then
    echo "VPC '$VPC_NAME' already exists: $EXISTING_VPC"
    VPC_ID="$EXISTING_VPC"
  else
    VPC_ID=$(aws ec2 create-vpc \
      --region "$REGION" \
      --cidr-block "$CIDR" \
      --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=$VPC_NAME}]" \
      --query 'Vpc.VpcId' --output text)
    echo "Created VPC: $VPC_ID"

    aws ec2 modify-vpc-attribute --region "$REGION" --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}'
    aws ec2 modify-vpc-attribute --region "$REGION" --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'
  fi

  # Get AZs
  AZ1=$(aws ec2 describe-availability-zones --region "$REGION" --query 'AvailabilityZones[0].ZoneName' --output text)
  AZ2=$(aws ec2 describe-availability-zones --region "$REGION" --query 'AvailabilityZones[1].ZoneName' --output text)

  # Create subnets if they don't exist
  create_subnet() {
    local name=$1 cidr=$2 az=$3 public=$4
    local existing
    existing=$(aws ec2 describe-subnets --region "$REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=$name" \
      --query 'Subnets[0].SubnetId' --output text 2>/dev/null || echo "None")

    if [ "$existing" != "None" ] && [ -n "$existing" ]; then
      echo "  Subnet '$name' exists: $existing" >&2
      echo "$existing"
      return
    fi

    local sid
    sid=$(aws ec2 create-subnet --region "$REGION" \
      --vpc-id "$VPC_ID" --cidr-block "$cidr" --availability-zone "$az" \
      --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=$name}]" \
      --query 'Subnet.SubnetId' --output text)
    echo "  Created subnet '$name': $sid" >&2

    if [ "$public" = "true" ]; then
      aws ec2 modify-subnet-attribute --region "$REGION" --subnet-id "$sid" --map-public-ip-on-launch
    fi
    echo "$sid"
  }

  PUB1=$(create_subnet "${VPC_NAME}-Public-1" "10.0.0.0/24" "$AZ1" "true")
  PUB2=$(create_subnet "${VPC_NAME}-Public-2" "10.0.1.0/24" "$AZ2" "true")
  PRIV1=$(create_subnet "${VPC_NAME}-Private-1" "10.0.2.0/24" "$AZ1" "false")
  PRIV2=$(create_subnet "${VPC_NAME}-Private-2" "10.0.3.0/24" "$AZ2" "false")

  SUBNET_ID="$PRIV1"

  # Internet Gateway
  IGW=$(aws ec2 describe-internet-gateways --region "$REGION" \
    --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
    --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null || echo "None")

  if [ "$IGW" = "None" ] || [ -z "$IGW" ]; then
    IGW=$(aws ec2 create-internet-gateway --region "$REGION" \
      --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=${VPC_NAME}-igw}]" \
      --query 'InternetGateway.InternetGatewayId' --output text)
    aws ec2 attach-internet-gateway --region "$REGION" --internet-gateway-id "$IGW" --vpc-id "$VPC_ID"
    echo "Created IGW: $IGW"
  else
    echo "IGW exists: $IGW"
  fi

  # Public route table
  PUB_RT=$(aws ec2 describe-route-tables --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=${VPC_NAME}-public-rt" \
    --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null || echo "None")

  if [ "$PUB_RT" = "None" ] || [ -z "$PUB_RT" ]; then
    PUB_RT=$(aws ec2 create-route-table --region "$REGION" --vpc-id "$VPC_ID" \
      --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${VPC_NAME}-public-rt}]" \
      --query 'RouteTable.RouteTableId' --output text)
    aws ec2 create-route --region "$REGION" --route-table-id "$PUB_RT" \
      --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW" > /dev/null
    echo "Created public route table: $PUB_RT"
  fi

  aws ec2 associate-route-table --region "$REGION" --route-table-id "$PUB_RT" --subnet-id "$PUB1" > /dev/null 2>&1 || true
  aws ec2 associate-route-table --region "$REGION" --route-table-id "$PUB_RT" --subnet-id "$PUB2" > /dev/null 2>&1 || true

  # NAT Gateway (single)
  EIP_ALLOC=$(aws ec2 describe-nat-gateways --region "$REGION" \
    --filter "Name=vpc-id,Values=$VPC_ID" "Name=state,Values=available" \
    --query 'NatGateways[0].NatGatewayId' --output text 2>/dev/null || echo "None")

  if [ "$EIP_ALLOC" = "None" ] || [ -z "$EIP_ALLOC" ]; then
    EIP_ALLOC=$(aws ec2 allocate-address --region "$REGION" --domain vpc \
      --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=${VPC_NAME}-nat-eip}]" \
      --query 'AllocationId' --output text)
    NAT_GW=$(aws ec2 create-nat-gateway --region "$REGION" \
      --subnet-id "$PUB1" --allocation-id "$EIP_ALLOC" \
      --tag-specifications "ResourceType=natgateway,Tags=[{Key=Name,Value=${VPC_NAME}-nat}]" \
      --query 'NatGateway.NatGatewayId' --output text)
    echo "Created NAT Gateway: $NAT_GW (waiting for it to become available...)"
    aws ec2 wait nat-gateway-available --region "$REGION" --nat-gateway-ids "$NAT_GW"
  else
    NAT_GW="$EIP_ALLOC"
    echo "NAT Gateway exists: $NAT_GW"
  fi

  # Private route table
  PRIV_RT=$(aws ec2 describe-route-tables --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=${VPC_NAME}-private-rt" \
    --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null || echo "None")

  if [ "$PRIV_RT" = "None" ] || [ -z "$PRIV_RT" ]; then
    PRIV_RT=$(aws ec2 create-route-table --region "$REGION" --vpc-id "$VPC_ID" \
      --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${VPC_NAME}-private-rt}]" \
      --query 'RouteTable.RouteTableId' --output text)
    aws ec2 create-route --region "$REGION" --route-table-id "$PRIV_RT" \
      --destination-cidr-block 0.0.0.0/0 --nat-gateway-id "$NAT_GW" > /dev/null
    echo "Created private route table: $PRIV_RT"
  fi

  aws ec2 associate-route-table --region "$REGION" --route-table-id "$PRIV_RT" --subnet-id "$PRIV1" > /dev/null 2>&1 || true
  aws ec2 associate-route-table --region "$REGION" --route-table-id "$PRIV_RT" --subnet-id "$PRIV2" > /dev/null 2>&1 || true

  # Security Group
  SG_NAME="${VPC_NAME}-appstream-sg"
  SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

  if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    SG_ID=$(aws ec2 create-security-group --region "$REGION" \
      --group-name "$SG_NAME" --description "SG for WorkSpaces Applications Agent Demo" \
      --vpc-id "$VPC_ID" --query 'GroupId' --output text)
    echo "Created Security Group: $SG_ID"
  else
    echo "Security Group exists: $SG_ID"
  fi
  SG_IDS="$SG_ID"

  echo ""
  echo "VPC setup complete: VPC=$VPC_ID  Subnet=$SUBNET_ID  SG=$SG_IDS"
  echo ""
fi

# ── Image Builder (optional) ──────────────────────────────────
if [ "$IB_CREATE" = "True" ]; then
  echo "=== Creating Image Builder ==="
  IB_NAME=$(cfg '["imageBuilder"]["name"]')
  IB_INSTANCE=$(cfg '["imageBuilder"]["instanceType"]')
  IB_IMAGE=$(cfg '["imageBuilder"]["imageName"]')
  IB_INTERNET=$(cfg '["imageBuilder"].get("enableDefaultInternetAccess",False)')
  IB_IAM=$(cfg '["imageBuilder"].get("iamRoleArn","")')

  # Check if it already exists
  IB_EXISTS=$(aws appstream describe-image-builders --region "$REGION" \
    --names "$IB_NAME" --query 'ImageBuilders[0].Name' --output text 2>/dev/null || echo "None")

  if [ "$IB_EXISTS" != "None" ] && [ -n "$IB_EXISTS" ]; then
    echo "Image Builder '$IB_NAME' already exists."
  else
    IB_CMD=(aws appstream create-image-builder --region "$REGION"
      --name "$IB_NAME"
      --instance-type "$IB_INSTANCE"
      --image-name "$IB_IMAGE"
      --vpc-config "{\"SubnetIds\":[\"$SUBNET_ID\"],\"SecurityGroupIds\":[\"$SG_IDS\"]}")

    if [ "$IB_INTERNET" = "True" ]; then
      IB_CMD+=(--enable-default-internet-access)
    else
      IB_CMD+=(--no-enable-default-internet-access)
    fi

    if [ -n "$IB_IAM" ]; then
      IB_CMD+=(--iam-role-arn "$IB_IAM")
    fi

    "${IB_CMD[@]}" > /dev/null
    echo "Created Image Builder: $IB_NAME"
  fi
  echo ""
fi

# ── Fleet ─────────────────────────────────────────────────────
echo "=== Creating Fleet ==="

FLEET_EXISTS=$(aws appstream describe-fleets --region "$REGION" \
  --names "$FLEET_NAME" --query 'Fleets[0].Name' --output text 2>/dev/null || echo "None")

if [ "$FLEET_EXISTS" != "None" ] && [ -n "$FLEET_EXISTS" ]; then
  echo "Fleet '$FLEET_NAME' already exists."
else
  FLEET_INTERNET_FLAG="--no-enable-default-internet-access"
  if [ "$FLEET_INTERNET" = "True" ]; then
    FLEET_INTERNET_FLAG="--enable-default-internet-access"
  fi

  FLEET_CMD=(aws appstream create-fleet --region "$REGION"
    --name "$FLEET_NAME"
    --instance-type "$FLEET_INSTANCE"
    --fleet-type "$FLEET_TYPE"
    --compute-capacity "DesiredInstances=$FLEET_DESIRED"
    --vpc-config "{\"SubnetIds\":[\"$SUBNET_ID\"],\"SecurityGroupIds\":[\"$SG_IDS\"]}"
    --max-user-duration-in-seconds "$FLEET_MAX_DURATION"
    --disconnect-timeout-in-seconds "$FLEET_DISCONNECT"
    --idle-disconnect-timeout-in-seconds "$FLEET_IDLE"
    --stream-view DESKTOP
    "$FLEET_INTERNET_FLAG")

  if [ -n "$FLEET_IMAGE" ]; then
    FLEET_CMD+=(--image-name "$FLEET_IMAGE")
  fi

  if [ -n "$FLEET_IAM" ]; then
    FLEET_CMD+=(--iam-role-arn "$FLEET_IAM")
  fi

  "${FLEET_CMD[@]}" > /dev/null
  echo "Created Fleet: $FLEET_NAME"

  # Start the fleet
  aws appstream start-fleet --region "$REGION" --name "$FLEET_NAME" > /dev/null 2>&1 || true
  echo "Fleet start requested."
fi
echo ""

# ── Stack ─────────────────────────────────────────────────────
echo "=== Creating Stack ==="

STACK_EXISTS=$(aws appstream describe-stacks --region "$REGION" \
  --names "$STACK_NAME" --query 'Stacks[0].Name' --output text 2>/dev/null || echo "None")

if [ "$STACK_EXISTS" != "None" ] && [ -n "$STACK_EXISTS" ]; then
  echo "Stack '$STACK_NAME' already exists."
else
  # AgentAccessConfig is not yet in the public AWS CLI, so we send a
  # SigV4-signed PhotonAdminProxyService.CreateStack request directly.
  # This requires botocore. Prefer a project venv Python if available
  # (install.sh/.ps1 put it there); otherwise fall back to $PYTHON_BIN.
  SIGV4_PYTHON="$PYTHON_BIN"
  _venv_python_unix="$SCRIPT_DIR/../venv/bin/python"
  _venv_python_win="$SCRIPT_DIR/../venv/Scripts/python.exe"
  if [ -x "$_venv_python_unix" ]; then
    SIGV4_PYTHON="$_venv_python_unix"
  elif [ -x "$_venv_python_win" ]; then
    SIGV4_PYTHON="$_venv_python_win"
  fi
  if ! "$SIGV4_PYTHON" -c 'import botocore' &>/dev/null; then
    echo "ERROR: botocore is not importable by $SIGV4_PYTHON." >&2
    echo "  Run scripts/install.sh (or install.ps1) first to create the project venv." >&2
    exit 1
  fi

  # We fail hard on any error: creating a stack without AgentAccessConfig
  # would silently break the Agent Access MCP flow at runtime.
  echo "Creating agent stack with AgentAccessConfig..."
  "$SIGV4_PYTHON" -c "
import json, sys, urllib.request, urllib.error
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

endpoint = 'https://appstream2.$REGION.amazonaws.com/'
body = json.dumps({
    'Name': '$STACK_NAME',
    'DisplayName': '$STACK_DISPLAY',
    'Description': '$STACK_DESC',
    'AgentAccessConfig': {
        'Settings': [
            {'AgentAction': 'COMPUTER_VISION', 'Permission': 'ENABLED'},
            {'AgentAction': 'COMPUTER_INPUT', 'Permission': 'ENABLED'}
        ],
        'ScreenResolution': 'W_1280xH_720',
        'ScreenImageFormat': 'PNG'
    }
})
headers = {
    'Content-Type': 'application/x-amz-json-1.1',
    'X-Amz-Target': 'PhotonAdminProxyService.CreateStack',
}
creds = Session().get_credentials().get_frozen_credentials()
req = AWSRequest(method='POST', url=endpoint, data=body, headers=headers)
SigV4Auth(creds, 'appstream', '$REGION').add_auth(req)

http_req = urllib.request.Request(endpoint, data=body.encode('utf-8'),
                                  headers=dict(req.headers), method='POST')
try:
    with urllib.request.urlopen(http_req, timeout=30) as r:
        status = r.status
        text = r.read().decode('utf-8', errors='replace')
except urllib.error.HTTPError as e:
    status = e.code
    text = e.read().decode('utf-8', errors='replace')
except urllib.error.URLError as e:
    sys.stderr.write(f'CreateStack network error: {e}\n')
    sys.exit(1)

if status >= 400:
    sys.stderr.write(f'CreateStack failed with {status}: {text[:500]}\n')
    sys.exit(1)
print('Created Stack: $STACK_NAME (with AgentAccessConfig)')
" || {
    echo "ERROR: Could not create stack with AgentAccessConfig." >&2
    echo "  The Agent Access MCP Server requires this config on the stack." >&2
    exit 1
  }
fi
echo ""

# ── Fleet ↔ Stack Association ─────────────────────────────────
echo "=== Associating Fleet with Stack ==="
aws appstream associate-fleet --region "$REGION" \
  --fleet-name "$FLEET_NAME" \
  --stack-name "$STACK_NAME" > /dev/null 2>&1 || true
echo "Fleet '$FLEET_NAME' associated with Stack '$STACK_NAME'."

echo ""
echo "=== Deploy complete ==="
echo "  VPC:    $VPC_ID"
echo "  Subnet: $SUBNET_ID"
echo "  SG:     $SG_IDS"
echo "  Fleet:  $FLEET_NAME"
echo "  Stack:  $STACK_NAME"
echo ""

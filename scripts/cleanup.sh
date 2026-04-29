#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# WorkSpaces Applications — CLI Cleanup
#
# Tears down resources created by deploy.sh in reverse order.
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: config.json not found at $CONFIG" >&2
  exit 1
fi

# ── Helper: read config values ────────────────────────────────
cfg() {
  python3 -c "
import json,re,sys
raw=open('$CONFIG').read()
raw=re.sub(r'(?m)^\s*//.*','',raw)
d=json.loads(raw)
v=eval('d$1')
if v is None:
    print('')
elif isinstance(v, bool):
    print(str(v))
else:
    print(v)
" 2>/dev/null || echo ""
}

# ── Read config ───────────────────────────────────────────────
USE_EXISTING=$(cfg '["vpc"]["useExisting"]')
ENV_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"

if [ "$USE_EXISTING" = "True" ]; then
  CFG_REGION=$(cfg '["vpc"]["existing"]["region"]')
else
  CFG_REGION=$(cfg '["vpc"]["new"]["region"]')
fi
REGION="${ENV_REGION:-${CFG_REGION:-us-east-1}}"

FLEET_NAME=$(cfg '["fleet"].get("name","WorkspacesAgentDemo")')
STACK_NAME=$(cfg '["stack"]["name"]')
IB_NAME=$(cfg '["imageBuilder"]["name"]')
VPC_NAME=$(cfg '["vpc"]["new"]["name"]')

echo "=== WorkSpaces Agent Framework — Cleanup ==="
echo "Region: $REGION"
echo ""

# ── Disassociate Fleet from Stack ─────────────────────────────
echo "--- Disassociating fleet from stack..."
aws appstream disassociate-fleet --region "$REGION" \
  --fleet-name "$FLEET_NAME" \
  --stack-name "$STACK_NAME" 2>/dev/null && echo "  ✓ Disassociated" || echo "  (skipped — not associated)"

# ── Delete Stack ──────────────────────────────────────────────
echo "--- Deleting stack: $STACK_NAME..."
aws appstream delete-stack --region "$REGION" \
  --name "$STACK_NAME" 2>/dev/null && echo "  ✓ Deleted stack" || echo "  (skipped — not found)"

# ── Stop and Delete Fleet ─────────────────────────────────────
echo "--- Stopping fleet: $FLEET_NAME..."
aws appstream stop-fleet --region "$REGION" \
  --name "$FLEET_NAME" 2>/dev/null && echo "  ✓ Stop requested" || echo "  (skipped — not running)"

# Wait for fleet to stop
echo "  Waiting for fleet to stop..."
for i in $(seq 1 30); do
  STATE=$(aws appstream describe-fleets --region "$REGION" \
    --names "$FLEET_NAME" --query 'Fleets[0].State' --output text 2>/dev/null || echo "GONE")
  if [ "$STATE" = "STOPPED" ] || [ "$STATE" = "GONE" ]; then
    break
  fi
  sleep 10
done

echo "--- Deleting fleet: $FLEET_NAME..."
aws appstream delete-fleet --region "$REGION" \
  --name "$FLEET_NAME" 2>/dev/null && echo "  ✓ Deleted fleet" || echo "  (skipped — not found)"

# ── Delete Image Builder (if it exists) ───────────────────────
if [ -n "$IB_NAME" ]; then
  echo "--- Deleting image builder: $IB_NAME..."
  aws appstream stop-image-builder --region "$REGION" \
    --name "$IB_NAME" 2>/dev/null || true
  sleep 5
  aws appstream delete-image-builder --region "$REGION" \
    --name "$IB_NAME" 2>/dev/null && echo "  ✓ Deleted image builder" || echo "  (skipped — not found)"
fi

# ── Delete VPC resources (only if we created them) ────────────
if [ "$USE_EXISTING" != "True" ] && [ -n "$VPC_NAME" ]; then
  echo ""
  echo "--- Cleaning up VPC: $VPC_NAME..."

  VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" \
    --filters "Name=tag:Name,Values=$VPC_NAME" \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo "None")

  if [ "$VPC_ID" = "None" ] || [ -z "$VPC_ID" ]; then
    echo "  (skipped — VPC not found)"
  else
    # Delete NAT Gateways
    NAT_GWS=$(aws ec2 describe-nat-gateways --region "$REGION" \
      --filter "Name=vpc-id,Values=$VPC_ID" "Name=state,Values=available" \
      --query 'NatGateways[].NatGatewayId' --output text 2>/dev/null || echo "")
    for ngw in $NAT_GWS; do
      echo "  Deleting NAT Gateway: $ngw..."
      aws ec2 delete-nat-gateway --region "$REGION" --nat-gateway-id "$ngw" > /dev/null
    done

    # Wait for NAT Gateways to delete
    if [ -n "$NAT_GWS" ]; then
      echo "  Waiting for NAT Gateways to delete..."
      for i in $(seq 1 30); do
        REMAINING=$(aws ec2 describe-nat-gateways --region "$REGION" \
          --filter "Name=vpc-id,Values=$VPC_ID" "Name=state,Values=available,deleting" \
          --query 'NatGateways | length(@)' --output text 2>/dev/null || echo "0")
        if [ "$REMAINING" = "0" ]; then break; fi
        sleep 10
      done
    fi

    # Release Elastic IPs tagged with our VPC name
    EIP_ALLOCS=$(aws ec2 describe-addresses --region "$REGION" \
      --filters "Name=tag:Name,Values=${VPC_NAME}-nat-eip" \
      --query 'Addresses[].AllocationId' --output text 2>/dev/null || echo "")
    for eip in $EIP_ALLOCS; do
      echo "  Releasing EIP: $eip"
      aws ec2 release-address --region "$REGION" --allocation-id "$eip" 2>/dev/null || true
    done

    # Detach and delete Internet Gateway
    IGW=$(aws ec2 describe-internet-gateways --region "$REGION" \
      --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
      --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null || echo "None")
    if [ "$IGW" != "None" ] && [ -n "$IGW" ]; then
      echo "  Detaching IGW: $IGW"
      aws ec2 detach-internet-gateway --region "$REGION" --internet-gateway-id "$IGW" --vpc-id "$VPC_ID" 2>/dev/null || true
      aws ec2 delete-internet-gateway --region "$REGION" --internet-gateway-id "$IGW" 2>/dev/null || true
    fi

    # Delete subnets
    SUBNETS=$(aws ec2 describe-subnets --region "$REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'Subnets[].SubnetId' --output text 2>/dev/null || echo "")
    for sub in $SUBNETS; do
      echo "  Deleting subnet: $sub"
      aws ec2 delete-subnet --region "$REGION" --subnet-id "$sub" 2>/dev/null || true
    done

    # Delete custom route tables (skip the main one)
    RTS=$(aws ec2 describe-route-tables --region "$REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'RouteTables[?Associations[0].Main != `true`].RouteTableId' --output text 2>/dev/null || echo "")
    for rt in $RTS; do
      # Disassociate first
      ASSOCS=$(aws ec2 describe-route-tables --region "$REGION" \
        --route-table-ids "$rt" \
        --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text 2>/dev/null || echo "")
      for assoc in $ASSOCS; do
        aws ec2 disassociate-route-table --region "$REGION" --association-id "$assoc" 2>/dev/null || true
      done
      echo "  Deleting route table: $rt"
      aws ec2 delete-route-table --region "$REGION" --route-table-id "$rt" 2>/dev/null || true
    done

    # Delete security groups (skip default)
    SGS=$(aws ec2 describe-security-groups --region "$REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'SecurityGroups[?GroupName != `default`].GroupId' --output text 2>/dev/null || echo "")
    for sg in $SGS; do
      echo "  Deleting security group: $sg"
      aws ec2 delete-security-group --region "$REGION" --group-id "$sg" 2>/dev/null || true
    done

    # Delete VPC
    echo "  Deleting VPC: $VPC_ID"
    aws ec2 delete-vpc --region "$REGION" --vpc-id "$VPC_ID" 2>/dev/null && echo "  ✓ VPC deleted" || echo "  ✗ Could not delete VPC (may have remaining dependencies)"
  fi
fi

echo ""
echo "=== Cleanup complete ==="

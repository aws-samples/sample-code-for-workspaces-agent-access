#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Replay Session Setup Script
# Turns on Replay Session for an existing agent access stack: creates a private
# S3 bucket for the recordings (unless you pass one), optionally creates a
# customer managed KMS key for the captured frames, and updates the stack's
# AgentAccessConfig with ReplaySessionEnabled, S3BucketArn and KmsKeyArn.
#
# Prerequisites:
#   - AWS credentials for the account that owns the stack
#   - A stack with agent access and COMPUTER_VISION enabled (scripts/setup.sh creates one)
#   - The account enabled for Replay Session (and for customer managed keys, with --kms)
#   - Python with boto3 (the venv that scripts/setup.sh creates)
#
# Usage:
#   ./scripts/setup_replay_session.sh [OPTIONS]
#
# Options:
#   --region REGION                AWS region (default: $AWS_REGION or us-east-1)
#   --stack-name NAME              Stack to update (default: stack.name in scripts/config.json)
#   --bucket NAME                  Use this existing bucket instead of creating one. It is not modified.
#   --retention-days N             Expire recordings after N days in a bucket this script creates (default: 30)
#   --kms                          Also create a customer managed KMS key and set it as KmsKeyArn
#   --kms-mcp-via-service NAME     kms:ViaService for the frame encryption calls
#                                  (default: agentaccess-mcp.REGION.amazonaws.com)
#   --kms-appstream-via-service NAME
#                                  kms:ViaService for the stack's key validation
#                                  (default: appstream.REGION.amazonaws.com)
#   --appstream-endpoint URL       WorkSpaces Applications API endpoint
#                                  (default: https://appstream2.REGION.amazonaws.com/)
#   --disable                      Turn Replay Session off on the stack (also clears its KmsKeyArn)
#   --dry-run                      Print what would be sent; change nothing

set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
STACK_NAME=""
BUCKET=""
RETENTION_DAYS=30
USE_KMS=false
MCP_VIA_SERVICE=""
APPSTREAM_VIA_SERVICE=""
APPSTREAM_ENDPOINT=""
DISABLE=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --region) REGION="$2"; shift 2;;
    --stack-name) STACK_NAME="$2"; shift 2;;
    --bucket) BUCKET="$2"; shift 2;;
    --retention-days) RETENTION_DAYS="$2"; shift 2;;
    --kms) USE_KMS=true; shift;;
    --kms-mcp-via-service) MCP_VIA_SERVICE="$2"; shift 2;;
    --kms-appstream-via-service) APPSTREAM_VIA_SERVICE="$2"; shift 2;;
    --appstream-endpoint) APPSTREAM_ENDPOINT="$2"; shift 2;;
    --disable) DISABLE=true; shift;;
    --dry-run) DRY_RUN=true; shift;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-$PROJECT_ROOT/venv/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="python3"
if ! "$PYTHON" -c "import boto3" 2>/dev/null; then
  echo "ERROR: $PYTHON cannot import boto3. Run scripts/setup.sh first, or set PYTHON." >&2
  exit 1
fi

if [ -z "$STACK_NAME" ]; then
  STACK_NAME=$("$PYTHON" -c "
import json, re
raw = re.sub(r'(?m)^\s*//.*$', '', open('$SCRIPT_DIR/config.json').read())
print(json.loads(raw)['stack']['name'])
")
fi

MCP_VIA_SERVICE="${MCP_VIA_SERVICE:-agentaccess-mcp.${REGION}.amazonaws.com}"
APPSTREAM_VIA_SERVICE="${APPSTREAM_VIA_SERVICE:-appstream.${REGION}.amazonaws.com}"
APPSTREAM_ENDPOINT="${APPSTREAM_ENDPOINT:-https://appstream2.${REGION}.amazonaws.com/}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# ─── AppStream API helper ───
# The bundled AWS CLI/SDK model may not include ReplaySessionEnabled and KmsKeyArn
# yet: it strips them from requests and responses. This helper signs the API call
# with SigV4 directly so the fields reach the service and are read back intact.
# Once your CLI/SDK includes them, `aws appstream update-stack` works the same way.
appstream_call() {
  "$PYTHON" - "$REGION" "$APPSTREAM_ENDPOINT" "$1" "$2" <<'PY'
import json, sys
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

region, endpoint, action, body = sys.argv[1:5]
request = AWSRequest(method="POST", url=endpoint, data=body, headers={
    "Content-Type": "application/x-amz-json-1.1",
    "X-Amz-Target": f"PhotonAdminProxyService.{action}",
})
SigV4Auth(boto3.Session().get_credentials().get_frozen_credentials(), "appstream", region).add_auth(request)
response = URLLib3Session().send(request.prepare())
payload = json.loads(response.content or b"{}")
if response.status_code != 200:
    print(f"ERROR: {action} failed (HTTP {response.status_code}): "
          f"{payload.get('__type', '')} {payload.get('Message') or payload.get('message', '')}", file=sys.stderr)
    sys.exit(1)
print(json.dumps(payload))
PY
}

echo "=== Replay Session Setup ==="
echo "Account: $ACCOUNT_ID"
echo "Region:  $REGION"
echo "Stack:   $STACK_NAME"
$DRY_RUN && echo "Mode:    dry run (nothing is changed)"
echo ""

# ─── Disable ───
if $DISABLE; then
  BODY="{\"Name\": \"$STACK_NAME\", \"AgentAccessConfig\": {\"ReplaySessionEnabled\": false}}"
  if $DRY_RUN; then echo "Would send UpdateStack: $BODY"; exit 0; fi
  appstream_call UpdateStack "$BODY" >/dev/null
  echo "✓ Replay Session turned off for $STACK_NAME (its KmsKeyArn is cleared too)."
  echo "  The recordings bucket and any KMS key are left in place."
  exit 0
fi

# ─── Step 1: Check the stack ───
echo "Step 1: Checking the stack..."
STACK_JSON=$(appstream_call DescribeStacks "{\"Names\": [\"$STACK_NAME\"]}")
"$PYTHON" - "$STACK_JSON" <<'PY'
import json, sys
stacks = json.loads(sys.argv[1]).get("Stacks", [])
if not stacks:
    sys.exit("ERROR: stack not found")
config = stacks[0].get("AgentAccessConfig")
if not config:
    sys.exit("ERROR: the stack has no AgentAccessConfig; enable agent access first (scripts/setup.sh)")
vision = [s for s in config.get("Settings", []) if s.get("AgentAction") == "COMPUTER_VISION"]
if not vision or vision[0].get("Permission") != "ENABLED":
    sys.exit("ERROR: Replay Session requires the COMPUTER_VISION agent action to be ENABLED on the stack")
print("  Agent access with COMPUTER_VISION is enabled.")
PY

# ─── Step 2: Recordings bucket ───
echo "Step 2: Preparing the recordings bucket..."
if [ -n "$BUCKET" ]; then
  # An existing bucket is used as-is: it must be in this account, and nothing about it is changed.
  aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --region "$REGION" >/dev/null
  echo "  Using existing bucket $BUCKET (not modified)."
else
  BUCKET="replay-session-${ACCOUNT_ID}-${REGION}"
  if aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --region "$REGION" >/dev/null 2>&1; then
    echo "  Reusing $BUCKET."
  elif $DRY_RUN; then
    echo "  Would create $BUCKET (private, default encryption, TLS-only, ${RETENTION_DAYS}-day expiry)."
  else
    if [ "$REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
    else
      aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
    aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
      --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
    # Recordings are delivered with no encryption header, so this default is what encrypts them.
    aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" \
      --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
    aws s3api put-bucket-policy --bucket "$BUCKET" --region "$REGION" --policy "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"DenyInsecureTransport\", \"Effect\": \"Deny\", \"Principal\": \"*\",
        \"Action\": \"s3:*\",
        \"Resource\": [\"arn:aws:s3:::$BUCKET\", \"arn:aws:s3:::$BUCKET/*\"],
        \"Condition\": {\"Bool\": {\"aws:SecureTransport\": \"false\"}}
      }]
    }"
    aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
      --lifecycle-configuration "{\"Rules\":[{\"ID\":\"ExpireReplaySessionRecordings\",\"Status\":\"Enabled\",
        \"Filter\":{\"Prefix\":\"agentaccess/replay-session/\"},\"Expiration\":{\"Days\":$RETENTION_DAYS}}]}"
    echo "  Created $BUCKET (private, default encryption, TLS-only, ${RETENTION_DAYS}-day expiry)."
  fi
fi

# ─── Step 3: Optional customer managed key ───
KMS_KEY_ARN=""
if $USE_KMS; then
  echo "Step 3: Preparing the customer managed KMS key..."
  ALIAS="alias/replay-session-${STACK_NAME}"
  KMS_KEY_ARN=$(aws kms describe-key --key-id "$ALIAS" --region "$REGION" \
    --query KeyMetadata.Arn --output text 2>/dev/null || true)
  if [ -n "$KMS_KEY_ARN" ]; then
    echo "  Reusing $ALIAS."
  elif $DRY_RUN; then
    echo "  Would create a KMS key with alias $ALIAS."
    KMS_KEY_ARN="arn:aws:kms:${REGION}:${ACCOUNT_ID}:key/<new-key-id>"
  else
    # The key is used with your own forwarded credentials, so the policy admits callers
    # in this account when the request arrives through the named service. No service
    # principal is added.
    POLICY="{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {\"Sid\": \"EnableAccountAdmin\", \"Effect\": \"Allow\",
         \"Principal\": {\"AWS\": \"arn:aws:iam::${ACCOUNT_ID}:root\"},
         \"Action\": \"kms:*\", \"Resource\": \"*\"},
        {\"Sid\": \"AllowFrameEncryptionViaAgentAccess\", \"Effect\": \"Allow\",
         \"Principal\": {\"AWS\": \"*\"},
         \"Action\": [\"kms:GenerateDataKey\", \"kms:Decrypt\", \"kms:DescribeKey\"],
         \"Resource\": \"*\",
         \"Condition\": {\"StringEquals\": {\"kms:ViaService\": \"${MCP_VIA_SERVICE}\",
                                           \"kms:CallerAccount\": \"${ACCOUNT_ID}\"}}},
        {\"Sid\": \"AllowKeyValidationViaWorkSpacesApplications\", \"Effect\": \"Allow\",
         \"Principal\": {\"AWS\": \"*\"},
         \"Action\": \"kms:DescribeKey\", \"Resource\": \"*\",
         \"Condition\": {\"StringEquals\": {\"kms:ViaService\": \"${APPSTREAM_VIA_SERVICE}\",
                                           \"kms:CallerAccount\": \"${ACCOUNT_ID}\"}}}
      ]
    }"
    KMS_KEY_ARN=$(aws kms create-key --region "$REGION" \
      --description "Replay Session frames for stack ${STACK_NAME}" \
      --policy "$POLICY" --query KeyMetadata.Arn --output text)
    aws kms create-alias --alias-name "$ALIAS" --target-key-id "$KMS_KEY_ARN" --region "$REGION"
    aws kms enable-key-rotation --key-id "$KMS_KEY_ARN" --region "$REGION"
    echo "  Created $ALIAS -> $KMS_KEY_ARN"
  fi
else
  echo "Step 3: No customer managed key (pass --kms to add one)."
fi

# ─── Step 4: Update the stack ───
echo "Step 4: Turning on Replay Session..."
CONFIG="{\"ReplaySessionEnabled\": true, \"S3BucketArn\": \"arn:aws:s3:::${BUCKET}\""
[ -n "$KMS_KEY_ARN" ] && CONFIG="${CONFIG}, \"KmsKeyArn\": \"${KMS_KEY_ARN}\""
BODY="{\"Name\": \"${STACK_NAME}\", \"AgentAccessConfig\": ${CONFIG}}}"
if $DRY_RUN; then
  echo "  Would send UpdateStack: $BODY"
  exit 0
fi
# UpdateStack merges AgentAccessConfig: fields not sent here keep their current values.
appstream_call UpdateStack "$BODY" >/dev/null

# ─── Step 5: Verify ───
echo "Step 5: Verifying..."
STACK_JSON=$(appstream_call DescribeStacks "{\"Names\": [\"$STACK_NAME\"]}")
"$PYTHON" - "$STACK_JSON" "$KMS_KEY_ARN" <<'PY'
import json, sys
config = json.loads(sys.argv[1])["Stacks"][0].get("AgentAccessConfig", {})
if config.get("ReplaySessionEnabled") is not True:
    sys.exit("ERROR: the stack accepted the update but ReplaySessionEnabled is not true. "
             "Check that your account is enabled for Replay Session.")
if sys.argv[2] and config.get("KmsKeyArn") != sys.argv[2]:
    sys.exit("ERROR: KmsKeyArn was not applied. Check that your account is enabled for customer managed keys.")
print(f"  ReplaySessionEnabled: true, S3BucketArn: {config.get('S3BucketArn')}"
      + (f", KmsKeyArn: {config['KmsKeyArn']}" if config.get("KmsKeyArn") else ""))
PY

echo ""
echo "✓ Replay Session is on for $STACK_NAME. It applies to sessions started from now on."
echo ""
echo "Recordings are delivered to:"
echo "  s3://${BUCKET}/agentaccess/replay-session/year=YYYY/month=MM/day=DD/<mcpSessionId>/"
echo ""
echo "Run the demo:"
echo "  source venv/bin/activate"
echo "  STREAMING_URL=\$(scripts/streaming_url.sh)"
echo "  python3 agents/replay_session_demo/agent.py --streaming-url \"\$STREAMING_URL\" \\"
echo "      --stack-name ${STACK_NAME} --expire-session-on-exit"

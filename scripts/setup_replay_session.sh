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
#   - Python with boto3 (the venv that scripts/setup.sh creates)
#
# Usage:
#   ./scripts/setup_replay_session.sh [OPTIONS]
#
# Options:
#   --region REGION                AWS region (default: $AWS_REGION or us-east-1)
#   --stack-name NAME              Stack to update (default: stack.name in scripts/config.json)
#   --bucket NAME                  Use this existing bucket instead of creating one. It is not modified, so its
#                                  policy must allow s3:ListBucket for appstream.amazonaws.com (UpdateStack checks it).
#                                  Without it, a bucket the stack already has (S3BucketArn, which
#                                  screenshot uploads also use) is reused.
#   --retention-days N             Expire recordings after N days in a bucket this script creates (default: 30).
#                                  On a re-run, that bucket's public-access block, encryption, policy and
#                                  lifecycle rule are set again, replacing any changes made to them.
#   --kms                          Also create a customer managed KMS key and set it as KmsKeyArn.
#                                  On a re-run the key is reused and its policy is replaced with this one.
#   --kms-mcp-via-service NAME     kms:ViaService for the frame encryption calls
#                                  (default: agentaccess-mcp.REGION.amazonaws.com)
#   --kms-appstream-via-service NAME
#                                  kms:ViaService for the stack's key validation
#                                  (default: appstream.REGION.amazonaws.com)
#   --disable                      Turn Replay Session off on the stack (also clears its KmsKeyArn,
#                                  and its S3BucketArn when screenshot uploads are off)
#   --dry-run                      Print what would be sent; change nothing

set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
STACK_NAME=""
BUCKET=""
RETENTION_DAYS=30
USE_KMS=false
MCP_VIA_SERVICE=""
APPSTREAM_VIA_SERVICE=""
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
    --disable) DISABLE=true; shift;;
    --dry-run) DRY_RUN=true; shift;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

if [[ ! "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: --retention-days must be a positive integer, got '$RETENTION_DAYS'" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-$PROJECT_ROOT/venv/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="python3"
if ! "$PYTHON" -c "import boto3" 2>/dev/null; then
  echo "ERROR: $PYTHON cannot import boto3. Run scripts/setup.sh first, or set PYTHON." >&2
  exit 1
fi

# stack.name in scripts/config.json: the default stack, and the one scripts/streaming_url.sh uses.
CONFIG_STACK_NAME=""
if [ -f "$SCRIPT_DIR/config.json" ]; then
  CONFIG_STACK_NAME=$("$PYTHON" -c "
import json, re
raw = re.sub(r'(?m)^\s*//.*$', '', open('$SCRIPT_DIR/config.json').read())
print((json.loads(raw).get('stack') or {}).get('name') or '')
")
fi
STACK_NAME="${STACK_NAME:-$CONFIG_STACK_NAME}"
if [ -z "$STACK_NAME" ]; then
  echo "ERROR: no stack: pass --stack-name, or set stack.name in scripts/config.json" >&2
  exit 1
fi

# kms:ViaService values that the key policy (--kms) admits. Agent Access encrypts and decrypts
# each frame (GenerateDataKey, Decrypt) with your forwarded credentials through
# agentaccess-mcp.REGION.amazonaws.com.
# WorkSpaces Applications checks the key (DescribeKey) at UpdateStack through
# appstream.REGION.amazonaws.com
# (expected value for the public endpoints). If UpdateStack rejects the key, override it
# with --kms-appstream-via-service.
MCP_VIA_SERVICE="${MCP_VIA_SERVICE:-agentaccess-mcp.${REGION}.amazonaws.com}"
APPSTREAM_VIA_SERVICE="${APPSTREAM_VIA_SERVICE:-appstream.${REGION}.amazonaws.com}"
# The regional endpoint, unless AWS_ENDPOINT_URL_APPSTREAM points the AWS CLI/SDK elsewhere.
APPSTREAM_ENDPOINT="${AWS_ENDPOINT_URL_APPSTREAM:-https://appstream2.${REGION}.amazonaws.com/}"
CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"
ACCOUNT_ID="$(echo "$CALLER_ARN" | cut -d: -f5)"
# The partition (aws, aws-cn, aws-us-gov) of every ARN this script builds, from the caller's own ARN.
PARTITION="$(echo "$CALLER_ARN" | cut -d: -f2)"

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
try:
    payload = json.loads(response.content or b"{}")
except ValueError:
    payload = {}
if response.status_code != 200:
    detail = (f"{payload.get('__type', '')} "
              f"{payload.get('Message') or payload.get('message', '')}").strip()
    if not detail:
        detail = (response.content or b"")[:500].decode("utf-8", "replace")
    print(f"ERROR: {action} failed (HTTP {response.status_code}): {detail}", file=sys.stderr)
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

# ─── Recordings bucket settings ───
# The settings of the bucket this script creates. Each call replaces the whole value (public-access
# block, encryption, bucket policy, lifecycle configuration), so a re-run repairs a run that stopped
# part way. The expiry also applies to recordings already in the bucket.
OWN_BUCKET_SETTINGS="private, default encryption, TLS-only, WorkSpaces Applications bucket check, ${RETENTION_DAYS}-day expiry"
apply_own_bucket_settings() {
  local bucket="$1"
  aws s3api put-public-access-block --bucket "$bucket" --region "$REGION" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
  # Recordings are delivered with no encryption header, so this default is what encrypts them.
  aws s3api put-bucket-encryption --bucket "$bucket" --region "$REGION" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
  # UpdateStack checks the bucket with S3 HeadBucket as the WorkSpaces Applications service, which needs
  # s3:ListBucket on the bucket. Recordings are written with your own credentials, so nothing else is granted.
  aws s3api put-bucket-policy --bucket "$bucket" --region "$REGION" --policy "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Sid\": \"DenyInsecureTransport\", \"Effect\": \"Deny\", \"Principal\": \"*\",
      \"Action\": \"s3:*\",
      \"Resource\": [\"arn:${PARTITION}:s3:::$bucket\", \"arn:${PARTITION}:s3:::$bucket/*\"],
      \"Condition\": {\"Bool\": {\"aws:SecureTransport\": \"false\"}}
    }, {
      \"Sid\": \"AllowWorkSpacesApplicationsBucketCheck\", \"Effect\": \"Allow\",
      \"Principal\": {\"Service\": \"appstream.amazonaws.com\"},
      \"Action\": \"s3:ListBucket\",
      \"Resource\": \"arn:${PARTITION}:s3:::$bucket\",
      \"Condition\": {\"StringEquals\": {\"aws:SourceAccount\": \"${ACCOUNT_ID}\"}}
    }]
  }"
  aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --region "$REGION" \
    --lifecycle-configuration "{\"Rules\":[{\"ID\":\"ExpireReplaySessionRecordings\",\"Status\":\"Enabled\",
      \"Filter\":{\"Prefix\":\"agentaccess/replay-session/\"},\"Expiration\":{\"Days\":$RETENTION_DAYS}}]}" >/dev/null
}

# ─── Disable ───
if $DISABLE; then
  BODY="{\"Name\": \"$STACK_NAME\", \"AgentAccessConfig\": {\"ReplaySessionEnabled\": false}}"
  if $DRY_RUN; then echo "Would send UpdateStack: $BODY"; exit 0; fi
  appstream_call UpdateStack "$BODY" >/dev/null
  echo "✓ Replay Session turned off for $STACK_NAME (its KmsKeyArn is cleared too, and so is its"
  echo "  S3BucketArn when screenshot uploads are off)."
  echo "  The recordings bucket and any KMS key are left in place."
  exit 0
fi

# ─── Step 1: Check the stack ───
echo "Step 1: Checking the stack..."
STACK_JSON=$(appstream_call DescribeStacks "{\"Names\": [\"$STACK_NAME\"]}")
# Prints the bucket in the stack's current S3BucketArn (empty if none); messages go to stderr.
STACK_BUCKET=$("$PYTHON" - "$STACK_JSON" <<'PY'
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
print("  Agent access with COMPUTER_VISION is enabled.", file=sys.stderr)
print((config.get("S3BucketArn") or "").split(":::", 1)[-1])
PY
)

# ─── Step 2: Recordings bucket ───
echo "Step 2: Preparing the recordings bucket..."
# S3BucketArn is also where agent screenshots are uploaded, so keep a bucket the stack already has.
# This script's own bucket is left to the branch below, which sets its settings again on a re-run.
OWN_BUCKET="replay-session-${ACCOUNT_ID}-${REGION}"
if [ -z "$BUCKET" ] && [ -n "$STACK_BUCKET" ] && [ "$STACK_BUCKET" != "$OWN_BUCKET" ]; then
  BUCKET="$STACK_BUCKET"
  echo "  The stack already uses bucket $BUCKET (S3BucketArn); recordings go there too."
  echo "  No expiry is set on it; add a lifecycle rule for agentaccess/replay-session/ if you want one."
elif [ -n "$BUCKET" ] && [ -n "$STACK_BUCKET" ] && [ "$BUCKET" != "$STACK_BUCKET" ]; then
  echo "  WARNING: the stack's S3BucketArn changes from $STACK_BUCKET to $BUCKET." \
    "Agent screenshot uploads, if enabled, move to $BUCKET as well."
fi
if [ -n "$BUCKET" ]; then
  # An existing bucket is used as-is: it must be in this account, and nothing about it is changed.
  aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --region "$REGION" >/dev/null
  echo "  Using existing bucket $BUCKET (not modified)."
  echo "  Its policy must allow s3:ListBucket for appstream.amazonaws.com, or UpdateStack refuses it in Step 4."
else
  BUCKET="$OWN_BUCKET"
  # Create the bucket only when it doesn't exist (404); any other head-bucket error (owned by
  # another account, access denied, expired credentials) stops here with the real cause.
  BUCKET_EXISTS=true
  BUCKET_ERR=$(mktemp)
  if ! aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --region "$REGION" \
      >/dev/null 2>"$BUCKET_ERR"; then
    if ! grep -qF "(404)" "$BUCKET_ERR"; then
      cat "$BUCKET_ERR" >&2
      rm -f "$BUCKET_ERR"
      exit 1
    fi
    BUCKET_EXISTS=false
  fi
  rm -f "$BUCKET_ERR"
  if $BUCKET_EXISTS; then
    # This script's own bucket: set its settings again, in case an earlier run stopped before them.
    if $DRY_RUN; then
      echo "  Would reuse $BUCKET and re-apply its settings ($OWN_BUCKET_SETTINGS)."
    else
      apply_own_bucket_settings "$BUCKET"
      echo "  Reusing $BUCKET (re-applied: $OWN_BUCKET_SETTINGS)."
    fi
  elif $DRY_RUN; then
    echo "  Would create $BUCKET ($OWN_BUCKET_SETTINGS)."
  else
    if [ "$REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
    else
      aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
    apply_own_bucket_settings "$BUCKET"
    echo "  Created $BUCKET ($OWN_BUCKET_SETTINGS)."
  fi
fi

# ─── Step 3: Optional customer managed key ───
KMS_KEY_ARN=""
if $USE_KMS; then
  echo "Step 3: Preparing the customer managed KMS key..."
  # Stack names may contain "." but KMS aliases may not, so replace anything else with "-".
  ALIAS="alias/replay-session-$(printf '%s' "$STACK_NAME" | tr -c 'a-zA-Z0-9_-' '-')"
  # Create a key only when the alias doesn't exist; any other describe-key error
  # (access denied, expired credentials) stops here instead of orphaning a new key.
  KMS_ERR=$(mktemp)
  if ! KMS_KEY_ARN=$(aws kms describe-key --key-id "$ALIAS" --region "$REGION" \
      --query KeyMetadata.Arn --output text 2>"$KMS_ERR"); then
    if ! grep -q NotFoundException "$KMS_ERR"; then
      cat "$KMS_ERR" >&2
      rm -f "$KMS_ERR"
      exit 1
    fi
    KMS_KEY_ARN=""
  fi
  rm -f "$KMS_ERR"
  # The key is used with your own forwarded credentials, so the policy admits callers
  # in this account when the request arrives through the named service. No service
  # principal is added.
  POLICY="{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {\"Sid\": \"EnableAccountAdmin\", \"Effect\": \"Allow\",
         \"Principal\": {\"AWS\": \"arn:${PARTITION}:iam::${ACCOUNT_ID}:root\"},
         \"Action\": \"kms:*\", \"Resource\": \"*\"},
        {\"Sid\": \"AllowFrameEncryptionViaAgentAccess\", \"Effect\": \"Allow\",
         \"Principal\": {\"AWS\": \"*\"},
         \"Action\": [\"kms:GenerateDataKey\", \"kms:Decrypt\"],
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
  if [ -n "$KMS_KEY_ARN" ]; then
    # Re-apply the policy so a changed --kms-*-via-service takes effect on a re-run.
    if $DRY_RUN; then
      echo "  Would reuse $ALIAS and re-apply its key policy."
    else
      aws kms put-key-policy --key-id "$KMS_KEY_ARN" --policy-name default \
        --policy "$POLICY" --region "$REGION"
      echo "  Reusing $ALIAS (key policy re-applied)."
    fi
  elif $DRY_RUN; then
    echo "  Would create a KMS key with alias $ALIAS."
    KMS_KEY_ARN="arn:${PARTITION}:kms:${REGION}:${ACCOUNT_ID}:key/<new-key-id>"
  else
    KMS_KEY_ARN=$(aws kms create-key --region "$REGION" \
      --description "Replay Session frames for stack ${STACK_NAME}" \
      --policy "$POLICY" --query KeyMetadata.Arn --output text)
    if ! aws kms create-alias --alias-name "$ALIAS" --target-key-id "$KMS_KEY_ARN" --region "$REGION"; then
      # Without the alias the next run could not find this key and would create another.
      if aws kms schedule-key-deletion --key-id "$KMS_KEY_ARN" --pending-window-in-days 7 \
          --region "$REGION" >/dev/null; then
        echo "ERROR: could not create $ALIAS; the new key $KMS_KEY_ARN is scheduled for deletion." >&2
      else
        echo "ERROR: could not create $ALIAS, and could not schedule $KMS_KEY_ARN for deletion;" \
          "delete it manually." >&2
      fi
      exit 1
    fi
    aws kms enable-key-rotation --key-id "$KMS_KEY_ARN" --region "$REGION"
    echo "  Created $ALIAS -> $KMS_KEY_ARN"
  fi
else
  echo "Step 3: No customer managed key (pass --kms to add one)."
fi

# ─── Step 4: Update the stack ───
echo "Step 4: Turning on Replay Session..."
CONFIG="{\"ReplaySessionEnabled\": true, \"S3BucketArn\": \"arn:${PARTITION}:s3:::${BUCKET}\""
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
    sys.exit("ERROR: the stack accepted the update, but reading it back shows ReplaySessionEnabled "
             f"{config.get('ReplaySessionEnabled')!r}, not true.")
if sys.argv[2] and config.get("KmsKeyArn") != sys.argv[2]:
    sys.exit("ERROR: the stack accepted the update, but reading it back shows KmsKeyArn "
             f"{config.get('KmsKeyArn')!r}, not {sys.argv[2]}.")
# UpdateStack merges AgentAccessConfig; confirm COMPUTER_VISION, checked in Step 1, is still ENABLED.
vision = [s for s in config.get("Settings", []) if s.get("AgentAction") == "COMPUTER_VISION"]
if not vision or vision[0].get("Permission") != "ENABLED":
    sys.exit("ERROR: COMPUTER_VISION is no longer ENABLED on the stack after the update.")
print(f"  ReplaySessionEnabled: true, S3BucketArn: {config.get('S3BucketArn')}"
      + (f", KmsKeyArn: {config['KmsKeyArn']}" if config.get("KmsKeyArn") else ""))
PY

echo ""
echo "✓ Replay Session is on for $STACK_NAME. It applies to sessions started from now on."
echo ""
echo "Recordings are delivered to:"
echo "  s3://${BUCKET}/agentaccess/replay-session/year=YYYY/month=MM/day=DD/<mcpSessionId>/"
echo ""
echo "Run the agent with --save-replay; it downloads the recording when the session ends:"
echo "  source venv/bin/activate"
if [ "$STACK_NAME" = "$CONFIG_STACK_NAME" ]; then
  # scripts/streaming_url.sh and get_replay_session.py both default to this stack (as retry_command
  # in lib/replay_session.py leaves --stack-name out for it)
  STACK_OPTION=""
  echo "  STREAMING_URL=\$(AWS_REGION=${REGION} scripts/streaming_url.sh)"
else
  # scripts/streaming_url.sh uses the config.json stack, so mint the URL for this one
  STACK_OPTION=" --stack-name ${STACK_NAME}"
  echo "  STREAMING_URL=\$(aws appstream create-streaming-url --stack-name ${STACK_NAME} --fleet-name YOUR_FLEET_NAME --user-id testuser --validity 3600 --region ${REGION} --query StreamingURL --output text)"
fi
# --save-replay reads the stack from the streaming URL
echo "  python3 agents/generic_cua/agent.py --streaming-url \"\$STREAMING_URL\" --expire-session-on-exit --save-replay"
echo ""
echo "For another agent, retrieve its recording with its MCP session ID:"
echo "  python3 scripts/get_replay_session.py --session-id SESSION_ID${STACK_OPTION} --region ${REGION}"

#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# Read stack + fleet names from scripts/config.json and generate
# a fresh AppStream streaming URL (default: 1 hour validity).
#
# Usage:
#   STREAMING_URL=$(scripts/streaming_url.sh)
#   python3 agents/pdf_extractor_demo/agent.py --streaming-url "$STREAMING_URL"
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
USER_ID="${USER_ID:-testuser}"
VALIDITY="${VALIDITY:-3600}"

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: $CONFIG not found" >&2
  exit 1
fi

read -r STACK FLEET <<EOF
$(python3 -c "
import json, re
raw = open('$CONFIG').read()
raw = re.sub(r'(?m)^\s*//.*$', '', raw)
d = json.loads(raw)
print(d['stack']['name'], d['fleet']['name'])
")
EOF

aws appstream create-streaming-url \
  --region "$REGION" \
  --stack-name "$STACK" \
  --fleet-name "$FLEET" \
  --user-id "$USER_ID" \
  --validity "$VALIDITY" \
  --query StreamingURL --output text

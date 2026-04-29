#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -euo pipefail

# ──────────────────────────────────────────────────────────────
# Migrate from local agentic client to Agent Access MCP Server
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"

# Read the MCP endpoint from env or config.json. Fail fast if neither
# provides one — we intentionally do not ship a default to avoid leaking
# internal infrastructure or silently sending traffic to the wrong endpoint.
MCP_ENDPOINT=$(python3 -c "
import json, re, os, sys
p = os.path.join('$SCRIPT_DIR', 'config.json')
endpoint = os.environ.get('MCP_ENDPOINT', '')
if not endpoint and os.path.isfile(p):
    raw = re.sub(r'(?m)^\s*//.*\$', '', open(p).read())
    d = json.loads(raw)
    endpoint = (d.get('mcp', {}) or {}).get('endpoint') or d.get('mcpEndpoint') or ''
if not endpoint:
    sys.stderr.write('ERROR: MCP_ENDPOINT is required (env var or scripts/config.json mcp.endpoint).\n')
    sys.exit(1)
print(endpoint)
")
REMOVE_LOCAL=false

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
    --region)       REGION="$2"; shift 2 ;;
    --endpoint)     MCP_ENDPOINT="$2"; shift 2 ;;
    --remove-local) REMOVE_LOCAL=true; shift ;;
    --help)
      echo "Usage: $0 [--region REGION] [--endpoint URL] [--remove-local]"
      exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
done

echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Migrate to Agent Access MCP Server${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Region:   $REGION"
echo "  Endpoint: $MCP_ENDPOINT"
echo ""

# ── Step 1: Install mcp-proxy-for-aws ─────────────────────────
info "Step 1: Installing mcp-proxy-for-aws..."

if [ -d "venv" ]; then
  source venv/bin/activate
fi

pip install --quiet "mcp-proxy-for-aws==1.4.0" 2>/dev/null
ok "mcp-proxy-for-aws installed"

# Verify import works
python3 -c "from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client; print('  Import OK')" 2>/dev/null || \
  fail "Could not import mcp_proxy_for_aws — check your Python environment"

# ── Step 2: Verify MCP endpoint connectivity ─────────────────
info "Step 2: Verifying MCP endpoint connectivity..."

HTTP_CODE=$(python3 -c "
import boto3, json
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session
import urllib.request, urllib.error

creds = Session().get_credentials().get_frozen_credentials()
body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-03-26', 'capabilities': {},
    'clientInfo': {'name': 'migration-check', 'version': '1.0'}
}})
req = AWSRequest(method='POST', url='$MCP_ENDPOINT', data=body,
    headers={'Content-Type': 'application/json', 'Host': '$MCP_ENDPOINT'.split('//')[1].split('/')[0]})
service = os.environ.get('AWS_SERVICE_NAME', '')
if not service:
    sys.stderr.write('ERROR: AWS_SERVICE_NAME env var is required.\n')
    sys.exit(1)
SigV4Auth(creds, service, '$REGION').add_auth(req)

import httpx
resp = httpx.post('$MCP_ENDPOINT', headers=dict(req.headers), content=body, timeout=15)
print(resp.status_code)
" 2>/dev/null || echo "error")

case "$HTTP_CODE" in
  200) ok "MCP endpoint reachable (200 OK)" ;;
  401) warn "MCP endpoint returned 401 — check IAM credentials" ;;
  403) warn "MCP endpoint returned 403 — check IAM permissions for the MCP service" ;;
  400) ok "MCP endpoint reachable (400 — expected without streaming URL)" ;;
  *)   warn "MCP endpoint returned $HTTP_CODE — check endpoint URL and network" ;;
esac

echo ""

# ── Step 3: Remove local client (optional) ────────────────────
if [ "$REMOVE_LOCAL" = true ]; then
  info "Step 3: Removing local agentic client..."
  rm -rf "$PROJECT_ROOT/agentic-client/"
  rm -f "$PROJECT_ROOT"/agentic-client*.rpm
  ok "Local client removed"
elif [ -d "$PROJECT_ROOT/agentic-client" ] || ls "$PROJECT_ROOT"/agentic-client*.rpm 1>/dev/null 2>&1; then
  info "Step 3: Local agentic client still present"
  echo "  Run with --remove-local to clean up (~130MB):"
  echo "  ./scripts/migrate_to_mcp.sh --remove-local"
else
  info "Step 3: No local client found — nothing to clean up"
fi

echo ""

# ── Summary ───────────────────────────────────────────────────
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Migration Complete${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Your agents now use the Agent Access MCP Server."
echo ""
echo "  Run with a streaming URL:"
echo "    python3 agents/pdf_extractor_demo/agent.py --streaming-url \"<URL>\""
echo ""
echo "  See scripts/MIGRATION.md for the full migration guide."
echo ""

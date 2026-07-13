#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Local CI — mirrors .github/workflows/ci.yml so you can verify a change before
# opening a PR. Runs two fast, dependency-free checks:
#   1. Byte-compiles all Python sources (syntax / version check)
#   2. Validates every skill JSON file
#
# Usage:
#   ./scripts/ci_local.sh
#
# Exit status is 0 only if both checks pass (same contract as CI).

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_ROOT"

PYTHON_CMD="${PYTHON_CMD:-python3}"

echo "=========================================="
echo "Local CI — compile + validate skill JSON"
echo "=========================================="
echo "  Python: $($PYTHON_CMD --version 2>&1)"
echo ""

# --- 1. Byte-compile all Python sources ---
echo "1. Byte-compiling Python sources..."
$PYTHON_CMD -m compileall -q agents lib scripts mcp_servers
# Root-level scripts (quickstart.py, etc.) not under the package dirs above.
ROOT_PY=$(git ls-files '*.py' | grep -vE '^(agents|lib|scripts|mcp_servers)/' || true)
if [ -n "$ROOT_PY" ]; then
    # shellcheck disable=SC2086
    $PYTHON_CMD -m compileall -q $ROOT_PY
fi
echo -e "${GREEN}✓${NC} All Python sources compile"
echo ""

# --- 2. Validate skill JSON files ---
echo "2. Validating skill JSON files..."
$PYTHON_CMD - <<'PY'
import glob, json, sys

files = sorted(glob.glob("agents/**/skills/*.json", recursive=True))
if not files:
    print("  No skill JSON files found — nothing to validate.")
    sys.exit(0)

failures = []
for path in files:
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
        print(f"  OK   {path}")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  FAIL {path}: {exc}")
        failures.append(path)

if failures:
    print(f"\n  {len(failures)} invalid skill JSON file(s).")
    sys.exit(1)
print(f"\n  All {len(files)} skill JSON file(s) are valid.")
PY

echo ""
echo "=========================================="
echo -e "${GREEN}Local CI passed.${NC}"
echo "=========================================="

#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# Package script for WorkSpaces Agent Framework
# Creates a clean zip file with only source code, excluding runtime artifacts

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo "=========================================="
echo "WorkSpaces Agent Framework - Packaging"
echo "=========================================="
echo ""

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
PROJECT_NAME=$(basename "$PROJECT_ROOT")

cd "$PROJECT_ROOT"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
ZIP_NAME="${PROJECT_NAME}.zip"

echo -e "${BLUE}Project:${NC} $PROJECT_NAME"
echo -e "${BLUE}Output:${NC} $ZIP_NAME"
echo ""

if ! command -v zip &> /dev/null; then
    echo -e "${RED}Error: 'zip' command not found${NC}"
    exit 1
fi

TEMP_DIR=$(mktemp -d)
STAGE_DIR="$TEMP_DIR/$PROJECT_NAME"

echo "Staging files..."

rsync -a \
    --exclude='venv/' \
    --exclude='env/' \
    --exclude='ENV/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.pyd' \
    --exclude='.Python' \
    --exclude='*.so' \
    --exclude='*.egg' \
    --exclude='*.egg-info/' \
    --exclude='.eggs/' \
    --exclude='dist/' \
    --exclude='build/' \
    --exclude='logs/' \
    --exclude='metrics/' \
    --exclude='screenshots/' \
    --exclude='agents/*/logs/' \
    --exclude='agents/*/metrics/' \
    --exclude='agents/*/screenshots/' \
    --exclude='reports/' \
    --exclude='*.log' \
    --exclude='*.png' \
    --exclude='*.jpg' \
    --exclude='*.jpeg' \
    --exclude='.git/' \
    --exclude='.DS_Store' \
    --exclude='Thumbs.db' \
    --exclude='.vscode/' \
    --exclude='.idea/' \
    --exclude='*.swp' \
    --exclude='*.swo' \
    --exclude='*~' \
    --exclude='*.mov' \
    --exclude='*.mp4' \
    --exclude='*.tar' \
    --exclude='*.tar.gz' \
    --exclude='*.tar.zst' \
    --exclude='*.zip' \
    --exclude='*.rpm' \
    --exclude='agentic-client/' \
    --exclude='.agentcore-build/*' \
    --exclude='certs/' \
    --exclude='Config' \
    "$PROJECT_ROOT/" "$STAGE_DIR/"

# Create the zip file
echo ""
echo "Creating zip archive..."
cd "$TEMP_DIR"
zip -r -q "$PROJECT_ROOT/$ZIP_NAME" "$PROJECT_NAME"

# Clean up
rm -rf "$TEMP_DIR"

FILE_SIZE=$(ls -lh "$PROJECT_ROOT/$ZIP_NAME" | awk '{print $5}')
FILE_COUNT=$(unzip -l "$PROJECT_ROOT/$ZIP_NAME" | tail -1 | awk '{print $2}')

echo ""
echo "=========================================="
echo -e "${GREEN}Package Created Successfully!${NC}"
echo "=========================================="
echo ""
echo -e "${BLUE}File:${NC} $ZIP_NAME"
echo -e "${BLUE}Size:${NC} $FILE_SIZE"
echo -e "${BLUE}Files:${NC} $FILE_COUNT"
echo ""
echo "Contents include:"
echo "  ✓ Source code (lib/, agents/, skills/)"
echo "  ✓ Configuration files (requirements.txt, JSON configs)"
echo "  ✓ Agent prompts and skills"
echo "  ✓ Documentation (README.md)"
echo "  ✓ Scripts"
echo ""
echo "Excluded:"
echo "  ✗ Virtual environments (venv/, env/)"
echo "  ✗ Runtime artifacts (logs/, metrics/, screenshots/, reports/)"
echo "  ✗ Python cache files (__pycache__/, *.pyc)"
echo "  ✗ IDE files (.vscode/, .idea/)"
echo "  ✗ Git files (.git/)"
echo ""
echo -e "${GREEN}Ready to distribute!${NC}"
echo ""
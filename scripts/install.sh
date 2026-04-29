#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Installation script for WorkSpaces Agent Framework
# Creates a Python venv and installs Python packages.

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_ROOT"

echo "=========================================="
echo "WorkSpaces Agent Framework - Installation"
echo "=========================================="
echo ""

# --- 1. System dependencies ---
echo "1. Checking system dependencies..."

if command -v python3 &> /dev/null || command -v python3.11 &> /dev/null || command -v python3.12 &> /dev/null; then
    echo -e "${GREEN}✓${NC} Python 3 found"
else
    echo -e "${YELLOW}⚠${NC}  Python 3 not found. Install Python 3.10+ and try again."
    exit 1
fi

# --- 2. Python virtual environment ---
echo ""
echo "2. Setting up Python environment..."

PYTHON_CMD=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v $cmd &> /dev/null; then
        PYTHON_CMD=$cmd
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}✗${NC} Python 3 not found. Install Python 3.10+ and try again."
    exit 1
fi

PYTHON_VERSION=$($PYTHON_CMD --version | cut -d' ' -f2)
echo -e "${GREEN}✓${NC} Found Python $PYTHON_VERSION"

if [ -d "venv" ]; then
    echo -e "${GREEN}✓${NC} Virtual environment already exists"
else
    $PYTHON_CMD -m venv venv
    echo -e "${GREEN}✓${NC} Virtual environment created"
fi

source venv/bin/activate
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt > /dev/null 2>&1
echo -e "${GREEN}✓${NC} Python dependencies installed"

# --- Done ---
echo ""
echo "=========================================="
echo -e "${GREEN}Installation complete!${NC}"
echo "=========================================="
echo ""
echo "To run an agent:"
echo -e "  ${YELLOW}source venv/bin/activate${NC}"
echo -e "  ${YELLOW}python3 agents/paint_demo/agent.py --streaming-url \"<URL>\"${NC}"
echo ""

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

# Version check happens in step 2; here we just confirm *some* python3 exists.
if ! command -v python3 &> /dev/null \
    && ! command -v python3.10 &> /dev/null \
    && ! command -v python3.11 &> /dev/null \
    && ! command -v python3.12 &> /dev/null; then
    echo -e "${YELLOW}⚠${NC}  Python 3 not found. Install Python 3.10+ and try again."
    exit 1
fi
echo -e "${GREEN}✓${NC} Python 3 found"

# --- 2. Python virtual environment ---
echo ""
echo "2. Setting up Python environment..."

PYTHON_CMD=""
MIN_MINOR=10
for cmd in python3.12 python3.11 python3.10; do
    if command -v $cmd &> /dev/null; then
        PYTHON_CMD=$cmd
        break
    fi
done

# Fall back to `python3` only if it reports >= 3.10
if [ -z "$PYTHON_CMD" ] && command -v python3 &> /dev/null; then
    if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,$MIN_MINOR) else 1)" 2>/dev/null; then
        PYTHON_CMD=python3
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}✗${NC} Python 3.10+ not found."
    echo "  Install Python 3.10 or newer, then re-run this script."
    echo "  On Amazon Linux 2023: sudo dnf install -y python3.11 python3.11-pip"
    echo "  On CloudShell: CloudShell ships Python 3.9 by default."
    echo "                 Install 3.11 with: sudo dnf install -y python3.11 python3.11-pip"
    exit 1
fi

PYTHON_VERSION=$($PYTHON_CMD --version | cut -d' ' -f2)
echo -e "${GREEN}✓${NC} Found Python $PYTHON_VERSION ($PYTHON_CMD)"

if [ -d "venv" ]; then
    echo -e "${GREEN}✓${NC} Virtual environment already exists"
else
    $PYTHON_CMD -m venv venv
    echo -e "${GREEN}✓${NC} Virtual environment created"
fi

source venv/bin/activate
echo "  Upgrading pip..."
pip install --upgrade pip
echo "  Installing requirements.txt..."
pip install -r requirements.txt
echo -e "${GREEN}✓${NC} Python dependencies installed"

# --- Done ---
echo ""
echo "=========================================="
echo -e "${GREEN}Installation complete!${NC}"
echo "=========================================="
echo ""
echo "To run an agent:"
echo -e "  ${YELLOW}source venv/bin/activate${NC}"
echo -e "  ${YELLOW}python3 agents/pdf_extractor_demo/agent.py --streaming-url \"<URL>\"${NC}"
echo ""

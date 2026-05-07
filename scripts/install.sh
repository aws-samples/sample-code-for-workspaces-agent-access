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

# --- 1. Python virtual environment ---
echo "1. Setting up Python environment..."

PYTHON_CMD=""
MIN_MINOR=10

# Check `python3` first, then try versioned binaries as fallback.
if command -v python3 &> /dev/null; then
    if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,$MIN_MINOR) else 1)" 2>/dev/null; then
        PYTHON_CMD=python3
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    for cmd in $(compgen -c python3. 2>/dev/null | sort -t. -k2 -rn | uniq); do
        if command -v "$cmd" &> /dev/null; then
            if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,$MIN_MINOR) else 1)" 2>/dev/null; then
                PYTHON_CMD="$cmd"
                break
            fi
        fi
    done
fi

if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}✗${NC} Python 3.10+ not found."
    echo ""
    echo "  Please install Python 3.10 or newer, then re-run this script."
    echo ""
    echo "  Suggested install commands:"
    echo "    macOS:          brew install python@3.12"
    echo "    Ubuntu/Debian:  sudo apt install -y python3 python3-venv"
    echo "    Amazon Linux:   sudo dnf install -y python3.11 python3.11-pip"
    echo "    Windows:        https://www.python.org/downloads/"
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

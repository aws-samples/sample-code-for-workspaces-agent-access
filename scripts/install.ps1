#Requires -Version 5.1
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Installation script for WorkSpaces Agent Framework (Windows PowerShell)
# Creates a Python venv and installs Python packages.

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

function Write-Step { param([string]$Text) Write-Host $Text -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "[OK] $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "[WARN] $Text" -ForegroundColor Yellow }
function Write-Fail { param([string]$Text) Write-Host "[FAIL] $Text" -ForegroundColor Red; exit 1 }

Write-Host "=========================================="
Write-Host "WorkSpaces Agent Framework - Installation"
Write-Host "=========================================="
Write-Host ""

# --- 1. System dependencies ---
Write-Step "1. Checking system dependencies..."

$PythonCmd = $null
foreach ($candidate in @('python3.12', 'python3.11', 'python3.10', 'python3', 'python')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        # Skip Microsoft Store python stubs — they print "not found" to stderr.
        if ($cmd.Source -and $cmd.Source -match '\\WindowsApps\\') {
            continue
        }
        $versionOutput = ''
        try {
            $versionOutput = & $candidate --version 2>&1 | Out-String
        } catch {
            continue
        }
        if ($versionOutput -match 'Python\s+(\d+)\.(\d+)') {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 10) {
                $PythonCmd = $candidate
                Write-Ok "Found Python $($Matches[0]) ($candidate)"
                break
            }
        }
    }
}

if (-not $PythonCmd) {
    Write-Fail "Python 3.10+ not found. Install Python and try again: https://www.python.org/downloads/"
}

# --- 2. Python virtual environment ---
Write-Host ""
Write-Step "2. Setting up Python environment..."

if (Test-Path "venv") {
    Write-Ok "Virtual environment already exists"
} else {
    & $PythonCmd -m venv venv
    if ($LASTEXITCODE -ne 0) { Write-Fail "Failed to create virtual environment" }
    Write-Ok "Virtual environment created"
}

$VenvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Fail "venv python not found at $VenvPython"
}

& $VenvPython -m pip install --upgrade pip *> $null
# Run pip with output visible so any failures (missing hashes, network, etc.)
# surface to the user instead of being swallowed.
& $VenvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Fail "Failed to install Python dependencies" }
Write-Ok "Python dependencies installed"

# --- Done ---
Write-Host ""
Write-Host "=========================================="
Write-Host "Installation complete!" -ForegroundColor Green
Write-Host "=========================================="
Write-Host ""
Write-Host "To run an agent:"
Write-Host "  .\venv\Scripts\Activate.ps1" -ForegroundColor Yellow
Write-Host "  python agents\pdf_extractor_demo\agent.py --streaming-url `"<URL>`"" -ForegroundColor Yellow
Write-Host ""

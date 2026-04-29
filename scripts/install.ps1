#Requires -Version 5.1
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
        $versionOutput = & $candidate --version 2>&1
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
& $VenvPython -m pip install -r requirements.txt *> $null
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
Write-Host "  python agents\paint_demo\agent.py --streaming-url `"<URL>`"" -ForegroundColor Yellow
Write-Host ""

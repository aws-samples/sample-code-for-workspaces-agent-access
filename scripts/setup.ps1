#Requires -Version 5.1
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ──────────────────────────────────────────────────────────────
# WorkSpaces Agent Demo — One-Step Setup (Windows PowerShell)
#
# Run from any Windows machine with Python 3.11+ and valid AWS
# credentials. This script will point you at the right installer
# if the AWS CLI isn't present and walk you through signing in if
# your credentials aren't configured.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Steps:
#   1. Ensures AWS CLI v2 is installed + credentials are valid
#   2. Checks for Python 3.11+
#   3. Installs agent dependencies into a local venv
#   4. Deploys AWS resources (VPC, Fleet, Stack) via bash scripts/deploy.sh
#   5. Waits for the fleet to reach RUNNING state
#   6. Generates a streaming URL
#   7. Runs the demo agent
#
# Step 4 requires a bash interpreter — Git for Windows, WSL, or the
# Windows 11 built-in Bash all work. Install Git for Windows from
# https://git-scm.com/download/win if you don't have one yet.
# ──────────────────────────────────────────────────────────────

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

$FleetWaitTimeout = 900
$PollInterval = 15

function Write-Info { param([string]$Text) Write-Host "[INFO]  $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "[OK]    $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "[WARN]  $Text" -ForegroundColor Yellow }
function Write-Fail { param([string]$Text) Write-Host "[FAIL]  $Text" -ForegroundColor Red; exit 1 }

function Write-Separator {
    param([string]$Text)
    Write-Host ""
    Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor White
    Write-Host "  $Text" -ForegroundColor White
    Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor White
    Write-Host ""
}

# Resolve a usable python executable (for reading config.json)
function Get-PythonCommand {
    foreach ($candidate in @('python3.12', 'python3.11', 'python3.10', 'python3', 'python')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            # The Microsoft Store "python.exe" / "python3.exe" aliases live in
            # WindowsApps and print an error to stderr when invoked. Skip them.
            if ($cmd.Source -and $cmd.Source -match '\\WindowsApps\\') {
                continue
            }
            $versionOutput = ''
            try {
                # Suppress non-terminating native errors from this probe so the
                # script's ErrorActionPreference=Stop doesn't bail out here.
                $versionOutput = & $candidate --version 2>&1 | Out-String
            } catch {
                continue
            }
            if ($versionOutput -match 'Python\s+(\d+)\.(\d+)') {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 10) {
                    return $candidate
                }
            }
        }
    }
    return $null
}

# Strip JSONC comments and read a nested key path from config.json.
# Keys are passed as separate arguments (e.g. 'fleet', 'name') to avoid any
# Python string-escaping issues in the embedded script.
function Get-ConfigValue {
    param(
        [Parameter(Mandatory=$true)][string]$PythonCmd,
        [Parameter(Mandatory=$true)][string]$ConfigPath,
        [Parameter(Mandatory=$true)][string[]]$Keys,
        [string]$Default = ''
    )
    $pyScript = @'
import json, re, sys
try:
    raw = open(sys.argv[1], encoding='utf-8').read()
    raw = re.sub(r'(?m)^\s*//.*$', '', raw)
    d = json.loads(raw)
    for k in sys.argv[2:]:
        d = d[k]
    print('' if d is None else d)
except Exception:
    print(sys.argv[-1] if False else '')
'@
    $argList = @('-c', $pyScript, $ConfigPath) + $Keys
    $value = & $PythonCmd @argList 2>$null
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

$ConfigPath = Join-Path $ScriptDir 'config.json'
$Region = if ($env:AWS_REGION) { $env:AWS_REGION } elseif ($env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION } else { 'us-east-1' }

# ── Step 1: AWS CLI + credentials ─────────────────────────────
Write-Separator "Step 1/7: Checking AWS CLI and credentials"

$awsCmd = Get-Command aws -ErrorAction SilentlyContinue
if ($awsCmd) {
    $awsVersion = (& aws --version 2>&1 | Select-Object -First 1)
    Write-Ok "AWS CLI found: $awsVersion"
} else {
    Write-Warn "AWS CLI v2 is not installed."
    Write-Host ""
    Write-Host "  Install it with one of the options below, then re-run this setup script."
    Write-Host ""
    Write-Host "  # Windows — MSI installer (recommended)" -ForegroundColor Cyan
    Write-Host "  https://awscli.amazonaws.com/AWSCLIV2.msi"
    Write-Host ""
    Write-Host "  # Windows — winget" -ForegroundColor Cyan
    Write-Host "  winget install -e --id Amazon.AWSCLI"
    Write-Host ""
    Write-Fail "AWS CLI not installed. Install it with the commands above, then re-run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
}

$CallerAccount = ''
try {
    # Temporarily suppress errors so aws's stderr on expired creds doesn't
    # turn into a NativeCommandError under ErrorActionPreference=Stop.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $CallerAccount = (& aws sts get-caller-identity --query Account --output text 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { $CallerAccount = '' }
} catch {
    $CallerAccount = ''
} finally {
    $ErrorActionPreference = $prev
}

if ([string]::IsNullOrWhiteSpace($CallerAccount) -or $CallerAccount -eq 'None') {
    Write-Warn "AWS credentials are missing or expired."
    Write-Host ""
    Write-Host "  Sign in with one of the following, then re-run this setup script."
    Write-Host ""
    Write-Host "  # AWS SSO / IAM Identity Center (recommended)" -ForegroundColor Cyan
    Write-Host "  aws configure sso          # first time only"
    Write-Host "  aws sso login              # every session"
    Write-Host ""
    Write-Host "  # Long-lived access keys" -ForegroundColor Cyan
    Write-Host "  aws configure"
    Write-Host ""
    Write-Host "  # Already have a named profile configured?" -ForegroundColor Cyan
    Write-Host "  `$env:AWS_PROFILE = '<your-profile>'"
    Write-Host ""
    Write-Host "  # Verify sign-in worked" -ForegroundColor Cyan
    Write-Host "  aws sts get-caller-identity"
    Write-Host ""
    Write-Fail "Not signed in. Sign in with the commands above, then re-run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
}

$CallerArn = ''
try {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $CallerArn = (& aws sts get-caller-identity --query Arn --output text 2>&1 | Out-String).Trim()
} catch {
    $CallerArn = ''
} finally {
    $ErrorActionPreference = $prev
}

# ── Step 2: Python ────────────────────────────────────────────
Write-Separator "Step 2/7: Checking Python 3.11+"

$PythonCmd = Get-PythonCommand
if (-not $PythonCmd) {
    Write-Warn "Python 3.11+ is not installed."
    Write-Host ""
    Write-Host "  Install it with one of the options below, then re-run this setup script."
    Write-Host ""
    Write-Host "  # Windows — official installer" -ForegroundColor Cyan
    Write-Host "  https://www.python.org/downloads/windows/"
    Write-Host ""
    Write-Host "  # Windows — winget" -ForegroundColor Cyan
    Write-Host "  winget install -e --id Python.Python.3.11"
    Write-Host ""
    Write-Fail "Python not installed. Install it with the commands above, then re-run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
}
Write-Ok "Python found: $PythonCmd"

# Now that python is available, read the fleet name out of config.json
$FleetName = Get-ConfigValue -PythonCmd $PythonCmd -ConfigPath $ConfigPath `
    -Keys 'fleet', 'name' -Default 'WorkspacesAgentDemo'
Write-Ok "Signed in as: $CallerArn"
Write-Ok "Account: $CallerAccount | Region: $Region | Fleet: $FleetName"

# ── Step 3: Agent dependencies ────────────────────────────────
Write-Separator "Step 3/7: Installing agent dependencies"

Write-Info "Running install.ps1..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir 'install.ps1')
if ($LASTEXITCODE -ne 0) { Write-Fail "install.ps1 failed" }
Write-Ok "Agent dependencies installed."

$VenvPython = Join-Path $ProjectRoot 'venv\Scripts\python.exe'

# ── Step 4: Deploy WorkSpaces resources ───────────────────────
Write-Separator "Step 4/7: Deploying AWS resources (VPC, Fleet, Stack)"

# deploy.sh is a bash script that uses aws + python3 only. Run it via
# whichever bash is available: Git Bash, WSL, or the Windows 11 built-in.
# Git for Windows does NOT add bash to PATH by default (only Git\cmd), so
# we also probe common Git install locations directly.
function Get-BashPath {
    $cmd = Get-Command bash -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "$env:ProgramFiles\Git\bin\bash.exe",
        "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
        "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

$BashPath = Get-BashPath
if (-not $BashPath) {
    Write-Warn "bash not found on PATH or in standard Git for Windows install locations."
    Write-Host ""
    Write-Host "  scripts\deploy.sh is a bash script. Install one of the following, then re-run this setup."
    Write-Host ""
    Write-Host "  # Git for Windows (provides bash at C:\Program Files\Git\bin\bash.exe)" -ForegroundColor Cyan
    Write-Host "  https://git-scm.com/download/win"
    Write-Host "  winget install -e --id Git.Git"
    Write-Host ""
    Write-Host "  # Windows Subsystem for Linux" -ForegroundColor Cyan
    Write-Host "  wsl --install"
    Write-Host ""
    Write-Fail "bash not installed. Install it with the commands above, then re-run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
}

Write-Info "Running deploy.sh via bash ($BashPath) — this may take several minutes..."
# Pass region through so deploy.sh picks the same region we resolved.
$env:AWS_REGION = $Region
& $BashPath (Join-Path $ScriptDir 'deploy.sh')
if ($LASTEXITCODE -ne 0) { Write-Fail "deploy.sh failed (exit $LASTEXITCODE)" }
Write-Ok "AWS resources deployed."

# ── Step 5: Wait for fleet ────────────────────────────────────
Write-Separator "Step 5/7: Waiting for fleet to reach RUNNING state"

Write-Info "Fleet: $FleetName | Region: $Region"
Write-Info "This can take a few minutes..."

$Elapsed = 0
while ($true) {
    $FleetState = & aws appstream describe-fleets --region $Region --names $FleetName --query 'Fleets[0].State' --output text 2>$null
    if ([string]::IsNullOrWhiteSpace($FleetState)) { $FleetState = 'UNKNOWN' }

    if ($FleetState -eq 'RUNNING') {
        Write-Ok "Fleet '$FleetName' is RUNNING."
        break
    }

    if ($Elapsed -ge $FleetWaitTimeout) {
        Write-Fail "Fleet did not reach RUNNING state within $([int]($FleetWaitTimeout / 60)) minutes (current: $FleetState)."
    }

    Write-Info "Fleet state: $FleetState — waiting ${PollInterval}s... (${Elapsed}s / ${FleetWaitTimeout}s)"
    Start-Sleep -Seconds $PollInterval
    $Elapsed += $PollInterval
}

# ── Step 6: Generate streaming URL ────────────────────────────
Write-Separator "Step 6/7: Generating streaming URL"

$StackName = Get-ConfigValue -PythonCmd $PythonCmd -ConfigPath $ConfigPath `
    -Keys 'stack', 'name' -Default 'Workspaces-Apps-AgentDemo'

Write-Info "Creating streaming URL..."
$StreamingUrl = & aws appstream create-streaming-url `
    --region $Region `
    --stack-name $StackName `
    --fleet-name $FleetName `
    --user-id testuser `
    --validity 3600 `
    --query 'StreamingURL' --output text 2>$null

if ([string]::IsNullOrWhiteSpace($StreamingUrl) -or $StreamingUrl -eq 'None') {
    Write-Warn "Could not create streaming URL. The fleet may still be starting."
    Write-Warn "Generate one manually after the fleet is running:"
    Write-Host "  aws appstream create-streaming-url --stack-name $StackName --fleet-name $FleetName --user-id testuser --validity 3600"
    $StreamingUrl = $null
} else {
    Write-Host ""
    Write-Host "  Streaming URL: " -NoNewline
    Write-Host $StreamingUrl -ForegroundColor Cyan
    Write-Host ""
}

# ── Step 7: Run demo agent ────────────────────────────────────
Write-Separator "Step 7/7: Running demo agent"

Write-Info "Starting pdf_extractor_demo..."
Write-Info "Press Ctrl+C to stop."
Write-Host ""

if ($StreamingUrl) {
    & $VenvPython (Join-Path $ProjectRoot 'agents\pdf_extractor_demo\agent.py') `
        --streaming-url $StreamingUrl
} else {
    Write-Host ""
    Write-Host "No streaming URL available." -ForegroundColor Red
    Write-Host "Generate one and run manually:"
    Write-Host ""
    Write-Host "  aws appstream create-streaming-url ``"
    Write-Host "    --stack-name $StackName --fleet-name $FleetName ``"
    Write-Host "    --user-id testuser --validity 3600 ``"
    Write-Host "    --query StreamingURL --output text"
    Write-Host ""
    Write-Host "  .\venv\Scripts\python.exe agents\pdf_extractor_demo\agent.py --streaming-url '<URL>'"
    Write-Host ""
}

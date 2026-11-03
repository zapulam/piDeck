#Requires -Version 5.1
<#
.SYNOPSIS
  Create a local Windows desktop environment for piDeck.

.DESCRIPTION
  Clones are the supported Windows install path. This script creates .venv,
  installs desktop dependencies, writes run-desktop.cmd, and optionally wires
  Claude Code / Codex / Cursor hooks to the local server.

.PARAMETER NoHooks
  Skip hook installation.

.PARAMETER Url
  piDeck server address for hooks (default http://localhost:8765).

.PARAMETER Token
  Optional PIDECK_TOKEN when pointing hooks at a remote Pi.

.PARAMETER Python
  Explicit python.exe path. When omitted, tries py -3, then python.
#>
[CmdletBinding()]
param(
    [switch] $NoHooks,
    [string] $Url = "http://localhost:8765",
    [string] $Token = "",
    [string] $Python = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$versionCheck = "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
$bootstrap = $null
if ($Python) {
    if (-not (Test-Path $Python)) { throw "Python executable not found: $Python" }
    & $Python -c $versionCheck | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Python 3.11+ is required at $Python" }
    $bootstrap = @{ Exe = $Python; Prefix = @() }
} else {
    $ok = $false
    try {
        & py -3 -c $versionCheck | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $bootstrap = @{ Exe = "py"; Prefix = @("-3") }
            $ok = $true
        }
    } catch { $ok = $false }
    if (-not $ok) {
        try {
            & python -c $versionCheck | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $bootstrap = @{ Exe = "python"; Prefix = @() }
                $ok = $true
            }
        } catch { $ok = $false }
    }
    if (-not $ok) {
        throw "Python 3.11+ is required. Install it from https://www.python.org/downloads/ and re-run."
    }
}

Write-Host "Using: $($bootstrap.Exe) $($bootstrap.Prefix -join ' ')"
& $bootstrap.Exe @($bootstrap.Prefix + @("-c", "import sys; print(sys.version)"))

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv ..."
    & $bootstrap.Exe @($bootstrap.Prefix + @("-m", "venv", ".venv"))
    if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv" }
}

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
Write-Host "Installing desktop dependencies ..."
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $venvPython -m pip install -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed" }

$launcher = Join-Path $Root "run-desktop.cmd"
@(
    "@echo off"
    "setlocal"
    "cd /d ""%~dp0"""
    """.venv\Scripts\python.exe"" -m pideck.desktop %*"
) | Set-Content -Path $launcher -Encoding ASCII
Write-Host "Wrote $launcher"

if (-not $NoHooks) {
    Write-Host "Installing agent hooks for $Url ..."
    $hookArgs = @("hooks\install.py", "--url", $Url, "--no-check")
    if ($Token) { $hookArgs += @("--token", $Token) }
    & $venvPython @hookArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Hook install reported an error. Re-run after fixing agents, or pass -NoHooks."
    } else {
        Write-Host "Hooks installed. Restart Claude Code / Codex / Cursor sessions."
    }
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "  Start the window:  .\run-desktop.cmd"
Write-Host "  Demo data:         .\run-desktop.cmd --mock"
Write-Host "  Or open a browser: .\.venv\Scripts\python.exe -m pideck --mock"
Write-Host "                     then visit http://localhost:8765"
Write-Host "  Confirm hooks:     .\.venv\Scripts\python.exe hooks\install.py --url $Url --test"
if ($Url -like "http://localhost*" -or $Url -like "http://127.0.0.1*") {
    Write-Host "  Local desktop hooks do not need a token."
}
Write-Host ""
Write-Host "If Windows reports a missing WebView2 runtime, install the Evergreen package from Microsoft."

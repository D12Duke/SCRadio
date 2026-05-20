# SC Lock Radio - setup.ps1
# ASCII only. No Unicode.

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "[setup] $msg" }
function Write-Fail($msg) { Write-Host "[setup][ERROR] $msg" -ForegroundColor Red }

# 1. Check Python 3.11+
Write-Step "Checking Python 3.11+ ..."
$pythonCmd = $null
foreach ($candidate in @('py -3', 'python', 'python3')) {
    try {
        $parts = $candidate.Split(' ')
        $exe = $parts[0]
        $args = @()
        if ($parts.Length -gt 1) { $args = $parts[1..($parts.Length - 1)] }
        $out = & $exe @args --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $out -match 'Python\s+(\d+)\.(\d+)') {
            $maj = [int]$Matches[1]
            $min = [int]$Matches[2]
            if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
                $pythonCmd = $candidate
                Write-Step "Found $out via '$candidate'"
                break
            }
        }
    } catch { }
}

if (-not $pythonCmd) {
    Write-Fail "Python 3.11 or later not found."
    Write-Host "Download Python from: https://www.python.org/downloads/"
    Write-Host "After installing, re-run this script from the SCLockRadio folder."
    exit 1
}

# 2. Check pip
Write-Step "Checking pip ..."
$pipOk = $false
try {
    $parts = $pythonCmd.Split(' ')
    $exe = $parts[0]
    $args = @()
    if ($parts.Length -gt 1) { $args = $parts[1..($parts.Length - 1)] }
    & $exe @args -m pip --version | Out-Null
    if ($LASTEXITCODE -eq 0) { $pipOk = $true }
} catch { }

if (-not $pipOk) {
    Write-Fail "pip is not available for the detected Python. Install pip and retry."
    exit 1
}
Write-Step "pip OK."

# 3. Create venv
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
$venvPath = Join-Path $scriptDir 'venv'

if (Test-Path $venvPath) {
    Write-Step "venv already exists at $venvPath - reusing."
} else {
    Write-Step "Creating virtual environment at $venvPath ..."
    try {
        $parts = $pythonCmd.Split(' ')
        $exe = $parts[0]
        $args = @()
        if ($parts.Length -gt 1) { $args = $parts[1..($parts.Length - 1)] }
        & $exe @args -m venv venv
        if ($LASTEXITCODE -ne 0) { throw "venv creation returned $LASTEXITCODE" }
    } catch {
        Write-Fail "Failed to create virtual environment: $_"
        exit 1
    }
}

# 4. Activate venv
$activate = Join-Path $venvPath 'Scripts\Activate.ps1'
if (-not (Test-Path $activate)) {
    Write-Fail "Activate script not found at $activate"
    exit 1
}
Write-Step "Activating venv ..."
try {
    . $activate
} catch {
    Write-Fail "Failed to activate venv: $_"
    exit 1
}

# 5. pip install requirements
$reqFile = Join-Path $scriptDir 'requirements.txt'
if (-not (Test-Path $reqFile)) {
    Write-Fail "requirements.txt not found at $reqFile"
    exit 1
}
Write-Step "Upgrading pip ..."
try {
    python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade returned $LASTEXITCODE" }
} catch {
    Write-Fail "pip upgrade failed: $_"
    exit 1
}

Write-Step "Installing requirements ..."
try {
    pip install -r $reqFile
    if ($LASTEXITCODE -ne 0) { throw "pip install returned $LASTEXITCODE" }
} catch {
    Write-Fail "pip install failed: $_"
    exit 1
}

# 6. Done
Write-Host ""
Write-Host "Setup complete. Run SCLockRadio.py to start."
Write-Host "From this folder you can also double-click launch.bat."

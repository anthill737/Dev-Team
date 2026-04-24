# Dev Team launcher (PowerShell) -- local mode, no Docker.
#
# Runs the backend (uvicorn) and frontend (vite) directly on your machine.
# Uses a Python virtual environment and a local node_modules so nothing is
# installed system-wide.
#
# On first run this takes 2-4 minutes to install Python packages and
# frontend dependencies. Subsequent runs start in about 5 seconds.
#
# PIDs of the two background processes are saved to .devteam-run/ so the
# stop script can cleanly kill them later.

$ErrorActionPreference = "Stop"

# ---------- Helpers -----------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> " -ForegroundColor Cyan -NoNewline
    Write-Host $Message
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    [ok] " -ForegroundColor Green -NoNewline
    Write-Host $Message
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    [warn] " -ForegroundColor Yellow -NoNewline
    Write-Host $Message
}

function Write-Fail {
    param([string]$Message)
    Write-Host "    [error] " -ForegroundColor Red -NoNewline
    Write-Host $Message
}

function Test-Command {
    param([string]$Name)
    $null = Get-Command $Name -ErrorAction SilentlyContinue
    return $?
}

function Get-PythonVersion {
    try {
        $v = & python --version 2>&1
        if ($v -match 'Python (\d+)\.(\d+)\.(\d+)') {
            return [version]"$($matches[1]).$($matches[2]).$($matches[3])"
        }
    } catch {}
    return $null
}

function Get-NodeVersion {
    try {
        $v = & node --version 2>&1
        if ($v -match 'v(\d+)\.(\d+)\.(\d+)') {
            return [version]"$($matches[1]).$($matches[2]).$($matches[3])"
        }
    } catch {}
    return $null
}

function Wait-ForUrl {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 120
    )
    $elapsed = 0
    $dot = 0
    Write-Host "    " -NoNewline
    while ($elapsed -lt $TimeoutSeconds) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) {
                if ($dot -gt 0) { Write-Host "" }
                return $true
            }
        } catch {}
        Start-Sleep -Seconds 2
        $elapsed += 2
        Write-Host "." -NoNewline
        $dot++
    }
    if ($dot -gt 0) { Write-Host "" }
    return $false
}

# ---------- Main --------------------------------------------------------------

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$runDir = Join-Path $projectRoot ".devteam-run"
if (-not (Test-Path $runDir)) {
    New-Item -ItemType Directory -Path $runDir | Out-Null
}
$backendPidFile = Join-Path $runDir "backend.pid"
$frontendPidFile = Join-Path $runDir "frontend.pid"
$backendLogFile = Join-Path $runDir "backend.log"
$frontendLogFile = Join-Path $runDir "frontend.log"

Write-Host ""
Write-Host " Dev Team " -ForegroundColor Black -BackgroundColor Yellow -NoNewline
Write-Host "  autonomous dev team powered by Claude"
Write-Host ""
Write-Host "Project: $projectRoot"
Write-Host "Mode:    local (no Docker)"

# Warn if running from inside OneDrive, Dropbox, or Google Drive -- cloud sync
# on folders with thousands of small files (node_modules, .venv) causes file
# locks, sync loops, and bizarre install failures.
if ($projectRoot -match 'OneDrive' -or $projectRoot -match 'Dropbox' -or $projectRoot -match 'Google Drive') {
    Write-Host ""
    Write-Warn "This project is inside a cloud-synced folder (OneDrive / Dropbox / Google Drive)."
    Write-Host "    Cloud sync can corrupt node_modules and .venv because they contain"
    Write-Host "    thousands of files that get locked while syncing."
    Write-Host ""
    Write-Host "    Strongly recommended: move this folder OUT of the synced location,"
    Write-Host "    e.g. to C:\Users\$env:USERNAME\dev-team\. Then double-click Start Dev"
    Write-Host "    Team.bat from there."
    Write-Host ""
    Write-Host "    Press Enter to continue anyway, or Ctrl+C to cancel and move the folder."
    [void](Read-Host)
}

# ---- 1. Check Python ----

Write-Step "Checking Python..."

if (-not (Test-Command "python")) {
    Write-Fail "Python is not installed (or not on PATH)."
    Write-Host ""
    Write-Host "    Install Python 3.11 or newer from:"
    Write-Host "    https://www.python.org/downloads/" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "    IMPORTANT: on the first install screen, check"
    Write-Host "    'Add python.exe to PATH' at the bottom. You can use the"
    Write-Host "    'Install for this user only' option (no admin rights needed)."
    exit 1
}

$pyVersion = Get-PythonVersion
if ($null -eq $pyVersion -or $pyVersion -lt [version]"3.11.0") {
    Write-Fail "Python 3.11 or newer is required. Found: $pyVersion"
    Write-Host ""
    Write-Host "    Install a newer version from https://www.python.org/downloads/"
    exit 1
}

Write-Ok "Python $pyVersion found."

# ---- 2. Check Node ----

Write-Step "Checking Node.js..."

if (-not (Test-Command "node")) {
    Write-Fail "Node.js is not installed (or not on PATH)."
    Write-Host ""
    Write-Host "    Install Node.js 20 LTS or newer from:"
    Write-Host "    https://nodejs.org/" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "    The standard installer may require admin rights. If it does,"
    Write-Host "    use 'fnm' instead, which installs per-user:"
    Write-Host "    https://github.com/Schniz/fnm" -ForegroundColor Cyan
    exit 1
}

$nodeVersion = Get-NodeVersion
if ($null -eq $nodeVersion -or $nodeVersion -lt [version]"18.0.0") {
    Write-Fail "Node.js 18 or newer is required. Found: $nodeVersion"
    exit 1
}

if ($nodeVersion -lt [version]"20.0.0") {
    Write-Warn "Node $nodeVersion found. Node 20+ is recommended but 18 should work."
} else {
    Write-Ok "Node.js $nodeVersion found."
}

# ---- 3. Set up Python virtual environment ----

Write-Step "Setting up Python environment..."

$venvDir = Join-Path $projectRoot "backend\.venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPip = Join-Path $venvDir "Scripts\pip.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "    Creating virtual environment at backend\.venv ..."
    & python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to create virtual environment."
        exit 1
    }
    Write-Ok "Virtual environment created."
} else {
    Write-Ok "Virtual environment already exists."
}

# Install or update backend deps. We use a marker file so we skip this on
# subsequent runs unless pyproject.toml has changed.
$backendMarker = Join-Path $venvDir ".devteam-installed"
$pyprojectPath = Join-Path $projectRoot "backend\pyproject.toml"
$needInstall = $true
if (Test-Path $backendMarker) {
    $markerTime = (Get-Item $backendMarker).LastWriteTime
    $pyprojectTime = (Get-Item $pyprojectPath).LastWriteTime
    if ($markerTime -gt $pyprojectTime) {
        $needInstall = $false
    }
}

if ($needInstall) {
    Write-Host "    Installing backend dependencies (2-4 minutes first time)..."
    Push-Location (Join-Path $projectRoot "backend")
    try {
        & $venvPip install --quiet --upgrade pip
        & $venvPip install --quiet -e .
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "pip install failed."
            Pop-Location
            exit 1
        }
    } finally {
        Pop-Location
    }
    Set-Content -Path $backendMarker -Value "installed"
    Write-Ok "Backend dependencies installed."
} else {
    Write-Ok "Backend dependencies up to date."
}

# ---- 4. Install frontend dependencies ----

Write-Step "Setting up frontend..."

$frontendDir = Join-Path $projectRoot "frontend"
$nodeModulesDir = Join-Path $frontendDir "node_modules"
$frontendMarker = Join-Path $nodeModulesDir ".devteam-installed"
$packageJsonPath = Join-Path $frontendDir "package.json"
$needNpmInstall = $true
if (Test-Path $frontendMarker) {
    $markerTime = (Get-Item $frontendMarker).LastWriteTime
    $packageTime = (Get-Item $packageJsonPath).LastWriteTime
    if ($markerTime -gt $packageTime) {
        $needNpmInstall = $false
    }
}

if ($needNpmInstall) {
    Write-Host "    Installing frontend dependencies (2-4 minutes first time)..."
    Push-Location $frontendDir
    try {
        & npm.cmd install --no-audit --no-fund --loglevel=error
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "npm install failed."
            Pop-Location
            exit 1
        }
    } finally {
        Pop-Location
    }
    Set-Content -Path $frontendMarker -Value "installed"
    Write-Ok "Frontend dependencies installed."
} else {
    Write-Ok "Frontend dependencies up to date."
}

# ---- 5. Stop any previously-running Dev Team processes ----

function Stop-TrackedProcess {
    param([string]$PidFile)
    if (-not (Test-Path $PidFile)) { return }
    $oldPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($oldPid) {
        try {
            $p = Get-Process -Id $oldPid -ErrorAction Stop
            # Only kill if the process name looks like ours to avoid killing
            # some other process that coincidentally got this PID.
            if ($p.ProcessName -in @("python", "node", "uvicorn", "npm")) {
                Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
            }
        } catch {
            # Process no longer exists -- fine.
        }
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

Stop-TrackedProcess $backendPidFile
Stop-TrackedProcess $frontendPidFile

# ---- 6. Start backend ----

Write-Step "Starting backend..."

# Load .env if present, so ANTHROPIC_API_KEY and other config propagates to the
# backend subprocess. Lines like KEY=value; comments (#) and blank lines ignored.
# If the user already has ANTHROPIC_API_KEY set in their Windows env, that wins
# (we don't overwrite existing env vars).
$envFile = Join-Path $projectRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $eq = $line.IndexOf("=")
            if ($eq -gt 0) {
                $name = $line.Substring(0, $eq).Trim()
                $value = $line.Substring($eq + 1).Trim()
                # Strip surrounding quotes if any
                if ($value.StartsWith('"') -and $value.EndsWith('"')) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
                # Don't overwrite already-set env vars
                if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
                    [Environment]::SetEnvironmentVariable($name, $value, "Process")
                }
            }
        }
    }
    Write-Ok ".env loaded."
}

$backendArgs = @(
    "-m", "uvicorn",
    "app.main:app",
    "--host", "127.0.0.1",
    "--port", "8000"
)

# Start backend in a VISIBLE PowerShell window so the user can see live logs
# and kill it by closing the window. We pipe output through Tee-Object so logs
# also go to the file (handy for post-mortem) but the window is the primary UI.
# The window title makes it obvious which window to close.
$backendCmd = "`$Host.UI.RawUI.WindowTitle = 'Dev Team - Backend (close this window to stop)'; " +
              "Write-Host 'Dev Team Backend' -ForegroundColor Cyan; " +
              "Write-Host 'Close this window to stop the backend.' -ForegroundColor Yellow; " +
              "Write-Host ''; " +
              "& '$venvPython' $($backendArgs -join ' ') 2>&1 | Tee-Object -FilePath '$backendLogFile'"

$backendProc = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList @("-NoExit", "-Command", $backendCmd) `
    -WorkingDirectory (Join-Path $projectRoot "backend") `
    -PassThru

Set-Content -Path $backendPidFile -Value $backendProc.Id
Write-Ok "Backend started (PID $($backendProc.Id)) in its own window."

if (-not (Wait-ForUrl -Url "http://localhost:8000/health" -TimeoutSeconds 60)) {
    Write-Fail "Backend did not respond within 60 seconds."
    Write-Host ""
    Write-Host "    Check the log: $backendLogFile"
    Write-Host "    Last 20 lines:"
    if (Test-Path $backendLogFile) {
        Get-Content $backendLogFile -Tail 20 | ForEach-Object { Write-Host "      $_" }
    }
    if (Test-Path (Join-Path $runDir "backend.err.log")) {
        Write-Host "    Last 20 error lines:"
        Get-Content (Join-Path $runDir "backend.err.log") -Tail 20 | ForEach-Object { Write-Host "      $_" }
    }
    exit 1
}
Write-Ok "Backend is up at http://localhost:8000"

# ---- 7. Start frontend (dev mode - fast startup, no build step) ----

Write-Step "Starting frontend..."

$frontendCmd = "`$Host.UI.RawUI.WindowTitle = 'Dev Team - Frontend (close this window to stop)'; " +
               "Write-Host 'Dev Team Frontend' -ForegroundColor Cyan; " +
               "Write-Host 'Close this window to stop the frontend.' -ForegroundColor Yellow; " +
               "Write-Host ''; " +
               "& npm.cmd run dev 2>&1 | Tee-Object -FilePath '$frontendLogFile'"

$frontendProc = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList @("-NoExit", "-Command", $frontendCmd) `
    -WorkingDirectory $frontendDir `
    -PassThru

Set-Content -Path $frontendPidFile -Value $frontendProc.Id
Write-Ok "Frontend started (PID $($frontendProc.Id)) in its own window."

if (-not (Wait-ForUrl -Url "http://localhost:3939" -TimeoutSeconds 60)) {
    Write-Fail "Frontend did not respond within 60 seconds."
    Write-Host "    Check the log: $frontendLogFile"
    if (Test-Path $frontendLogFile) {
        Get-Content $frontendLogFile -Tail 20 | ForEach-Object { Write-Host "      $_" }
    }
    exit 1
}
Write-Ok "Frontend is up at http://localhost:3939"

# ---- 8. Browser ----
# Vite auto-opens the browser via `open: true` in vite.config.ts. That's more
# reliable than launching from PowerShell because it fires inside Vite once the
# server is actually ready — no timing guesses, no process-parent issues.

Write-Host ""
Write-Host "================================================================"
Write-Host " Dev Team is running. " -ForegroundColor Black -BackgroundColor Green
Write-Host "================================================================"
Write-Host ""
Write-Host "  Web UI:  http://localhost:3939"
Write-Host "  Backend: http://localhost:8000"
Write-Host ""
Write-Host "  Two terminal windows opened -- one for backend, one for frontend."
Write-Host "  Live logs stream in each. To stop Dev Team, close BOTH windows"
Write-Host "  (or run Stop Dev Team.bat from the project folder)."
Write-Host ""
Write-Host "  This launcher window can be closed safely; the two process"
Write-Host "  windows keep running."
Write-Host ""

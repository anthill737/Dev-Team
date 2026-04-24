# Dev Team stop script.
#
# Reads PID files from .devteam-run/ and stops the backend and frontend
# processes cleanly. Your project data is preserved.

$ErrorActionPreference = "Continue"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $projectRoot ".devteam-run"

Write-Host ""
Write-Host "Stopping Dev Team..."
Write-Host ""

function Stop-ByPidFile {
    param(
        [string]$PidFile,
        [string]$Label
    )
    if (-not (Test-Path $PidFile)) {
        Write-Host "  $Label`: no PID file -- already stopped." -ForegroundColor DarkGray
        return
    }
    $pidValue = Get-Content $PidFile -ErrorAction SilentlyContinue
    if (-not $pidValue) {
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    try {
        $p = Get-Process -Id $pidValue -ErrorAction Stop
        # Also stop child processes (npm spawns node; uvicorn is the python process itself).
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$pidValue" -ErrorAction SilentlyContinue
        if ($children) {
            foreach ($c in $children) {
                try { Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
            }
        }
        Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
        Write-Host "  $Label`: stopped (PID $pidValue)." -ForegroundColor Green
    } catch {
        Write-Host "  $Label`: process $pidValue was not running." -ForegroundColor DarkGray
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

Stop-ByPidFile (Join-Path $runDir "backend.pid") "Backend"
Stop-ByPidFile (Join-Path $runDir "frontend.pid") "Frontend"

# Belt-and-suspenders: anything still listening on 8000 or 3939 that we spawned
# should be cleaned up. Skip if the ports are held by totally unrelated processes.
function Stop-PortOwner {
    param(
        [int]$Port,
        [string]$ExpectedProcessName
    )
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            try {
                $p = Get-Process -Id $c.OwningProcess -ErrorAction Stop
                if ($p.ProcessName -like "$ExpectedProcessName*") {
                    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                }
            } catch {}
        }
    } catch {}
}

Stop-PortOwner 8000 "python"
Stop-PortOwner 3939 "node"

Write-Host ""
Write-Host "Dev Team stopped. Your project data is preserved." -ForegroundColor Green
Write-Host ""

<#
.SYNOPSIS
    Start all long-running simulated-trading services.

.DESCRIPTION
    Uses the active Conda environment (when available) or python on PATH.
    Standard output/error goes to .runtime\logs and process metadata to .runtime\pids.
    A repeated invocation skips healthy processes started by this script.

    Daily settlement is not a resident service. Order rebuilding and migrations are opt-in.

.EXAMPLE
    .\scripts\start_all.ps1

.EXAMPLE
    .\scripts\start_all.ps1 -RunMigrations -RebuildActiveOrders
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$ApiHost = "127.0.0.1",
    [int]$ApiPort = 8000,
    [switch]$RunMigrations,
    [switch]$RebuildActiveOrders,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$LogDirectory = Join-Path $RuntimeRoot "logs"
$PidDirectory = Join-Path $RuntimeRoot "pids"

foreach ($directory in @($RuntimeRoot, $LogDirectory, $PidDirectory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

function Resolve-PythonExecutable {
    param([string]$RequestedPython)

    if ($RequestedPython) {
        if (-not (Test-Path -LiteralPath $RequestedPython -PathType Leaf)) {
            throw "Requested Python executable does not exist: $RequestedPython"
        }
        return (Resolve-Path -LiteralPath $RequestedPython).Path
    }

    if ($env:CONDA_PREFIX) {
        $condaPython = Join-Path $env:CONDA_PREFIX "python.exe"
        if (Test-Path -LiteralPath $condaPython -PathType Leaf) {
            return $condaPython
        }
    }

    # This project conventionally uses a Conda environment named sim_trade_env.
    # Discover it from the first Conda-style Python on PATH when the shell is not activated.
    $pythonCommands = @(Get-Command python -CommandType Application -All -ErrorAction Stop)
    $firstCondaPython = $pythonCommands | Where-Object {
        $_.Source -match "[\\/]envs[\\/]"
    } | Select-Object -First 1
    if ($null -ne $firstCondaPython) {
        $environmentRoot = Split-Path (Split-Path $firstCondaPython.Source -Parent) -Parent
        $projectPython = Join-Path $environmentRoot "sim_trade_env\python.exe"
        if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
            return $projectPython
        }
    }

    return ($pythonCommands | Select-Object -First 1).Source
}

function Test-ManagedProcessRunning {
    param([string]$Name)

    $pidFile = Join-Path $PidDirectory "$Name.json"
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        return $false
    }

    try {
        $record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
        if ($null -ne $process -and $process.StartTime.ToUniversalTime().ToString("o") -eq $record.started_at_utc) {
            return $true
        }
    }
    catch {
        # A stale/corrupted PID record (or a reused PID) is treated as not running.
    }

    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    return $false
}

function Start-ManagedProcess {
    param(
        [string]$Name,
        [string[]]$Arguments
    )

    if (Test-ManagedProcessRunning -Name $Name) {
        Write-Host "[skip] $Name is already running"
        return
    }

    $commandPreview = "$PythonPath " + ($Arguments -join " ")
    if ($DryRun) {
        Write-Host "[dry-run] $Name -> $commandPreview"
        return
    }

    $stdoutLog = Join-Path $LogDirectory "$Name.out.log"
    $stderrLog = Join-Path $LogDirectory "$Name.err.log"
    $process = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList $Arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    $record = [ordered]@{
        name = $Name
        pid = $process.Id
        started_at_utc = $process.StartTime.ToUniversalTime().ToString("o")
        command = $commandPreview
    } | ConvertTo-Json
    Set-Content -LiteralPath (Join-Path $PidDirectory "$Name.json") -Value $record -Encoding utf8
    Write-Host "[started] $Name (PID $($process.Id))"
}

$PythonPath = Resolve-PythonExecutable -RequestedPython $PythonExe
Write-Host "Project root: $ProjectRoot"
Write-Host "Python: $PythonPath"

Push-Location $ProjectRoot
try {
    if ($RunMigrations) {
        if ($DryRun) {
            Write-Host "[dry-run] database migration -> $PythonPath -m alembic upgrade head"
        }
        else {
            Write-Host "[run] database migration"
            & $PythonPath -m alembic upgrade head
            if ($LASTEXITCODE -ne 0) {
                throw "Database migration failed with exit code: $LASTEXITCODE"
            }
        }
    }

    if ($RebuildActiveOrders) {
        if ($DryRun) {
            Write-Host "[dry-run] active order rebuild -> $PythonPath -m app.workers.active_order_rebuild_worker"
        }
        else {
            Write-Host "[run] active order rebuild"
            & $PythonPath -m app.workers.active_order_rebuild_worker
            if ($LASTEXITCODE -ne 0) {
                throw "Active order rebuild failed with exit code: $LASTEXITCODE"
            }
        }
    }

    # Establish producers before consumers to minimize startup event gaps.
    $services = @(
        @{ Name = "api"; Arguments = @("-m", "uvicorn", "app.main:app", "--host", $ApiHost, "--port", "$ApiPort", "--no-access-log") },
        @{ Name = "outbox-publisher"; Arguments = @("-m", "app.workers.outbox_publisher_worker") },
        @{ Name = "order-event-consumer"; Arguments = @("-m", "app.workers.order_event_consumer_worker") },
        @{ Name = "futures-market-data"; Arguments = @("-m", "app.workers.futures_market_data_worker") },
        @{ Name = "securities-market-data"; Arguments = @("-m", "app.workers.securities_market_data_worker") },
        @{ Name = "futures-matching"; Arguments = @("-m", "app.workers.futures_matching_worker") },
        @{ Name = "securities-matching"; Arguments = @("-m", "app.workers.securities_matching_worker") },
        @{ Name = "trade-event-pnl"; Arguments = @("-m", "app.workers.trade_event_pnl_worker") },
        @{ Name = "realtime-pnl"; Arguments = @("-m", "app.workers.realtime_pnl_worker") },
        @{ Name = "pnl-snapshot-persistence"; Arguments = @("-m", "app.workers.pnl_snapshot_persistence_worker") },
        @{ Name = "cash-valuation-tick"; Arguments = @("-m", "app.workers.run_cash_security_valuation_tick_worker") },
        @{ Name = "cash-valuation-fact"; Arguments = @("-m", "app.workers.run_cash_security_valuation_fact_worker") },
        @{ Name = "cash-valuation-persistence"; Arguments = @("-m", "app.workers.run_cash_security_valuation_persistence_worker") },
        @{ Name = "risk-monitor"; Arguments = @("-m", "app.workers.risk_monitor_worker") },
        @{ Name = "realtime-event-projection"; Arguments = @("-m", "app.workers.realtime_event_projection_worker") },
        # Gateway address and port come from WS_GATEWAY_HOST / WS_GATEWAY_PORT in .env.
        @{ Name = "websocket-gateway"; Arguments = @("-m", "app.scripts.run_websocket_gateway") }
    )

    foreach ($service in $services) {
        Start-ManagedProcess -Name $service.Name -Arguments $service.Arguments
        if (-not $DryRun) {
            Start-Sleep -Milliseconds 250
        }
    }
}
finally {
    Pop-Location
}

if ($DryRun) {
    Write-Host "Dry-run complete. No processes were started."
}
else {
    Write-Host "Processes were started. Check $LogDirectory for *.err.log health failures."
}

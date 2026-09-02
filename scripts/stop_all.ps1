<#
.SYNOPSIS
    Stop resident processes recorded by scripts\start_all.ps1.
#>
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PidDirectory = Join-Path $ProjectRoot ".runtime\pids"
$ControlDirectory = Join-Path $ProjectRoot ".runtime\control"
$GracefulStopTimeoutSeconds = 20

function Stop-MarketDataSubscriberGracefully {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$ProcessId,
        [string]$Name
    )

    New-Item -ItemType Directory -Path $ControlDirectory -Force | Out-Null
    $requestPath = Join-Path $ControlDirectory "market-data-subscriber.$ProcessId.stop"
    Set-Content -LiteralPath $requestPath -Value "stop" -Encoding ascii

    try {
        $deadline = [DateTime]::UtcNow.AddSeconds($GracefulStopTimeoutSeconds)
        while (-not $Process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 200
            $Process.Refresh()
        }

        if ($Process.HasExited) {
            Write-Host "[stopped] $Name (PID $ProcessId, graceful)"
            return
        }

        Write-Warning (
            "$Name (PID $ProcessId) did not exit within " +
            "$GracefulStopTimeoutSeconds seconds; forcing termination"
        )
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        Write-Host "[stopped] $Name (PID $ProcessId, forced)"
    }
    finally {
        Remove-Item -LiteralPath $requestPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $PidDirectory -PathType Container)) {
    Write-Host "No script-managed processes were found."
    exit 0
}

Get-ChildItem -LiteralPath $PidDirectory -Filter "*.json" -File | ForEach-Object {
    try {
        $record = Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
        if ($null -ne $process -and $process.StartTime.ToUniversalTime().ToString("o") -eq $record.started_at_utc) {
            if ($PSCmdlet.ShouldProcess("$($record.name) (PID $($record.pid))", "Stop")) {
                if ($record.name -in @("futures-market-data", "securities-market-data")) {
                    Stop-MarketDataSubscriberGracefully `
                        -Process $process `
                        -ProcessId ([int]$record.pid) `
                        -Name $record.name
                }
                else {
                    Stop-Process -Id ([int]$record.pid) -ErrorAction Stop
                    Write-Host "[stopped] $($record.name) (PID $($record.pid))"
                }
            }
        }
    }
    catch {
        Write-Warning "Unable to stop $($_.Name): $($_.Exception.Message)"
    }
    finally {
        Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

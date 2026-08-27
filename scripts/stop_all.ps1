<#
.SYNOPSIS
    Stop resident processes recorded by scripts\start_all.ps1.
#>
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PidDirectory = Join-Path $ProjectRoot ".runtime\pids"

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
                Stop-Process -Id ([int]$record.pid) -ErrorAction Stop
                Write-Host "[stopped] $($record.name) (PID $($record.pid))"
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

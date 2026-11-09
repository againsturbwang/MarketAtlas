# Start the EastMoney monitor (collector + dashboard) in the background.
#   .\scripts\start.ps1            -> http://127.0.0.1:8787
#   .\scripts\start.ps1 -Port 9000
param([int]$Port = 8787)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*run.py run*' }
if ($existing) {
    Write-Host "already running (pid $($existing.ProcessId -join ','))"
    exit 0
}

$out = Join-Path $root 'logs\run.out'
$err = Join-Path $root 'logs\run.err'
Start-Process -FilePath 'python' `
    -ArgumentList 'run.py', 'run', '--port', $Port `
    -WorkingDirectory $root `
    -RedirectStandardOutput $out -RedirectStandardError $err `
    -WindowStyle Hidden

Start-Sleep -Seconds 3
try {
    $health = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 "http://127.0.0.1:$Port/api/health").Content
    Write-Host "running on http://127.0.0.1:$Port  ($health)"
} catch {
    Write-Host "failed to start - see $err"
    Get-Content $err -Tail 15
    exit 1
}

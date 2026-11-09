# Stop the EastMoney monitor.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*run.py run*' -or $_.CommandLine -like '*run.py serve*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
        Write-Host "stopped pid $($_.ProcessId)"
    }

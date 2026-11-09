# Restarts the POS API (port 8001) and the queue worker so they run the latest code.
# Run it yourself: right-click this file > "Run with PowerShell" (or: powershell -File restart-pos-servers.ps1).

$backend = Join-Path $PSScriptRoot 'backend'
$py = Join-Path $backend 'venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    $py = 'python'
}

$old = Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match 'manage\.py' -and ($_.CommandLine -match 'runserver 127\.0\.0\.1:8001' -or $_.CommandLine -match 'run_workers'))
}
foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# Settings the API runs with
$env:SAP_ENABLED = 'False'
$env:SAP_SCHEDULER = 'False'
$env:DJANGO_DEBUG = 'True'
$env:BRAND_NAME = 'Aura Salon & Spa'

Start-Process -FilePath $py -ArgumentList 'manage.py', 'runserver', '127.0.0.1:8001', '--noreload' `
    -WorkingDirectory $backend -WindowStyle Hidden `
    -RedirectStandardOutput "$env:TEMP\pos-api.out.log" -RedirectStandardError "$env:TEMP\pos-api.err.log"

Start-Sleep -Seconds 4
$up = (netstat -ano | Select-String ':8001 .*LISTENING')
if ($up) { Write-Host 'API is running on 127.0.0.1:8001. Reload the browser (Ctrl+F5).' }
else { Write-Host 'The API did not start. See' "$env:TEMP\pos-api.err.log" }

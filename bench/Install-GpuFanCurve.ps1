# Install-GpuFanCurve.ps1 — install a custom NVIDIA GPU fan curve via nvfancontrol,
# persistent at logon (scheduled task, highest privileges).
#
# Default curve is more aggressive than the silent stock curve
# (raises idle/high-temp fan speed to fight heat and VRAM hotspot).
#
# Usage:
#   .\Install-GpuFanCurve.ps1                       # default curve + logon task
#   .\Install-GpuFanCurve.ps1 -Curve '40:40,55:55,70:75,80:95'
#   .\Install-GpuFanCurve.ps1 -Uninstall            # remove task + restore auto fan
param(
    [string]$Curve = '40:45,50:55,60:70,68:80,75:92,82:100',
    [int]$Gpu = 0,
    [string]$InstallDir = 'C:\ProgramData\nvfancontrol',
    [switch]$Uninstall,
    [switch]$NoTask
)

function Elevate([string]$file, [string]$args) {
    Start-Process $file -ArgumentList $args -Verb RunAs -Wait
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName 'NVIDIA GPU Fan Curve' -Confirm:$false -ErrorAction SilentlyContinue
    Get-Process nvfancontrol -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    "Removed task + stopped nvfancontrol (reboot for stock fan control, or run nvapi-cmd -fan $Gpu 0 -1)."
    exit 0
}

# 1) fetch nvfancontrol (RTX build)
$zip = Join-Path $env:TEMP 'nvfancontrol-rtx.zip'
if (-not (Test-Path (Join-Path $InstallDir 'nvfancontrol.exe'))) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $url = 'https://github.com/foucault/nvfancontrol/releases/download/0.5.1/nvfancontrol-rtx-win32-x64.zip'
    Write-Host "[fan] downloading nvfancontrol ..."
    curl.exe -L --fail -o $zip $url | Out-Null
    Expand-Archive $zip -DestinationPath $InstallDir -Force
}
$exe = Join-Path $InstallDir 'nvfancontrol.exe'
if (-not (Test-Path $exe)) { Write-Error "nvfancontrol.exe not found"; exit 1 }

# 2) write curve config (pairs "temp speed" per line) to %APPDATA%
$cfg = ($Curve -split ',' | ForEach-Object { $p = $_ -split ':'; "$($p[0])    $($p[1])" }) -join "`r`n"
Set-Content -Path "$env:APPDATA\nvfancontrol.conf" -Value $cfg -Encoding ASCII
Write-Host "[fan] curve -> $env:APPDATA\nvfancontrol.conf"
Get-Content "$env:APPDATA\nvfancontrol.conf"

# 3) launcher + logon task (highest privileges)
$cmd = Join-Path $InstallDir 'run.cmd'
Set-Content -Path $cmd -Value "@echo off`r`n`"$exe`" -f -l 0,100 -g $Gpu" -Encoding ASCII
if (-not $NoTask) {
    $task = @"
`$action = New-ScheduledTaskAction -Execute '$cmd'
`$trigger = New-ScheduledTaskTrigger -AtLogOn
`$principal = New-ScheduledTaskPrincipal -UserId "`$env:USERDOMAIN\`$env:USERNAME" -LogonType Interactive -RunLevel Highest
`$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName 'NVIDIA GPU Fan Curve' -Action `$action -Trigger `$trigger -Principal `$principal -Settings `$settings -Force | Out-Null
Start-ScheduledTask -TaskName 'NVIDIA GPU Fan Curve'
"@
    $tmp = Join-Path $env:TEMP 'install-fancurve.ps1'
    Set-Content -Path $tmp -Value $task -Encoding UTF8
    Elevate 'powershell' "-NoProfile -ExecutionPolicy Bypass -File `"$tmp`""
}

Start-Sleep -Seconds 6
"task: $((Get-ScheduledTask -TaskName 'NVIDIA GPU Fan Curve' -ErrorAction SilentlyContinue).State)"
"process: $((Get-Process nvfancontrol -ErrorAction SilentlyContinue | Measure-Object).Count)"
& nvidia-smi --query-gpu=temperature.gpu,fan.speed --format=csv

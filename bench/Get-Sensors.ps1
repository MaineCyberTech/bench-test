# Get-Sensors.ps1 - dump all hardware sensors (temperatures, fans, voltages, power, loads,
# clocks) for CPU / motherboard / GPU / storage via the LibreHardwareMonitor library.
#
# Run ELEVATED for the most complete readings (SMBus/EC access). Downloads the library on
# first use. Usage: .\Get-Sensors.ps1 [-Json] [-OutFile <path>]
param(
    [switch]$Json,
    [string]$OutFile,
    [string]$LibDir = "$PSScriptRoot\..\tools\lhm"
)
$ErrorActionPreference = 'Continue'
$dll = Join-Path $LibDir 'LibreHardwareMonitorLib.dll'
if (-not (Test-Path $dll)) {
    # Pin the release and verify its SHA-256 before loading the DLL in-process.
    # A vendored copy under tools\lhm (or a caller-supplied -LibDir) skips this
    # entirely, so offline machines are unaffected. BENCH_LHM_URL may point at a
    # mirror of the SAME pinned artifact (it must match the hash).
    $lhmVersion = 'v0.9.6'
    $lhmSha256 = '086D9F1B5A99E643EDC2CFAAAC16051685B551E4C5AC0B32A57C58C0E529C001'
    $url = if ($env:BENCH_LHM_URL) { $env:BENCH_LHM_URL } else {
        "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/$lhmVersion/LibreHardwareMonitor.zip"
    }
    New-Item -ItemType Directory -Force -Path $LibDir | Out-Null
    $zip = Join-Path $env:TEMP 'lhm.zip'
    Write-Host "[sensors] downloading LibreHardwareMonitor $lhmVersion ..."
    & curl.exe -L --fail -o $zip $url | Out-Null
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash
    if (-not $got -or $got.ToUpperInvariant() -ne $lhmSha256) {
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        throw "LibreHardwareMonitor.zip failed SHA-256 verification (expected $lhmSha256, got $got) - refusing to load it"
    }
    Expand-Archive $zip -DestinationPath $LibDir -Force
}
if (-not (Test-Path $dll)) { throw "LibreHardwareMonitorLib.dll not found in $LibDir" }

Add-Type -Path $dll -ErrorAction Stop
$c = New-Object LibreHardwareMonitor.Hardware.Computer
$c.IsCpuEnabled = $true; $c.IsGpuEnabled = $true; $c.IsMemoryEnabled = $true
$c.IsMotherboardEnabled = $true; $c.IsControllerEnabled = $true
$c.IsStorageEnabled = $true; $c.IsNetworkEnabled = $true; $c.IsPsuEnabled = $true
$c.Open()
Start-Sleep -Seconds 2

$sensors = New-Object System.Collections.ArrayList
function Walk($hw, $parent) {
    $hw.Update()
    $label = if ($parent) { "$parent / $($hw.Name)" } else { $hw.Name }
    foreach ($s in $hw.Sensors) {
        [void]$sensors.Add([ordered]@{
            hardware = $label; hardwareType = "$($hw.HardwareType)"
            type = "$($s.SensorType)"; name = $s.Name
            value = if ($null -ne $s.Value) { [math]::Round([double]$s.Value, 3) } else { $null }
            identifier = $s.Identifier.ToString()
        })
    }
    foreach ($sub in $hw.SubHardware) { Walk $sub $label }
}
foreach ($hw in $c.Hardware) { Walk $hw $null }
$c.Close()

$out = [ordered]@{ host = $env:COMPUTERNAME; timestamp = (Get-Date).ToString('o'); elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); sensors = $sensors }
if ($OutFile) { $out | ConvertTo-Json -Depth 6 | Set-Content $OutFile -Encoding UTF8 }
if ($Json) { $out | ConvertTo-Json -Depth 6 } else {
    "Hardware sensors ($($sensors.Count)) [elevated=$($out.elevated)]:"
    foreach ($s in $sensors) {
        if ($s.type -in 'Temperature', 'Fan', 'Control', 'Voltage', 'Power', 'Load', 'Clock', 'Level') {
            "{0,-42} {1,-12} {2,-26} {3}" -f $s.hardware, $s.type, $s.name, $s.value
        }
    }
}

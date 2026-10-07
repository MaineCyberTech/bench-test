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
    New-Item -ItemType Directory -Force -Path $LibDir | Out-Null
    $zip = Join-Path $env:TEMP 'lhm.zip'
    Write-Host "[sensors] downloading LibreHardwareMonitor ..."
    & curl.exe -L --fail -o $zip 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest/download/LibreHardwareMonitor.zip' | Out-Null
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

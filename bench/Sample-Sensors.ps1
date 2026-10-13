# Sample-Sensors.ps1 - 1 Hz hardware-sensor stream via LibreHardwareMonitor.
#
# Prints one "key=value key=value ..." line per interval so a runner can fold GPU
# temperature/power/clocks/utilisation/VRAM and CPU temperature into its telemetry
# CSV on machines without nvidia-smi (AMD/Intel GPUs on Windows). Best-effort:
# a sensor the library cannot read comes out blank, never zero.
#
# Usage: .\Sample-Sensors.ps1 [-IntervalMs 1000] [-Lines N] [-LibDir <dir>]
#   (Lines 0 = run until killed; the runner stops it.)
param(
    [int]$IntervalMs = 1000,
    [int]$Lines = 0,
    [string]$LibDir = "$PSScriptRoot\..\tools\lhm"
)
$ErrorActionPreference = 'Continue'

$dll = Join-Path $LibDir 'LibreHardwareMonitorLib.dll'
if (-not (Test-Path $dll)) {
    New-Item -ItemType Directory -Force -Path $LibDir | Out-Null
    $zip = Join-Path $env:TEMP 'lhm.zip'
    try {
        & curl.exe -L --fail -o $zip 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest/download/LibreHardwareMonitor.zip' 2>$null | Out-Null
        if (Test-Path $zip) { Expand-Archive $zip -DestinationPath $LibDir -Force }
    } catch { }
}
if (-not (Test-Path $dll)) { Write-Output 'error=no-lhm'; exit 3 }

Add-Type -Path $dll -ErrorAction Stop
$computer = New-Object LibreHardwareMonitor.Hardware.Computer
$computer.IsCpuEnabled = $true
$computer.IsGpuEnabled = $true
$computer.IsMemoryEnabled = $true
$computer.Open()

function Get-First($hardware, $type, $patterns) {
    foreach ($p in $patterns) {
        foreach ($hw in $hardware) {
            $hw.Update()
            $all = @($hw.Sensors) + @($hw.SubHardware | ForEach-Object { $_.Update(); $_.Sensors })
            foreach ($s in $all) {
                if ("$($s.SensorType)" -eq $type -and $s.Name -match $p -and $null -ne $s.Value) {
                    return [double]$s.Value
                }
            }
        }
    }
    return $null
}
function Fmt($v, $dec) {
    if ($null -eq $v) { return '' }
    return [math]::Round([double]$v, $dec)
}

$n = 0
while ($true) {
    try {
        $gpus = @($computer.Hardware | Where-Object { "$($_.HardwareType)" -like 'Gpu*' })
        $cpus = @($computer.Hardware | Where-Object { "$($_.HardwareType)" -eq 'Cpu' })

        $g_temp = Get-First $gpus 'Temperature' @('GPU Core', 'Hot ?Spot', 'GPU VR', 'Edge')
        $g_power = Get-First $gpus 'Power' @('GPU Core', 'Package', 'GPU')
        $g_clk = Get-First $gpus 'Clock' @('GPU Core', 'Core')
        $g_mclk = Get-First $gpus 'Clock' @('GPU Memory', 'Memory')
        $g_util = Get-First $gpus 'Load' @('GPU Core', 'D3D 3D')
        $g_vram = Get-First $gpus 'SmallData' @('GPU Memory Used')
        $g_fan = Get-First $gpus 'Fan' @('.')
        $g_fanp = Get-First $gpus 'Control' @('GPU Fan', 'Fan')
        $c_temp = Get-First $cpus 'Temperature' @('Core \(Tctl/Tdie\)', 'CPU Package', 'CCD', 'Core')
        $c_power = Get-First $cpus 'Power' @('Package')

        '{0} line={1} gpu_temp={2} gpu_power={3} gpu_clk={4} mem_clk={5} gpu_util={6} vram_used={7} gpu_fan_rpm={8} gpu_fan_pct={9} cpu_temp={10} cpu_power={11}' -f @(
            ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()),
            $n,
            (Fmt $g_temp 1), (Fmt $g_power 1), (Fmt $g_clk 0), (Fmt $g_mclk 0),
            (Fmt $g_util 1), (Fmt $g_vram 0), (Fmt $g_fan 0), (Fmt $g_fanp 0),
            (Fmt $c_temp 1), (Fmt $c_power 1)
        ) | Write-Output
    } catch {
        Write-Output ("{0} line={1} error={2}" -f ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()), $n, ($_.Exception.Message -replace '\s+', '_'))
    }
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds $IntervalMs
    $n++
    if ($Lines -gt 0 -and $n -ge $Lines) { break }
}
$computer.Close()

# Sample-Sensors.ps1 - 1 Hz hardware-sensor stream via LibreHardwareMonitor.
#
# Prints one "key=value key=value ..." line per interval so a runner can fold GPU
# and CPU telemetry into its CSV on machines without nvidia-smi (AMD/Intel GPUs,
# handhelds, laptops). Best-effort and additive: a sensor the library cannot read
# comes out blank, never a fake zero, and a key the runner does not know is
# ignored - so this stays safe to extend.
#
# Values never contain spaces (a name is sanitised to underscores), because the
# runner splits the line on whitespace.
#
# Usage: .\Sample-Sensors.ps1 [-IntervalMs 1000] [-Lines N] [-LibDir <dir>] [-ParentPid <pid>]
#   (Lines 0 = run until killed; the runner stops it.)
#   -ParentPid: exit as soon as that process is gone, so a runner killed abruptly
#   (e.g. the dashboard window closed mid-run) cannot leave this sampler holding
#   the LibreHardwareMonitor dll and the toolkit folder.
param(
    [int]$IntervalMs = 1000,
    [int]$Lines = 0,
    [string]$LibDir = "$PSScriptRoot\..\tools\lhm",
    [int]$ParentPid = 0
)
$ErrorActionPreference = 'Continue'

function Find-Lhm {
    # already-vendored copies first, so an offline machine still collects: the
    # bundle ships tools\lhm next to the exe and the repo keeps it under tools\.
    $cands = @($LibDir,
               (Join-Path $PSScriptRoot '..\tools\lhm'),
               (Join-Path $PSScriptRoot 'tools\lhm'))
    foreach ($d in $cands) {
        if ($d -and (Test-Path (Join-Path $d 'LibreHardwareMonitorLib.dll'))) { return $d }
    }
    $dest = $LibDir
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    # Pin the release and verify its SHA-256 before extracting the DLL. A mirror
    # (BENCH_LHM_URL) may be used instead, but must serve the same pinned artifact.
    $lhmVersion = 'v0.9.6'
    $lhmSha256 = '086D9F1B5A99E643EDC2CFAAAC16051685B551E4C5AC0B32A57C58C0E529C001'
    $urls = @()
    if ($env:BENCH_LHM_URL) { $urls += $env:BENCH_LHM_URL }
    $urls += "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/$lhmVersion/LibreHardwareMonitor.zip"
    $zip = Join-Path $env:TEMP 'lhm.zip'
    foreach ($u in $urls) {
        try {
            & curl.exe -L --fail --retry 2 --retry-delay 1 --max-time 90 -o $zip $u 2>$null | Out-Null
            if (-not (Test-Path $zip)) { continue }
            $got = (Get-FileHash $zip -Algorithm SHA256).Hash
            if (-not $got -or $got.ToUpperInvariant() -ne $lhmSha256) {
                Remove-Item $zip -Force -ErrorAction SilentlyContinue
                continue
            }
            $tmp = Join-Path $env:TEMP ('lhmx_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
            Expand-Archive $zip -DestinationPath $tmp -Force
            $dll = Get-ChildItem $tmp -Recurse -Filter 'LibreHardwareMonitorLib.dll' |
                   Select-Object -First 1
            if ($dll) {
                Copy-Item (Join-Path $dll.DirectoryName '*') $dest -Force -Recurse
                Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
                if (Test-Path (Join-Path $dest 'LibreHardwareMonitorLib.dll')) { return $dest }
            }
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        } catch { }
    }
    return $null
}

$libDir = Find-Lhm
if (-not $libDir) { Write-Output 'error=no-lhm'; exit 3 }
$dll = Join-Path $libDir 'LibreHardwareMonitorLib.dll'
try {
    Add-Type -Path $dll -ErrorAction Stop
} catch {
    Write-Output ('error=lhm-load:' + (($_.Exception.Message -replace '\s+', '_')))
    exit 4
}

$isAdmin = $false
try {
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
                [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { }

$computer = New-Object LibreHardwareMonitor.Hardware.Computer
$computer.IsCpuEnabled = $true
$computer.IsGpuEnabled = $true
$computer.IsMemoryEnabled = $true
$computer.IsStorageEnabled = $true
try { $computer.Open() } catch {
    Write-Output ('error=lhm-open:' + (($_.Exception.Message -replace '\s+', '_')))
    exit 5
}

function San($s) { return ($s -replace '\s+', '_') }

# Update every piece of hardware exactly once per interval, then read the flat
# sensor list. The old shape called Update() per pattern per device, which made a
# line far slower than the interval it was trying to keep.
function Update-All {
    foreach ($hw in $computer.Hardware) {
        try { $hw.Update() } catch { }
        foreach ($sub in @($hw.SubHardware)) {
            try { $sub.Update() } catch { }
        }
    }
}

function Sensors-Of($hwtype) {
    $out = @()
    foreach ($hw in $computer.Hardware) {
        if ("$($hw.HardwareType)" -notlike $hwtype) { continue }
        $out += @($hw.Sensors)
        foreach ($sub in @($hw.SubHardware)) { $out += @($sub.Sensors) }
    }
    return $out
}

function Pick($sensors, $type, $patterns) {
    foreach ($p in $patterns) {
        foreach ($s in $sensors) {
            if ("$($s.SensorType)" -eq $type -and $s.Name -match $p -and $null -ne $s.Value) {
                return [double]$s.Value
            }
        }
    }
    return $null
}

function Pick-Hardware($hwtype, $patterns) {
    foreach ($p in $patterns) {
        foreach ($hw in $computer.Hardware) {
            if ("$($hw.HardwareType)" -like $hwtype -and $hw.Name -match $p) { return $hw.Name }
        }
    }
    return $null
}

# A reading of exactly 0 for these is not physical (0 C, 0 W, 0 MHz), so it means
# "cannot read" and must go out blank rather than as a fake 0. A Load or a Fan of
# 0 is legitimate (idle, zero-RPM mode), so those are left alone.
function Fmt($v, $dec, $zeroBlank = $false) {
    if ($null -eq $v) { return '' }
    $d = [double]$v
    if ($zeroBlank -and $d -eq 0) { return '' }
    return [math]::Round($d, $dec)
}

$n = 0
while ($true) {
    if ($ParentPid -gt 0 -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
    try {
        Update-All
        $gpus = Sensors-Of 'Gpu*'
        $cpus = Sensors-Of 'Cpu'
        $mems = Sensors-Of 'Memory'
        $stors = Sensors-Of 'Storage'

        $g_temp = Pick $gpus 'Temperature' @('GPU Core', 'Hot ?Spot', 'GPU VR', 'Edge', 'GPU')
        $g_power = Pick $gpus 'Power' @('GPU Core', 'Package', 'GPU')
        $g_clk = Pick $gpus 'Clock' @('GPU Core', 'Core')
        $g_mclk = Pick $gpus 'Clock' @('GPU Memory', 'Memory')
        $g_util = Pick $gpus 'Load' @('GPU Core', 'D3D 3D', 'D3D High Priority 3D', 'GPU')
        $g_3d = Pick $gpus 'Load' @('D3D 3D')
        $g_copy = Pick $gpus 'Load' @('D3D Copy')
        $g_enc = Pick $gpus 'Load' @('D3D Video Codec', 'Video Codec')
        $g_dec = Pick $gpus 'Load' @('D3D Video JPEG', 'Video Decode', 'JPEG')
        $g_vram = Pick $gpus 'SmallData' @('GPU Memory Used')
        $g_vtot = Pick $gpus 'SmallData' @('GPU Memory Total', 'D3D Dedicated Memory Total')
        $g_fan = Pick $gpus 'Fan' @('.')
        $g_fanp = Pick $gpus 'Control' @('GPU Fan', 'Fan')
        # VRAM load, derived from the GPU's own memory figures: the Memory hardware's
        $g_memload = $null    # see the VRAM note below
        # 'Memory' load is SYSTEM RAM, not VRAM, so using it here made AMD/Intel
        # runs incomparable with NVIDIA (which gets true VRAM from nvidia-smi)
        if ($null -eq $g_memload -and $null -ne $g_vram -and $null -ne $g_vtot -and $g_vtot -gt 0) {
            $g_memload = 100.0 * $g_vram / $g_vtot
        }

        $c_temp = Pick $cpus 'Temperature' @('Core \(Tctl/Tdie\)', 'CPU Package', 'CCD', 'Core')
        $c_power = Pick $cpus 'Power' @('Package')
        $c_util = Pick $cpus 'Load' @('CPU Total')
        $c_max = Pick $cpus 'Load' @('CPU Core Max')
        $c_mhz = Pick $cpus 'Clock' @('Cores \(Average\)', 'Core Average', 'CPU Core')
        $c_volt = Pick $cpus 'Voltage' @('CPU Core', 'Core')

        # per-core loads (fills the per-core graphs). LHM names these "CPU Core #N"
        # on AMD and "CPU Core #N Thread #M" on Intel, so match both and average a
        # core's threads into one series.
        $coreVals = @{}
        foreach ($s in $cpus) {
            if ("$($s.SensorType)" -ne 'Load') { continue }
            if ($s.Name -match '^CPU Core #(\d+)(?: Thread #\d+)?$' -and $null -ne $s.Value) {
                $i = [int]$Matches[1]
                if (-not $coreVals.ContainsKey($i)) { $coreVals[$i] = @() }
                $coreVals[$i] += [double]$s.Value
            }
        }
        $coreStr = (($coreVals.Keys | Sort-Object) | ForEach-Object {
            'c{0}={1}' -f $_, (Fmt (($coreVals[$_] | Measure-Object -Average).Average) 1)
        }) -join ' '

        $s_temp = Pick $stors 'Temperature' @('.')
        $s_used = Pick $stors 'Load' @('Used Space', 'Total Activity')
        $s_act = Pick $stors 'Throughput' @('Total Activity', '.')

        $g_name = Pick-Hardware 'Gpu*' @('.')
        $c_name = Pick-Hardware 'Cpu' @('.')

        $line = '{0} line={1} elevated={2} gpu_temp={3} gpu_power={4} gpu_clk={5} mem_clk={6} ' +
                'gpu_util={7} gpu_3d={8} gpu_copy={9} gpu_enc={10} gpu_dec={11} ' +
                'vram_used={12} vram_total={13} mem_util={14} gpu_fan_rpm={15} gpu_fan_pct={16} ' +
                'cpu_temp={17} cpu_power={18} cpu_util={19} cpu_max_core={20} cpu_mhz={21} ' +
                'cpu_volt={22} storage_temp={23} storage_used={24} ncores={25} ' +
                'gpu_name={26} cpu_name={27} {28}'
        $line -f @(
            ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()), $n, $(if ($isAdmin) { 1 } else { 0 }),
            (Fmt $g_temp 1 $true), (Fmt $g_power 1 $true), (Fmt $g_clk 0 $true), (Fmt $g_mclk 0 $true),
            (Fmt $g_util 1), (Fmt $g_3d 1), (Fmt $g_copy 1), (Fmt $g_enc 1), (Fmt $g_dec 1),
            (Fmt $g_vram 0), (Fmt $g_vtot 0), (Fmt $g_memload 1),
            (Fmt $g_fan 0), (Fmt $g_fanp 0),
            (Fmt $c_temp 1 $true), (Fmt $c_power 1 $true), (Fmt $c_util 1), (Fmt $c_max 1),
            (Fmt $c_mhz 0 $true), (Fmt $c_volt 2),
            (Fmt $s_temp 1 $true), (Fmt $s_used 1),
            $coreVals.Count,
            (San $g_name), (San $c_name), $coreStr
        ) | Write-Output
    } catch {
        Write-Output ("{0} line={1} error={2}" -f ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()),
                      $n, ($_.Exception.Message -replace '\s+', '_'))
    }
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds $IntervalMs
    $n++
    if ($Lines -gt 0 -and $n -ge $Lines) { break }
}
$computer.Close()

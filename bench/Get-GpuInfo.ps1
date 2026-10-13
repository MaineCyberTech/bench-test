# Get-GpuInfo.ps1 - report system, GPU, and NVIDIA driver info.
# Usage: .\Get-GpuInfo.ps1 [-Json]
param([switch]$Json)

$info = [ordered]@{}

$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$bios = Get-CimInstance Win32_BIOS
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$info.Host = $env:COMPUTERNAME
$info.Manufacturer = $cs.Manufacturer
$info.Model = $cs.Model
$info.OS = "$($os.Caption) build $($os.BuildNumber) $($os.OSArchitecture)"
$info.BIOS = "$($bios.SMBIOSBIOSVersion) ($($bios.ReleaseDate.ToString('yyyy-MM-dd')))"
$info.CPU = "$($cpu.Name) ($($cpu.NumberOfCores)c/$($cpu.NumberOfLogicalProcessors)t)"
$info.RAM_GiB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)

$vc = Get-CimInstance Win32_VideoController
$info.GPUs = @()
foreach ($g in $vc) {
    $mem = $null
    try { if ($g.AdapterRAM) { $mem = [math]::Round($g.AdapterRAM / 1GB, 2) } } catch {}
    $info.GPUs += [ordered]@{
        Name = $g.Name
        Vendor = $g.AdapterCompatibility
        DriverVersion = $g.DriverVersion
        DriverDate = $g.DriverDate
        VideoProcessor = $g.VideoProcessor
        AdapterRAM_GiB = $mem
        PnpDeviceID = $g.PNPDeviceID
        Status = $g.Status
    }
}

# Display adapter registry detail (dedicated VRAM + engine/AMD/NVIDIA name)
$regPath = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000'
if (Test-Path $regPath) {
    $rp = Get-ItemProperty $regPath -ErrorAction SilentlyContinue
    if ($rp.DriverDesc) { $info.RegGpuName = $rp.DriverDesc }
    if ($rp.DriverVersion) { $info.RegDriverVersion = $rp.DriverVersion }
    if ($rp.'HardwareInformation.qwMemorySize') { $info.DedicatedVram_GiB = [math]::Round($rp.'HardwareInformation.qwMemorySize' / 1GB, 2) }
}

$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $q = & nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap,power.max_limit,temperature.gpu --format=csv,noheader 2>$null
    $info.nvidia_smi = $q
} else {
    $info.nvidia_smi = "not found"
}

# PCI device id of the NVIDIA GPU (for driver lookup)
$nvgpu = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
    Where-Object { $_.DeviceID -like '*VEN_10DE*' -and $_.PNPClass -eq 'Display' } | Select-Object -First 1
if ($nvgpu) { $info.GpuPciId = ($nvgpu.DeviceID -split '\\')[1] }

# Generic primary display PCI id (any vendor, e.g. AMD VEN_1002 / Intel VEN_8086)
$disp = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
    Where-Object { $_.PNPClass -eq 'Display' } | Select-Object -First 1
if ($disp) { $info.PrimaryDisplayPciId = ($disp.DeviceID -split '\\')[1] }

if ($Json) { $info | ConvertTo-Json -Depth 5 } else {
    foreach ($k in $info.Keys) {
        if ($k -eq 'GPUs') {
            foreach ($g in $info.GPUs) { "GPU: $($g.Name)  driver=$($g.DriverVersion) ($($g.DriverDate)) status=$($g.Status)" }
        } else { "{0,-16} {1}" -f $k, $info[$k] }
    }
}

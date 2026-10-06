# Get-GpuInfo.ps1 — report system, GPU, and NVIDIA driver info.
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
    $info.GPUs += [ordered]@{
        Name = $g.Name
        DriverVersion = $g.DriverVersion
        DriverDate = $g.DriverDate
        Status = $g.Status
    }
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

if ($Json) { $info | ConvertTo-Json -Depth 5 } else {
    foreach ($k in $info.Keys) {
        if ($k -eq 'GPUs') {
            foreach ($g in $info.GPUs) { "GPU: $($g.Name)  driver=$($g.DriverVersion) ($($g.DriverDate)) status=$($g.Status)" }
        } else { "{0,-16} {1}" -f $k, $info[$k] }
    }
}

# Get-SystemInfo.ps1 - deep system inventory (CPU, GPU, memory, storage, network, displays, USB, OS).
# Usage: .\Get-SystemInfo.ps1 [-Json] [-OutFile <path>]
param([switch]$Json, [string]$OutFile)

$info = [ordered]@{}
$cs = Get-CimInstance Win32_ComputerSystem
$bb = Get-CimInstance Win32_BaseBoard
$bios = Get-CimInstance Win32_BIOS
$os = Get-CimInstance Win32_OperatingSystem
$info.Host = $env:COMPUTERNAME
$info.Manufacturer = $cs.Manufacturer
$info.Model = $cs.Model
$info.Board = "$($bb.Manufacturer) $($bb.Product) rev $($bb.Version)"
$info.BIOS = [ordered]@{ version = $bios.SMBIOSBIOSVersion; date = $bios.ReleaseDate.ToString('yyyy-MM-dd'); vendor = $bios.Manufacturer }
$info.OS = [ordered]@{ caption = $os.Caption; build = $os.BuildNumber; version = $os.Version; arch = $os.OSArchitecture }
$info.Uptime_hours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
$info.LastBootTime = $os.LastBootUpTime.ToString('o')

# CPU
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$info.CPU = [ordered]@{
    name = $cpu.Name; cores = $cpu.NumberOfCores; threads = $cpu.NumberOfLogicalProcessors
    max_clock_mhz = $cpu.MaxClockSpeed; current_clock_mhz = $cpu.CurrentClockSpeed
    l2_cache_kb = $cpu.L2CacheSize; l3_cache_kb = $cpu.L3CacheSize
    virtualization = [bool]$cpu.VirtualizationFirmwareEnabled; processor_id = $cpu.ProcessorId
}

# Memory DIMMs
$dims = Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue
$info.RAM = [ordered]@{
    total_gib = [math]::Round((($dims | Measure-Object -Property Capacity -Sum).Sum) / 1GB, 1)
    dimm_count = @($dims).Count
    dimms = @($dims | ForEach-Object {
        [ordered]@{ slot = $_.DeviceLocator; size_gib = [math]::Round($_.Capacity / 1GB, 0)
            speed_mts = $_.Speed; configured_mts = $_.ConfiguredClockSpeed
            mfg = $_.Manufacturer; part = $_.PartNumber; serial = $_.SerialNumber }
    })
}

# GPU (WMI + nvidia-smi)
$vcs = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue
$info.GPUs = @($vcs | ForEach-Object {
    [ordered]@{ name = $_.Name; driver = $_.DriverVersion; date = $_.DriverDate; status = $_.Status
        vram_mib = if ($_.AdapterRAM -gt 0) { [math]::Round($_.AdapterRAM / 1MB, 0) } else { $null }
        resolution = "$($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution)"
        refresh = $_.CurrentRefreshRate; pnp = $_.PNPDeviceID }
})
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gq = & nvidia-smi --query-gpu=name,driver_version,vbios_version,serial,uuid,pci.bus_id,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,power.default_limit,power.max_limit,memory.total,clocks.max.sm,clocks.max.mem --format=csv,noheader 2>$null
    $info.nvidia = $gq
}

# Storage: disks, volumes, partitions
$info.Disks = @()
foreach ($pd in Get-PhysicalDisk -ErrorAction SilentlyContinue) {
    $rc = $pd | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
    $info.Disks += [ordered]@{
        name = $pd.FriendlyName; media = "$($pd.MediaType)"; bus = "$($pd.BusType)"
        size_gib = [math]::Round($pd.Size / 1GB, 0); health = "$($pd.HealthStatus)"
        operational = "$($pd.OperationalStatus)"; firmware = "$($pd.FirmwareVersion)"
        temp_c = if ($rc) { $rc.Temperature } else { $null }
        wear = if ($rc) { $rc.Wear } else { $null }
        read_errors = if ($rc) { $rc.ReadErrorsTotal } else { $null }
        write_errors = if ($rc) { $rc.WriteErrorsTotal } else { $null }
        power_on_hours = if ($rc) { $rc.PowerOnHours } else { $null }
    }
}
$info.Volumes = @(Get-Volume -ErrorAction SilentlyContinue | ForEach-Object {
    [ordered]@{ drive = "$($_.DriveLetter)"; fs = "$($_.FileSystem)"; label = $_.FileSystemLabel
        size_gib = [math]::Round($_.Size / 1GB, 0); free_gib = [math]::Round($_.SizeRemaining / 1GB, 0); health = "$($_.HealthStatus)" }
})

# Network adapters (up)
$info.NICs = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' } | ForEach-Object {
    [ordered]@{ name = $_.Name; desc = $_.InterfaceDescription; speed_mbps = "$($_.LinkSpeed)"
        mac = $_.MacAddress; ip = (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress -join ',' }
})

# Displays
$info.Monitors = @(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue | ForEach-Object {
    $n = ($_.UserFriendlyName | Where-Object { $_ -gt 0 } | ForEach-Object { [char]$_ }) -join ''
    $m = ($_.ManufacturerName | Where-Object { $_ -gt 0 } | ForEach-Object { [char]$_ }) -join ''
    [ordered]@{ manufacturer = $m; name = $n; year = $_.YearOfManufacture }
})

# USB controllers
$info.USB = @(Get-CimInstance Win32_USBController -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })

# Hotfixes (last 10)
$info.RecentHotfixes = @(Get-HotFix -ErrorAction SilentlyContinue | Sort-Object InstalledOn -Descending | Select-Object -First 10 | ForEach-Object { "$($_.HotFixID) $($_.InstalledOn)" })

if ($OutFile) { $info | ConvertTo-Json -Depth 8 | Set-Content $OutFile -Encoding UTF8 }
if ($Json) { $info | ConvertTo-Json -Depth 8 } else {
    "Host     $($info.Host) ($($info.Manufacturer) $($info.Model))"
    "Board    $($info.Board)  BIOS $($info.BIOS.version) $($info.BIOS.date)"
    "OS       $($info.OS.caption) build $($info.OS.build)  uptime $($info.Uptime_hours) h"
    "CPU      $($info.CPU.name)  $($info.CPU.cores)c/$($info.CPU.threads)t  max $($info.CPU.max_clock_mhz) MHz  L3 $($info.CPU.l3_cache_kb) KB"
    "RAM      $($info.RAM.total_gib) GiB in $($info.RAM.dimm_count) DIMMs"
    foreach ($d in $info.RAM.dimms) { "   DIMM $($d.slot): $($d.size_gib) GiB @ $($d.configured_mts) MT/s  $($d.mfg) $($d.part.Trim())" }
    foreach ($g in $info.GPUs) { "GPU      $($g.name)  driver $($g.driver)  $($g.resolution)@$($g.refresh)Hz" }
    if ($info.nvidia) { "NVIDIA   $($info.nvidia)" }
    foreach ($d in $info.Disks) { "Disk     $($d.name)  $($d.media) $($d.bus)  $($d.size_gib) GiB  $($d.health)  fw $($d.firmware)  temp=$($d.temp_c)C wear=$($d.wear)" }
    foreach ($v in $info.Volumes) { "Volume   $($v.drive): $($v.fs) $($v.size_gib) GiB ($($v.free_gib) free) $($v.label)" }
    foreach ($n in $info.NICs) { "NIC      $($n.name) $($n.speed_mbps)  $($n.mac)  $($n.ip)" }
    foreach ($m in $info.Monitors) { "Monitor  $($m.manufacturer) $($m.name) ($($m.year))" }
}

# Get-PcieInfo.ps1 - PCI/PCIe device topology + driver versions.
# Usage: .\Get-PcieInfo.ps1 [-Json] [-OutFile <path>]
param([switch]$Json, [string]$OutFile)

$devices = New-Object System.Collections.ArrayList
$pnp = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | Where-Object { $_.DeviceID -like 'PCI\*' }
foreach ($d in $pnp) {
    $ids = $d.DeviceID -split '&'
    $ven = ($ids | Where-Object { $_ -like 'VEN_*' }) -replace 'VEN_', ''
    $dev = ($ids | Where-Object { $_ -like 'DEV_*' }) -replace 'DEV_', ''
    $cls = ($d.DeviceID -split '\\')[-1]
    $drv = (Get-PnpDeviceProperty -InstanceId $d.DeviceID -KeyName 'DEVPKEY_Device_DriverVersion' -ErrorAction SilentlyContinue).Data
    [void]$devices.Add([ordered]@{
        name = $d.Name; vendor_id = $ven; device_id = $dev; class = $d.PNPClass
        status = $d.Status; service = $d.Service; driver_version = $drv; instance = $d.DeviceID
    })
}

# GPU PCIe link (NVIDIA)
$gpu = @()
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = & nvidia-smi --query-gpu=index,name,pci.bus_id,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max --format=csv,noheader 2>$null
}

$out = [ordered]@{ host = $env:COMPUTERNAME; timestamp = (Get-Date).ToString('o'); device_count = $devices.Count; devices = $devices; nvidia_link = $gpu }
if ($OutFile) { $out | ConvertTo-Json -Depth 5 | Set-Content $OutFile -Encoding UTF8 }
if ($Json) { $out | ConvertTo-Json -Depth 5 } else {
    "PCI/PCIe devices ($($devices.Count)):"
    foreach ($d in $devices) { "{0,-46} {1,-14} ven=$($d.vendor_id) dev=$($d.device_id) drv=$($d.driver_version) [{0}]" -f $d.name, $d.class }
    if ($gpu) { "NVIDIA PCIe: $gpu" }
}

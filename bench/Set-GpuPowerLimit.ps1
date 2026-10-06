# Set-GpuPowerLimit.ps1 — query or set the GPU power limit (needs admin to set).
# Usage:
#   .\Set-GpuPowerLimit.ps1 -Info              # show min/default/max/current
#   .\Set-GpuPowerLimit.ps1 -Watts 220         # cap to 220 W (one UAC prompt)
#   .\Set-GpuPowerLimit.ps1 -Watts 285         # restore full
#
# Why: lower power = less heat/noise/coil-whine with small throughput loss
# (e.g. ~200 W ran ~93% of the 285 W throughput on an RTX 4070 Ti SUPER, ~10 C cooler).
param(
    [int]$Watts,
    [switch]$Info,
    [int]$Gpu = 0
)

if ($Info -or -not $Watts) {
    & nvidia-smi -q -d POWER | Select-String -Pattern 'Power Limit|Min Power Limit|Max Power Limit|Default Power Limit|Requested Power Limit'
    exit 0
}

# set requires admin — relaunch just the nvidia-smi call elevated
$p = Start-Process nvidia-smi -ArgumentList '-i', "$Gpu", '-pl', "$Watts" -Verb RunAs -Wait -PassThru
Start-Sleep -Seconds 1
"set rc=$($p.ExitCode)"
& nvidia-smi --query-gpu=power.limit,enforced.power.limit --format=csv

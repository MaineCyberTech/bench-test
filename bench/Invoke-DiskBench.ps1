# Invoke-DiskBench.ps1 - disk (HDD/SSD/NVMe) throughput + SMART health report.
# Usage: .\Invoke-DiskBench.ps1 [-Drive C] [-SizeMB 2048] [-Seconds 15] [-OutDir .\results]
#        .\Invoke-DiskBench.ps1 -Path 'D:\bench' -SizeMB 4096
param(
    [string]$Path = '',
    [string]$Drive = 'C',
    [int]$SizeMB = 2048,
    [int]$Seconds = 15,
    [string]$OutDir = '',
    [switch]$Keep
)
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot '..\results' }
$ErrorActionPreference = 'Continue'
$bench = Join-Path $PSScriptRoot 'disk_bench.py'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$hostname = $env:COMPUTERNAME
$base = Join-Path $OutDir "$hostname-disk-$stamp"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found on PATH" }
if (-not $Path) {
    if ($Drive -eq ($env:SystemDrive.TrimEnd(':'))) { $Path = $env:TEMP }
    else { $Path = "$($Drive):\benchtest" }
}
try {
    if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Force -Path $Path -ErrorAction Stop | Out-Null }
} catch {
    Write-Host "[disk] cannot use $Path ($($_.Exception.Message)); falling back to $env:TEMP"
    $Path = $env:TEMP
}
if (-not (Test-Path $Path)) { throw "path not found: $Path" }

# SMART / health for the physical disk backing $Path
function Get-DiskHealth {
    $part = Get-Partition -DriveLetter $Drive -ErrorAction SilentlyContinue | Select-Object -First 1
    $pd = if ($part) { Get-PhysicalDisk -ErrorAction SilentlyContinue | Where-Object { $_.DeviceId -eq (Get-Disk -Number $part.DiskNumber).Number } } else { $null }
    if (-not $pd) { $pd = Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object -First 1 }
    $rc = if ($pd) { $pd | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue } else { $null }
    return [ordered]@{
        friendly = if ($pd) { $pd.FriendlyName } else { $null }
        media = if ($pd) { $pd.MediaType } else { $null }
        bus = if ($pd) { $pd.BusType } else { $null }
        health = if ($pd) { $pd.HealthStatus } else { $null }
        temperature_c = if ($rc) { $rc.Temperature } else { $null }
        wear = if ($rc) { $rc.Wear } else { $null }
        read_errors = if ($rc) { $rc.ReadErrorsTotal } else { $null }
        write_errors = if ($rc) { $rc.WriteErrorsTotal } else { $null }
        power_on_hours = if ($rc) { $rc.PowerOnHours } else { $null }
    }
}

function Invoke-Phase($name, $extraArgs) {
    Write-Host "[disk] $name" -ForegroundColor Cyan
    $out = & python $bench $name --path $Path --size-mb $SizeMB --seconds $Seconds --force @extraArgs 2>&1
    $out | Where-Object { $_ -notmatch '^RESULT_JSON:' } | ForEach-Object { Write-Host "    $_" }
    $jl = $out | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
    if ($jl) { ($jl -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { $null }
}

$health = Get-DiskHealth
$phases = @()
$phases += Invoke-Phase 'seqwrite' @()
$phases += Invoke-Phase 'seqread' @()
$phases += Invoke-Phase 'randread' @()
$phases += Invoke-Phase 'randwrite' @()
$phases += Invoke-Phase 'randmix' @()

$report = [ordered]@{ host = $hostname; timestamp = (Get-Date).ToString('o'); path = $Path; size_mb = $SizeMB; health = $health; phases = $phases }
$report | ConvertTo-Json -Depth 8 | Set-Content "$base.json" -Encoding UTF8

$md = @(); $md += "# Disk bench - $hostname"; $md += ""; $md += "Generated: $(Get-Date -Format o)"
$md += ""; $md += "**Drive:** $($health.friendly) | $($health.media) $($health.bus) | health=$($health.health) | temp=$($health.temperature_c) C | wear=$($health.wear) | err r/w=$($health.read_errors)/$($health.write_errors)"
$md += ""; $md += "**Test file:** $SizeMB MiB in $Path"; $md += ""
$md += "| phase | result |"; $md += "|---|---|"
foreach ($p in $phases) { if (-not $p) { continue }
    switch ($p.phase) {
        'seqwrite' { $md += "| seq write | $($p.mbps) MB/s ($($p.seconds) s) |" }
        'seqread' { $md += "| seq read | $($p.mbps) MB/s ($($p.seconds) s) |" }
        'randread' { $md += "| rand 4K read | $($p.iops) IOPS, $($p.mbps) MB/s |" }
        'randwrite' { $md += "| rand 4K write | $($p.iops) IOPS, $($p.mbps) MB/s |" }
        'randmix' { $md += "| rand 4K mix (70/30) | $($p.iops) IOPS, $($p.mbps) MB/s |" }
    }
}
$md += ""; $md += "_Buffered I/O: sequential reads may be cache-served. Use a file larger than RAM for truer reads._"
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8
Write-Host "[done] wrote $base.json and $base.md" -ForegroundColor Green

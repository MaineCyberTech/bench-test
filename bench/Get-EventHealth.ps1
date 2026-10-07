# Get-EventHealth.ps1 - hardware/thermal/stability event history (WHEA, TDR, disk, power).
# Usage: .\Get-EventHealth.ps1 [-Days 7] [-Json] [-OutFile <path>]
param([int]$Days = 7, [switch]$Json, [string]$OutFile)

$since = (Get-Date).AddDays(-$Days)
$ids = @{
    'Microsoft-Windows-WHEA-Logger' = @(1, 17, 18, 19, 20, 46, 47, 48)
}
$interesting = @{}
$events = Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $since } -ErrorAction SilentlyContinue
$hits = $events | Where-Object {
    $_.ProviderName -match 'WHEA|BugCheck|nvlddmkm|Display|disk|storahci|stornvme|Ntfs|Kernel-Power|Kernel-Processor-Power|Thermal' -and
    ($_.Id -in 1, 17, 18, 19, 20, 41, 46, 47, 48, 1001, 4101, 7, 11, 51, 153, 35, 37, 86)
}

$byKey = $hits | Group-Object { "$($_.ProviderName)/$($_.Id)" } | Sort-Object Count -Descending
$summary = @($byKey | ForEach-Object {
    $p = $_.Name -split '/'
    [ordered]@{ provider = $p[0]; id = $p[1]; count = $_.Count; last = ($_.Group | Sort-Object TimeCreated -Descending | Select-Object -First 1).TimeCreated.ToString('o') }
})
$recent = @($hits | Sort-Object TimeCreated -Descending | Select-Object -First 40 | ForEach-Object {
    [ordered]@{ time = $_.TimeCreated.ToString('o'); id = $_.Id; level = "$($_.LevelDisplayName)"; provider = $_.ProviderName
        msg = (($_.Message -replace '\s+', ' ').Substring(0, [Math]::Min(160, $_.Message.Length))) }
})

# reliability counters for all disks
$disks = @()
foreach ($pd in Get-PhysicalDisk -ErrorAction SilentlyContinue) {
    $rc = $pd | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
    $disks += [ordered]@{ name = $pd.FriendlyName; health = "$($pd.HealthStatus)"; temperature_c = if ($rc) { $rc.Temperature } else { $null }; wear = if ($rc) { $rc.Wear } else { $null }; read_errors = if ($rc) { $rc.ReadErrorsTotal } else { $null }; write_errors = if ($rc) { $rc.WriteErrorsTotal } else { $null } }
}

$out = [ordered]@{ host = $env:COMPUTERNAME; timestamp = (Get-Date).ToString('o'); days = $Days; count = @($hits).Count; summary = $summary; recent = $recent; disks = $disks }
if ($OutFile) { $out | ConvertTo-Json -Depth 6 | Set-Content $OutFile -Encoding UTF8 }
if ($Json) { $out | ConvertTo-Json -Depth 6 } else {
    "Health events (last $Days days): $(@($hits).Count)"
    foreach ($s in $summary) { "  $($s.provider)/$($s.id): $($s.count) (last $($s.last))" }
    foreach ($d in $disks) { "  disk $($d.name): health=$($d.health) temp=$($d.temperature_c)C wear=$($d.wear) rerr=$($d.read_errors) werr=$($d.write_errors)" }
}

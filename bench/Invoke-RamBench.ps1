# Invoke-RamBench.ps1 - RAM bandwidth, latency, integrity and soak, with a report.
# Usage: .\Invoke-RamBench.ps1 [-OutDir .\results] [-SoakSeconds 120] [-SizeMB 1024] [-FillPct 70]
param(
    [string]$OutDir = '',
    [int]$SoakSeconds = 120,
    [int]$SizeMB = 1024,
    [int]$FillPct = 70,
    [int]$Passes = 2,
    [switch]$NoInstall
)
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot '..\results' }
$ErrorActionPreference = 'Continue'
$bench = Join-Path $PSScriptRoot 'ram_bench.py'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$hostname = $env:COMPUTERNAME
$base = Join-Path $OutDir "$hostname-ram-$stamp"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found on PATH" }
$hasNp = & python -c "import importlib.util as u,sys; sys.stdout.write('1' if u.find_spec('numpy') else '0')" 2>$null
if ($hasNp -ne '1') {
    if ($NoInstall) { throw "numpy required for RAM bench (-NoInstall set)" }
    & python -m pip install --quiet numpy 2>&1 | Out-Null
}

function Invoke-Phase($name, $extraArgs, $monitorSeconds) {
    Write-Host "[ram] $name" -ForegroundColor Cyan
    $mon = $null; $samples = @()
    if ($monitorSeconds -gt 0) {
        $mon = Start-Job -ScriptBlock {
            param($s)
            $end = (Get-Date).AddSeconds($s); $a = @()
            while ((Get-Date) -lt $end) {
                $os = Get-CimInstance Win32_OperatingSystem
                $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
                $a += [pscustomobject]@{ freeMiB = [math]::Round($os.FreePhysicalMemory / 1024, 0); cpu = [math]::Round($cpu, 0) }
                Start-Sleep -Seconds 1
            }
            return $a
        } -ArgumentList $monitorSeconds
    }
    $out = & python $bench $name --size-mb $SizeMB --fill-pct $FillPct --passes $Passes --force @extraArgs 2>&1
    $out | Where-Object { $_ -notmatch '^RESULT_JSON:' } | ForEach-Object { Write-Host "    $_" }
    $jl = $out | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
    $res = if ($jl) { ($jl -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { $null }
    if ($mon) { $samples = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue }
    $tel = $null
    if ($samples.Count) {
        $m = $samples | Measure-Object freeMiB, cpu -Minimum -Maximum -Average
        $tel = @{ freeMiB_min = ($m | Where-Object { $_.Property -eq 'freeMiB' }).Minimum; cpu_avg = [math]::Round(($m | Where-Object { $_.Property -eq 'cpu' }).Average, 0); cpu_max = ($m | Where-Object { $_.Property -eq 'cpu' }).Maximum }
    }
    return [pscustomobject]@{ phase = $name; result = $res; telemetry = $tel }
}

$phases = @()
$phases += Invoke-Phase 'info' @() 0
$phases += Invoke-Phase 'bandwidth' @('--seconds', '10') 0
$phases += Invoke-Phase 'latency' @() 0
$phases += Invoke-Phase 'integrity' @() ($Passes * 6)
$phases += Invoke-Phase 'soak' @('--seconds', "$SoakSeconds") $SoakSeconds

$report = [ordered]@{ host = $hostname; timestamp = (Get-Date).ToString('o'); phases = $phases }
$report | ConvertTo-Json -Depth 8 | Set-Content "$base.json" -Encoding UTF8

$md = @(); $md += "# RAM bench - $hostname"; $md += ""; $md += "Generated: $(Get-Date -Format o)"; $md += ""
$md += "| phase | result | mem/CPU |"; $md += "|---|---|---|"
foreach ($p in $phases) {
    if (-not $p.result) { continue }
    $t = if ($p.telemetry) { "free>= $($p.telemetry.freeMiB_min) MiB; CPU $($p.telemetry.cpu_avg)/$($p.telemetry.cpu_max)%" } else { "" }
    $r = $p.result
    switch ($p.phase) {
        'info' { $md += "| info | total $($r.total_gib) GiB, avail $($r.available_gib) GiB, cpus $($r.logical_cpus) | $t |" }
        'bandwidth' { foreach ($k in $r.results.PSObject.Properties.Name) { $md += "| bandwidth $k | $($r.results.$k) GB/s | $t |" } }
        'latency' { $md += "| latency | ~$($r.ns_per_access_approx) ns/access, gather $($r.gather_gbps_approx) GB/s | $t |" }
        'integrity' { $md += "| integrity | $($r.giB) GiB, passes $($r.passes), bad $($r.bad_elements) | $t |" }
        'soak' { $md += "| soak | $($r.gbps) GB/s over $($r.seconds)s, errors $($r.errors) | $t |" }
    }
}
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8
Write-Host "[done] wrote $base.json and $base.md" -ForegroundColor Green

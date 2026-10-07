# Invoke-CpuBench.ps1 - run the CPU compute/memory phases and write a report.
# Usage: .\Invoke-CpuBench.ps1 [-OutDir .\results] [-SoakSeconds 120] [-Size 4096]
param(
    [string]$OutDir = "$PSScriptRoot\..\results",
    [int]$SoakSeconds = 120,
    [int]$Size = 4096
)

$ErrorActionPreference = 'Continue'
$bench = Join-Path $PSScriptRoot 'cpu_bench.py'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$hostname = $env:COMPUTERNAME
$base = Join-Path $OutDir "$hostname-cpu-$stamp"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found on PATH" }

function Invoke-Phase($name, $extraArgs) {
    Write-Host "[cpu-phase] $name" -ForegroundColor Cyan
    $out = & python $bench $name --size $Size @extraArgs 2>&1
    $out | Where-Object { $_ -notmatch '^RESULT_JSON:' } | ForEach-Object { Write-Host "    $_" }
    $jsonLine = $out | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
    if ($jsonLine) { ($jsonLine -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { $null }
}

$phases = [ordered]@{}
$phases.info = Invoke-Phase 'info' @()
$phases.matmul = Invoke-Phase 'matmul' @('--seconds', '10')
$phases.membw = Invoke-Phase 'membw' @('--seconds', '8')
$phases.soak = Invoke-Phase 'soak' @('--seconds', "$SoakSeconds")

$report = [ordered]@{ host = $hostname; timestamp = (Get-Date).ToString('o'); phases = $phases }
$report | ConvertTo-Json -Depth 12 | Set-Content "$base.json" -Encoding UTF8

$md = @()
$md += "# CPU bench - $hostname"
$md += ""
$md += "Generated: $(Get-Date -Format o)"
$i = $phases.info
if ($i) { $md += "**CPU:** logical=$($i.logical_cpus) | numpy $($i.numpy)" }
$md += ""
$md += "| phase | metric |"
$md += "|---|---|"
if ($phases.matmul) { foreach ($k in $phases.matmul.results.PSObject.Properties.Name) { $md += "| matmul | $k $($phases.matmul.results.$k.gflops) GFLOPS |" } }
if ($phases.membw) { $m = $phases.membw.results; $md += "| membw | copy $($m.copy_gbps) / add $($m.add_gbps) / reduce $($m.reduce_gbps) GB/s |" }
if ($phases.soak) { $md += "| soak | $($phases.soak.gflops) GFLOPS over $($phases.soak.seconds)s |" }
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8

Write-Host ""
Write-Host "[done] wrote $base.json and $base.md" -ForegroundColor Green

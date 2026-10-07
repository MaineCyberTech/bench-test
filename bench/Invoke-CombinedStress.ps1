# Invoke-CombinedStress.ps1 - run the GPU and CPU soak phases concurrently and report thermals.
#
# Stresses the GPU (matmul+conv, 4 streams) and the CPU (numpy GEMM soak) at the same time,
# sampling GPU telemetry + total CPU utilisation once per interval, then writes a JSON+MD report.
#
# Usage: .\Invoke-CombinedStress.ps1 [-Seconds 300] [-OutDir .\results]
param(
    [string]$OutDir = "$PSScriptRoot\..\results",
    [int]$Seconds = 300,
    [int]$GpuSize = 10240,
    [int]$CpuSize = 2048
)

$ErrorActionPreference = 'Continue'
$gpuBench = Join-Path $PSScriptRoot 'gpu_bench.py'
$cpuBench = Join-Path $PSScriptRoot 'cpu_bench.py'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$hostname = $env:COMPUTERNAME
$base = Join-Path $OutDir "$hostname-combined-$stamp"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "python not found on PATH" }

Write-Host "[combined] GPU soak ($GpuSize) + CPU soak ($CpuSize) concurrently for ${Seconds}s" -ForegroundColor Cyan

# --- telemetry sampler: GPU via nvidia-smi, CPU via performance counter -------------
$mon = Start-Job {
    param($secs)
    $end = (Get-Date).AddSeconds($secs); $s = @()
    while ((Get-Date) -lt $end) {
        $line = & nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,memory.used,fan.speed --format=csv,noheader 2>$null
        $c = 0
        try { $c = [math]::Round((Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop).CounterSamples.CookedValue, 0) } catch {}
        if ($line -match ',') {
            $p = $line -split ',' | ForEach-Object { [double](($_ -replace '[^0-9\.]', '')) }
            $s += [pscustomobject]@{ util = $p[0]; temp = $p[1]; power = $p[2]; sm = $p[3]; mem = $p[4]; fan = $p[5]; cpu = $c }
        }
        Start-Sleep -Milliseconds 500
    }
    return $s
} -ArgumentList ($Seconds + 15)

# --- launch both soaks concurrently -------------------------------------------------
$gpuJob = Start-Job { param($b, $n, $t) & python $b soak --size $n --seconds $t --force 2>&1 } -ArgumentList $gpuBench, $GpuSize, $Seconds
$cpuJob = Start-Job { param($b, $n, $t) & python $b soak --size $n --seconds $t 2>&1 }         -ArgumentList $cpuBench, $CpuSize, $Seconds

$gpuOut = Receive-Job $gpuJob -Wait -ErrorAction SilentlyContinue; Remove-Job $gpuJob -Force -ErrorAction SilentlyContinue
$cpuOut = Receive-Job $cpuJob -Wait -ErrorAction SilentlyContinue; Remove-Job $cpuJob -Force -ErrorAction SilentlyContinue
$samples = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue

$gpuOut | Where-Object { $_ -notmatch '^RESULT_JSON:' } | ForEach-Object { Write-Host "  [gpu] $_" }
$cpuOut | Where-Object { $_ -notmatch '^RESULT_JSON:' } | ForEach-Object { Write-Host "  [cpu] $_" }

$gpuLine = $gpuOut | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
$cpuLine = $cpuOut | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
$gpuRes = if ($gpuLine) { ($gpuLine -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { $null }
$cpuRes = if ($cpuLine) { ($cpuLine -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { $null }

function Get-Stats($samples) {
    if (-not $samples -or $samples.Count -eq 0) { return $null }
    $m = $samples | Measure-Object util, temp, power, sm, mem, fan, cpu -Average -Maximum
    $o = @{}
    foreach ($p in 'util', 'temp', 'power', 'sm', 'mem', 'fan', 'cpu') {
        $x = $m | Where-Object { $_.Property -eq $p }
        $o[$p] = @{ avg = [math]::Round($x.Average, 1); max = [math]::Round($x.Maximum, 1) }
    }
    return $o
}
$t = Get-Stats $samples

$ev = Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = (Get-Date).AddSeconds(-($Seconds + 90)) } -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -eq 4101 -or $_.ProviderName -match 'nvlddmkm|WHEA|BugCheck' }
$throttle = & nvidia-smi -q -d PERFORMANCE 2>$null | Select-String -Pattern 'SW Power Cap|HW Thermal Slowdown|HW Slowdown|HW Power Braking|SW Thermal' |
    ForEach-Object { $_.Line.Trim() }

$report = [ordered]@{
    host      = $hostname
    timestamp = (Get-Date).ToString('o')
    seconds   = $Seconds
    gpu       = $gpuRes
    cpu       = $cpuRes
    telemetry = $t
    stability = [ordered]@{
        tdr_or_whea = @($ev | ForEach-Object { "$($_.TimeCreated) id=$($_.Id) $($_.ProviderName)" })
        throttle    = @($throttle)
    }
}
$report | ConvertTo-Json -Depth 12 | Set-Content "$base.json" -Encoding UTF8

$md = @()
$md += "# Combined CPU+GPU stress - $hostname"
$md += ""
$md += "Generated: $(Get-Date -Format o)"
$md += "Duration: ${Seconds}s concurrent"
$md += ""
$md += "| load | metric |"
$md += "|---|---|"
if ($gpuRes) { $md += "| GPU soak | $($gpuRes.matmul_tflops) TFLOPS over $($gpuRes.seconds)s (peak VRAM $($gpuRes.peak_vram_gib) GiB) |" }
if ($cpuRes) { $md += "| CPU soak | $($cpuRes.gflops) GFLOPS over $($cpuRes.seconds)s (iters $($cpuRes.iters)) |" }
if ($t) { $md += "| telemetry | GPU util $($t.util.avg)/$($t.util.max)% | temp $($t.temp.avg)/$($t.temp.max)C | power $($t.power.avg)/$($t.power.max)W | fan $($t.fan.avg)/$($t.fan.max)% | CPU $($t.cpu.avg)/$($t.cpu.max)% |" }
$md += ""
$md += "**Stability:** TDR/WHEA: $(if ($report.stability.tdr_or_whea.Count) { $report.stability.tdr_or_whea -join '; ' } else { 'none' })"
$md += ""
$md += "**Throttle:** $(($report.stability.throttle) -join ' | ')"
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8

Write-Host ""
Write-Host "[done] wrote $base.json and $base.md" -ForegroundColor Green

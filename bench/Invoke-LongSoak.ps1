# Invoke-LongSoak.ps1 - multi-hour endurance soak with telemetry and a report.
#
# Runs the GPU soak phase (4 CUDA streams of bf16 matmul + cuDNN conv) for Hours,
# sampling telemetry every IntervalSeconds, then writes <host>-longsoak-<stamp>.{md,json}
# to -OutDir (soaks are long; keep this process alive - it detaches from nothing).
#
# Usage:
#   .\Invoke-LongSoak.ps1 -Hours 4
#   .\Invoke-LongSoak.ps1 -Hours 8 -IntervalSeconds 5
#
# Tips: disable sleep first (powercfg -change -standby-timeout-ac 0), and decide the
# power limit up front (.\Set-GpuPowerLimit.ps1 -Watts 220 for a cooler/quieter soak).
param(
    [double]$Hours = 4,
    [string]$OutDir = "$PSScriptRoot\..\results",
    [int]$IntervalSeconds = 2,
    [int]$Gpu = 0
)

$ErrorActionPreference = 'Continue'
$bench = Join-Path $PSScriptRoot 'gpu_bench.py'
if (-not (Test-Path $bench)) { Write-Error "gpu_bench.py not found next to this script"; exit 1 }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$dur = [int]($Hours * 3600)
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = Join-Path $OutDir "$($env:COMPUTERNAME)-longsoak-$stamp"
$log = "$base.log"
"[long-soak] start $(Get-Date -Format o) duration=${dur}s interval=${IntervalSeconds}s" | Out-File $log -Encoding utf8

# keep the machine awake for the whole run (best-effort; needs no admin)
Add-Type -Namespace W -Name P -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true)] public static extern uint SetThreadExecutionState(uint esFlags);
'@
[uint32]$ES_CONT = [uint32]::Parse('2147483648'); [uint32]$ES_REQ = 1
[W.P]::SetThreadExecutionState([uint32]($ES_CONT -bor $ES_REQ)) | Out-Null
& powercfg -change -standby-timeout-ac 0 2>$null

$samples = [int]($dur / $IntervalSeconds)
$mon = Start-Job -ScriptBlock {
    param($n, $iv)
    $a = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $n; $i++) {
        $l = & nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,memory.used,fan.speed --format=csv,noheader 2>$null
        if ($l -match ',') {
            $p = $l -split ',' | ForEach-Object { [double](($_ -replace '[^0-9\.]', '')) }
            [void]$a.Add([pscustomobject]@{ util = $p[0]; temp = $p[1]; power = $p[2]; sm = $p[3]; mem = $p[4]; fan = $p[5] })
        }
        Start-Sleep -Seconds $iv
    }
    return $a
} -ArgumentList $samples, $IntervalSeconds

"[long-soak] running soak ${dur}s ..." | Add-Content $log
$out = & python $bench soak --seconds $dur --force 2>&1
$out | ForEach-Object { $_ | Add-Content $log }
$resultLine = ($out | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1)
$soak = if ($resultLine) { ($resultLine -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { $null }

$s = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue
$m = $s | Measure-Object util, temp, power, sm, mem, fan -Minimum -Maximum -Average
$stat = @{}
foreach ($p in 'util', 'temp', 'power', 'sm', 'mem', 'fan') {
    $x = $m | Where-Object { $_.Property -eq $p }
    $stat[$p] = @{ min = [math]::Round($x.Minimum, 1); avg = [math]::Round($x.Average, 1); max = [math]::Round($x.Maximum, 1) }
}
# steady-state drift: compare first 10% vs last 10% of the LOADED samples (exclude idle edges)
$load = $s | Where-Object { $_.util -gt 50 }
$n = $load.Count
if ($n -gt 20) {
    $k = [Math]::Max(1, [int]($n * 0.10))
    $first = $load[0..($k - 1)]; $last = $load[($n - $k)..($n - 1)]
    $drift = @{
        temp = @{ first = [math]::Round(($first | Measure-Object temp -Average).Average, 1); last = [math]::Round(($last | Measure-Object temp -Average).Average, 1) }
        power = @{ first = [math]::Round(($first | Measure-Object power -Average).Average, 0); last = [math]::Round(($last | Measure-Object power -Average).Average, 0) }
        n_loaded = $n
    }
} else { $drift = @{ note = 'too few loaded samples' } }

$throttle = & nvidia-smi -q -d PERFORMANCE 2>$null | Select-String -Pattern 'SW Power Cap|HW Thermal Slowdown|HW Slowdown|HW Power Braking|SW Thermal' | ForEach-Object { $_.Line.Trim() }
$ev = Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = (Get-Date).AddHours(-($Hours + 1)) } -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -eq 4101 -or $_.ProviderName -match 'nvlddmkm|WHEA|BugCheck' }

$report = [ordered]@{
    host = $env:COMPUTERNAME; hours = $Hours; timestamp = (Get-Date).ToString('o')
    soak = $soak; samples = $s.Count; stats = $stat; drift = $drift
    throttle = @($throttle)
    errors = if ($ev) { @($ev | ForEach-Object { "$($_.TimeCreated) id=$($_.Id) $($_.ProviderName)" }) } else { @() }
}
$report | ConvertTo-Json -Depth 8 | Set-Content "$base.json" -Encoding utf8

$md = @()
$md += "# Long soak ($Hours h) - $($env:COMPUTERNAME)"
$md += ""
$md += "Generated: $(Get-Date -Format o)"
if ($soak) { $md += "**Throughput:** $($soak.matmul_tflops) TFLOPS sustained, $($soak.matmuls) matmuls + $($soak.convs) convs over $($soak.seconds)s" }
$md += ""
$md += "| metric | min | avg | max |"
$md += "|---|---|---|---|"
foreach ($p in 'util', 'temp', 'power', 'sm', 'mem', 'fan') { $md += "| $p | $($stat[$p].min) | $($stat[$p].avg) | $($stat[$p].max) |" }
$md += ""
if ($drift.temp) { $md += "**Steady-state drift (loaded samples):** temp $($drift.temp.first) -> $($drift.temp.last) C, power $($drift.power.first) -> $($drift.power.last) W" }
$md += ""
$md += "**Throttle:** $(($throttle) -join ' | ')"
$md += ""
$md += "**Errors:** $(if ($report.errors.Count) { $report.errors -join '; ' } else { 'none' })"
$md -join "`n" | Set-Content "$base.md" -Encoding utf8

[W.P]::SetThreadExecutionState([uint32]$ES_CONT) | Out-Null
"[long-soak] done $(Get-Date -Format o)" | Add-Content $log
Write-Host "[long-soak] wrote $base.json and $base.md" -ForegroundColor Green

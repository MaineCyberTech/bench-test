# Invoke-FullSystemSoak.ps1 - run GPU + CPU + RAM + disk soaks CONCURRENTLY and report.
# Usage: .\Invoke-FullSystemSoak.ps1 [-Seconds 300] [-OutDir .\results]
param(
    [int]$Seconds = 300,
    [int]$GpuSize = 10240,
    [int]$CpuSize = 2048,
    [int]$RamSizeMB = 1024,
    [int]$DiskSizeMB = 512,
    [string]$DiskPath = '',
    [string]$OutDir = '',
    [switch]$UntilStable,
    [int]$StableHold = 60
)
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot '..\results' }
if (-not $DiskPath) { $DiskPath = $env:TEMP }
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = Join-Path $OutDir "$($env:COMPUTERNAME)-fullsoak-$stamp"

$gpu = Join-Path $PSScriptRoot 'gpu_bench.py'
$cpu = Join-Path $PSScriptRoot 'cpu_bench.py'
$ram = Join-Path $PSScriptRoot 'ram_bench.py'
$dsk = Join-Path $PSScriptRoot 'disk_bench.py'

Write-Host "[fullsoak] GPU+CPU+RAM+disk concurrently for ${Seconds}s"
$jobs = @{
    gpu = Start-Job -ScriptBlock {
        param($p,$s,$n,$st,$hold)
        $a = @('soak','--seconds',"$s",'--size',"$n",'--force')
        if ($st) { $a += @('--until-stable','--stable-hold',"$hold") }
        & python $p @a
    } -ArgumentList $gpu, $Seconds, $GpuSize, [bool]$UntilStable, $StableHold
    cpu = Start-Job -ScriptBlock { param($p,$s,$n) & python $p soak --seconds $s --size $n } -ArgumentList $cpu, $Seconds, $CpuSize
    ram = Start-Job -ScriptBlock { param($p,$s,$m) & python $p soak --seconds $s --size-mb $m --force } -ArgumentList $ram, $Seconds, $RamSizeMB
    dsk = Start-Job -ScriptBlock { param($p,$s,$m,$path) & python $p soak --seconds $s --size-mb $m --path $path --force } -ArgumentList $dsk, $Seconds, $DiskSizeMB, $DiskPath
}

$mon = Start-Job -ScriptBlock {
    param($s)
    $end = (Get-Date).AddSeconds($s + 20)
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    while ((Get-Date) -lt $end) {
        $os = Get-CimInstance Win32_OperatingSystem
        $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
        $cpuR = if ($null -ne $cpu) { [math]::Round($cpu, 0) } else { $null }
        $free = if ($null -ne $os.FreePhysicalMemory) { [math]::Round($os.FreePhysicalMemory / 1024, 0) } else { $null }
        if ($smi) {
            $l = & nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,fan.speed --format=csv,noheader 2>$null
            if ($l -match ',') {
                $p = $l -split ',' | ForEach-Object { $t = ($_ -replace '[^0-9\.]', ''); if ($t -eq '' -or $t -eq '.') { $null } else { [double]$t } }
                # Emit each sample as it is taken: the job is stopped before its
                # final return, so an accumulated array would never reach the report.
                [pscustomobject]@{ gpu = $p[0]; temp = $p[1]; power = $p[2]; sm = $p[3]; fan = $p[4]
                    cpu = $cpuR; freeMiB = $free }
            }
        } else {
            # Vendor-agnostic fallback (AMD/Intel on Windows): total GPU busy %.
            # $u stays $null on a failed counter read, and a sample is only emitted
            # with a real reading, so it cannot be averaged as util 0.
            $u = $null
            try {
                $c = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop).CounterSamples
                $sum = ($c | Where-Object { $_.InstanceName -match 'engtype_3D' } | Measure-Object CookedValue -Sum).Sum
                if ($sum) { $u = [math]::Min(100.0, [math]::Round($sum, 1)) }
            } catch { }
            if ($null -ne $u) {
                [pscustomobject]@{ gpu = $u; temp = $null; power = $null; sm = $null; fan = $null
                    cpu = $cpuR; freeMiB = $free }
            }
        }
        Start-Sleep -Seconds 2
    }
} -ArgumentList $Seconds

$results = @{}
foreach ($k in $jobs.Keys) {
    $o = Receive-Job $jobs[$k] -Wait -ErrorAction SilentlyContinue
    $jl = $o | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
    $results[$k] = if ($jl) { ($jl -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { @{ error = 'no result' } }
    Remove-Job $jobs[$k] -Force -ErrorAction SilentlyContinue
}
Stop-Job $mon -ErrorAction SilentlyContinue
$s = @(Receive-Job $mon -Wait -ErrorAction SilentlyContinue)
Remove-Job $mon -Force -ErrorAction SilentlyContinue
# Per-property stats: Measure-Object would average an unreadable (null) sensor as 0,
# and a metric with no readings at all is omitted instead of reported as 0. freeMiB
# keeps its minimum: it is the free-RAM floor, not a peak.
$stat = @{}
foreach ($p in 'gpu', 'temp', 'power', 'sm', 'fan', 'cpu', 'freeMiB') {
    $vals = @($s | ForEach-Object { $_.$p } | Where-Object { $null -ne $_ })
    if ($vals.Count -eq 0) { continue }
    $stat[$p] = @{ avg = [math]::Round((($vals | Measure-Object -Average).Average), 0)
        min = [math]::Round((($vals | Measure-Object -Minimum).Minimum), 0)
        max = [math]::Round((($vals | Measure-Object -Maximum).Maximum), 0) }
}
$throttle = & nvidia-smi -q -d PERFORMANCE 2>$null | Select-String -Pattern 'SW Power Cap|HW Thermal Slowdown|HW Slowdown|HW Power Braking|SW Thermal' | Where-Object { $_.Line -notmatch 'Not Active' } | ForEach-Object { $_.Line.Trim() }
$ev = Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = (Get-Date).AddMinutes(-($Seconds / 60 + 5)) } -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -eq 4101 -or $_.ProviderName -match 'nvlddmkm|WHEA|BugCheck' }

$report = [ordered]@{ host = $env:COMPUTERNAME; timestamp = (Get-Date).ToString('o'); seconds = $Seconds
    results = $results; telemetry = $stat; throttle = @($throttle)
    errors = if ($ev) { @($ev | ForEach-Object { "$($_.TimeCreated) id=$($_.Id) $($_.ProviderName)" }) } else { @() } }
$report | ConvertTo-Json -Depth 8 | Set-Content "$base.json" -Encoding UTF8

$md = @("# Full-system soak ($Seconds s) - $($env:COMPUTERNAME)", "", "Generated: $(Get-Date -Format o)", "")
$md += "| subsystem | result |"; $md += "|---|---|"
if ($results.gpu.matmul_tflops) { $md += "| GPU | $($results.gpu.matmul_tflops) TFLOPS |" }
if ($results.cpu.gflops) { $md += "| CPU | $($results.cpu.gflops) GFLOPS |" }
if ($results.ram.gbps) { $md += "| RAM | $($results.ram.gbps) GB/s, errors $($results.ram.errors) |" }
if ($results.dsk.seq_write_mbps) { $md += "| disk | seq write $($results.dsk.seq_write_mbps) MB/s, rand $($results.dsk.rand_iops_avg) IOPS |" }
$md += ""
$tel = @()
if ($stat.gpu) { $tel += "GPU util $($stat.gpu.avg)/$($stat.gpu.max)%" }
if ($stat.temp) { $tel += "GPU temp $($stat.temp.avg)/$($stat.temp.max) C" }
if ($stat.power) { $tel += "GPU power $($stat.power.avg)/$($stat.power.max) W" }
if ($stat.fan) { $tel += "GPU fan $($stat.fan.avg)/$($stat.fan.max)%" }
if ($stat.cpu) { $tel += "CPU $($stat.cpu.avg)/$($stat.cpu.max)%" }
if ($stat.freeMiB) { $tel += "free RAM min $($stat.freeMiB.min) MiB (avg $($stat.freeMiB.avg))" }
$telLine = if ($tel.Count) { $tel -join ' - ' } else { 'no samples' }
$md += "**Telemetry:** $telLine"
$md += ""
$md += "**Stability:** $(if ($report.errors.Count) { $report.errors -join '; ' } else { 'no TDR/WHEA/BugCheck' })"
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8
Write-Host "[done] wrote $base.json and $base.md" -ForegroundColor Green

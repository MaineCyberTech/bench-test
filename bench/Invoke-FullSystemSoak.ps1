# Invoke-FullSystemSoak.ps1 - run GPU + CPU + RAM + disk soaks CONCURRENTLY and report.
# Usage: .\Invoke-FullSystemSoak.ps1 [-Seconds 300] [-OutDir .\results]
param(
    [int]$Seconds = 300,
    [int]$GpuSize = 10240,
    [int]$CpuSize = 2048,
    [int]$RamSizeMB = 1024,
    [int]$DiskSizeMB = 512,
    [string]$DiskPath = '',
    [string]$OutDir = ''
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
    gpu = Start-Job -ScriptBlock { param($p,$s,$n) & python $p soak --seconds $s --size $n --force } -ArgumentList $gpu, $Seconds, $GpuSize
    cpu = Start-Job -ScriptBlock { param($p,$s,$n) & python $p soak --seconds $s --size $n } -ArgumentList $cpu, $Seconds, $CpuSize
    ram = Start-Job -ScriptBlock { param($p,$s,$m) & python $p soak --seconds $s --size-mb $m --force } -ArgumentList $ram, $Seconds, $RamSizeMB
    dsk = Start-Job -ScriptBlock { param($p,$s,$m,$path) & python $p soak --seconds $s --size-mb $m --path $path --force } -ArgumentList $dsk, $Seconds, $DiskSizeMB, $DiskPath
}

$mon = Start-Job -ScriptBlock {
    param($s)
    $end = (Get-Date).AddSeconds($s + 20); $a = @()
    while ((Get-Date) -lt $end) {
        $l = & nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,fan.speed --format=csv,noheader 2>$null
        $os = Get-CimInstance Win32_OperatingSystem
        $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
        if ($l -match ',') {
            $p = $l -split ',' | ForEach-Object { [double](($_ -replace '[^0-9\.]', '')) }
            $a += [pscustomobject]@{ gpu = $p[0]; temp = $p[1]; power = $p[2]; sm = $p[3]; fan = $p[4]
                cpu = [math]::Round($cpu, 0); freeMiB = [math]::Round($os.FreePhysicalMemory / 1024, 0) }
        }
        Start-Sleep -Seconds 2
    }
    return $a
} -ArgumentList $Seconds

$results = @{}
foreach ($k in $jobs.Keys) {
    $o = Receive-Job $jobs[$k] -Wait -ErrorAction SilentlyContinue
    $jl = $o | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
    $results[$k] = if ($jl) { ($jl -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { @{ error = 'no result' } }
    Remove-Job $jobs[$k] -Force -ErrorAction SilentlyContinue
}
$s = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue
$m = $s | Measure-Object gpu, temp, power, sm, fan, cpu, freeMiB -Average -Maximum
$stat = @{}
foreach ($p in 'gpu', 'temp', 'power', 'sm', 'fan', 'cpu', 'freeMiB') {
    $x = $m | Where-Object { $_.Property -eq $p }
    $stat[$p] = @{ avg = [math]::Round($x.Average, 0); max = [math]::Round($x.Maximum, 0) }
}
$throttle = & nvidia-smi -q -d PERFORMANCE 2>$null | Select-String -Pattern 'HW Thermal Slowdown|HW Power Braking|SW Thermal' | ForEach-Object { $_.Line.Trim() }
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
$md += "**Telemetry:** GPU util $($stat.gpu.avg)/$($stat.gpu.max)% - GPU temp $($stat.temp.avg)/$($stat.temp.max) C - GPU power $($stat.power.avg)/$($stat.power.max) W - GPU fan $($stat.fan.avg)/$($stat.fan.max)% - CPU $($stat.cpu.avg)/$($stat.cpu.max)% - free RAM min $($stat.freeMiB.max - 0) MiB (avg $($stat.freeMiB.avg))"
$md += ""
$md += "**Stability:** $(if ($report.errors.Count) { $report.errors -join '; ' } else { 'no TDR/WHEA/BugCheck' })"
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8
Write-Host "[done] wrote $base.json and $base.md" -ForegroundColor Green

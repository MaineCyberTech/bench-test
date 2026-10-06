# Sample-GpuTelemetry.ps1 — sample GPU telemetry for a duration and report min/avg/max.
# Usage: .\Sample-GpuTelemetry.ps1 -Seconds 60 [-IntervalMs 1000] [-Json]
param(
    [int]$Seconds = 60,
    [int]$IntervalMs = 1000,
    [switch]$Json
)

$fields = 'utilization.gpu,temperature.gpu,power.draw,clocks.sm,memory.used,fan.speed'
$n = [int]($Seconds * 1000 / $IntervalMs)
$samples = @()
for ($i = 0; $i -lt $n; $i++) {
    $line = & nvidia-smi --query-gpu=$fields --format=csv,noheader 2>$null
    if ($line -match ',') {
        $p = $line -split ',' | ForEach-Object { [double](($_ -replace '[^0-9\.]', '')) }
        $samples += [pscustomobject]@{
            util = $p[0]; temp = $p[1]; power = $p[2]; sm = $p[3]; mem = $p[4]; fan = $p[5]
        }
    }
    Start-Sleep -Milliseconds $IntervalMs
}

if ($samples.Count -eq 0) { Write-Error "no telemetry samples"; exit 1 }
$m = $samples | Measure-Object util, temp, power, sm, mem, fan -Minimum -Maximum -Average
$out = [ordered]@{ samples = $samples.Count }
foreach ($p in 'util', 'temp', 'power', 'sm', 'mem', 'fan') {
    $x = $m | Where-Object { $_.Property -eq $p }
    $out[$p] = [ordered]@{ min = [math]::Round($x.Minimum, 1); avg = [math]::Round($x.Average, 1); max = [math]::Round($x.Maximum, 1) }
}

if ($Json) {
    $out | ConvertTo-Json -Depth 4
} else {
    "Telemetry over $($samples.Count) samples:"
    foreach ($p in 'util', 'temp', 'power', 'sm', 'mem', 'fan') {
        $v = $out[$p]
        "{0,-6} min={1,-8} avg={2,-8} max={3,-8}" -f $p, $v.min, $v.avg, $v.max
    }
}

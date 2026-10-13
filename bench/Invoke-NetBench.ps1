# Invoke-NetBench.ps1 - network latency + download/upload throughput.
# Uses Cloudflare's speed endpoints (no account): https://speed.cloudflare.com/__down?bytes=N
# and POST https://speed.cloudflare.com/__up
#
# Usage: .\Invoke-NetBench.ps1 [-DownMB 100] [-UpMB 25] [-OutDir .\results]
param(
    [int]$DownMB = 100,
    [int]$UpMB = 25,
    [string]$PingTarget = '1.1.1.1',
    [int]$PingCount = 12,
    [string]$OutDir = ''
)
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot '..\results' }
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = Join-Path $OutDir "$($env:COMPUTERNAME)-net-$stamp"

# gateway
$gw = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1).NextHop
function Ping-Avg($target, $count) {
    $r = Test-Connection -ComputerName $target -Count $count -ErrorAction SilentlyContinue
    if (-not $r) { return $null }
    return [math]::Round((($r | Measure-Object -Property ResponseTime -Average).Average), 1)
}

$latGw = if ($gw) { Ping-Avg $gw 8 } else { $null }
$latRef = Ping-Avg $PingTarget $PingCount
Write-Host "[net] gateway=$gw ping=$latGw ms ; $PingTarget ping=$latRef ms"

# download (curl prints bytes/sec). Cloudflare answers 403 for large byte counts
# (100MB), and a 403 body is a single byte - which was being recorded as a real
# "0 Mbps" measurement. Check the status and step down instead.
$downBps = $null; $downSecs = ''; $downErr = ''
foreach ($mb in @($DownMB, 50, 25, 10)) {
    if ($mb -le 0) { continue }
    $durl = "https://speed.cloudflare.com/__down?bytes=$($mb * 1024 * 1024)"
    $dout = & curl.exe -s -o NUL -w '%{speed_download} %{time_total} %{http_code}' --max-time 120 $durl 2>$null
    $dparts = ($dout -split '\s+')
    $bps = if ($dparts[0] -match '^[0-9\.]+$') { [double]$dparts[0] } else { $null }
    if ($dparts[2] -eq '200' -and $null -ne $bps -and $bps -gt 0) {
        $downBps = $bps; $downSecs = $dparts[1]; $downErr = ''; break
    }
    $downErr = "HTTP $($dparts[2]) for ${mb}MB"
}
# No successful transfer means no rate: null (with download_error saying why), never 0.0.
$downMbps = if ($null -ne $downBps) { [math]::Round($downBps * 8 / 1e6, 1) } else { $null }
$downMBs = if ($null -ne $downBps) { [math]::Round($downBps / 1e6, 1) } else { $null }
if ($downErr) {
    Write-Host "[net] download FAILED: $downErr"
} else {
    Write-Host ("[net] download {0} Mbps ({1} MB/s) in {2}s" -f $downMbps, $downMBs, $downSecs)
}

# upload
$tmp = Join-Path $env:TEMP "bench_up_$stamp.bin"
$fs = [IO.File]::Create($tmp); $buf = New-Object byte[] (1MB)
for ($i = 0; $i -lt $UpMB; $i++) { $fs.Write($buf, 0, $buf.Length) }
$fs.Close()
$uout = & curl.exe -s -o NUL -w '%{speed_upload} %{time_total} %{http_code}' --max-time 180 -X POST --data-binary "@$tmp" 'https://speed.cloudflare.com/__up' 2>$null
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
$uparts = ($uout -split '\s+')
$upBps = if ($uparts[0] -match '^[0-9\.]+$') { [double]$uparts[0] } else { $null }
# A non-200 response is not a measurement, whatever curl printed for its speed.
if ($uparts[2] -ne '200') { $upBps = $null }
$upErr = if ($uparts[2] -eq '200') { '' } else { "HTTP $($uparts[2])" }
$upMbps = if ($null -ne $upBps) { [math]::Round($upBps * 8 / 1e6, 1) } else { $null }
$upMBs = if ($null -ne $upBps) { [math]::Round($upBps / 1e6, 1) } else { $null }
if ($upErr) {
    Write-Host "[net] upload FAILED: $upErr"
} else {
    Write-Host ("[net] upload   {0} Mbps ({1} MB/s) in {2}s" -f $upMbps, $upMBs, $uparts[1])
}

$report = [ordered]@{
    host = $env:COMPUTERNAME; timestamp = (Get-Date).ToString('o')
    gateway = $gw; ping_gateway_ms = $latGw; ping_target = $PingTarget; ping_target_ms = $latRef
    download_mbps = $downMbps; download_mbs = $downMBs
    download_error = $downErr
    upload_mbps = $upMbps; upload_mbs = $upMBs
    upload_error = $upErr
}
$report | ConvertTo-Json -Depth 5 | Set-Content "$base.json" -Encoding UTF8
$downCell = if ($null -ne $downMbps) { "$downMbps Mbps ($downMBs MB/s)" } elseif ($downErr) { "failed: $downErr" } else { 'no result' }
$upCell = if ($null -ne $upMbps) { "$upMbps Mbps ($upMBs MB/s)" } elseif ($upErr) { "failed: $upErr" } else { 'no result' }
$md = @("# Network bench - $($env:COMPUTERNAME)", "", "Generated: $(Get-Date -Format o)", "",
    "| metric | value |", "|---|---|",
    "| gateway | $gw |", "| ping gateway | $latGw ms |", "| ping $PingTarget | $latRef ms |",
    "| download | $downCell |",
    "| upload | $upCell |")
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8
Write-Host "[done] wrote $base.json and $base.md" -ForegroundColor Green

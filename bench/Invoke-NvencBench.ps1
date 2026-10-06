# Invoke-NvencBench.ps1 — NVENC/NVDEC video-engine benchmark via ffmpeg.
# Usage: .\Invoke-NvencBench.ps1 [-FfmpegPath <ffmpeg.exe>] [-Seconds 30] [-OutFile <path>]
param(
    [string]$FfmpegPath,
    [int]$Seconds = 30,
    [string]$OutFile
)

function Find-Ffmpeg {
    if ($FfmpegPath -and (Test-Path $FfmpegPath)) { return $FfmpegPath }
    $c = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $cand = Get-ChildItem "$PSScriptRoot\..", "$env:USERPROFILE\Downloads", 'C:\tools', 'C:\ffmpeg' -Recurse -Filter ffmpeg.exe -Depth 4 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cand) { return $cand.FullName }
    return $null
}
$ff = Find-Ffmpeg
if (-not $ff) { Write-Error "ffmpeg.exe not found (pass -FfmpegPath). Download: https://www.gyan.dev/ffmpeg/builds/"; exit 1 }
Write-Host "[nvenc] ffmpeg: $ff"

$tmp = Join-Path $env:TEMP "bench_nvenc_$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$src = Join-Path $tmp 'src.mp4'
& $ff -hide_banner -y -f lavfi -i 'testsrc2=size=1920x1080:rate=60' -t 20 -c:v h264_nvenc -preset p4 -b:v 20M $src 2>&1 | Out-Null

function Bench($label, $vcodec) {
    $mon = Start-Job { param($s) $end=(Get-Date).AddSeconds($s); $a=@(); while((Get-Date) -lt $end){ $l = & nvidia-smi --query-gpu=utilization.encoder,utilization.decoder,power.draw,temperature.gpu --format=csv,noheader 2>$null; if($l -match ','){ $p=$l -split ','|%{[double](($_ -replace '[^0-9\.]',''))}; $a+=[pscustomobject]@{enc=$p[0];dec=$p[1];pw=$p[2];t=$p[3]} }; Start-Sleep -Milliseconds 1000 }; return $a } -ArgumentList $Seconds
    $out = & $ff -hide_banner -benchmark -hwaccel cuda -i $src -c:v $vcodec -preset p5 -b:v 12M -f null - 2>&1
    $rt = ($out | Select-String -Pattern 'bench:.*rtime=([0-9\.]+)' | Select-Object -Last 1)
    $rtime = if ($rt -match 'rtime=([0-9\.]+)') { [double]$Matches[1] } else { 0 }
    $fps = if ($rtime -gt 0) { [math]::Round(1200.0 / $rtime, 0) } else { 0 }
    $s = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue
    $encMax = ($s.enc | Measure-Object -Maximum).Maximum
    Write-Host "    $label fps=$fps enc%<=$encMax"
    return [ordered]@{ codec = $vcodec; fps = $fps; enc_util_max = $encMax }
}

$res = @()
$res += Bench 'H264' 'h264_nvenc'
$res += Bench 'HEVC' 'hevc_nvenc'
$res += Bench 'AV1' 'av1_nvenc'
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

$out = [ordered]@{ ffmpeg = $ff; results = $res }
if ($OutFile) { $out | ConvertTo-Json -Depth 5 | Set-Content $OutFile -Encoding UTF8; Write-Host "[nvenc] wrote $OutFile" }
$out | ConvertTo-Json -Depth 5

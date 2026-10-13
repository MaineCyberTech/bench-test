# Invoke-NvencBench.ps1 - hardware video-engine benchmark via ffmpeg.
# Auto-detects the GPU vendor and uses the matching encoder family:
#   NVIDIA -> *_nvenc, AMD -> *_amf, Intel -> *_qsv (falls back to whatever ffmpeg exposes).
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
    $roots = @("$PSScriptRoot\..", "$env:USERPROFILE\Downloads", 'C:\tools', 'C:\ffmpeg',
               "$env:LOCALAPPDATA\Microsoft\WinGet\Packages")
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        $cand = Get-ChildItem $r -Recurse -Filter ffmpeg.exe -Depth 5 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cand) { return $cand.FullName }
    }
    return $null
}
$ff = Find-Ffmpeg
if (-not $ff) { throw "ffmpeg.exe not found (pass -FfmpegPath). Download: https://www.gyan.dev/ffmpeg/builds/" }
Write-Host "[video] ffmpeg: $ff"

# --- detect GPU vendor + available encoders ---
$vendor = 'unknown'
$vc = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
    Where-Object { $_.AdapterCompatibility -or $_.Name }
$amd = $vc | Where-Object { $_.AdapterCompatibility -match 'AMD|Advanced Micro' -or $_.Name -match 'Radeon' } | Select-Object -First 1
$nvi = $vc | Where-Object { $_.AdapterCompatibility -match 'NVIDIA' -or $_.Name -match 'NVIDIA|GeForce|Quadro|RTX' } | Select-Object -First 1
$int = $vc | Where-Object { $_.AdapterCompatibility -match 'Intel' -or $_.Name -match 'Intel' } | Select-Object -First 1
if ($nvi) { $vendor = 'nvidia' } elseif ($amd) { $vendor = 'amd' } elseif ($int) { $vendor = 'intel' }
$encoders = (& $ff -hide_banner -encoders 2>&1 | ForEach-Object { $_.Trim() }) -join "`n"
Write-Host "[video] vendor=$vendor"

$candidates = switch ($vendor) {
    'nvidia' { @(@('H264', 'h264_nvenc'), @('HEVC', 'hevc_nvenc'), @('AV1', 'av1_nvenc')) }
    'amd' { @(@('H264', 'h264_amf'), @('HEVC', 'hevc_amf'), @('AV1', 'av1_amf')) }
    'intel' { @(@('H264', 'h264_qsv'), @('HEVC', 'hevc_qsv'), @('AV1', 'av1_qsv')) }
    default { @(@('H264', 'h264_nvenc'), @('H264', 'h264_amf'), @('H264', 'h264_qsv')) }
}
$tests = @()
foreach ($c in $candidates) {
    if ($encoders -match [regex]::Escape($c[1])) { $tests += , $c }
}
if ($tests.Count -eq 0) { $tests = @(@('H264', 'libx264')) }

function EncoderArgs($vcodec) {
    if ($vcodec -match '_nvenc$') { return @('-c:v', $vcodec, '-preset', 'p5', '-b:v', '12M') }
    if ($vcodec -match '_amf$') { return @('-c:v', $vcodec, '-quality', 'speed', '-usage', 'transcoding', '-b:v', '12M') }
    if ($vcodec -match '_qsv$') { return @('-c:v', $vcodec, '-preset', 'medium', '-b:v', '12M') }
    return @('-c:v', $vcodec, '-preset', 'veryfast', '-b:v', '12M')
}

$tmp = Join-Path $env:TEMP "bench_video_$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$src = Join-Path $tmp 'src.mp4'
Write-Host "[video] generating 1080p60 source (libx264) ..."
& $ff -hide_banner -y -f lavfi -i 'testsrc2=size=1920x1080:rate=60' -t 20 -c:v libx264 -preset veryfast -b:v 20M $src 2>&1 | Out-Null

function Bench($label, $vcodec) {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    $mon = Start-Job {
        param($s, $useSmi)
        $end = (Get-Date).AddSeconds($s); $a = @()
        while ((Get-Date) -lt $end) {
            if ($useSmi) {
                $l = & nvidia-smi --query-gpu=utilization.encoder,utilization.decoder,power.draw,temperature.gpu --format=csv,noheader 2>$null
                if ($l -match ',') { $p = $l -split ',' | ForEach-Object { [double](($_ -replace '[^0-9\.]', '')) }; $a += [pscustomobject]@{ enc = $p[0]; dec = $p[1]; pw = $p[2]; t = $p[3] } }
            } else {
                $e = $d = 0
                try {
                    $c = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop).CounterSamples
                    $e = [math]::Min(100, [math]::Round((($c | Where-Object { $_.InstanceName -match 'engtype_VideoEncode' } | Measure-Object CookedValue -Sum).Sum), 0))
                    $d = [math]::Min(100, [math]::Round((($c | Where-Object { $_.InstanceName -match 'engtype_VideoDecode' } | Measure-Object CookedValue -Sum).Sum), 0))
                } catch { }
                $a += [pscustomobject]@{ enc = $e; dec = $d; pw = 0; t = 0 }
            }
            Start-Sleep -Milliseconds 1000
        }
        return $a
    } -ArgumentList $Seconds, ([bool]$smi)

    $decArgs = @()
    if ($vendor -eq 'nvidia') { $decArgs = @('-hwaccel', 'cuda') }
    $eArgs = EncoderArgs $vcodec
    $out = & $ff -hide_banner -benchmark @decArgs -i $src @eArgs -f null - 2>&1
    $rt = ($out | Select-String -Pattern 'bench:.*rtime=([0-9\.]+)' | Select-Object -Last 1)
    $rtime = if ($rt -match 'rtime=([0-9\.]+)') { [double]$Matches[1] } else { 0 }
    $fps = if ($rtime -gt 0) { [math]::Round(1200.0 / $rtime, 0) } else { 0 }
    $s = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue
    $encMax = ($s.enc | Measure-Object -Maximum).Maximum
    Write-Host "    $label ($vcodec) fps=$fps enc%<=$encMax"
    return [ordered]@{ codec = $vcodec; fps = $fps; enc_util_max = $encMax }
}

$res = @()
foreach ($t in $tests) { $res += Bench $t[0] $t[1] }
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

# Record what the fps means: the source is pre-encoded (frame generation is
# outside the timed run) and the pipeline is decode+encode, with CUDA decode on
# NVIDIA. The Python bench records the same fields for cross-platform comparison.
$pipeline = if ($vendor -eq 'nvidia') { 'decode (hwaccel cuda) + encode to null muxer' } else { 'decode + encode to null muxer' }
$out = [ordered]@{ ffmpeg = $ff; vendor = $vendor; frames = 1200
    source = 'testsrc2 1920x1080@60 pre-encoded with libx264'; pipeline = $pipeline; results = $res }
if ($OutFile) { $out | ConvertTo-Json -Depth 5 | Set-Content $OutFile -Encoding UTF8; Write-Host "[video] wrote $OutFile" }
$out | ConvertTo-Json -Depth 5

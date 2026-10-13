# Invoke-FurMarkBench.ps1 - GPU render benchmark using FurMark 2.
#
# Mirrors the Linux gfx phase (Run-LinuxBench.sh): the same FurMark 2 CLI, the same
# text fields parsed out of it, so a Windows box and a live stick produce the same
# gfx metric keys and can be compared. Valley remains available as an alternative
# engine (Run-WorkstationBench.ps1 -GfxEngine valley).
#
# Get FurMark 2: https://geeks3d.com/furmark/ (the installer puts furmark.exe under
# C:\Program Files\Geeks3D\FurMark2_x64). Pass -FurMarkBin or set $env:FURMARK.
#
# Usage: .\Invoke-FurMarkBench.ps1 [-FurMarkBin <path>] [-Seconds 30] [-OutDir .\results]
param(
    [string]$FurMarkBin = '',
    [int]$Width = 1920,
    [int]$Height = 1080,
    [int]$Seconds = 30,
    [string]$Demo = 'furmark-gl',
    [string]$OutDir = ''
)
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot '..\results' }
$ErrorActionPreference = 'Continue'

function Resolve-FurMark {
    param([string]$Given)
    $cands = @()
    if ($Given) { $cands += $Given }
    if ($env:FURMARK) { $cands += $env:FURMARK }
    $cands += (Join-Path $PSScriptRoot 'tools\furmark\furmark.exe')
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if (-not $root) { continue }
        $cands += (Join-Path $root 'Geeks3D\FurMark2_x64\furmark.exe')
        $cands += (Join-Path $root 'FurMark2_x64\furmark.exe')
    }
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c -PathType Leaf)) { return (Resolve-Path $c).Path }
    }
    # last resort: the GUI's folder may exist with a differently-cased name
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $root) { continue }
        $hit = Get-ChildItem -Path $root -Filter 'furmark.exe' -Recurse -Depth 3 -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return ''
}

$fm = Resolve-FurMark -Given $FurMarkBin
if (-not $fm) {
    throw "furmark.exe not found (pass -FurMarkBin, set `$env:FURMARK, or install FurMark 2)"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = Join-Path $OutDir "$($env:COMPUTERNAME)-furmark-$stamp"
$export = Join-Path $base 'furmark'
New-Item -ItemType Directory -Force -Path $export | Out-Null
$log = "$base.log.txt"

Write-Host "[gfx] FurMark 2 ($Demo, ${Width}x${Height}, ${Seconds}s)"
Write-Host "      binary: $fm"

$fargs = @('--demo', $Demo, '--width', "$Width", '--height', "$Height", '--benchmark',
           '--duration-ms', "$($Seconds * 1000)", '--no-score-box', '--vsync', '0',
           '--log-gpu-data', '--gpu-monitor-print', '--export-dir', $export,
           '--hw-polling-interval', '1000')

$rc = 1
$timeoutMs = ($Seconds * 2 + 120) * 1000
try {
    $p = Start-Process -FilePath $fm -ArgumentList $fargs -WorkingDirectory (Split-Path $fm) `
        -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    if (-not $p.WaitForExit($timeoutMs)) {
        try { $p.Kill() } catch { }
        $rc = 124
        Write-Host "  furmark timed out after $($timeoutMs / 1000)s"
    } else {
        $p.Refresh()
        $rc = if ($null -ne $p.ExitCode) { [int]$p.ExitCode } else { 0 }
    }
} catch {
    Write-Host "  furmark failed to start: $($_.Exception.Message)"
}

$text = ''
if (Test-Path $log) { $text = Get-Content $log -Raw -ErrorAction SilentlyContinue }
if (Test-Path "$log.err") {
    $err = Get-Content "$log.err" -Raw -ErrorAction SilentlyContinue
    if ($err) { $text = "$text`n$err" }
}
$text | Write-Host

function Num([string]$pattern, [string]$cast = 'double') {
    $m = [regex]::Match($text, $pattern)
    if (-not $m.Success) { return $null }
    if ($cast -eq 'int') { return [int]$m.Groups[1].Value }
    return [double]$m.Groups[1].Value
}

$api = [regex]::Match($text, '3D API\s*:\s*(.+)')
$gpuCsv = $null
try {
    $csv = Get-ChildItem $export -Filter '*.csv' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($csv) { $gpuCsv = $csv.Name }
} catch { }

$result = [ordered]@{
    phase        = 'furmark'
    demo         = $Demo
    width        = $Width
    height       = $Height
    seconds      = $Seconds
    exit         = $rc
    score        = Num 'SCORE\s*:\s*(\d+)' 'int'
    fps_min      = Num 'FPS \(min/avg/max\)\s*:\s*([\d.]+)'
    fps_avg      = Num 'FPS \(min/avg/max\)\s*:\s*[\d.]+\s*/\s*([\d.]+)'
    fps_max      = Num 'FPS \(min/avg/max\)\s*:\s*[\d.]+\s*/\s*[\d.]+\s*/\s*([\d.]+)'
    temp_max_c   = Num 'max temperature:\s*(\d+)' 'int'
    api          = if ($api.Success) { $api.Groups[1].Value.Trim() } else { $null }
    gpu_csv      = $gpuCsv
    binary       = $fm
}
if ($null -eq $result.score -and $rc -ne 0) {
    $result.skipped = $true
    $result.reason = "furmark exited $rc"
}

$report = [ordered]@{
    host      = $env:COMPUTERNAME
    timestamp = (Get-Date).ToString('o')
    engine    = 'furmark'
    result    = $result
}
$report | ConvertTo-Json -Depth 6 | Set-Content "$base.json" -Encoding UTF8

$md = @("# FurMark 2 ($Demo) - $($env:COMPUTERNAME)", "",
        "Generated: $(Get-Date -Format o)", "",
        "| metric | value |", "|---|---|",
        "| score | $($result.score) |",
        "| FPS min/avg/max | $($result.fps_min) / $($result.fps_avg) / $($result.fps_max) |",
        "| 3D API | $($result.api) |",
        "| max temperature | $($result.temp_max_c) C |",
        "| duration | ${Seconds}s |",
        "| exit | $rc |")
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8

# keep FurMark's own log next to the report (post-mortems, like the Linux runner)
foreach ($extra in @('_furmark_log.txt', '_geexlab_log.txt')) {
    $src = Join-Path (Split-Path $fm) $extra
    if (Test-Path $src) { Copy-Item $src "$base.$($extra.TrimStart('_'))" -Force }
}

if ($result.skipped) { Write-Host "  furmark produced no score (exit $rc)" }
else { Write-Host "  score $($result.score) - FPS $($result.fps_min)/$($result.fps_avg)/$($result.fps_max)" }
Write-Host "  wrote $base.json"

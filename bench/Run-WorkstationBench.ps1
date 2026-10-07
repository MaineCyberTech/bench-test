# Run-WorkstationBench.ps1 - one-command workstation benchmark battery.
#
# Collects system/GPU info, runs the GPU and CPU compute phases with telemetry, runs a
# combined CPU+GPU stress, optionally runs LLM (Ollama), NVENC (ffmpeg) and a game-like
# render (Unigine Valley), and writes results to -OutDir.
#
# Usage:
#   .\Run-WorkstationBench.ps1
#   .\Run-WorkstationBench.ps1 -SoakSeconds 1800 -CombinedSeconds 1800
#   .\Run-WorkstationBench.ps1 -SkipCpu -SkipCombined -SkipLlm -SkipNvenc -SkipValley -NoInstall
param(
    [string]$OutDir = "$PSScriptRoot\..\results",
    [int]$SoakSeconds = 600,
    [int]$CpuSoakSeconds = 120,
    [int]$CombinedSeconds = 300,
    [string]$TorchIndex = 'https://download.pytorch.org/whl/cu128',
    [int]$Size = 12288,
    [string]$ValleyBin,
    [int]$RamSoakSeconds = 120,
    [string]$DiskDrive = 'C',
    [int]$DiskSizeMB = 2048,
    [switch]$SkipRam,
    [switch]$SkipDisk,
    [switch]$SkipNet,
    [switch]$SkipCpu,
    [switch]$SkipCombined,
    [switch]$SkipLlm,
    [switch]$SkipNvenc,
    [switch]$SkipValley,
    [switch]$NoInstall
)

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$base = Join-Path $OutDir "$($env:COMPUTERNAME)-$(Get-Date -Format yyyyMMdd-HHmmss)"
$summary = [ordered]@{ host = $env:COMPUTERNAME; timestamp = (Get-Date).ToString('o'); artifacts = [ordered]@{} }

function Get-NewestArtifact([string]$pattern) {
    Get-ChildItem $OutDir -Filter $pattern -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

Write-Host "=== [1/10] system + GPU inventory + sensors ===" -ForegroundColor Yellow
& "$PSScriptRoot\Get-GpuInfo.ps1" -Json | Set-Content "$base.info.json" -Encoding UTF8
& "$PSScriptRoot\Get-GpuInfo.ps1"
& "$PSScriptRoot\Get-SystemInfo.ps1" -OutFile "$base.sysinfo.json"
try { & "$PSScriptRoot\Get-Sensors.ps1" -OutFile "$base.sensors.json" } catch { Write-Host "  sensors skipped: $($_.Exception.Message)" }
$summary.artifacts.info = "$base.info.json"
$summary.artifacts.sysinfo = "$base.sysinfo.json"
$summary.artifacts.sensors = "$base.sensors.json"

Write-Host "=== [2/10] GPU compute battery (telemetry + report) ===" -ForegroundColor Yellow
& "$PSScriptRoot\Invoke-GpuBench.ps1" -OutDir $OutDir -SoakSeconds $SoakSeconds -TorchIndex $TorchIndex -Size $Size -NoInstall:$NoInstall

if (-not $SkipCpu) {
    Write-Host "=== [3/10] CPU compute battery ===" -ForegroundColor Yellow
    try {
        & "$PSScriptRoot\Invoke-CpuBench.ps1" -OutDir $OutDir -SoakSeconds $CpuSoakSeconds | Out-Null
        $f = Get-NewestArtifact "$($env:COMPUTERNAME)-cpu-*.json"
        if ($f) { $summary.artifacts.cpu = $f.FullName }
    } catch { Write-Host "  cpu skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [3/10] CPU skipped ===" -ForegroundColor DarkGray }

if (-not $SkipCombined) {
    Write-Host "=== [4/10] combined CPU+GPU stress ===" -ForegroundColor Yellow
    try {
        & "$PSScriptRoot\Invoke-CombinedStress.ps1" -OutDir $OutDir -Seconds $CombinedSeconds | Out-Null
        $f = Get-NewestArtifact "$($env:COMPUTERNAME)-combined-*.json"
        if ($f) { $summary.artifacts.combined = $f.FullName }
    } catch { Write-Host "  combined skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [4/10] combined skipped ===" -ForegroundColor DarkGray }

if (-not $SkipLlm) {
    Write-Host "=== [5/10] LLM inference (Ollama) ===" -ForegroundColor Yellow
    try { & "$PSScriptRoot\Invoke-LlmBench.ps1" -OutFile "$base.llm.json" | Out-Null; if (Test-Path "$base.llm.json") { $summary.artifacts.llm = "$base.llm.json" } }
    catch { Write-Host "  llm skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [5/10] LLM skipped ===" -ForegroundColor DarkGray }

if (-not $SkipNvenc) {
    Write-Host "=== [6/10] NVENC video engine ===" -ForegroundColor Yellow
    try { & "$PSScriptRoot\Invoke-NvencBench.ps1" -OutFile "$base.nvenc.json" | Out-Null; if (Test-Path "$base.nvenc.json") { $summary.artifacts.nvenc = "$base.nvenc.json" } }
    catch { Write-Host "  nvenc skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [6/10] NVENC skipped ===" -ForegroundColor DarkGray }

if (-not $SkipValley) {
    Write-Host "=== [7/10] game-like render (Unigine Valley) ===" -ForegroundColor Yellow
    if ($ValleyBin) {
        try { & "$PSScriptRoot\Invoke-ValleyBench.ps1" -ValleyBin $ValleyBin -OutDir $OutDir | Out-Null; $summary.artifacts.valley = "see screenshot in $OutDir" }
        catch { Write-Host "  valley skipped: $($_.Exception.Message)" }
    } else {
        Write-Host "  skipped: pass -ValleyBin '<path-to-valley\bin>' to include it" -ForegroundColor DarkGray
    }
} else { Write-Host "=== [7/10] Valley skipped ===" -ForegroundColor DarkGray }

if (-not $SkipRam) {
    Write-Host "=== [8/10] RAM bandwidth / latency / integrity / soak ===" -ForegroundColor Yellow
    try {
        & "$PSScriptRoot\Invoke-RamBench.ps1" -OutDir $OutDir -SoakSeconds $RamSoakSeconds | Out-Null
        $f = Get-NewestArtifact "$($env:COMPUTERNAME)-ram-*.json"
        if ($f) { $summary.artifacts.ram = $f.FullName }
    } catch { Write-Host "  ram skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [8/10] RAM skipped ===" -ForegroundColor DarkGray }

if (-not $SkipDisk) {
    Write-Host "=== [9/10] disk throughput + SMART health ===" -ForegroundColor Yellow
    try {
        & "$PSScriptRoot\Invoke-DiskBench.ps1" -OutDir $OutDir -Drive $DiskDrive -SizeMB $DiskSizeMB | Out-Null
        $f = Get-NewestArtifact "$($env:COMPUTERNAME)-disk-*.json"
        if ($f) { $summary.artifacts.disk = $f.FullName }
    } catch { Write-Host "  disk skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [9/10] disk skipped ===" -ForegroundColor DarkGray }

if (-not $SkipNet) {
    Write-Host "=== [10/10] network latency + throughput ===" -ForegroundColor Yellow
    try {
        & "$PSScriptRoot\Invoke-NetBench.ps1" -OutDir $OutDir | Out-Null
        $f = Get-NewestArtifact "$($env:COMPUTERNAME)-net-*.json"
        if ($f) { $summary.artifacts.net = $f.FullName }
    } catch { Write-Host "  network skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [10/10] network skipped ===" -ForegroundColor DarkGray }

$summary | ConvertTo-Json -Depth 5 | Set-Content "$base.manifest.json" -Encoding UTF8
Write-Host ""
Write-Host "[done] results in: $OutDir" -ForegroundColor Green
Get-ChildItem $OutDir -Filter "$($env:COMPUTERNAME)-*" | Select-Object Name, Length | Format-Table -AutoSize

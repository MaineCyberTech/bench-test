# Run-WorkstationBench.ps1 - one-command workstation benchmark battery.
#
# Collects system/GPU info, runs the GPU compute phases with telemetry, optionally
# runs LLM (Ollama), NVENC (ffmpeg) and a game-like render (Unigine Valley), and
# writes results to -OutDir.
#
# Usage:
#   .\Run-WorkstationBench.ps1
#   .\Run-WorkstationBench.ps1 -SoakSeconds 1800
#   .\Run-WorkstationBench.ps1 -SkipLlm -SkipNvenc -SkipValley -NoInstall
param(
    [string]$OutDir = "$PSScriptRoot\..\results",
    [int]$SoakSeconds = 600,
    [string]$TorchIndex = 'https://download.pytorch.org/whl/cu128',
    [int]$Size = 12288,
    [string]$ValleyBin,
    [switch]$SkipLlm,
    [switch]$SkipNvenc,
    [switch]$SkipValley,
    [switch]$NoInstall
)

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$base = Join-Path $OutDir "$($env:COMPUTERNAME)-$(Get-Date -Format yyyyMMdd-HHmmss)"
$summary = [ordered]@{ host = $env:COMPUTERNAME; timestamp = (Get-Date).ToString('o'); artifacts = [ordered]@{} }

Write-Host "=== [1/5] system + GPU info ===" -ForegroundColor Yellow
& "$PSScriptRoot\Get-GpuInfo.ps1" -Json | Set-Content "$base.info.json" -Encoding UTF8
& "$PSScriptRoot\Get-GpuInfo.ps1"
$summary.artifacts.info = "$base.info.json"

Write-Host "=== [2/5] GPU compute battery (telemetry + report) ===" -ForegroundColor Yellow
& "$PSScriptRoot\Invoke-GpuBench.ps1" -OutDir $OutDir -SoakSeconds $SoakSeconds -TorchIndex $TorchIndex -Size $Size -NoInstall:$NoInstall

if (-not $SkipLlm) {
    Write-Host "=== [3/5] LLM inference (Ollama) ===" -ForegroundColor Yellow
    try { & "$PSScriptRoot\Invoke-LlmBench.ps1" -OutFile "$base.llm.json" | Out-Null; $summary.artifacts.llm = "$base.llm.json" }
    catch { Write-Host "  llm skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [3/5] LLM skipped ===" -ForegroundColor DarkGray }

if (-not $SkipNvenc) {
    Write-Host "=== [4/5] NVENC video engine ===" -ForegroundColor Yellow
    try { & "$PSScriptRoot\Invoke-NvencBench.ps1" -OutFile "$base.nvenc.json" | Out-Null; $summary.artifacts.nvenc = "$base.nvenc.json" }
    catch { Write-Host "  nvenc skipped: $($_.Exception.Message)" }
} else { Write-Host "=== [4/5] NVENC skipped ===" -ForegroundColor DarkGray }

if (-not $SkipValley) {
    Write-Host "=== [5/5] game-like render (Unigine Valley) ===" -ForegroundColor Yellow
    if ($ValleyBin) {
        try { & "$PSScriptRoot\Invoke-ValleyBench.ps1" -ValleyBin $ValleyBin -OutDir $OutDir | Out-Null; $summary.artifacts.valley = "see screenshot in $OutDir" }
        catch { Write-Host "  valley skipped: $($_.Exception.Message)" }
    } else {
        Write-Host "  skipped: pass -ValleyBin '<path-to-valley\bin>' to include it" -ForegroundColor DarkGray
    }
} else { Write-Host "=== [5/5] Valley skipped ===" -ForegroundColor DarkGray }

$summary | ConvertTo-Json -Depth 5 | Set-Content "$base.manifest.json" -Encoding UTF8
Write-Host ""
Write-Host "[done] results in: $OutDir" -ForegroundColor Green
Get-ChildItem $OutDir -Filter "$($env:COMPUTERNAME)-*" | Select-Object Name, Length | Format-Table -AutoSize

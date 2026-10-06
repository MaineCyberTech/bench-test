# Invoke-GpuBench.ps1 — run the GPU compute phases with live telemetry and write a report.
# Usage: .\Invoke-GpuBench.ps1 [-OutDir .\results] [-SoakSeconds 600] [-NoInstall] [-TorchIndex <url>]
param(
    [string]$OutDir = "$PSScriptRoot\..\results",
    [int]$SoakSeconds = 600,
    [string]$TorchIndex = 'https://download.pytorch.org/whl/cu128',
    [int]$Size = 12288,
    [switch]$NoInstall
)

$ErrorActionPreference = 'Continue'
$bench = Join-Path $PSScriptRoot 'gpu_bench.py'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$hostname = $env:COMPUTERNAME
$base = Join-Path $OutDir "$hostname-$stamp"

function Get-TelemetryStats($samples) {
    if (-not $samples -or $samples.Count -eq 0) { return $null }
    $m = $samples | Measure-Object util, temp, power, sm, mem, fan -Average -Maximum
    $o = @{}
    foreach ($p in 'util', 'temp', 'power', 'sm', 'mem', 'fan') {
        $x = $m | Where-Object { $_.Property -eq $p }
        $o[$p] = @{ avg = [math]::Round($x.Average, 1); max = [math]::Round($x.Maximum, 1) }
    }
    return $o
}

function Invoke-Phase($name, $extraArgs, $telemetrySeconds) {
    Write-Host "[phase] $name" -ForegroundColor Cyan
    $mon = $null; $samples = @()
    if ($telemetrySeconds -gt 0) {
        $mon = Start-Job -ScriptBlock {
            param($secs)
            $end = (Get-Date).AddSeconds($secs); $s = @()
            while ((Get-Date) -lt $end) {
                $line = & nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,memory.used,fan.speed --format=csv,noheader 2>$null
                if ($line -match ',') {
                    $p = $line -split ',' | ForEach-Object { [double](($_ -replace '[^0-9\.]', '')) }
                    $s += [pscustomobject]@{ util = $p[0]; temp = $p[1]; power = $p[2]; sm = $p[3]; mem = $p[4]; fan = $p[5] }
                }
                Start-Sleep -Milliseconds 1000
            }
            return $s
        } -ArgumentList $telemetrySeconds
    }
    $out = & python $bench $name --size $Size --force @extraArgs 2>&1
    $out | Where-Object { $_ -notmatch '^RESULT_JSON:' } | ForEach-Object { Write-Host "    $_" }
    $jsonLine = $out | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
    $result = if ($jsonLine) { ($jsonLine -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { $null }
    if ($mon) { $samples = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue }
    return [pscustomobject]@{ phase = $name; result = $result; telemetry = (Get-TelemetryStats $samples) }
}

# --- ensure python + torch ---
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Error "python not found on PATH"; exit 1 }
$haveTorch = & python -c "import torch,sys; sys.stdout.write('1' if torch.cuda.is_available() else '0')" 2>$null
if ($haveTorch -ne '1') {
    if ($NoInstall) { Write-Error "torch+cuda not available and -NoInstall set"; exit 1 }
    Write-Host "[setup] installing PyTorch ($TorchIndex) ..." -ForegroundColor Yellow
    & python -m pip install --upgrade --index-url $TorchIndex torch 2>&1 | Select-Object -Last 3
    $haveTorch = & python -c "import torch,sys; sys.stdout.write('1' if torch.cuda.is_available() else '0')" 2>$null
    if ($haveTorch -ne '1') { Write-Error "PyTorch CUDA still unavailable"; exit 1 }
}

# --- run phases (short measurements per phase, long soak) ---
$phases = @()
$phases += Invoke-Phase 'info' @() 0
$phases += Invoke-Phase 'matmul' @('--seconds', '10') 0
$phases += Invoke-Phase 'membw' @('--seconds', '8') 0
$phases += Invoke-Phase 'pcie' @('--seconds', '8') 0
$phases += Invoke-Phase 'conv' @('--seconds', '15') 0
$phases += Invoke-Phase 'integrity' @() 0
$phases += Invoke-Phase 'streams' @('--seconds', '8') 0
$phases += Invoke-Phase 'soak' @('--seconds', "$SoakSeconds") $SoakSeconds

# --- stability scan ---
$ev = Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = (Get-Date).AddMinutes(-1) } -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -eq 4101 -or $_.ProviderName -match 'nvlddmkm|WHEA|BugCheck' }
$throttle = & nvidia-smi -q -d PERFORMANCE 2>$null | Select-String -Pattern 'SW Power Cap|HW Thermal Slowdown|HW Slowdown|HW Power Braking|SW Thermal' |
    ForEach-Object { $_.Line.Trim() }
$stability = [ordered]@{
    tdr_or_whea = if ($ev) { @($ev | ForEach-Object { "$($_.TimeCreated) id=$($_.Id) $($_.ProviderName)" }) } else { @() }
    throttle = @($throttle)
}

# --- write report ---
$report = [ordered]@{
    host = $hostname
    timestamp = (Get-Date).ToString('o')
    info = ($phases | Where-Object { $_.phase -eq 'info' }).result
    phases = $phases
    stability = $stability
}
$report | ConvertTo-Json -Depth 12 | Set-Content "$base.json" -Encoding UTF8

$md = @()
$md += "# GPU bench — $hostname"
$md += ""
$md += "Generated: $(Get-Date -Format o)"
$i = $report.info
if ($i) { $md += "**GPU:** $($i.device) | driver/torch cuda $($i.cuda) | SMs $($i.sm_count) | VRAM $($i.vram_gib) GiB" }
$md += ""
$md += "| phase | metric | telemetry (avg/max) |"
$md += "|---|---|---|"
foreach ($p in $phases) {
    if (-not $p.result) { continue }
    $t = $p.telemetry
    $tm = if ($t) { "util $($t.util.avg)/$($t.util.max)% · $($t.temp.avg)/$($t.temp.max)C · $($t.power.avg)/$($t.power.max)W · fan $($t.fan.avg)/$($t.fan.max)%" } else { "" }
    $r = $p.result
    switch ($p.phase) {
        'matmul' { foreach ($k in $r.results.PSObject.Properties.Name) { $md += "| matmul | $k $($r.results.$k.tflops) TFLOPS | $tm |" } }
        'membw' { foreach ($k in $r.results.PSObject.Properties.Name) { $md += "| membw | $k add $($r.results.$k.add_gbps) / reduce $($r.results.$k.reduce_gbps) GB/s | $tm |" } }
        'pcie' { $md += "| pcie | H2D $($r.h2d_gbps) / D2H $($r.d2h_gbps) GB/s | $tm |" }
        'conv' { $md += "| conv2d | $($r.tflops) TFLOPS | $tm |" }
        'integrity' { $md += "| vram | $($r.gib) GiB, mismatched=$($r.float_mismatched) bad=$($r.int_bad_elements) | $tm |" }
        'streams' { foreach ($k in $r.results.PSObject.Properties.Name) { $md += "| streams ($k) | $($r.results.$k) TFLOPS | $tm |" } }
        'soak' { $md += "| soak | $($r.matmul_tflops) TFLOPS over $($r.seconds)s | $tm |" }
    }
}
$md += ""
$md += "**Stability:** TDR/WHEA: $(if ($stability.tdr_or_whea.Count) { $stability.tdr_or_whea -join '; ' } else { 'none' })"
$md += ""
$md += "**Throttle:** $(($stability.throttle) -join ' | ')"
$md -join "`n" | Set-Content "$base.md" -Encoding UTF8

Write-Host ""
Write-Host "[done] wrote $base.json and $base.md" -ForegroundColor Green

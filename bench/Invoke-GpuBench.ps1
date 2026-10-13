# Invoke-GpuBench.ps1 - run the GPU compute phases with live telemetry and write a report.
# Usage: .\Invoke-GpuBench.ps1 [-OutDir .\results] [-SoakSeconds 600] [-NoInstall] [-TorchIndex <url>]
param(
    [string]$OutDir = "$PSScriptRoot\..\results",
    [int]$SoakSeconds = 600,
    [string]$TorchIndex = 'https://download.pytorch.org/whl/cu128',
    [int]$Size = 12288,
    [ValidateSet('auto', 'cuda', 'dml', 'cpu')]
    [string]$Backend = 'auto',
    [switch]$UntilStable,
    [int]$StableHold = 60,
    [switch]$NoInstall
)

$ErrorActionPreference = 'Continue'
$bench = Join-Path $PSScriptRoot 'gpu_bench.py'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$hostname = $env:COMPUTERNAME
$base = Join-Path $OutDir "$hostname-$stamp"
# The stability scan must cover the whole run: a TDR an hour into a soak is
# exactly what it looks for, and a one-minute window at the end would miss it.
$runStart = Get-Date

function Get-TelemetryStats($samples) {
    if (-not $samples -or $samples.Count -eq 0) { return $null }
    # Measure-Object counts a $null property as 0 in the average, so an unreadable
    # sensor drags the mean down ({72,73,$null} averaged 48.3). Filter per property,
    # and omit one that has no readings at all instead of reporting 0.
    $o = @{}
    foreach ($p in 'util', 'temp', 'power', 'sm', 'mem', 'fan') {
        $vals = @($samples | ForEach-Object { $_.$p } | Where-Object { $null -ne $_ })
        if ($vals.Count -eq 0) { continue }
        $o[$p] = @{ avg = [math]::Round((($vals | Measure-Object -Average).Average), 1); max = [math]::Round((($vals | Measure-Object -Maximum).Maximum), 1) }
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
            $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
            while ((Get-Date) -lt $end) {
                if ($smi) {
                    $line = & nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,memory.used,fan.speed --format=csv,noheader 2>$null
                    if ($line -match ',') {
                        $p = $line -split ',' | ForEach-Object { $t = ($_ -replace '[^0-9\.]', ''); if ($t -eq '' -or $t -eq '.') { $null } else { [double]$t } }
                        $s += [pscustomobject]@{ util = $p[0]; temp = $p[1]; power = $p[2]; sm = $p[3]; mem = $p[4]; fan = $p[5] }
                    }
                } else {
                    # Vendor-agnostic fallback (AMD/Intel on Windows): total GPU busy %.
                    $u = 0
                    try {
                        $c = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop).CounterSamples
                        $sum = ($c | Where-Object { $_.InstanceName -match 'engtype_3D' } | Measure-Object CookedValue -Sum).Sum
                        if ($sum) { $u = [math]::Min(100.0, [math]::Round($sum, 1)) }
                    } catch { }
                    $s += [pscustomobject]@{ util = $u; temp = $null; power = $null; sm = $null; mem = $null; fan = $null }
                }
                Start-Sleep -Milliseconds 1000
            }
            return $s
        } -ArgumentList $telemetrySeconds
    }
    $out = & python $bench $name --size $Size --force --backend $Backend @extraArgs 2>&1
    $out | Where-Object { $_ -notmatch '^RESULT_JSON:' -and $_ -notmatch '^\[backend\]' } | ForEach-Object { Write-Host "    $_" }
    $jsonLine = $out | Where-Object { $_ -match '^RESULT_JSON:' } | Select-Object -Last 1
    $result = if ($jsonLine) { ($jsonLine -replace '^RESULT_JSON:', '') | ConvertFrom-Json } else { $null }
    if ($mon) { $samples = Stop-Job $mon -ErrorAction SilentlyContinue; Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue }
    return [pscustomobject]@{ phase = $name; result = $result; telemetry = (Get-TelemetryStats $samples) }
}

# --- ensure python + a usable compute backend (cuda -> dml -> cpu) ---
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Error "python not found on PATH"; exit 1 }

$probe = & python $bench probe --backend $Backend 2>$null
$hasCuda = ($probe | Select-String '^CUDA=1') -ne $null
$hasDml = ($probe | Select-String '^DML=1') -ne $null
$detected = (($probe | Select-String '^BACKEND=') -replace '^BACKEND=', '')
Write-Host "[setup] probe: cuda=$hasCuda dml=$hasDml backend=$detected" -ForegroundColor DarkGray

if (-not $hasCuda -and -not $hasDml -and $Backend -ne 'cpu') {
    if ($NoInstall) {
        Write-Error "no CUDA or DirectML backend available and -NoInstall set"; exit 1
    } else {
        Write-Host "[setup] installing GPU backend: onnxruntime-directml (+ numpy, onnx) ..." -ForegroundColor Yellow
        & python -m pip install --upgrade onnxruntime-directml onnx numpy 2>&1 | Select-Object -Last 3
        $probe = & python $bench probe --backend $Backend 2>$null
        $hasDml = ($probe | Select-String '^DML=1') -ne $null
        if (-not $hasDml -and -not $hasCuda) {
            Write-Host "[setup] DirectML unavailable; trying PyTorch CPU" -ForegroundColor Yellow
            & python -m pip install --upgrade torch 2>&1 | Select-Object -Last 2
        }
    }
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
$soakArgs = @('--seconds', "$SoakSeconds")
if ($UntilStable) { $soakArgs += @('--until-stable', '--stable-hold', "$StableHold") }
$phases += Invoke-Phase 'soak' $soakArgs $SoakSeconds

# --- stability scan ---
$ev = Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $runStart } -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -eq 4101 -or $_.ProviderName -match 'nvlddmkm|WHEA|BugCheck' }
$throttle = @()
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $throttle = @(& nvidia-smi -q -d PERFORMANCE 2>$null | Select-String -Pattern 'SW Power Cap|HW Thermal Slowdown|HW Slowdown|HW Power Braking|SW Thermal' | Where-Object { $_.Line -notmatch 'Not Active' } |
        ForEach-Object { $_.Line.Trim() })
}
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
$md += "# GPU bench - $hostname"
$md += ""
$md += "Generated: $(Get-Date -Format o)"
$i = $report.info
if ($i) { $md += "**GPU:** $($i.device) | backend $($i.backend) | $($i.cuda) | SMs $($i.sm_count) | VRAM $($i.vram_gib) GiB" }
$md += ""
$md += "| phase | metric | telemetry (avg/max) |"
$md += "|---|---|---|"
foreach ($p in $phases) {
    if (-not $p.result) { $md += "| $($p.phase) | (no result) | |"; continue }
    if ($p.result.error) { $md += "| $($p.phase) | error: $($p.result.error) | |"; continue }
    if ($p.result.skipped) { $md += "| $($p.phase) | skipped | |"; continue }
    $t = $p.telemetry
    $tm = if ($t) { "util $($t.util.avg)/$($t.util.max)% | $($t.temp.avg)/$($t.temp.max)C | $($t.power.avg)/$($t.power.max)W | fan $($t.fan.avg)/$($t.fan.max)%" } else { "" }
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

# Invoke-LlmBench.ps1 - measure local LLM inference speed via Ollama.
# Usage: .\Invoke-LlmBench.ps1 [-Models 'qwen3:8b'] [-Tokens 128] [-OutFile <path>]
param(
    [string[]]$Models = @(),
    [int]$Tokens = 128,
    [string]$OutFile,
    [int]$CliTimeoutSec = 30,
    [int]$RequestTimeoutSec = 600
)

$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if (-not (Test-Path $ollama)) { throw "ollama not found" }

function Invoke-OllamaCli {
    # `ollama list` and `ollama ps` talk to the server and block FOREVER when it is
    # wedged - which is exactly how a full run sat on a handheld for eight hours with
    # the machine idle: the phase never got past `ollama list`, so no model line was
    # ever printed. Every CLI call is bounded here, and a timeout becomes a reason in
    # the report instead of a hung run.
    param([string]$Exe, [string[]]$CliArgs, [int]$TimeoutSec = 30)
    $job = Start-Job -ScriptBlock {
        param($e, $a)
        & $e @a 2>$null
    } -ArgumentList $Exe, $CliArgs
    try {
        if (-not (Wait-Job $job -Timeout $TimeoutSec)) {
            throw "ollama $($CliArgs -join ' ') timed out after ${TimeoutSec}s (is the server wedged?)"
        }
        return @(Receive-Job $job)
    } finally {
        Stop-Job $job -ErrorAction SilentlyContinue
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
}

# Fail fast with a reason when the server is not answering at all, rather than
# discovering it one unbounded CLI call at a time.
try {
    Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5 | Out-Null
} catch {
    throw "ollama server not responding on 127.0.0.1:11434: $($_.Exception.Message)"
}

if ($Models.Count -eq 0) {
    $Models = (Invoke-OllamaCli -Exe $ollama -CliArgs @('list') -TimeoutSec $CliTimeoutSec |
        Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ })
}
if ($Models.Count -eq 0) { throw "no ollama models installed" }

$results = @()
foreach ($m in $Models) {
    Write-Host "[llm] $m" -ForegroundColor Cyan
    $body = @{ model = $m; prompt = 'Write a detailed paragraph about graphics processing.'; stream = $false; options = @{ num_predict = $Tokens } } | ConvertTo-Json -Depth 4
    try {
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/generate' -Method Post -Body $body -ContentType 'application/json' -TimeoutSec $RequestTimeoutSec
        $dur = $r.eval_duration / 1e9
        $tps = if ($dur -gt 0) { [math]::Round($r.eval_count / $dur, 1) } else { 0 }
        "    tok/s=$tps eval=$($r.eval_count) in=$([math]::Round($dur,2))s"
        $results += [ordered]@{ model = $m; tok_per_s = $tps; eval_tokens = $r.eval_count; eval_seconds = [math]::Round($dur, 2) }
    } catch { Write-Host "    error: $($_.Exception.Message)"; $results += [ordered]@{ model = $m; error = $_.Exception.Message } }
}
# a `ps` timeout must not lose the results that were already measured
$proc = @()
try { $proc = Invoke-OllamaCli -Exe $ollama -CliArgs @('ps') -TimeoutSec $CliTimeoutSec }
catch { Write-Host "    ps: $($_.Exception.Message)" }
$out = [ordered]@{ results = $results; ps = @($proc) }
if ($OutFile) { $out | ConvertTo-Json -Depth 6 | Set-Content $OutFile -Encoding UTF8; Write-Host "[llm] wrote $OutFile" }
$out | ConvertTo-Json -Depth 6

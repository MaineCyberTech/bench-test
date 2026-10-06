# Invoke-LlmBench.ps1 - measure local LLM inference speed via Ollama.
# Usage: .\Invoke-LlmBench.ps1 [-Models 'qwen3:8b'] [-Tokens 128] [-OutFile <path>]
param(
    [string[]]$Models = @(),
    [int]$Tokens = 128,
    [string]$OutFile
)

$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if (-not (Test-Path $ollama)) { throw "ollama not found" }

if ($Models.Count -eq 0) {
    $Models = (& $ollama list 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ })
}
if ($Models.Count -eq 0) { throw "no ollama models installed" }

$results = @()
foreach ($m in $Models) {
    Write-Host "[llm] $m" -ForegroundColor Cyan
    $body = @{ model = $m; prompt = 'Write a detailed paragraph about graphics processing.'; stream = $false; options = @{ num_predict = $Tokens } } | ConvertTo-Json -Depth 4
    try {
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/generate' -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 600
        $dur = $r.eval_duration / 1e9
        $tps = if ($dur -gt 0) { [math]::Round($r.eval_count / $dur, 1) } else { 0 }
        "    tok/s=$tps eval=$($r.eval_count) in=$([math]::Round($dur,2))s"
        $results += [ordered]@{ model = $m; tok_per_s = $tps; eval_tokens = $r.eval_count; eval_seconds = [math]::Round($dur, 2) }
    } catch { Write-Host "    error: $($_.Exception.Message)"; $results += [ordered]@{ model = $m; error = $_.Exception.Message } }
}
$proc = & $ollama ps 2>$null
$out = [ordered]@{ results = $results; ps = @($proc) }
if ($OutFile) { $out | ConvertTo-Json -Depth 6 | Set-Content $OutFile -Encoding UTF8; Write-Host "[llm] wrote $OutFile" }
$out | ConvertTo-Json -Depth 6

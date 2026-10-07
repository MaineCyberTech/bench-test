# Invoke-ValleyBench.ps1 - optional game-like render test using Unigine Valley.
#
# Runs the Valley engine directly (Valley.exe), starts the built-in benchmark, and
# captures timed screenshots (Unigine writes no machine-readable score file - read the
# frame that shows the result dialog).
#
# Get Valley: https://benchmark.unigine.com/valley (self-extracting; extract, pass -ValleyBin).
# Usage: .\Invoke-ValleyBench.ps1 -ValleyBin 'C:\Unigine\valley\bin' [-Width 1920 -Height 1080 -Quality HIGH]
param(
    [Parameter(Mandatory = $true)][string]$ValleyBin,
    [int]$Width = 1920,
    [int]$Height = 1080,
    [ValidateSet('LOW', 'MEDIUM', 'HIGH', 'ULTRA')][string]$Quality = 'HIGH',
    [int]$Multisample = 0,
    [switch]$TessellationExtreme,
    [string]$OutDir = '',
    [int]$CaptureEverySeconds = 15,
    [int]$CaptureCount = 18
)
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot '..\results' }
$exe = Join-Path $ValleyBin 'Valley.exe'
if (-not (Test-Path $exe)) { throw "Valley.exe not found in $ValleyBin" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = Join-Path $OutDir "valley-$($env:COMPUTERNAME)-$stamp"

Add-Type -Namespace W -Name M -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint f,uint dx,uint dy,uint d,uint e);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int c);
[DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
public struct RECT { public int Left,Top,Right,Bottom; }
public struct POINT { public int X,Y; }
'@

$defines = ",RELEASE,LANGUAGE_EN,QUALITY_$Quality"
if ($TessellationExtreme) { $defines += ",TESSELLATION_EXTREME" }
$argstr = "-project_name Valley -data_path ../ -engine_config ../data/valley_1.0.cfg -system_script valley/unigine.cpp -sound_app openal -video_app direct3d11 -video_multisample $Multisample -video_fullscreen 1 -video_mode -1 -video_height $Height -video_width $Width -extern_define `"$defines`" -extern_plugin `",GPUMonitor`""

$mon = Start-Job { param($s) $end=(Get-Date).AddSeconds($s); $a=@(); while((Get-Date) -lt $end){ $l=& nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,fan.speed --format=csv,noheader 2>$null; if($l -match ','){$p=$l -split ','|%{[double](($_ -replace '[^0-9\.]',''))}; $a+=[pscustomobject]@{u=$p[0];t=$p[1];pw=$p[2];sm=$p[3];f=$p[4]}}; Start-Sleep -Milliseconds 1000 }; return $a } -ArgumentList ($CaptureEverySeconds * $CaptureCount + 60)

$p = Start-Process $exe -ArgumentList $argstr -WorkingDirectory $ValleyBin -PassThru
Write-Host "[valley] launched pid=$($p.Id); waiting for the engine window ..."
# find the engine window (title contains 'Unigine Valley')
$win = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $proc = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
    if ($proc -and $proc.MainWindowHandle -ne 0) { $win = $proc; break }
    $alt = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like 'Unigine Valley*' } | Select-Object -First 1
    if ($alt) { $win = $alt; break }
}
if (-not $win) { Write-Host "[valley] engine window not found; continuing anyway" }

if ($win -and $win.MainWindowHandle -ne 0) {
    [W.M]::ShowWindow($win.MainWindowHandle, 9) | Out-Null   # SW_RESTORE
    [W.M]::SetForegroundWindow($win.MainWindowHandle) | Out-Null
    Start-Sleep -Seconds 1
    $rc = New-Object W.M+RECT; [void][W.M]::GetClientRect($win.MainWindowHandle, [ref]$rc)
    $pt = New-Object W.M+POINT; [void][W.M]::ClientToScreen($win.MainWindowHandle, [ref]$pt)
    Write-Host "[valley] window client origin=($($pt.X),$($pt.Y)) -> clicking Benchmark at (+47,+13)"
    [W.M]::SetCursorPos($pt.X + 47, $pt.Y + 13) | Out-Null
    Start-Sleep -Milliseconds 300
    [W.M]::mouse_event(0x0002, 0, 0, 0, 0); Start-Sleep -Milliseconds 80; [W.M]::mouse_event(0x0004, 0, 0, 0, 0)  # focus
    Start-Sleep -Milliseconds 500
    [W.M]::SetCursorPos($pt.X + 47, $pt.Y + 13) | Out-Null
    [W.M]::mouse_event(0x0002, 0, 0, 0, 0); Start-Sleep -Milliseconds 80; [W.M]::mouse_event(0x0004, 0, 0, 0, 0)  # press
} else {
    Write-Host "[valley] clicking Benchmark at screen (47,13) as fallback"
    [W.M]::SetCursorPos(47, 13) | Out-Null; Start-Sleep -Milliseconds 300
    [W.M]::mouse_event(0x0002, 0, 0, 0, 0); Start-Sleep -Milliseconds 80; [W.M]::mouse_event(0x0004, 0, 0, 0, 0)
    Start-Sleep -Milliseconds 500
    [W.M]::mouse_event(0x0002, 0, 0, 0, 0); Start-Sleep -Milliseconds 80; [W.M]::mouse_event(0x0004, 0, 0, 0, 0)
}

Write-Host "[valley] benchmarking; capturing $CaptureCount frames every ${CaptureEverySeconds}s ..."
Add-Type -AssemblyName System.Drawing, System.Windows.Forms
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$shots = @()
for ($i = 1; $i -le $CaptureCount; $i++) {
    Start-Sleep -Seconds $CaptureEverySeconds
    if (-not (Get-Process -Id $p.Id -ErrorAction SilentlyContinue)) { break }
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
    $f = "$base-f$('{0:00}' -f $i).png"; $bmp.Save($f); $g.Dispose(); $bmp.Dispose()
    $shots += $f
}
Write-Host "[valley] frames: $($shots.Count) -> $base-f*.png" -ForegroundColor Green

$s = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue
$m = $s | Measure-Object u, t, pw, sm, f -Average -Maximum
"valley telemetry: util avg/max=$([math]::Round(($m|?{$_.Property -eq 'u'}).Average,0))/$([math]::Round(($m|?{$_.Property -eq 'u'}).Maximum,0))%  temp max=$([math]::Round(($m|?{$_.Property -eq 't'}).Maximum,0))C  power max=$([math]::Round(($m|?{$_.Property -eq 'pw'}).Maximum,0))W  fan max=$([math]::Round(($m|?{$_.Property -eq 'f'}).Maximum,0))%"

Get-Process -Id $p.Id -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
[pscustomobject]@{ frames = $shots; util_max = ($m | Where-Object { $_.Property -eq 'u' }).Maximum; temp_max = ($m | Where-Object { $_.Property -eq 't' }).Maximum } | ConvertTo-Json

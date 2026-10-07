# Invoke-ValleyBench.ps1 - game-like render test using Unigine Valley.
#
# Tries the direct engine (Valley.exe). If the engine comes up WINDOWED (which happens when
# the desktop is busy, and hides the benchmark result panel), it falls back to driving the
# Unigine LAUNCHER, which launches the engine fullscreen. Then it starts the built-in
# benchmark and captures a series of frames (Unigine writes no machine-readable score file -
# read the frame that shows the result dialog).
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
    [int]$CaptureEverySeconds = 10,
    [int]$CaptureCount = 32,
    [ValidateSet('auto', 'direct', 'launcher')][string]$Mode = 'auto',
    [string]$LauncherRunFraction = '0.874,0.819'
)
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot '..\results' }
$exe = Join-Path $ValleyBin 'Valley.exe'
$launcher = Join-Path $ValleyBin 'browser_x86.exe'
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
Add-Type -AssemblyName System.Drawing, System.Windows.Forms
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds

function Click([int]$x, [int]$y) {
    [W.M]::SetCursorPos($x, $y) | Out-Null; Start-Sleep -Milliseconds 300
    [W.M]::mouse_event(0x0002, 0, 0, 0, 0); Start-Sleep -Milliseconds 80; [W.M]::mouse_event(0x0004, 0, 0, 0, 0)
}
function Click2([int]$x, [int]$y) { Click $x $y; Start-Sleep -Milliseconds 500; Click $x $y }
function Get-EngineWin { Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like 'Unigine Valley Benchmark 1.0*' } | Select-Object -First 1 }
function Get-LauncherWin { Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like 'Unigine Valley Benchmark (Basic Edition)*' } | Select-Object -First 1 }
function Get-ClientOrigin($h) { $pt = New-Object W.M+POINT; [void][W.M]::ClientToScreen($h, [ref]$pt); return $pt }
function Is-Fullscreen($h) { $r = New-Object W.M+RECT; [void][W.M]::GetClientRect($h, [ref]$r); return (($r.Right - $r.Left) -ge $screen.Width -and ($r.Bottom - $r.Top) -ge $screen.Height) }

$defines = ",RELEASE,LANGUAGE_EN,QUALITY_$Quality"
if ($TessellationExtreme) { $defines += ",TESSELLATION_EXTREME" }
$engineArgs = "-project_name Valley -data_path ../ -engine_config ../data/valley_1.0.cfg -system_script valley/unigine.cpp -sound_app openal -video_app direct3d11 -video_multisample $Multisample -video_fullscreen 1 -video_mode -1 -video_height $Height -video_width $Width -extern_define `"$defines`" -extern_plugin `",GPUMonitor`""

Get-Process browser_x86, Valley -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$win = $null
if ($Mode -ne 'launcher') {
    Write-Host "[valley] direct engine launch ..."
    $p = Start-Process $exe -ArgumentList $engineArgs -WorkingDirectory $ValleyBin -PassThru
    for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 1; $win = Get-EngineWin; if ($win) { break } }
    if ($win -and (Is-Fullscreen $win.MainWindowHandle)) {
        Write-Host "[valley] direct engine is fullscreen"
    } else {
        Write-Host "[valley] direct engine not fullscreen (or absent); switching to launcher"
        Get-Process -Id $p.Id -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Get-Process Valley -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        $win = $null
    }
}
if (-not $win -and $Mode -ne 'direct') {
    if (-not (Test-Path $launcher)) { throw "launcher browser_x86.exe not found in $ValleyBin" }
    Write-Host "[valley] launcher launch ..."
    $lp = Start-Process $launcher -ArgumentList '-config', '..\data\launcher\launcher.xml' -WorkingDirectory $ValleyBin -PassThru
    for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Seconds 1; $lw = Get-LauncherWin; if ($lw) { break } }
    if ($lw) {
        [W.M]::ShowWindow($lw.MainWindowHandle, 9) | Out-Null
        [W.M]::SetForegroundWindow($lw.MainWindowHandle) | Out-Null
        Start-Sleep -Seconds 1
        $o = Get-ClientOrigin $lw.MainWindowHandle
        $r = New-Object W.M+RECT; [void][W.M]::GetClientRect($lw.MainWindowHandle, [ref]$r)
        $frac = $LauncherRunFraction -split ','
        $rx = [int]($o.X + [double]$frac[0] * ($r.Right - $r.Left))
        $ry = [int]($o.Y + [double]$frac[1] * ($r.Bottom - $r.Top))
        Write-Host "[valley] launcher RUN at ($rx,$ry)"
        Click2 $rx $ry
    }
    for ($i = 0; $i -lt 40; $i++) { Start-Sleep -Seconds 1; $win = Get-EngineWin; if ($win) { break } }
}

if ($win -and $win.MainWindowHandle -ne 0) {
    [W.M]::ShowWindow($win.MainWindowHandle, 9) | Out-Null
    [W.M]::SetForegroundWindow($win.MainWindowHandle) | Out-Null
    Start-Sleep -Seconds 1
    $o = Get-ClientOrigin $win.MainWindowHandle
    Write-Host "[valley] engine client origin=($($o.X),$($o.Y)) -> Benchmark at (+47,+13)"
    Click2 ($o.X + 47) ($o.Y + 13)
} else {
    Write-Host "[valley] engine window not found; fallback click (47,13)"
    Click2 47 13
}

$mon = Start-Job { param($s) $end=(Get-Date).AddSeconds($s); $a=@(); while((Get-Date) -lt $end){ $l=& nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,fan.speed --format=csv,noheader 2>$null; if($l -match ','){$p=$l -split ','|%{[double](($_ -replace '[^0-9\.]',''))}; $a+=[pscustomobject]@{u=$p[0];t=$p[1];pw=$p[2];sm=$p[3];f=$p[4]}}; Start-Sleep -Milliseconds 1000 }; return $a } -ArgumentList ($CaptureEverySeconds * $CaptureCount + 30)

Write-Host "[valley] benchmarking; capturing $CaptureCount frames every ${CaptureEverySeconds}s ..."
$shots = @()
for ($i = 1; $i -le $CaptureCount; $i++) {
    Start-Sleep -Seconds $CaptureEverySeconds
    if (-not (Get-EngineWin)) { Write-Host "[valley] engine closed at f$i"; break }
    $bmp = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
    $fp = "$base-f$('{0:00}' -f $i).png"; $bmp.Save($fp); $g.Dispose(); $bmp.Dispose()
    $shots += $fp
}
Write-Host "[valley] frames: $($shots.Count) -> $base-f*.png" -ForegroundColor Green

$s = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue
$m = $s | Measure-Object u, t, pw, sm, f -Average -Maximum
"valley telemetry: util avg/max=$([math]::Round(($m|?{$_.Property -eq 'u'}).Average,0))/$([math]::Round(($m|?{$_.Property -eq 'u'}).Maximum,0))%  temp max=$([math]::Round(($m|?{$_.Property -eq 't'}).Maximum,0))C  power max=$([math]::Round(($m|?{$_.Property -eq 'pw'}).Maximum,0))W  fan max=$([math]::Round(($m|?{$_.Property -eq 'f'}).Maximum,0))%"

Get-Process browser_x86, Valley -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
[pscustomobject]@{ frames = $shots; util_max = ($m | Where-Object { $_.Property -eq 'u' }).Maximum; temp_max = ($m | Where-Object { $_.Property -eq 't' }).Maximum } | ConvertTo-Json

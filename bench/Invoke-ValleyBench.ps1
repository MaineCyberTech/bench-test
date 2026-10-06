# Invoke-ValleyBench.ps1 - optional game-like render test using Unigine Valley.
#
# Unigine Valley is a launcher/engine pair; this script runs the engine directly
# (Valley.exe) at a chosen resolution/quality, starts the built-in benchmark, and
# screenshots the result dialog (Unigine does not write a machine-readable score file).
#
# Get Valley: https://benchmark.unigine.com/valley  (self-extracting; extract and pass -ValleyBin).
# Usage: .\Invoke-ValleyBench.ps1 -ValleyBin 'C:\Unigine\valley\bin' [-Width 1920 -Height 1080 -Quality ULTRA -Multisample 3]
param(
    [Parameter(Mandatory = $true)][string]$ValleyBin,
    [int]$Width = 1920,
    [int]$Height = 1080,
    [ValidateSet('LOW', 'MEDIUM', 'HIGH', 'ULTRA')][string]$Quality = 'HIGH',
    [int]$Multisample = 0,          # 0=off,1=2x,2=4x,3=8x
    [switch]$TessellationExtreme,
    [string]$OutDir = "$PSScriptRoot\..\results"
)
$exe = Join-Path $ValleyBin 'Valley.exe'
if (-not (Test-Path $exe)) { Write-Error "Valley.exe not found in $ValleyBin"; exit 1 }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y); [DllImport("user32.dll")] public static extern void mouse_event(uint f,uint dx,uint dy,uint d,uint e);' -Name M -Namespace W

$defines = ",RELEASE,LANGUAGE_EN,QUALITY_$Quality"
if ($TessellationExtreme) { $defines += ",TESSELLATION_EXTREME" }
$argstr = "-project_name Valley -data_path ../ -engine_config ../data/valley_1.0.cfg -system_script valley/unigine.cpp -sound_app openal -video_app direct3d11 -video_multisample $Multisample -video_fullscreen 1 -video_mode -1 -video_height $Height -video_width $Width -extern_define `"$defines`" -extern_plugin `",GPUMonitor`""

$mon = Start-Job { param($s) $end=(Get-Date).AddSeconds($s); $a=@(); while((Get-Date) -lt $end){ $l=& nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw,clocks.sm,fan.speed --format=csv,noheader 2>$null; if($l -match ','){$p=$l -split ','|%{[double](($_ -replace '[^0-9\.]',''))}; $a+=[pscustomobject]@{u=$p[0];t=$p[1];pw=$p[2];sm=$p[3];f=$p[4]}}; Start-Sleep -Milliseconds 1000 }; return $a } -ArgumentList 260

$p = Start-Process $exe -ArgumentList $argstr -WorkingDirectory $ValleyBin -PassThru
Write-Host "[valley] launched pid=$($p.Id); starting benchmark in 20s ..."
Start-Sleep -Seconds 20
$wsh = New-Object -ComObject WScript.Shell
$wsh.AppActivate('Unigine Valley') | Out-Null
Start-Sleep -Seconds 1
[W.M]::SetCursorPos(47, 13) | Out-Null   # the on-screen "Benchmark" button (top-left)
Start-Sleep -Milliseconds 300
[W.M]::mouse_event(0x0002, 0, 0, 0, 0); Start-Sleep -Milliseconds 80; [W.M]::mouse_event(0x0004, 0, 0, 0, 0)
Write-Host "[valley] benchmark running (~3 min) ..."
Start-Sleep -Seconds 220

Add-Type -AssemblyName System.Drawing, System.Windows.Forms
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$shot = Join-Path $OutDir "valley-$($env:COMPUTERNAME)-$(Get-Date -Format yyyyMMdd-HHmmss).png"
$bmp.Save($shot); $g.Dispose(); $bmp.Dispose()
Write-Host "[valley] result screenshot: $shot" -ForegroundColor Green

$s = Receive-Job $mon -Wait -ErrorAction SilentlyContinue; Remove-Job $mon -Force -ErrorAction SilentlyContinue
$m = $s | Measure-Object u, t, pw, sm, f -Average -Maximum
"valley telemetry: util avg/max=$([math]::Round(($m|?{$_.Property -eq 'u'}).Average,0))/$([math]::Round(($m|?{$_.Property -eq 'u'}).Maximum,0))%  temp max=$([math]::Round(($m|?{$_.Property -eq 't'}).Maximum,0))C  power max=$([math]::Round(($m|?{$_.Property -eq 'pw'}).Maximum,0))W  fan max=$([math]::Round(($m|?{$_.Property -eq 'f'}).Maximum,0))%"

Get-Process Valley -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
[pscustomobject]@{ screenshot = $shot; util_max = ($m | Where-Object { $_.Property -eq 'u' }).Maximum; temp_max = ($m | Where-Object { $_.Property -eq 't' }).Maximum } | ConvertTo-Json

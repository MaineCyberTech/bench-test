# Get-EnvInfo.ps1 - environment: OS update level, drivers, software, security, battery, Bluetooth.
# Usage: .\Get-EnvInfo.ps1 [-Json] [-OutFile <path>]
param([switch]$Json, [string]$OutFile)
$ErrorActionPreference = 'Continue'

$info = [ordered]@{ host = $env:COMPUTERNAME; timestamp = (Get-Date).ToString('o') }

# OS update level
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
$info.OS = [ordered]@{
    product = $cv.ProductName; edition = $cv.EditionID; display_version = $cv.DisplayVersion
    build = $cv.CurrentBuild; ubr = $cv.UBR; build_full = "$($cv.CurrentBuild).$($cv.UBR)"
    install_date = if ($cv.InstallDate) { ([DateTimeOffset]::FromUnixTimeSeconds($cv.InstallDate)).ToString('yyyy-MM-dd') } else { $null }
}
$info.RecentHotfixes = @(Get-HotFix -ErrorAction SilentlyContinue | Sort-Object InstalledOn -Descending | Select-Object -First 15 | ForEach-Object { "$($_.HotFixID) $($_.InstalledOn)" })

# Windows Update: recently installed
try {
    $info.RecentUpdates = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-WindowsUpdateClient'; Id = 19 } -MaxEvents 20 -ErrorAction Stop |
        ForEach-Object { "$($_.TimeCreated.ToString('yyyy-MM-dd')) $((($_.Message -replace '\s+', ' ')).Substring(0,[Math]::Min(90,$_.Message.Length)))" })
} catch { $info.RecentUpdates = @() }

# Drivers (key classes via PnP signed driver)
try {
    $drv = Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue
    $info.driver_count = @($drv).Count
    $info.Drivers = @($drv | Where-Object { $_.DeviceClass -in 'DISPLAY', 'NET', 'SCSIADAPTER', 'SYSTEM', 'HDC' } |
        Select-Object -First 60 | ForEach-Object { [ordered]@{ class = $_.DeviceClass; name = $_.DeviceName; version = $_.DriverVersion; date = $_.DriverDate; provider = $_.DriverProviderName } })
} catch { }

# Installed software
$sw = @()
foreach ($k in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*') {
    $sw += Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName } | ForEach-Object { [ordered]@{ name = $_.DisplayName; version = $_.DisplayVersion; publisher = $_.Publisher } }
}
$info.software_count = $sw.Count
$info.Software = @($sw | Sort-Object name -Unique)

# Security posture
$sec = [ordered]@{}
try { $mp = Get-MpComputerStatus -ErrorAction Stop; $sec.defender = [ordered]@{ am_running = $mp.AMServiceEnabled; realtime = $mp.RealTimeProtectionEnabled; tamper = $mp.IsTamperProtected; engine = $mp.AMEngineVersion; sig = $mp.AntivirusSignatureVersion; sig_age_days = $mp.AntivirusSignatureAge } } catch { $sec.defender = 'unavailable' }
try { $sec.firewall = @(Get-NetFirewallProfile -ErrorAction Stop | ForEach-Object { [ordered]@{ profile = $_.Name; enabled = $_.Enabled } }) } catch { }
try { $sec.secureboot = (Confirm-SecureBootUEFI -ErrorAction Stop) } catch { $sec.secureboot = 'n/a' }
try { $tpm = Get-Tpm -ErrorAction Stop; $sec.tpm = [ordered]@{ present = $tpm.TpmPresent; ready = $tpm.TpmReady; version = (Get-CimInstance -Namespace root/cimv2/security/microsofttpm -ClassName Win32_Tpm -ErrorAction SilentlyContinue).SpecVersion } } catch { $sec.tpm = 'unavailable' }
try { $sec.bitlocker = @(Get-BitLockerVolume -ErrorAction Stop | ForEach-Object { [ordered]@{ volume = $_.MountPoint; status = "$($_.ProtectionStatus)"; pct = $_.EncryptionPercentage } }) } catch { }
$info.Security = $sec

# Battery / Bluetooth / services / processes / startup
$bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue
$info.Battery = if ($bat) { @($bat | ForEach-Object { [ordered]@{ name = $_.Name; status = "$($_.BatteryStatus)"; charge_pct = $_.EstimatedChargeRemaining } }) } else { 'none' }
$info.Bluetooth = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'OK' } | ForEach-Object { $_.FriendlyName })
$info.services_running = @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Running' }).Count
$info.processes = @(Get-Process -ErrorAction SilentlyContinue).Count
$info.Startup = @(Get-CimInstance Win32_StartupCommand -ErrorAction SilentlyContinue | ForEach-Object { "$($_.Name) :: $($_.Command)" })
$info.dns_servers = @((Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.ServerAddresses }).ServerAddresses | Sort-Object -Unique)

if ($OutFile) { $info | ConvertTo-Json -Depth 8 | Set-Content $OutFile -Encoding UTF8 }
if ($Json) { $info | ConvertTo-Json -Depth 8 } else {
    "OS       $($info.OS.product) $($info.OS.display_version) build $($info.OS.build_full) ($($info.OS.edition))  installed $($info.OS.install_date)"
    "Defender $($info.Security.defender.realtime) realtime, sig age $($info.Security.defender.sig_age_days)d; SecureBoot=$($info.Security.secureboot); TPM=$($info.Security.tpm.present)"
    "Firewall $((($info.Security.firewall | ForEach-Object { "$($_.profile)=$($_.enabled)" }) -join ', '))"
    "Software $($info.software_count) apps, $($info.driver_count) drivers, $($info.services_running) services, $($info.processes) processes"
    "DNS      $($info.dns_servers -join ', ')"
    if ($info.Bluetooth) { "Bluetooth $($info.Bluetooth -join ', ')" }
}

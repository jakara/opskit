[CmdletBinding()]
param(
    [switch]$ConfigureOnly,
    [switch]$Offline,
    [switch]$Reconfigure,
    [string]$InstallDir = '',
    [switch]$KeepLocation,
    [switch]$Overwrite
)

$ErrorActionPreference = 'Stop'
try {
    . "$PSScriptRoot/scripts/bootstrap.ps1"
    Invoke-OpsKitLocation -Entry $PSCommandPath -Parameters $PSBoundParameters
    $arguments = @()
    if ($ConfigureOnly) { $arguments += '--configure-only' }
    if ($Offline) { $arguments += '--offline' }
    if ($Reconfigure) { $arguments += '--reconfigure' }
    Invoke-OpsKitInstaller -Installer "$PSScriptRoot/scripts/install.py" -Arguments $arguments -ConfigureOnly:$ConfigureOnly -Offline:$Offline
    exit 0
} catch {
    [Console]::Error.WriteLine("安装未完成：$($_.Exception.Message)")
    exit 1
}

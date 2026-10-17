[CmdletBinding()]
param(
    [switch]$HubOnly,
    [switch]$ConfigureOnly,
    [switch]$Offline,
    [switch]$Reconfigure,
    [ValidateRange(1, 65535)][int]$Port = 80
)

$ErrorActionPreference = 'Stop'
try {
    . "$PSScriptRoot/scripts/bootstrap.ps1"
    $arguments = @()
    if ($HubOnly) { $arguments += @('--hub-only', '--port', [string]$Port) }
    if ($ConfigureOnly) { $arguments += '--configure-only' }
    if ($Offline) { $arguments += '--offline' }
    if ($Reconfigure) { $arguments += '--reconfigure' }
    Invoke-OpsKitInstaller -Installer "$PSScriptRoot/scripts/install.py" -Arguments $arguments -ConfigureOnly:$ConfigureOnly -Offline:$Offline
    exit 0
} catch {
    [Console]::Error.WriteLine("安装未完成：$($_.Exception.Message)")
    exit 1
}

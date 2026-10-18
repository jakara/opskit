[CmdletBinding()]
param(
    [switch]$HubOnly,
    [switch]$ConfigureOnly,
    [switch]$Offline,
    [switch]$Reconfigure,
    [switch]$ReuseConfig,
    [ValidateRange(1, 65535)][int]$Port = 80,
    [string]$InstallDir = '',
    [switch]$KeepLocation,
    [switch]$Overwrite
)

$ErrorActionPreference = 'Stop'
try {
    . "$PSScriptRoot/scripts/bootstrap.ps1"
    Invoke-OpsKitLocation -Entry $PSCommandPath -Parameters $PSBoundParameters
    $arguments = @()
    if ($HubOnly) {
        $arguments += '--hub-only'
        if ($PSBoundParameters.ContainsKey('Port')) { $arguments += @('--port', [string]$Port) }
    }
    if ($ConfigureOnly) { $arguments += '--configure-only' }
    if ($Offline) { $arguments += '--offline' }
    if ($Reconfigure) { $arguments += '--reconfigure' }
    if ($ReuseConfig) { $arguments += '--reuse-config' }
    Invoke-OpsKitInstaller -Installer "$PSScriptRoot/scripts/install.py" -Arguments $arguments -ConfigureOnly:$ConfigureOnly -Offline:$Offline
    exit 0
} catch {
    [Console]::Error.WriteLine("安装未完成：$($_.Exception.Message)")
    exit 1
}

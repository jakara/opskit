[CmdletBinding()]
param(
    [ValidateSet('all', 'hub', 'management', 'collector')]
    [string]$Module = 'all',
    [switch]$Yes,
    [switch]$PurgeData,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
try {
    $options = @{ Yes = $Yes; PurgeData = $PurgeData; DryRun = $DryRun }
    switch ($Module) {
        'all' { & "$PSScriptRoot/management/uninstall.ps1" @options -All }
        'hub' { & "$PSScriptRoot/management/uninstall.ps1" @options -HubOnly }
        'management' { & "$PSScriptRoot/management/uninstall.ps1" @options }
        'collector' { & "$PSScriptRoot/collector/uninstall.ps1" @options }
    }
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("卸载未完成：$($_.Exception.Message)")
    exit 1
}

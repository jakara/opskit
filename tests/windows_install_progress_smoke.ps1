[CmdletBinding()]
param([string]$Repository = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('opskit-progress-' + [guid]::NewGuid())
$oldCapture = $env:OPSKIT_PROGRESS_CAPTURE
try {
    $scripts = New-Item -ItemType Directory -Path (Join-Path $temporary 'management/scripts') -Force
    Copy-Item (Join-Path $Repository 'install.ps1') (Join-Path $temporary 'install.ps1')
    Copy-Item (Join-Path $Repository 'management/install.ps1') (Join-Path $temporary 'management/install.ps1')
    @'
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
if ($Arguments -contains 'read') { Write-Output '3' }
$global:LASTEXITCODE = 0
'@ | Set-Content (Join-Path $scripts.FullName 'python.ps1') -Encoding UTF8
    @'
function Invoke-OpsKitLocation { }
function Get-OpsKitPython { return (Join-Path $PSScriptRoot 'python.ps1') }
function Invoke-OpsKitInstaller([string]$Installer, [string[]]$Arguments, [switch]$ConfigureOnly, [switch]$Offline) {
    @{ installer = $Installer; arguments = @($Arguments) } | ConvertTo-Json | Set-Content $env:OPSKIT_PROGRESS_CAPTURE -Encoding UTF8
    $global:LASTEXITCODE = 0
}
'@ | Set-Content (Join-Path $scripts.FullName 'bootstrap.ps1') -Encoding UTF8
    $env:OPSKIT_PROGRESS_CAPTURE = Join-Path $temporary 'captured.json'
    $shell = (Get-Process -Id $PID).Path
    $cases = @(
        @{ entry = 'install.ps1'; arguments = @('-Module', 'hub', '-ConfigureOnly'); port = $null },
        @{ entry = 'install.ps1'; arguments = @('-Module', 'hub', '-ConfigureOnly', '-Port', '19090'); port = '19090' },
        @{ entry = 'install.ps1'; arguments = @('-ConfigureOnly'); port = $null },
        @{ entry = 'management/install.ps1'; arguments = @('-HubOnly', '-ConfigureOnly'); port = $null },
        @{ entry = 'management/install.ps1'; arguments = @('-HubOnly', '-ConfigureOnly', '-Port', '19091'); port = '19091' }
    )
    foreach ($case in $cases) {
        if (Test-Path $env:OPSKIT_PROGRESS_CAPTURE) { Remove-Item $env:OPSKIT_PROGRESS_CAPTURE }
        $arguments = $case.arguments
        '' | & $shell -NoLogo -NoProfile -File (Join-Path $temporary $case.entry) @arguments
        if ($LASTEXITCODE -ne 0) { throw 'Entry failed.' }
        $capture = Get-Content $env:OPSKIT_PROGRESS_CAPTURE -Raw | ConvertFrom-Json
        if ($capture.arguments -notcontains '--hub-only') { throw 'Saved module choice was not applied.' }
        $portIndex = [Array]::IndexOf($capture.arguments, '--port')
        if ($case.port) {
            if ($portIndex -lt 0 -or $capture.arguments[$portIndex + 1] -ne $case.port) { throw 'Explicit port was not preserved.' }
        } elseif ($portIndex -ge 0) { throw 'Default port would overwrite the saved value.' }
    }
    Write-Output 'PASS: 5 PowerShell entry cases preserve saved defaults and explicit ports.'
} finally {
    $env:OPSKIT_PROGRESS_CAPTURE = $oldCapture
    if (Test-Path $temporary) { Remove-Item $temporary -Recurse -Force }
}

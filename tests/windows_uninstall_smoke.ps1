# Run with pwsh -NoProfile -File tests/windows_uninstall_smoke.ps1; no Docker commands execute.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$checks = 0
function Assert-True($Value, [string]$Message) {
    if (-not $Value) { throw $Message }
    $script:checks++
}
foreach ($path in @('uninstall.ps1', 'management/uninstall.ps1', 'collector/uninstall.ps1',
                   'tests/windows_uninstall_smoke.ps1')) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root $path), [ref]$tokens, [ref]$errors) | Out-Null
    Assert-True ($errors.Count -eq 0) "Parse failed: $path $errors"
}

$temporary = Join-Path ([IO.Path]::GetTempPath()) ('opskit uninstall 中文 ' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
$fixture = Join-Path $temporary 'python fixture.ps1'
$capture = Join-Path $temporary 'capture.json'
$variableNames = @('OPSKIT_UNINSTALL_CAPTURE', 'OPSKIT_UNINSTALL_PYTHON', 'OPSKIT_UNINSTALL_CODE',
                   'OPSKIT_UNINSTALL_MISSING', 'PYTHONUTF8', 'PYTHONIOENCODING', 'Path')
$oldValues = @{}
foreach ($name in $variableNames) { $oldValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$oldEncoding = [Console]::OutputEncoding
try {
    $env:OPSKIT_UNINSTALL_CAPTURE = $capture
    $env:OPSKIT_UNINSTALL_PYTHON = $fixture
    $env:OPSKIT_UNINSTALL_CODE = '0'
    $env:PYTHONUTF8 = 'before-utf8'
    $env:PYTHONIOENCODING = 'before-io'
    @'
@{ arguments = @($args); utf8 = $env:PYTHONUTF8; io = $env:PYTHONIOENCODING } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $env:OPSKIT_UNINSTALL_CAPTURE -Encoding UTF8
$global:LASTEXITCODE = [int]$env:OPSKIT_UNINSTALL_CODE
'@ | Set-Content -LiteralPath $fixture -Encoding UTF8
    foreach ($module in @('management', 'collector')) {
        $directory = Join-Path $temporary $module
        New-Item -ItemType Directory -Path (Join-Path $directory 'scripts') | Out-Null
        Copy-Item -LiteralPath (Join-Path $root "$module/uninstall.ps1") -Destination (Join-Path $directory 'uninstall.ps1')
        @'
function Update-OpsKitProcessPath { $env:Path = 'changed-only-in-fixture' }
function Find-OpsKitPython {
    if ($env:OPSKIT_UNINSTALL_MISSING -eq '1') { return $null }
    return $env:OPSKIT_UNINSTALL_PYTHON
}
function Get-OpsKitPython { throw 'Uninstall must never install Python.' }
function Initialize-OpsKitDocker { throw 'Uninstall must never start/install Docker.' }
function Install-OpsKitPackage { throw 'Uninstall must never install packages.' }
'@ | Set-Content -LiteralPath (Join-Path $directory 'scripts/bootstrap.ps1') -Encoding UTF8
    }
    Copy-Item -LiteralPath (Join-Path $root 'uninstall.ps1') -Destination (Join-Path $temporary 'uninstall.ps1')
    $entry = Join-Path $temporary 'uninstall.ps1'
    $beforePath = $env:Path
    foreach ($module in @('all', 'hub', 'management', 'collector')) {
        & $entry $module -Yes -PurgeData -DryRun
        Assert-True ($LASTEXITCODE -eq 0) "Entry failed: $module"
        $data = Get-Content -LiteralPath $capture -Raw | ConvertFrom-Json
        Assert-True ($data.arguments[0] -eq '-u') 'Python unbuffered option lost.'
        $expectedModule = if ($module -eq 'collector') { 'collector' } else { 'management' }
        Assert-True ($data.arguments[1] -eq (Join-Path $temporary "$expectedModule/scripts/uninstall.py")) "Wrong module route: $module"
        foreach ($flag in @('--yes', '--purge-data', '--dry-run')) {
            Assert-True ($data.arguments -contains $flag) "Flag missing: $flag"
        }
        Assert-True (($data.arguments -contains '--all') -eq ($module -eq 'all')) 'All scope changed.'
        Assert-True (($data.arguments -contains '--hub-only') -eq ($module -eq 'hub')) 'Hub scope changed.'
        Assert-True ($data.utf8 -eq '1' -and $data.io -eq 'utf-8') 'Chinese encoding was not enabled.'
        Assert-True ($env:PYTHONUTF8 -eq 'before-utf8' -and $env:PYTHONIOENCODING -eq 'before-io') 'Python environment leaked.'
        Assert-True ($env:Path -eq $beforePath) 'Process path leaked.'
        Assert-True ([Console]::OutputEncoding.WebName -eq $oldEncoding.WebName) 'Console encoding leaked.'
    }
    & $entry -DryRun
    $data = Get-Content -LiteralPath $capture -Raw | ConvertFrom-Json
    Assert-True ($LASTEXITCODE -eq 0 -and $data.arguments -contains '--all') 'Root default must uninstall all known OpsKit modules.'
    Assert-True ($data.arguments -notcontains '--yes' -and $data.arguments -notcontains '--purge-data') 'Default invocation must not bypass confirmation or purge data.'
    $env:OPSKIT_UNINSTALL_CODE = '17'
    & $entry hub -Yes
    Assert-True ($LASTEXITCODE -eq 17) 'Python nonzero exit status was lost.'
    Assert-True ($env:PYTHONUTF8 -eq 'before-utf8' -and $env:Path -eq $beforePath) 'Failure did not restore environment.'
    $env:OPSKIT_UNINSTALL_MISSING = '1'
    Remove-Item -LiteralPath $capture
    & $entry collector -DryRun
    Assert-True ($LASTEXITCODE -eq 1) 'Missing Python must fail without installation.'
    Assert-True (-not (Test-Path -LiteralPath $capture)) 'Python ran despite missing runtime.'
    Assert-True ($env:PYTHONUTF8 -eq 'before-utf8' -and $env:Path -eq $beforePath) 'Missing-runtime failure leaked environment.'
    Write-Host "PASS: $checks PowerShell parser and isolated uninstall flow assertions. No containers or packages changed."
} finally {
    foreach ($name in $variableNames) { [Environment]::SetEnvironmentVariable($name, $oldValues[$name], 'Process') }
    [Console]::OutputEncoding = $oldEncoding
    Remove-Item -LiteralPath $temporary -Recurse -Force
}

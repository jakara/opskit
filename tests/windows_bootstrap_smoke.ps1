# Run with pwsh -NoProfile -File tests/windows_bootstrap_smoke.ps1; no software is installed.
$ErrorActionPreference = 'Stop'
$bootstrap = Join-Path $PSScriptRoot '../management/scripts/bootstrap.ps1'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$checks = 0
function Assert-True($Value, [string]$Message) {
    if (-not $Value) { throw $Message }
    $script:checks++
}
function Assert-Throws([scriptblock]$Action, [string]$Pattern) {
    $caught = $false
    try { & $Action } catch {
        $caught = $true
        Assert-True ($_.Exception.Message -match $Pattern) "Unexpected failure: $($_.Exception.Message)"
    }
    Assert-True $caught 'Expected failure did not occur.'
}

foreach ($path in @('install.ps1', 'management/install.ps1', 'collector/install.ps1',
                   'management/scripts/bootstrap.ps1', 'collector/scripts/bootstrap.ps1')) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root $path), [ref]$tokens, [ref]$errors) | Out-Null
    Assert-True ($errors.Count -eq 0) "Parse failed: $path $errors"
}

$temporary = Join-Path ([IO.Path]::GetTempPath()) ('opskit-windows-bootstrap-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
$fixture = Join-Path $temporary 'python-fixture.ps1'
$capture = Join-Path $temporary 'capture.json'
@'
@{ arguments = @($args); utf8 = $env:PYTHONUTF8; io = $env:PYTHONIOENCODING } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $env:OPSKIT_BOOTSTRAP_CAPTURE -Encoding UTF8
$global:LASTEXITCODE = 0
if ($env:OPSKIT_LOCATION_TEST_CODE) { $global:LASTEXITCODE = [int]$env:OPSKIT_LOCATION_TEST_CODE }
'@ | Set-Content -LiteralPath $fixture -Encoding UTF8
$oldCapture = $env:OPSKIT_BOOTSTRAP_CAPTURE
$env:OPSKIT_BOOTSTRAP_CAPTURE = $capture
try {
    & {
        . $bootstrap
        function Update-OpsKitProcessPath { }
        function Get-OpsKitDockerStatus { return @{ Linux = $true; Windows = $false } }
        function Install-OpsKitPackage { throw 'Healthy Docker must be reused.' }
        function Initialize-OpsKitWSL { throw 'Healthy Docker must not modify WSL.' }
        Initialize-OpsKitDocker
        Assert-True $true 'Healthy Docker reuse.'
    }
    & {
        . $bootstrap
        function Update-OpsKitProcessPath { }
        function Get-OpsKitDockerStatus { return @{ Linux = $false; Windows = $true } }
        function Install-OpsKitPackage { throw 'Windows containers must not trigger installation.' }
        Assert-Throws { Initialize-OpsKitDocker } 'Linux'
    }
    & {
        . $bootstrap
        function Update-OpsKitProcessPath { }
        function Get-OpsKitDockerStatus { return @{ Linux = $false; Windows = $false } }
        function Find-OpsKitDockerDesktop { return $null }
        function Install-OpsKitPackage { throw 'Offline install attempted.' }
        function Initialize-OpsKitWSL { throw 'Offline WSL install attempted.' }
        Assert-Throws { Initialize-OpsKitDocker -Offline } '离线'
    }
    & {
        . $bootstrap
        function Find-OpsKitPython { return $null }
        function Install-OpsKitPackage { throw 'Offline Python download attempted.' }
        Assert-Throws { Get-OpsKitPython -Offline } '离线'
    }
    & {
        . $bootstrap
        $script:installedPython = $false
        function Find-OpsKitPython { if ($script:installedPython) { return $fixture } }
        function Install-OpsKitPackage([string]$Id, [switch]$AcceptPackageAgreements, [switch]$UserScope) {
            Assert-True ($Id -eq 'Python.Python.3.13') 'Wrong Python package.'
            Assert-True ($AcceptPackageAgreements -and $UserScope) 'Python must use user scope.'
            $script:installedPython = $true
        }
        Assert-True ((Get-OpsKitPython) -eq $fixture) 'Python not re-checked after installation.'
    }
    & {
        . $bootstrap
        function Get-OpsKitWinGet { return $fixture }
        function Update-OpsKitProcessPath { }
        Install-OpsKitPackage -Id 'Docker.DockerDesktop'
        $data = Get-Content -LiteralPath $capture -Raw | ConvertFrom-Json
        Assert-True ($data.arguments -contains '--interactive') 'Docker installer UI was hidden.'
        Assert-True ($data.arguments -notcontains '--accept-package-agreements') 'Docker package agreements were accepted automatically.'
        Assert-True ($data.arguments -notcontains '--accept-license') 'Docker license accepted automatically.'
    }
    & {
        . $bootstrap
        function Test-OpsKitWindows { return $true }
        function Update-OpsKitProcessPath { }
        function Get-OpsKitPython([switch]$Offline) {
            Assert-True $Offline 'Offline flag lost at Python dependency check.'
            return $fixture
        }
        function Initialize-OpsKitDocker { throw 'Configure-only must not inspect/install Docker.' }
        $oldUtf8 = $env:PYTHONUTF8
        Invoke-OpsKitInstaller -Installer 'installer with spaces.py' -Arguments @('--configure-only', '--offline') -ConfigureOnly -Offline
        $data = Get-Content -LiteralPath $capture -Raw | ConvertFrom-Json
        Assert-True ($data.arguments.Count -eq 4 -and $data.arguments[0] -eq '-u' -and $data.arguments[1] -eq 'installer with spaces.py') 'Python argv changed.'
        Assert-True ($data.arguments -contains '--offline') 'Offline Python argument lost.'
        Assert-True ($data.utf8 -eq '1' -and $data.io -eq 'utf-8') 'Chinese Python encoding not configured.'
        Assert-True ($env:PYTHONUTF8 -eq $oldUtf8) 'Calling environment not restored.'
    }
    & {
        . $bootstrap
        $oldReady = $env:OPSKIT_LOCATION_READY
        try {
            $env:OPSKIT_LOCATION_READY = '1'
            function Get-OpsKitPython { throw 'Relocated entry must not run location selection twice.' }
            function Test-OpsKitWindows { throw 'Ready entry must return immediately.' }
            Invoke-OpsKitLocation -Entry 'unused/install.ps1' -Parameters @{}
            Assert-True $true 'Ready entry did not return.'
        } finally { $env:OPSKIT_LOCATION_READY = $oldReady }
    }
    $locationDriver = Join-Path $temporary 'location-driver.ps1'
    @'
param([string]$Bootstrap, [string]$Fixture)
$ErrorActionPreference = 'Stop'
. $Bootstrap
function Test-OpsKitWindows { return $true }
function Update-OpsKitProcessPath { }
function Get-OpsKitPython([switch]$Offline) {
    if (-not $Offline) { throw 'Location selection lost offline mode.' }
    return $Fixture
}
$env:OPSKIT_LOCATION_READY = ''
$env:PYTHONUTF8 = 'previous-utf8'
$env:PYTHONIOENCODING = 'previous-io'
$parameters = @{
    Module = 'hub'; Port = 8081; InstallDir = 'C:\现场工具\Ops Kit';
    KeepLocation = [switch]$true; ConfigureOnly = [switch]$true;
    Offline = [switch]$true; Reconfigure = [switch]$false; Overwrite = [switch]$true
}
try {
    Invoke-OpsKitLocation -Entry 'C:\下载包\install.ps1' -Parameters $parameters
} finally {
    @{ utf8 = $env:PYTHONUTF8; io = $env:PYTHONIOENCODING } | ConvertTo-Json | Set-Content -LiteralPath ($env:OPSKIT_BOOTSTRAP_CAPTURE + '.restored') -Encoding UTF8
}
throw 'Original entry continued after relocated installer returned.'
'@ | Set-Content -LiteralPath $locationDriver -Encoding UTF8
    $oldLocationCode = $env:OPSKIT_LOCATION_TEST_CODE
    try {
        $env:OPSKIT_LOCATION_TEST_CODE = '23'
        $shell = (Get-Process -Id $PID).Path
        & $shell -NoLogo -NoProfile -File $locationDriver -Bootstrap $bootstrap -Fixture $fixture
        Assert-True ($LASTEXITCODE -eq 23) 'Relocated child exit code was lost or original installer continued.'
        $data = Get-Content -LiteralPath $capture -Raw | ConvertFrom-Json
        Assert-True ($data.arguments[0] -eq '-u' -and $data.arguments[1] -eq (Join-Path (Split-Path (Resolve-Path $bootstrap).Path -Parent) 'location.py')) 'Location helper path changed.'
        $separator = [Array]::IndexOf($data.arguments, '--')
        Assert-True ($separator -gt 0) 'Original parameters were not separated from location options.'
        $locationArgs = @($data.arguments[2..($separator - 1)])
        $childArgs = @($data.arguments[($separator + 1)..($data.arguments.Count - 1)])
        Assert-True (($childArgs -contains '--install-dir') -and ($childArgs -contains 'C:\现场工具\Ops Kit')) 'Unicode destination with spaces changed.'
        Assert-True ($childArgs -contains '--keep-location') 'KeepLocation not mapped after the separator.'
        Assert-True ($childArgs -contains '--overwrite') 'Overwrite not mapped after the separator.'
        foreach ($value in @('-Module', 'hub', '-Port', '8081', '-ConfigureOnly', '-Offline')) {
            Assert-True ($childArgs -contains $value) "Original parameter lost: $value"
        }
        Assert-True ($childArgs -notcontains '-Reconfigure' -and $childArgs -notcontains '-InstallDir' -and $childArgs -notcontains '-KeepLocation' -and $childArgs -notcontains '-Overwrite') 'False or consumed switches reached the relocated installer.'
        Assert-True ($data.utf8 -eq '1' -and $data.io -eq 'utf-8') 'Location helper did not use UTF-8.'
        $restored = Get-Content -LiteralPath ($capture + '.restored') -Raw | ConvertFrom-Json
        Assert-True ($restored.utf8 -eq 'previous-utf8' -and $restored.io -eq 'previous-io') 'Location helper leaked encoding changes.'
    } finally { $env:OPSKIT_LOCATION_TEST_CODE = $oldLocationCode }
    & {
        . $bootstrap
        function Get-OpsKitApplication { return $null }
        Assert-Throws { Get-OpsKitWinGet -Offline } '离线'
    }
    & {
        . $bootstrap
        function Get-OpsKitApplication { return $null }
        Assert-Throws { Initialize-OpsKitWSL -Offline } '离线'
    }
    & {
        . $bootstrap
        $script:wslInstalled = $false
        function Get-OpsKitApplication { return 'wsl-fixture.exe' }
        function Test-OpsKitWSL { return $script:wslInstalled }
        function Start-Process([string]$FilePath, [string[]]$ArgumentList, [string]$Verb, [switch]$Wait, [switch]$PassThru) {
            Assert-True ($FilePath -eq 'wsl-fixture.exe') 'Wrong WSL executable.'
            Assert-True (($ArgumentList -join ' ') -eq '--install --no-distribution') 'WSL installed an unexpected distribution.'
            Assert-True ($Verb -eq 'RunAs' -and $Wait -and $PassThru) 'WSL UAC/exit status was skipped.'
            $script:wslInstalled = $true
            return @{ ExitCode = 0 }
        }
        Initialize-OpsKitWSL
        Assert-True $script:wslInstalled 'WSL was not re-checked.'
    }
    & {
        . $bootstrap
        function Update-OpsKitProcessPath { }
        function Get-OpsKitDockerStatus { return @{ Linux = $false; Windows = $false } }
        function Find-OpsKitDockerDesktop { return 'docker-desktop-fixture.exe' }
        function Start-Process { }
        function Wait-OpsKitDocker { return $false }
        function Initialize-OpsKitWSL { }
        Assert-Throws { Initialize-OpsKitDocker } '首次许可'
    }
    & {
        . $bootstrap
        Assert-True (-not (Test-OpsKitPython 'C:\Users\test\AppData\Local\Microsoft\WindowsApps\python.exe')) 'Store stub must never be launched.'
    }
    & {
        . $bootstrap
        $script:wingetReady = $false
        function Get-OpsKitApplication { if ($script:wingetReady) { return 'winget-fixture.exe' } }
        function Get-PackageProvider { return $true }
        function Get-Module { return $true }
        function Import-Module { }
        function Repair-WinGetPackageManager { $script:wingetReady = $true; return 'repair status output' }
        function Update-OpsKitProcessPath { }
        Assert-True ((Get-OpsKitWinGet) -eq 'winget-fixture.exe') 'Repair output polluted WinGet executable path.'
    }
    @'
$global:LASTEXITCODE = 3010
'@ | Set-Content -LiteralPath $fixture -Encoding UTF8
    & {
        . $bootstrap
        function Get-OpsKitWinGet { return $fixture }
        function Update-OpsKitProcessPath { }
        Assert-Throws { Install-OpsKitPackage -Id 'Docker.DockerDesktop' } '重启'
    }
    @'
$global:LASTEXITCODE = 17
'@ | Set-Content -LiteralPath $fixture -Encoding UTF8
    & {
        . $bootstrap
        function Test-OpsKitWindows { return $true }
        function Update-OpsKitProcessPath { }
        function Get-OpsKitPython { return $fixture }
        $oldUtf8 = $env:PYTHONUTF8
        Assert-Throws { Invoke-OpsKitInstaller -Installer 'installer.py' -ConfigureOnly } '17'
        Assert-True ($env:PYTHONUTF8 -eq $oldUtf8) 'Failed Python invocation leaked environment changes.'
    }
    Write-Host "PASS: $checks PowerShell parser and isolated flow assertions. No packages were installed."
} finally {
    $env:OPSKIT_BOOTSTRAP_CAPTURE = $oldCapture
    Remove-Item -LiteralPath $temporary -Recurse -Force
}

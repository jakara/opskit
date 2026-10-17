# Windows PowerShell 5.1；此文件与 collector/scripts/bootstrap.ps1 保持一致。

function Update-OpsKitProcessPath {
    $paths = @([Environment]::GetEnvironmentVariable('Path', 'Machine'),
               [Environment]::GetEnvironmentVariable('Path', 'User'), $env:Path)
    if ($env:ProgramFiles) { $paths += "$env:ProgramFiles\Docker\Docker\resources\bin" }
    $env:Path = ($paths | Where-Object { $_ }) -join ';'
}

function Get-OpsKitApplication([string]$Name) {
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
}

function Test-OpsKitPython([string]$Executable, [string[]]$Prefix = @()) {
    if (-not $Executable -or $Executable -match '\\Microsoft\\WindowsApps\\python(?:3(?:\.\d+)?)?\.exe$') { return }
    # Windows PowerShell 5.1 会把原生命令 stderr 作为错误流，探测以退出码为准。
    $ErrorActionPreference = 'Continue'
    try {
        $found = @(& $Executable @Prefix -c 'import sys; sys.exit(1) if sys.version_info < (3, 9) else print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $found.Count -eq 1) {
            $path = [string]$found[0]
            if ((Test-Path -LiteralPath $path -PathType Leaf) -and $path -notmatch '\\Microsoft\\WindowsApps\\python(?:3(?:\.\d+)?)?\.exe$') { return $path }
        }
    } catch { }
}

function Find-OpsKitPython {
    $launcher = Get-OpsKitApplication 'py.exe'
    if ($launcher) {
        $python = Test-OpsKitPython $launcher @('-3')
        if ($python) { return $python }
    }
    $candidates = @()
    foreach ($name in @('python.exe', 'python3.exe')) {
        $candidates += @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    }
    foreach ($key in @('HKCU:\Software\Python\PythonCore\*\InstallPath',
                       'HKLM:\Software\Python\PythonCore\*\InstallPath',
                       'HKLM:\Software\WOW6432Node\Python\PythonCore\*\InstallPath')) {
        foreach ($install in @(Get-Item $key -ErrorAction SilentlyContinue)) {
            $executable = $install.GetValue('ExecutablePath')
            if ($executable) { $candidates += $executable }
            elseif ($install.GetValue('')) { $candidates += Join-Path $install.GetValue('') 'python.exe' }
        }
    }
    if ($env:LOCALAPPDATA) { $candidates += "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        $python = Test-OpsKitPython $candidate
        if ($python) { return $python }
    }
}

function Get-OpsKitWinGet([switch]$Offline) {
    $winget = Get-OpsKitApplication 'winget.exe'
    if ($winget) { return $winget }
    if ($Offline) { throw '离线模式缺少 WinGet，禁止联网安装依赖。请先在联网环境准备依赖后重跑。' }
    Write-Host '正在通过微软官方 PowerShell 模块准备 WinGet……'
    try {
        if (-not (Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue)) {
            Install-PackageProvider -Name NuGet -MinimumVersion '2.8.5.201' -Scope CurrentUser -Force | Out-Null
        }
        if (-not (Get-Module -ListAvailable -Name Microsoft.WinGet.Client)) {
            Install-Module -Name Microsoft.WinGet.Client -Repository PSGallery -Scope CurrentUser -Force
        }
        Import-Module Microsoft.WinGet.Client -ErrorAction Stop
        Repair-WinGetPackageManager | Out-Host
    } catch {
        throw "WinGet 自动准备失败：$($_.Exception.Message)。如出现系统权限提示，请完成授权；更新系统或重启后重新运行安装脚本。"
    }
    Update-OpsKitProcessPath
    $winget = Get-OpsKitApplication 'winget.exe'
    if (-not $winget) { throw 'WinGet 已尝试准备，但当前会话仍不可用。请重新打开终端后运行同一安装命令。' }
    return $winget
}

function Install-OpsKitPackage([string]$Id, [switch]$Offline, [switch]$AcceptPackageAgreements, [switch]$UserScope) {
    if ($Offline) { throw "离线模式缺少依赖 $Id，禁止联网安装。请准备依赖后重跑。" }
    $winget = Get-OpsKitWinGet
    $arguments = @('install', '--exact', '--id', $Id, '--source', 'winget', '--accept-source-agreements')
    if ($AcceptPackageAgreements) { $arguments += '--accept-package-agreements' }
    if ($UserScope) { $arguments += @('--scope', 'user') }
    # 保留安装界面和厂商许可，由用户完成 UAC 及必要的许可确认。
    if ($Id -eq 'Docker.DockerDesktop') { $arguments += '--interactive' }
    & $winget @arguments | Out-Host
    $code = $LASTEXITCODE
    Update-OpsKitProcessPath
    if ($code -in 3010, 1641) { throw "$Id 已安装，Windows 需要重启。请重启后重新运行同一安装命令。" }
    if ($code -ne 0) { throw "$Id 安装未完成（退出码 $code）。请完成安装窗口或 UAC 提示后重新运行安装脚本。" }
}

function Get-OpsKitPython([switch]$Offline) {
    $python = Find-OpsKitPython
    if ($python) { return $python }
    if ($Offline) { throw '离线模式缺少 Python 3.9 或更新版本，禁止联网安装。请准备 Python 后重跑。' }
    Write-Host '未检测到 Python 3.9+，正在安装 Python 3.13（与现有版本并存）……'
    Install-OpsKitPackage -Id 'Python.Python.3.13' -AcceptPackageAgreements -UserScope
    $python = Find-OpsKitPython
    if (-not $python) { throw 'Python 已尝试安装，但无法验证运行。请完成系统提示，重新打开终端后运行同一安装命令。' }
    return $python
}

function Get-OpsKitDockerStatus {
    $ErrorActionPreference = 'Continue'
    $docker = Get-OpsKitApplication 'docker.exe'
    if (-not $docker) { return @{ Linux = $false; Windows = $false } }
    try {
        $kind = (& $docker info --format '{{.OSType}}' 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $kind -eq 'windows') { return @{ Linux = $false; Windows = $true } }
        if ($LASTEXITCODE -eq 0 -and $kind -eq 'linux') {
            & $docker compose version 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return @{ Linux = $true; Windows = $false } }
        }
    } catch { }
    return @{ Linux = $false; Windows = $false }
}

function Find-OpsKitDockerDesktop {
    foreach ($path in @("$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
                        "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe")) {
        if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    }
    return Get-OpsKitApplication 'Docker Desktop.exe'
}

function Test-OpsKitWSL([string]$Executable) {
    if (-not $Executable) { return $false }
    $ErrorActionPreference = 'Continue'
    try {
        & $Executable --version 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Initialize-OpsKitWSL([switch]$Offline) {
    $wsl = Get-OpsKitApplication 'wsl.exe'
    if (Test-OpsKitWSL $wsl) { return }
    if ($Offline) { throw 'WSL 2 尚未就绪，离线模式禁止下载。请准备 WSL 2 后重跑。' }
    if (-not $wsl) {
        $wsl = "$env:WINDIR\System32\wsl.exe"
        if (-not (Test-Path -LiteralPath $wsl -PathType Leaf)) { throw '系统未提供 wsl.exe。请更新到支持 WSL 2 的 Windows 10/11 后重新运行。' }
    }
    Write-Host '正在准备 WSL 2，系统可能弹出管理员授权；此步骤不安装额外 Linux 发行版。'
    try {
        $process = Start-Process -FilePath $wsl -ArgumentList @('--install', '--no-distribution') -Verb RunAs -Wait -PassThru
    } catch { throw 'WSL 2 安装需要 Windows 管理员授权。请完成 UAC 授权后重新运行同一安装命令。' }
    if ($process.ExitCode -in 3010, 1641) { throw 'WSL 2 已安装，需要重启 Windows。请重启后重新运行同一安装命令。' }
    if ($process.ExitCode -ne 0 -or -not (Test-OpsKitWSL $wsl)) {
        throw 'WSL 2 尚未就绪。请完成系统提示；如要求重启，请重启 Windows 后重新运行同一安装命令。'
    }
}

function Wait-OpsKitDocker {
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $status = Get-OpsKitDockerStatus
        if ($status.Windows) { throw 'Docker 当前运行 Windows 容器。请在 Docker Desktop 中切换到 Linux 容器后重新运行。' }
        if ($status.Linux) { return $true }
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Initialize-OpsKitDocker([switch]$Offline) {
    Update-OpsKitProcessPath
    $status = Get-OpsKitDockerStatus
    if ($status.Windows) { throw 'Docker 当前运行 Windows 容器。请在 Docker Desktop 中切换到 Linux 容器后重新运行。' }
    if ($status.Linux) { return }
    $desktop = Find-OpsKitDockerDesktop
    if (-not $desktop) {
        if ($Offline) { throw '离线模式缺少可运行的 Docker Desktop（Linux 容器）及 Compose，禁止联网安装。' }
        Initialize-OpsKitWSL
        Write-Host '正在安装 Docker Desktop，请按厂商安装界面完成必要的系统确认。'
        Install-OpsKitPackage -Id 'Docker.DockerDesktop'
        $desktop = Find-OpsKitDockerDesktop
    }
    if (-not $desktop) { throw '未找到 Docker Desktop。请完成安装窗口；如要求重启，请重启后运行同一安装命令。' }
    Write-Host '正在启动 Docker Desktop。首次启动请在 Docker 窗口中阅读并自行确认许可。'
    Start-Process -FilePath $desktop | Out-Null
    if (Wait-OpsKitDocker) { return }
    # 已安装但 WSL 尚未准备时，补装后仍以 Docker 实际健康状态为准。
    Initialize-OpsKitWSL -Offline:$Offline
    if (-not (Wait-OpsKitDocker)) {
        throw 'Docker 尚未就绪。请完成 Docker Desktop 首次许可、WSL 2 或系统重启提示，并确认使用 Linux 容器后重跑。'
    }
}

function Invoke-OpsKitLocation([string]$Entry, [System.Collections.IDictionary]$Parameters) {
    if ($env:OPSKIT_LOCATION_READY) { return }
    if (-not (Test-OpsKitWindows)) { throw 'Windows 入口仅适用于 Windows 10/11；Linux/macOS 请运行 install.sh。' }
    $oldUtf8 = $env:PYTHONUTF8
    $oldIoEncoding = $env:PYTHONIOENCODING
    $oldEncoding = [Console]::OutputEncoding
    try {
        $env:PYTHONUTF8 = '1'
        $env:PYTHONIOENCODING = 'utf-8'
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        Update-OpsKitProcessPath
        $python = Get-OpsKitPython -Offline:([bool]$Parameters['Offline'])
        $arguments = @('--source', (Split-Path -Parent $Entry), '--entry', 'install.ps1', '--')
        if ($Parameters['InstallDir']) { $arguments += @('--install-dir', [string]$Parameters['InstallDir']) }
        if ($Parameters['KeepLocation']) { $arguments += '--keep-location' }
        if ($Parameters['Overwrite']) { $arguments += '--overwrite' }
        foreach ($key in $Parameters.Keys) {
            if ($key -in 'InstallDir', 'KeepLocation', 'Overwrite') { continue }
            $value = $Parameters[$key]
            if ($value -is [System.Management.Automation.SwitchParameter]) {
                if ($value.IsPresent) { $arguments += "-$key" }
            } else {
                $arguments += @("-$key", [string]$value)
            }
        }
        & $python -u (Join-Path $PSScriptRoot 'location.py') @arguments
        $code = $LASTEXITCODE
    } finally {
        $env:PYTHONUTF8 = $oldUtf8
        $env:PYTHONIOENCODING = $oldIoEncoding
        [Console]::OutputEncoding = $oldEncoding
    }
    exit $code
}

function Invoke-OpsKitInstaller([string]$Installer, [string[]]$Arguments = @(), [switch]$ConfigureOnly, [switch]$Offline) {
    if (-not (Test-OpsKitWindows)) { throw 'Windows 入口仅适用于 Windows 10/11；Linux/macOS 请运行 install.sh。' }
    $oldUtf8 = $env:PYTHONUTF8
    $oldIoEncoding = $env:PYTHONIOENCODING
    $oldEncoding = [Console]::OutputEncoding
    try {
        $env:PYTHONUTF8 = '1'
        $env:PYTHONIOENCODING = 'utf-8'
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        Update-OpsKitProcessPath
        $python = Get-OpsKitPython -Offline:$Offline
        if (-not $ConfigureOnly) { Initialize-OpsKitDocker -Offline:$Offline }
        & $python -u $Installer @Arguments
        if ($LASTEXITCODE -ne 0) { throw "安装向导未完成（退出码 $LASTEXITCODE），修复提示的问题后可重新运行。" }
    } finally {
        $env:PYTHONUTF8 = $oldUtf8
        $env:PYTHONIOENCODING = $oldIoEncoding
        [Console]::OutputEncoding = $oldEncoding
    }
}

function Test-OpsKitWindows {
    return [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
}

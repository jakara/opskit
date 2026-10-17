[CmdletBinding()]
param(
    [switch]$All,
    [switch]$HubOnly,
    [switch]$Yes,
    [switch]$PurgeData,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$oldUtf8 = $env:PYTHONUTF8
$oldIoEncoding = $env:PYTHONIOENCODING
$oldEncoding = [Console]::OutputEncoding
$oldPath = $env:Path
$code = 1
try {
    . "$PSScriptRoot/scripts/bootstrap.ps1"
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    Update-OpsKitProcessPath
    $python = Find-OpsKitPython
    if (-not $python) { throw '未检测到 Python 3.9+。卸载不会安装依赖，请恢复安装时使用的 Python 后重试。' }
    $arguments = @()
    if ($All) { $arguments += '--all' }
    if ($HubOnly) { $arguments += '--hub-only' }
    if ($Yes) { $arguments += '--yes' }
    if ($PurgeData) { $arguments += '--purge-data' }
    if ($DryRun) { $arguments += '--dry-run' }
    # 保留 Python 的原始错误及退出码，兼容 Windows PowerShell 5.1 的 stderr 行为。
    $ErrorActionPreference = 'Continue'
    & $python -u "$PSScriptRoot/scripts/uninstall.py" @arguments
    $code = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("卸载未完成：$($_.Exception.Message)")
    $code = 1
} finally {
    $env:PYTHONUTF8 = $oldUtf8
    $env:PYTHONIOENCODING = $oldIoEncoding
    [Console]::OutputEncoding = $oldEncoding
    $env:Path = $oldPath
}
exit $code

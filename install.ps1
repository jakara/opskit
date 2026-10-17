[CmdletBinding()]
param(
    [ValidateSet('', 'management', 'collector', 'hub', 'production', 'development', '1', '2', '3')]
    [string]$Module = '',
    [switch]$ConfigureOnly,
    [switch]$Offline,
    [switch]$Reconfigure,
    [ValidateRange(1, 65535)][int]$Port = 80,
    [string]$InstallDir = '',
    [switch]$KeepLocation,
    [switch]$Overwrite
)

$ErrorActionPreference = 'Stop'
try {
    . "$PSScriptRoot/management/scripts/bootstrap.ps1"
    Invoke-OpsKitLocation -Entry $PSCommandPath -Parameters $PSBoundParameters
    $python = Get-OpsKitPython -Offline:$Offline
    if (-not $Module) {
        Write-Host "OpsKit 安装向导`n  1) 管理中心（Hub 及完整运维工具）`n  2) 日志采集端（Fluent Bit）`n  3) 精简管理端"
        $defaultModule = & $python -B "$PSScriptRoot/management/scripts/options.py" read $PSScriptRoot
        if ($LASTEXITCODE -ne 0) { throw '无法读取上次安装模块。' }
        $Module = Read-Host "请选择模块 [1/2/3，回车采用 $defaultModule]"
        if (-not $Module) { $Module = $defaultModule }
    }
    & $python -B "$PSScriptRoot/management/scripts/options.py" write $PSScriptRoot $Module
    if ($LASTEXITCODE -ne 0) { throw '无法保存安装模块。' }
    $options = @{ ConfigureOnly = $ConfigureOnly; Offline = $Offline; Reconfigure = $Reconfigure }
    if ($PSBoundParameters.ContainsKey('Port') -and $Module -notin @('2', 'collector')) { $options['Port'] = $Port }
    switch ($Module) {
        { $_ -in '1', 'management', 'production' } {
            & "$PSScriptRoot/management/install.ps1" @options
        }
        { $_ -in '2', 'collector' } {
            & "$PSScriptRoot/collector/install.ps1" @options
        }
        { $_ -in '3', 'hub', 'development' } {
            & "$PSScriptRoot/management/install.ps1" @options -HubOnly
        }
        default { throw '请选择 management、collector 或 hub。' }
    }
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("安装未完成：$($_.Exception.Message)")
    exit 1
}

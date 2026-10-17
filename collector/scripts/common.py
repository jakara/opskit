"""安装、检查和代理初始化共用的本地配置工具。"""
import hashlib
import base64
import ipaddress
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]


def load_env(path):
    """只解析数据，不执行 .env 中的命令。"""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            raise ValueError(f'{path.name} 中有无效配置行')
        parts = shlex.split(value, comments=False)
        if len(parts) > 1:
            raise ValueError(f'{key.strip()} 的值含空格时必须使用引号')
        values[key.strip()] = parts[0] if parts else ''
    return values


def powershell(script, **kwargs):
    """使用固定脚本执行系统查询；路径等外部数据通过环境变量传入。"""
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                          check=True, encoding='utf-8-sig', **kwargs)


def protect_private(path, existing=False):
    if sys.platform != 'win32':
        path.chmod(0o600)
        return
    # chmod 在 Windows 上不能限制其他用户读取，使用当前用户、SYSTEM、管理员的 NTFS ACL。
    # 已有 ACL 修改前另存 SDDL 并核验，可用 Set-Acl 恢复；新建临时文件没有待保留 ACL。
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$path = $env:OPSKIT_PRIVATE_PATH
$acl = [Security.AccessControl.FileSecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$sids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique
foreach ($sid in $sids) {
    $identity = [Security.Principal.SecurityIdentifier]::new($sid)
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
}
$old = Get-Acl -LiteralPath $path
$section = [Security.AccessControl.AccessControlSections]::Access
if ($old.GetSecurityDescriptorSddlForm($section) -eq $acl.GetSecurityDescriptorSddlForm($section)) { exit 0 }
if ($env:OPSKIT_BACKUP_ACL -eq '1') {
    $folder = Join-Path (Split-Path -Parent $path) ('.backups/' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss-fffffff'))
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    $backup = Join-Path $folder ((Split-Path -Leaf $path) + '.acl.txt')
    [IO.File]::WriteAllText($backup, '')
    Set-Acl -LiteralPath $backup -AclObject $acl
    $sddl = $old.Sddl
    [IO.File]::WriteAllText($backup, $sddl, [Text.UTF8Encoding]::new($false))
    if ([IO.File]::ReadAllText($backup) -cne $sddl) { throw 'ACL backup verification failed' }
}
Set-Acl -LiteralPath $path -AclObject $acl
$actual = Get-Acl -LiteralPath $path
if ($actual.GetSecurityDescriptorSddlForm($section) -ne $acl.GetSecurityDescriptorSddlForm($section)) { throw 'Private ACL verification failed' }
'''
    try:
        powershell(script, env=dict(os.environ, OPSKIT_PRIVATE_PATH=str(path.resolve()),
                                    OPSKIT_BACKUP_ACL='1' if existing else '0'),
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('无法限制 Windows 配置文件权限，请将安装目录放在当前用户可写的 NTFS 磁盘后重跑。') from exc


def save(path, content, private=True):
    """写入前备份并核验原文件，重复执行不重写相同内容。"""
    if path.is_symlink():
        raise ValueError(f'拒绝覆盖软链接：{path}')
    data = content.encode('utf-8')
    if path.exists() and path.read_bytes() == data:
        if private:
            protect_private(path, existing=True)
        return
    if path.exists():
        backup = path.parent / '.backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f') / path.name
        backup.parent.mkdir(mode=0o700, parents=True)
        backup.touch(mode=0o600)
        protect_private(backup)
        backup.write_bytes(path.read_bytes())
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(backup.read_bytes()).digest():
            raise RuntimeError(f'备份校验失败：{path}')
        print(f'已备份原配置：{backup}')
        if private and sys.platform == 'win32':
            protect_private(path, existing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.opskit-')
    try:
        os.close(fd)
        if private:
            protect_private(Path(temp))
        with open(temp, 'wb') as stream:
            stream.write(data)
        if not private:
            os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def env_text(values):
    for key, value in values.items():
        if any(c in str(value) for c in ("'", '\n', '\r', '\\')):
            raise ValueError(f'{key} 不能包含单引号、换行或反斜杠')
    return '# 本机配置及凭据，请勿提交或随交付包分发。\n' + ''.join(
        f"{key}='{value}'\n" for key, value in values.items())


def ask(label, default, validator, secret=False):
    import getpass
    while True:
        hint = ('回车采用自动生成密码' if default else '填写管理端发放的密码') if secret else str(default)
        prompt = f'{label} [{hint}]：'
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip() or str(default)
        if validator(value):
            return value
        print('输入无效，请按提示重新填写。')


def host(value):
    return bool(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value))


def ipv4(value):
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def port(value):
    return value.isdigit() and 1 <= int(value) <= 65535


def password(value):
    return bool(re.fullmatch(r'[A-Za-z0-9!@#%^&*()_+=.,:;/?~\-]{12,128}', value))


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def docker_ready():
    if not shutil.which('docker'):
        raise RuntimeError('未找到 Docker。请通过 install.sh（Windows 使用 install.cmd）运行安装引导；配置已保留。')
    run(['docker', 'compose', 'version'], stdout=subprocess.DEVNULL)
    run(['docker', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

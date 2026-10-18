"""安装、检查和代理初始化共用的本地配置工具。"""
import hashlib
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
_operator_sessions = {}


def operator_headers(values, refresh=False):
    """本机检查直连 HTTP 时显式携带 Secure Cookie，浏览器仍按 HTTPS 策略处理。"""
    from http.cookies import SimpleCookie
    from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    ip = values['OPSKIT_BIND_IP'] if values['OPSKIT_BIND_IP'] != '0.0.0.0' else '127.0.0.1'
    key = (ip, values['OPSKIT_HTTP_PORT'], values['OPS_PUBLIC_HOST'], values['OPS_ADMIN_USER'],
           hashlib.sha256(values['OPS_ADMIN_PASSWORD'].encode()).hexdigest())
    cached = _operator_sessions.get(key)
    if refresh or not cached or time.monotonic() >= cached[0]:
        request = Request(f'http://{ip}:{values["OPSKIT_HTTP_PORT"]}/auth/api/login',
                          data=json.dumps({'username': values['OPS_ADMIN_USER'], 'password': values['OPS_ADMIN_PASSWORD']}).encode(),
                          headers={'Content-Type': 'application/json', 'Host': values['OPS_PUBLIC_HOST']})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
                cookie = SimpleCookie()
                for value in response.headers.get_all('Set-Cookie', []):
                    cookie.load(value)
            names = [name for name in cookie if re.fullmatch(r'opskit_session(?:_[a-f0-9]{12})?', name)]
            if len(names) != 1:
                raise ValueError('invalid session cookie')
            cookie_name = names[0]
            token = cookie[cookie_name].value
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
                raise ValueError('invalid session')
        except Exception:
            raise RuntimeError('运维入口登录未通过，请核对入口账户、配置和服务状态') from None
        if len(_operator_sessions) > 32:
            _operator_sessions.clear()
        cached = (time.monotonic() + 600, cookie_name + '=' + token)
        _operator_sessions[key] = cached
    return {'Cookie': cached[1]}


def image_defaults(defaults):
    """发行包可锁定离线镜像别名，不从交付配置引入其他安装参数。"""
    path = ROOT / 'image-defaults.json'
    if not path.exists():
        return defaults
    values = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(values, dict) or set(values) - set(defaults):
        raise ValueError('image-defaults.json 包含未知镜像配置')
    if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]+', value)
           for value in values.values()):
        raise ValueError('image-defaults.json 包含无效镜像引用')
    return dict(defaults, **values)


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


def protect_private(path, existing=False, directory=False):
    if sys.platform != 'win32':
        path.chmod(0o700 if directory else 0o600)
        return
    # chmod 在 Windows 上不能限制其他用户读取，使用当前用户、SYSTEM、管理员的 NTFS ACL。
    # 已有 ACL 修改前另存 SDDL 并核验，可用 Set-Acl 恢复；新建临时文件没有待保留 ACL。
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$path = $env:OPSKIT_PRIVATE_PATH
$fileAcl = [Security.AccessControl.FileSecurity]::new()
$acl = if ($env:OPSKIT_PRIVATE_DIRECTORY -eq '1') { [Security.AccessControl.DirectorySecurity]::new() } else { [Security.AccessControl.FileSecurity]::new() }
$acl.SetAccessRuleProtection($true, $false)
$fileAcl.SetAccessRuleProtection($true, $false)
$sids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique
foreach ($sid in $sids) {
    $identity = [Security.Principal.SecurityIdentifier]::new($sid)
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow')
    $fileAcl.AddAccessRule($rule)
    if ($env:OPSKIT_PRIVATE_DIRECTORY -eq '1') {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')
    }
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
    Set-Acl -LiteralPath $backup -AclObject $fileAcl
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
                                    OPSKIT_BACKUP_ACL='1' if existing else '0',
                                    OPSKIT_PRIVATE_DIRECTORY='1' if directory else '0'),
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


def choose_configuration(path, reconfigure=False, reuse=False, draft=None, reinstall=None):
    if reconfigure or reuse or (draft and not reinstall) or not path.exists():
        return reconfigure
    print(f'\n发现已有配置：{path}\n  1) 复用已有配置\n  2) 重新配置（保留已有密码和数据）')
    if reinstall:
        print('  3) 完全重新安装（清空所有密码和数据，先备份）')
    choice = ask('请选择配置方式', '1', lambda s: s in (('1', '2', '3') if reinstall else ('1', '2')))
    if choice == '3':
        reinstall()
        return True
    return choice == '2'


def compose_start_command(values, command=None, files=None):
    """重建前核对项目归属，避免同名项目覆盖另一安装目录。"""
    command = command or ['docker', 'compose']
    files = files or os.environ.get('COMPOSE_FILE', values.get('COMPOSE_FILE', 'compose.yaml')).split(os.pathsep)
    normalize = lambda path: os.path.normcase(os.path.realpath(path))
    expected = {normalize(ROOT / file) for file in files}
    ids = run(command + ['ps', '--all', '--quiet'], capture_output=True, text=True).stdout.split()
    restarting = False
    if ids:
        template = '{"Labels":{{json .Config.Labels}},"Running":{{json .State.Running}}}'
        output = run(['docker', 'inspect', '--format', template, *ids], capture_output=True, text=True).stdout
        containers = [json.loads(line) for line in output.splitlines() if line.strip()]
        if len(containers) != len(ids):
            raise RuntimeError('无法完整确认已有容器归属，停止更新；请检查 Docker 状态后重试。')
        for container in containers:
            labels = container.get('Labels') or {}
            directory = labels.get('com.docker.compose.project.working_dir', '')
            actual = {normalize(file) for file in labels.get('com.docker.compose.project.config_files', '').split(',') if file}
            primary, legacy = normalize(ROOT / 'compose.yaml'), normalize(ROOT / 'legacy.compose.yaml')
            # 旧管理端升级时新增 8080 兼容覆盖文件，仍属于同一完整管理端。
            compatible = actual == expected or (expected == {primary, legacy} and actual == {primary})
            if not directory or normalize(directory) != normalize(ROOT) or not compatible:
                raise RuntimeError('同名 Compose 项目包含其他目录或安装范围的容器，已停止更新；请核对 COMPOSE_PROJECT_NAME。')
            restarting = restarting or bool(container.get('Running'))
    startup = command + ['up', '-d', '--pull', 'never']
    if restarting:
        print('检测到本安装目录的运行中容器；镜像准备完成后将重建并重启服务，保留持久化数据。')
        startup.append('--force-recreate')
    return startup


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


def image_failure(output):
    """分类用于恢复建议；只有网络类错误在同一来源重试一次。"""
    text = output.lower()
    if any(s in text for s in ('allowlist', 'whitelist', '白名单')):
        return '镜像加速服务白名单拒绝，请使用官方来源或同步到内网仓库', False
    if any(s in text for s in ('no space left', 'disk quota', 'read-only file system')):
        return 'Docker 存储空间不足或不可写，请检查 Docker 数据目录', False
    if any(s in text for s in ('no matching manifest', 'does not match the specified platform')):
        return '镜像没有当前 CPU 架构版本，请准备匹配架构的同版镜像', False
    if any(s in text for s in ('manifest unknown', 'not found', 'name unknown')):
        return '镜像地址或版本不存在，请核对镜像引用', False
    if any(s in text for s in ('unauthorized', 'authentication required', 'denied', 'forbidden', 'toomanyrequests', 'rate limit')):
        return '仓库拒绝访问或限流，请核对仓库权限、docker login 或镜像加速服务', False
    if any(s in text for s in ('timeout', 'timed out', 'connection reset', 'connection refused', 'no such host',
                               'network is unreachable', 'tls handshake', 'unexpected eof', 'bad gateway',
                               'service unavailable', '500 internal', '502', '503', '504')):
        return '镜像仓库网络连接失败，请检查 Docker 引擎的网络、代理和 DNS', True
    return '镜像拉取失败，请按下面的 Docker 原始错误排查', False


def prepare_images(values, config, command=None, offline=False):
    """逐个拉取，登记的国内/官方同版本来源可回退；缺项阻止启动。"""
    command = command or ['docker', 'compose']

    def services():
        result = run(command + ['config', '--format', 'json'], capture_output=True, text=True)
        return {name: item['image'] for name, item in json.loads(result.stdout)['services'].items()}

    # 固定登记的国内加速与官方同版来源互为备用，不泛化改写仓库地址。
    alternatives = {
        'WORKBENCH_IMAGE': ('m.daocloud.io/docker.io/library/python:3.12-alpine', 'python:3.12-alpine'),
        'NPM_IMAGE': ('m.daocloud.io/docker.io/jc21/nginx-proxy-manager:2.16.0', 'jc21/nginx-proxy-manager:2.16.0'),
        'SPUG_IMAGE': ('ghcr.io/openspug/spug:4.1', 'openspug/spug:4.1'),
        'MARIADB_IMAGE': ('m.daocloud.io/docker.io/library/mariadb:10.11', 'mariadb:10.11'),
        'DBX_IMAGE': ('docker.cnb.cool/dbxio.com/dbx:0.6.40', 't8y2/dbx:0.6.40'),
        'VICTORIALOGS_IMAGE': ('m.daocloud.io/docker.io/victoriametrics/victoria-logs:v1.53.0', 'victoriametrics/victoria-logs:v1.53.0'),
        'FLUENTBIT_IMAGE': ('m.daocloud.io/docker.io/fluent/fluent-bit:5.1.4', 'fluent/fluent-bit:5.1.4'),
    }
    ready, failures = set(), []
    for service, image in services().items():
        if image in ready:
            continue
        rollback = service in ('dbx', 'victorialogs') and bool(re.search(r':opskit-rollback-[a-f0-9]{32}$', image))
        print(f'准备镜像 [{service}]：{image}' + ('（保留已核验的本地回退镜像）' if rollback else '（仅检查本地）' if offline else '（下载期间请稍候）'), flush=True)
        if offline or rollback:
            try:
                run(['docker', 'image', 'inspect', image], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                ready.add(image)
            except subprocess.CalledProcessError:
                failures.append(f'{service}: {image}\n  本地镜像缺失，请先 docker load 导入匹配架构的镜像；离线模式不会联网。')
            continue
        key = next((key for key, refs in alternatives.items()
                    if values.get(key) == image and image in refs), None)
        candidates = [image]
        if key and key not in os.environ and not (ROOT / 'image-defaults.json').exists():
            candidates += [ref for ref in alternatives[key] if ref != image]
        errors = []
        for candidate in candidates:
            if candidate != image:
                print(f'切换到已登记的同版本备用来源：{candidate}', flush=True)
            downloaded = False
            for attempt in range(2):
                try:
                    run(['docker', 'pull', candidate], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding='utf-8', errors='replace')
                    downloaded = True
                    break
                except subprocess.CalledProcessError as exc:
                    output = (exc.stdout or '').strip()
                    reason, retry = image_failure(output)
                    if retry and attempt == 0:
                        print(f'{reason}；2 秒后重试一次。', flush=True)
                        time.sleep(2)
                        continue
                    errors.append(f'{candidate}\n  {reason}\n  {output[-1500:]}')
                    print(f'未能获取 {candidate}：{reason}', flush=True)
                    break
            if not downloaded:
                # 存储或架构故障不通过换来源掩盖。
                if any(s in output.lower() for s in ('no space left', 'disk quota', 'read-only file system',
                                                     'no matching manifest', 'does not match the specified platform')):
                    break
                continue
            if candidate != image:
                updated = dict(values, **{key: candidate})
                save(config, env_text(updated))
                values.update(updated)
                print(f'已保存可用镜像来源到 {config}，原配置已备份。')
            ready.add(candidate)
            print(f'通过：{service}', flush=True)
            break
        else:
            failures.append(f'{service}: ' + '\n  '.join(errors))
            continue
        if candidate not in ready:
            failures.append(f'{service}: ' + '\n  '.join(errors))
    if failures:
        resume = '.\\install.cmd' if sys.platform == 'win32' else './install.sh'
        scope = (' -HubOnly' if sys.platform == 'win32' else ' --hub-only') if config.name == 'hub.env' else ''
        raise RuntimeError('镜像准备未完成，尚未启动本次容器和发现进程：\n' + '\n'.join(failures) +
                           f'\n配置和已下载镜像已保留：{config}\n'
                           f'修复后在 {ROOT} 重跑 {resume}{scope}。'
                           '\n可修改上述配置中的对应 *_IMAGE 为可达的同版本仓库地址；私有仓库需先 docker login。'
                           '\n已离线导入全部镜像时使用 --offline（Windows：-Offline）。')
    if set(services().values()) - ready:
        raise RuntimeError(f'Compose 实际镜像与已准备镜像不一致，请检查环境变量或 Compose 覆盖配置；配置保留在 {config}')


def docker_ready():
    if not shutil.which('docker'):
        raise RuntimeError('未找到 Docker。请通过 install.sh（Windows 使用 install.cmd）运行安装引导；配置已保留。')
    run(['docker', 'compose', 'version'], stdout=subprocess.DEVNULL)
    run(['docker', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

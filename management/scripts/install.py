#!/usr/bin/env python3
"""管理中心交互式安装向导。"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys

from common import ROOT, ask, docker_ready, env_text, host, image_defaults, ipv4, load_env, password, port, powershell, prepare_images, run, save

IMAGES = image_defaults({
    'WORKBENCH_IMAGE': 'python:3.12-alpine',
    'NPM_IMAGE': 'jc21/nginx-proxy-manager:2.16.0',
    'SPUG_IMAGE': 'ghcr.io/openspug/spug:4.1',
    'MARIADB_IMAGE': 'mariadb:10.11',
    'DBX_IMAGE': 'docker.cnb.cool/dbxio.com/dbx:0.6.40',
    'VICTORIALOGS_IMAGE': 'victoriametrics/victoria-logs:v1.53.0',
})
PASSWORD_KEYS = ('NPM_ADMIN_PASSWORD', 'SPUG_ADMIN_PASSWORD', 'SPUG_DB_PASSWORD',
                 'DBX_PASSWORD', 'OPS_ADMIN_PASSWORD', 'LOG_AGENT_PASSWORD')


def validate(v):
    checks = {
        'OPS_PUBLIC_HOST': host, 'LOG_INGEST_HOST': host,
        'OPSKIT_BIND_IP': ipv4, 'NPM_BIND_IP': ipv4,
        'OPSKIT_HTTP_PORT': port, 'NPM_ADMIN_PORT': port, 'SPUG_HTTP_PORT': port, 'OPS_PUBLIC_PORT': port,
        'OPS_PUBLIC_SCHEME': lambda s: s in ('http', 'https'),
        'LOG_RETENTION_DAYS': lambda s: s.isdigit() and 1 <= int(s) <= 3650,
        'NPM_ADMIN_EMAIL': lambda s: bool(re.fullmatch(r'[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', s)),
        'SPUG_ADMIN_USER': lambda s: bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', s)),
        'OPS_ADMIN_USER': lambda s: bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', s)),
        'LOG_AGENT_USER': lambda s: bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', s)),
    }
    if v.get('OPSKIT_LEGACY_HTTP_PORT'):
        checks['OPSKIT_LEGACY_HTTP_PORT'] = port
    checks.update({key: password for key in PASSWORD_KEYS})
    for key, check in checks.items():
        if not check(v.get(key, '')) or v.get(key, '').startswith('REPLACE_'):
            raise ValueError(f'{key} 缺失或无效，请运行 ./install.sh --reconfigure')
    if v.get('HUB_ONLY') == 'true' and (v['OPSKIT_BIND_IP'] != '127.0.0.1' or v['NPM_BIND_IP'] != '127.0.0.1'):
        raise ValueError('单独 Hub 只允许绑定 127.0.0.1；远程访问请使用带鉴权的管理中心')
    bindings = [('OPSKIT_HTTP_PORT', 'OPSKIT_BIND_IP'), ('NPM_ADMIN_PORT', 'NPM_BIND_IP'), ('SPUG_HTTP_PORT', 'OPSKIT_BIND_IP')]
    if v.get('HUB_ONLY') == 'true':
        bindings = bindings[:2]
    elif v.get('OPSKIT_LEGACY_HTTP_PORT'):
        bindings.append(('OPSKIT_LEGACY_HTTP_PORT', 'OPSKIT_BIND_IP'))
    for index, (port_key, bind_key) in enumerate(bindings):
        for other_port, other_bind in bindings[index + 1:]:
            if v[port_key] == v[other_port] and (v[bind_key] == v[other_bind] or '0.0.0.0' in (v[bind_key], v[other_bind])):
                raise ValueError(f'{port_key} 与 {other_port} 的监听端口发生冲突')
    if v['OPS_ADMIN_USER'] == v['LOG_AGENT_USER']:
        raise ValueError('运维访问和日志写入必须使用不同账户')


def configure(existing, reconfigure=False, interactive=True, checkpoint=None, draft=None):
    v = dict(existing)

    def prompt(key, label, default, validator, **kwargs):
        value = ask(label, default, validator, **kwargs) if interactive else str(default)
        v[key] = value
        if checkpoint:
            checkpoint(v)

    if existing and 'SPUG_HTTP_PORT' not in existing:
        v['SPUG_HTTP_PORT'] = '8081'
        if v.get('OPSKIT_HTTP_PORT') == '8080':
            v['OPSKIT_HTTP_PORT'] = '80'
            v['OPSKIT_LEGACY_HTTP_PORT'] = '8080'
            files = v.get('COMPOSE_FILE', 'compose.yaml').split(os.pathsep)
            if 'legacy.compose.yaml' not in files:
                files.append('legacy.compose.yaml')
            v['COMPOSE_FILE'] = os.pathsep.join(files)
            print('HTTP 首页将迁移到 80，同时保留旧 8080 日志接入口；Spug 改用 8081，原配置写入前会备份。')
    if draft:
        v.update(draft)
    if not existing or reconfigure or draft or any(key not in v for key in (*PASSWORD_KEYS, 'NPM_ADMIN_EMAIL', 'LOG_INGEST_HOST')):
        print('\n管理中心配置：外层 Nginx 负责 HTTPS，本中心不生成或申请证书。')
        prompt('OPS_PUBLIC_HOST', '外层 Nginx 使用的访问域名（不含协议和路径）', v.get('OPS_PUBLIC_HOST', 'ops.example.internal'), host)
        prompt('OPS_PUBLIC_SCHEME', '浏览器最终使用的协议', v.get('OPS_PUBLIC_SCHEME', 'https'), lambda s: s in ('http', 'https'))
        prompt('OPS_PUBLIC_PORT', '浏览器最终访问端口', v.get('OPS_PUBLIC_PORT', '443' if v['OPS_PUBLIC_SCHEME'] == 'https' else '80'), port)
        prompt('OPSKIT_BIND_IP', '管理机 HTTP 监听 IPv4（外层代理在本机时用 127.0.0.1）', v.get('OPSKIT_BIND_IP', '127.0.0.1'), ipv4)
        prompt('OPSKIT_HTTP_PORT', '管理机 HTTP 端口', v.get('OPSKIT_HTTP_PORT', '80'), port)
        prompt('NPM_BIND_IP', 'NPM 管理界面监听 IPv4', v.get('NPM_BIND_IP', '127.0.0.1'), ipv4)
        prompt('NPM_ADMIN_PORT', 'NPM 管理界面端口', v.get('NPM_ADMIN_PORT', '8181'), port)
        prompt('SPUG_HTTP_PORT', 'Spug 独立 HTTP 端口（不能与前两项端口重复）', v.get('SPUG_HTTP_PORT', '8081'),
                                    lambda s: port(s) and s not in (v['OPSKIT_HTTP_PORT'], v['NPM_ADMIN_PORT']))
        prompt('LOG_INGEST_HOST', '业务机连接管理机的内网 IP 或域名', v.get('LOG_INGEST_HOST', v['OPS_PUBLIC_HOST']), host)
        prompt('LOG_RETENTION_DAYS', '日志保留天数', v.get('LOG_RETENTION_DAYS', '14'), lambda s: s.isdigit() and 1 <= int(s) <= 3650)
        prompt('NPM_ADMIN_EMAIL', 'NPM 管理员邮箱（仅作登录名，不发送邮件）', v.get('NPM_ADMIN_EMAIL', 'admin@example.internal'), lambda s: bool(re.fullmatch(r'[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', s)))
        for key, label, default in (
                ('SPUG_ADMIN_USER', 'Spug 管理员用户名', 'admin'),
                ('OPS_ADMIN_USER', '运维入口用户名', 'operator'),
                ('LOG_AGENT_USER', '日志写入用户名', 'logagent')):
            def valid_user(value):
                if key == 'LOG_AGENT_USER' and value == v['OPS_ADMIN_USER']:
                    print('运维访问和日志写入必须使用不同账户，请重新输入日志写入用户名。')
                    return False
                return bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', value))
            prompt(key, label, v.get(key, default), valid_user)
        print('密码要求：12～128 位英文字母、数字或常用符号；不含空白、美元符、引号和反斜杠。')
        for key, label in (
                ('NPM_ADMIN_PASSWORD', 'NPM 管理员密码'),
                ('SPUG_ADMIN_PASSWORD', 'Spug 初始管理员密码'),
                ('DBX_PASSWORD', 'DBX 登录密码'),
                ('OPS_ADMIN_PASSWORD', '运维入口密码'),
                ('LOG_AGENT_PASSWORD', '日志写入密码')):
            if not password(v.get(key, '')) or v[key].startswith('REPLACE_'):
                prompt(key, label, secrets.token_hex(24), password, secret=True)
        if not v.get('SPUG_DB_PASSWORD') or v['SPUG_DB_PASSWORD'].startswith('REPLACE_'):
            v['SPUG_DB_PASSWORD'] = secrets.token_hex(24)
            if checkpoint:
                checkpoint(v)
    for key, image in IMAGES.items():
        if not existing or reconfigure or draft:
            prompt(key, f'{key} 镜像地址（可填私有仓库或官方备用源）', v.get(key, image),
                         lambda s: bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]+', s)))
        else:
            v.setdefault(key, image)
    v.setdefault('SPUG_HTTP_PORT', '8081')
    validate(v)
    return v


def public_url(v):
    default = '443' if v['OPS_PUBLIC_SCHEME'] == 'https' else '80'
    suffix = '' if v['OPS_PUBLIC_PORT'] == default else ':' + v['OPS_PUBLIC_PORT']
    return f"{v['OPS_PUBLIC_SCHEME']}://{v['OPS_PUBLIC_HOST']}{suffix}"


def write_outputs(v):
    save(ROOT / '.env', env_text(v))
    connection = {
        'OPS_HOST': v['LOG_INGEST_HOST'], 'OPS_PORT': v['OPSKIT_HTTP_PORT'],
        'OPS_VHOST': v['OPS_PUBLIC_HOST'], 'OPS_SCHEME': 'http',
        'LOG_AGENT_USER': v['LOG_AGENT_USER'], 'LOG_AGENT_PASSWORD': v['LOG_AGENT_PASSWORD'],
    }
    save(ROOT / 'collector.env', env_text(connection))
    upstream = v['OPSKIT_BIND_IP'] if v['OPSKIT_BIND_IP'] != '0.0.0.0' else '管理机内网IP'
    save(ROOT / 'outer-nginx.conf.example', f'''# 将以下内容合并到已有 HTTPS server；证书仍由外层 Nginx 管理。
# 如果外层 Nginx 位于另一台主机或容器，请将 {upstream} 换成可达的管理机地址。
# 外层需要保留路径、Host、Basic Auth 和 WebSocket；不要在这里添加第二套 Basic Auth。
location / {{
    proxy_pass http://{upstream}:{v['OPSKIT_HTTP_PORT']};
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    client_max_body_size 64m;
    proxy_buffering off;
}}
''', private=False)


def hub_directory():
    directory = ROOT / '.workbench/hub'
    previous = ROOT / '.workbench/development'
    if not directory.exists() and (previous / 'entries.json').exists():
        directory.mkdir(mode=0o700, parents=True)
        data = (previous / 'entries.json').read_bytes()
        save(directory / 'entries.json', data.decode())
        if (directory / 'entries.json').read_bytes() != data:
            raise RuntimeError('旧工作台入口迁移校验失败，原目录已保留')
        if (previous / 'backups').exists():
            shutil.copytree(previous / 'backups', directory / 'backups')
        print(f'已迁移已有入口并核验内容；原目录保留：{previous}')
    return directory


def start_discovery():
    """宿主机只导出清单，网页容器不接触 Docker socket 或宿主机进程。"""
    directory = ROOT / '.workbench/discovery'
    command = [sys.executable, '-u', str(ROOT / 'portal/discovery.py'), '--output', str(directory / 'state.json')]
    run(command + ['--once'])
    if sys.platform != 'win32' and os.geteuid() == 0 and Path('/run/systemd/system').exists() and shutil.which('systemctl'):
        service = 'opskit-discovery-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:8] + '.service'
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        content = ('[Unit]\nDescription=OpsKit host service discovery\nAfter=docker.service network.target\n'
                   '[Service]\nType=simple\nExecStart=' + ' '.join(map(quote, command)) +
                   '\nEnvironment=' + quote('PATH=' + os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')) +
                   '\nRestart=always\nRestartSec=5\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n')
        save(Path('/etc/systemd/system') / service, content, private=False)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', service])
        run(['systemctl', 'restart', service])
        run(['systemctl', 'is-active', '--quiet', service])
        print(f'宿主机入口发现已启动并随系统启动：{service}')
        return
    pidfile = directory / 'discovery.pid'
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text(encoding='utf-8'))
            if pid <= 0:
                raise ValueError('无效 PID')
            if sys.platform == 'win32':
                existing = powershell("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
                                      f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
                                      capture_output=True, timeout=15).stdout
            else:
                existing = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, check=True).stdout
            if str(ROOT / 'portal/discovery.py') in existing and str(directory / 'state.json') in existing:
                os.kill(pid, 15)
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    background = {'creationflags': 0x00000008 | 0x00000200} if sys.platform == 'win32' else {'start_new_session': True}
    with (directory / 'discovery.log').open('a', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **background)
    save(pidfile, str(process.pid) + '\n')
    print('宿主机入口发现已在后台启动；当前环境未配置系统自启，主机重启后需重跑安装脚本。')


def install_hub(http_port, configure_only=False, offline=False):
    config = ROOT / '.workbench/hub.env'
    previous = load_env(config)
    http_port = int(previous.get('OPSKIT_HTTP_PORT', '80')) if http_port is None else http_port
    v = configure(previous, interactive=False)
    v.update(HUB_ONLY='true', OPS_PUBLIC_HOST='localhost', OPS_PUBLIC_SCHEME='http',
             OPS_PUBLIC_PORT=str(http_port), OPSKIT_HTTP_PORT=str(http_port),
             OPSKIT_BIND_IP='127.0.0.1', NPM_BIND_IP='127.0.0.1')
    validate(v)
    save(config, env_text(v))
    directory = hub_directory()
    sys.path.insert(0, str(ROOT / 'portal'))
    from server import Catalog
    # 只登记实际安装的 NPM，其余完整套件工具由 management 安装。
    previous = os.environ.get('OPSKIT_COMPONENTS')
    os.environ['OPSKIT_COMPONENTS'] = 'npm'
    try:
        Catalog(directory, seed_tools=True)
    finally:
        if previous is None:
            os.environ.pop('OPSKIT_COMPONENTS', None)
        else:
            os.environ['OPSKIT_COMPONENTS'] = previous
    if configure_only:
        print(f'Hub 配置已就绪：{directory}；运行 ./install.sh --hub-only 启动 NPM 和 Hub。')
        return
    docker_ready()
    command = ['docker', 'compose', '--env-file', str(config), '-f', 'hub.compose.yaml']
    run(command + ['config', '--quiet'])
    # 拒绝挤占其他程序的端口；重跑时允许复用本项目自己的监听。
    owned = run(command + ['ps', '-q', 'proxy-manager'], capture_output=True, text=True).stdout.strip()
    if not owned:
        for value in (http_port, int(v['NPM_ADMIN_PORT'])):
            with socket.socket() as listener:
                listener.settimeout(0.5)
                occupied = listener.connect_ex(('127.0.0.1', value)) == 0
            if occupied:
                raise RuntimeError(f'本机 {value} 端口已被占用，请调整配置或停止占用程序后重跑') from None
    prepare_images(v, config, command, offline)
    start_discovery()
    run(command + ['up', '-d', '--pull', 'never'])
    from configure_proxy import initialize
    initialize(v)
    suffix = '' if http_port == 80 else ':' + str(http_port)
    print(f'应用工作台已启动：http://127.0.0.1{suffix}/')
    print(f'NPM 管理界面：http://127.0.0.1:{v["NPM_ADMIN_PORT"]}；登录信息保存于 {config}')


def main():
    parser = argparse.ArgumentParser(description='OpsKit 管理中心安装向导')
    parser.add_argument('--hub-only', action='store_true', help='只运行统一 Hub，不安装其他运维工具')
    parser.add_argument('--mode', choices=('development', 'production'), help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, help='NPM 的本机 HTTP 端口，沿用已保存值，新装默认 80')
    parser.add_argument('--configure-only', action='store_true', help='只生成配置，不启动容器')
    parser.add_argument('--offline', action='store_true', help='仅使用已导入的本地镜像')
    parser.add_argument('--reconfigure', action='store_true', help='重新引导网络等配置，保留已有凭据')
    args = parser.parse_args()
    os.umask(0o077)
    if args.hub_only or args.mode == 'development':
        if args.port is not None and not 1 <= args.port <= 65535:
            parser.error('Hub 端口须为 1～65535')
        install_hub(args.port, args.configure_only, args.offline)
        return
    source = ROOT / '.env'
    if not source.exists() and (ROOT.parent / '.env').exists():
        source = ROOT.parent / '.env'
        print('发现 v0.1 根目录配置，将保留原文件并迁移已有密码。')
    existing = load_env(source)
    existing.pop('HUB_ONLY', None)
    progress = ROOT / '.env.install-progress'
    draft = load_env(progress)
    if draft:
        print('发现未完成配置，已将上次确认的选择作为默认值；已有密码继续保留。')
    v = configure(existing, args.reconfigure, draft=draft,
                  checkpoint=lambda values: save(progress, env_text(values)))
    write_outputs(v)
    progress.unlink(missing_ok=True)
    print(f'配置已保存：{ROOT / ".env"}（已限制文件访问权限，密码不显示在终端）')
    print('已生成 collector.env（只含采集连接凭据）及 outer-nginx.conf.example。')
    if args.configure_only:
        print('配置阶段完成。准备好 Docker 后重新运行 ./install.sh 即可继续。')
        return
    docker_ready()
    run(['docker', 'compose', 'config', '--quiet'])
    prepare_images(v, ROOT / '.env', offline=args.offline)
    start_discovery()
    run(['docker', 'compose', 'up', '-d', '--pull', 'never'])
    from configure_proxy import initialize
    initialize(v)
    from check import check
    check(v)
    print(f'\n应用工作台已启动：{public_url(v)}/')
    print(f'NPM 管理界面：http://{v["NPM_BIND_IP"]}:{v["NPM_ADMIN_PORT"]}')
    print('外层 Nginx、DNS 与防火墙需由其管理员按生成的示例配置。')
    print('下一步：复制 collector/ 到每台业务机，并只传递 collector.env 作为其 .env。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'安装未完成：{exc}', file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\n安装已中止。已保存的配置可在重跑时继续使用。', file=sys.stderr)
        sys.exit(1)

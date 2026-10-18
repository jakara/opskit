#!/usr/bin/env python3
"""管理中心交互式安装向导。"""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile

import console_ui as ui

from common import ROOT, ask, choose_configuration, compose_start_command, docker_ready, env_text, host, image_defaults, ipv4, load_env, password, port, powershell, prepare_images, run, save

IMAGES = image_defaults({
    'WORKBENCH_IMAGE': 'm.daocloud.io/docker.io/library/python:3.12-alpine',
    'NPM_IMAGE': 'm.daocloud.io/docker.io/jc21/nginx-proxy-manager:2.16.0',
    'SPUG_IMAGE': 'ghcr.io/openspug/spug:4.1',
    'MARIADB_IMAGE': 'm.daocloud.io/docker.io/library/mariadb:10.11',
    'DBX_IMAGE': 'docker.cnb.cool/dbxio.com/dbx:0.6.40',
    'VICTORIALOGS_IMAGE': 'm.daocloud.io/docker.io/victoriametrics/victoria-logs:v1.53.0',
    'FLUENTBIT_IMAGE': 'm.daocloud.io/docker.io/fluent/fluent-bit:5.1.4',
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
        if interactive:
            ui.step(1, 6, '访问入口', '外层 Nginx 负责 HTTPS，OpsKit 不申请证书。有效输入会自动保存，退出后可继续。')
        prompt('OPS_PUBLIC_HOST', '浏览器访问域名', v.get('OPS_PUBLIC_HOST', 'ops.example.internal'), host)
        prompt('OPS_PUBLIC_SCHEME', '浏览器最终使用的协议', v.get('OPS_PUBLIC_SCHEME', 'https'), lambda s: s in ('http', 'https'))
        prompt('OPS_PUBLIC_PORT', '浏览器访问端口', v.get('OPS_PUBLIC_PORT', '443' if v['OPS_PUBLIC_SCHEME'] == 'https' else '80'), port)
        prompt('OPSKIT_BIND_IP', '管理机监听 IPv4（外层代理在本机时用 127.0.0.1）', v.get('OPSKIT_BIND_IP', '127.0.0.1'), ipv4)
        prompt('OPSKIT_HTTP_PORT', '管理机 HTTP 端口', v.get('OPSKIT_HTTP_PORT', '80'), port)
        if interactive:
            ui.step(2, 6, '统一登录', '只需一个运维账号即可进入全部内置工具；组件内部账号由 OpsKit 自动管理。')
        prompt('OPS_ADMIN_USER', '统一运维账号', v.get('OPS_ADMIN_USER', 'operator'),
               lambda s: bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', s)))
        # 已有内部账号保持原值，避免重配时改坏组件的原生登录。
        v.setdefault('NPM_ADMIN_EMAIL', 'admin@example.internal')
        v.setdefault('SPUG_ADMIN_USER', 'admin')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', v.get('LOG_AGENT_USER', '')) or v.get('LOG_AGENT_USER') == v['OPS_ADMIN_USER']:
            v['LOG_AGENT_USER'] = 'logagent' if v['OPS_ADMIN_USER'] != 'logagent' else 'opskit-agent'
        if interactive:
            ui.note('密码要求：12～128 位，不含空白、美元符、引号和反斜杠；回车可自动生成。')
        for key in PASSWORD_KEYS:
            if not password(v.get(key, '')) or v[key].startswith('REPLACE_'):
                if key == 'OPS_ADMIN_PASSWORD':
                    prompt(key, '统一运维密码', secrets.token_hex(24), password, secret=True)
                else:
                    v[key] = secrets.token_hex(24)
                    if checkpoint:
                        checkpoint(v)
        if interactive:
            ui.step(3, 6, '日志接入', '采集器使用独立机器凭据，自动写入 collector.env，无需另设登录账号。')
        prompt('LOG_INGEST_HOST', '业务机连接管理机的内网 IP 或域名', v.get('LOG_INGEST_HOST', v['OPS_PUBLIC_HOST']), host)
        prompt('LOG_RETENTION_DAYS', '日志保留天数', v.get('LOG_RETENTION_DAYS', '14'), lambda s: s.isdigit() and 1 <= int(s) <= 3650)
        if interactive:
            ui.step(4, 6, '高级选项', '默认端口和镜像可直接使用；已有自定义值会保留。')
        advanced = ask('是否调整恢复端口和镜像来源（y/n）', 'n', lambda s: s in ('y', 'n')) == 'y' if interactive else False
        v.setdefault('NPM_BIND_IP', '127.0.0.1')
        v.setdefault('NPM_ADMIN_PORT', '8181')
        v.setdefault('SPUG_HTTP_PORT', '8081')
        v.setdefault('OPSKIT_LOG_FORWARD_PORT', '24224')
        if advanced:
            prompt('NPM_BIND_IP', 'NPM 恢复端口监听 IPv4', v['NPM_BIND_IP'], ipv4)
            prompt('NPM_ADMIN_PORT', 'NPM 恢复端口', v['NPM_ADMIN_PORT'], port)
            prompt('SPUG_HTTP_PORT', 'Spug 恢复端口', v['SPUG_HTTP_PORT'], lambda s: port(s) and s not in (v['OPSKIT_HTTP_PORT'], v['NPM_ADMIN_PORT']))
            prompt('OPSKIT_LOG_FORWARD_PORT', '内部日志接收端口', v['OPSKIT_LOG_FORWARD_PORT'], lambda s: port(s) and s not in (v['OPSKIT_HTTP_PORT'], v['NPM_ADMIN_PORT'], v['SPUG_HTTP_PORT']))
        for key, image in IMAGES.items():
            if advanced:
                prompt(key, key + ' 镜像地址', v.get(key, image), lambda s: bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]+', s)))
            else:
                v.setdefault(key, image)
        if checkpoint:
            checkpoint(v)
    for key, image in IMAGES.items():
        v.setdefault(key, image)
    v.setdefault('SPUG_HTTP_PORT', '8081')
    v.setdefault('OPSKIT_LOG_FORWARD_PORT', '24224')
    if v.get('HUB_ONLY') != 'true' and (not port(v['OPSKIT_LOG_FORWARD_PORT']) or v['OPSKIT_LOG_FORWARD_PORT'] in (
            v['OPSKIT_HTTP_PORT'], v['NPM_ADMIN_PORT'], v['SPUG_HTTP_PORT'])):
        raise ValueError('OPSKIT_LOG_FORWARD_PORT 无效或与访问端口冲突，请配置独立的本机日志接收端口')
    validate(v)
    return v


def public_url(v):
    default = '443' if v['OPS_PUBLIC_SCHEME'] == 'https' else '80'
    suffix = '' if v['OPS_PUBLIC_PORT'] == default else ':' + v['OPS_PUBLIC_PORT']
    return f"{v['OPS_PUBLIC_SCHEME']}://{v['OPS_PUBLIC_HOST']}{suffix}"


def show_credentials(v, config, hub_only=False):
    ui.summary('安装完成 · 请保存统一登录信息', [
        ('工作台', public_url(v) + '/'),
        ('统一运维账号', v['OPS_ADMIN_USER']),
        ('统一运维密码', v['OPS_ADMIN_PASSWORD']),
        ('配置与恢复凭据', config),
        ('组件入口', '/npm/' if hub_only else '/spug/  /dbx/  /logs/  /npm/'),
    ])
    ui.note('组件内部账号及采集凭据已自动管理，保存在上述私密配置中；不需要逐个登录。')
    ui.note('配置变更前的备份位于相应配置目录的 .backups/，已自动核验。')
    if not hub_only:
        ui.note('下一步：将 collector.env 交给每台业务机的采集端，不要分发管理端 .env。')


def write_outputs(v):
    save(ROOT / '.env', env_text(v))
    write_auth(v)
    connection = {
        'OPS_HOST': v['LOG_INGEST_HOST'], 'OPS_PORT': v['OPSKIT_HTTP_PORT'],
        'OPS_VHOST': v['OPS_PUBLIC_HOST'], 'OPS_SCHEME': 'http',
        'LOG_AGENT_USER': v['LOG_AGENT_USER'], 'LOG_AGENT_PASSWORD': v['LOG_AGENT_PASSWORD'],
    }
    save(ROOT / 'collector.env', env_text(connection))
    upstream = v['OPSKIT_BIND_IP'] if v['OPSKIT_BIND_IP'] != '0.0.0.0' else '管理机内网IP'
    save(ROOT / 'outer-nginx.conf.example', f'''# 将以下内容合并到已有 HTTPS server；证书仍由外层 Nginx 管理。
# 如果外层 Nginx 位于另一台主机或容器，请将 {upstream} 换成可达的管理机地址。
# 外层需要保留路径、Host、Cookie 和 WebSocket；不要添加浏览器 Basic Auth。
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


def write_auth(v):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'portal'))
    from auth import configuration
    name = 'hub-operator.json' if v.get('HUB_ONLY') == 'true' else 'operator.json'
    path = ROOT / '.workbench/auth' / name
    previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    config = configuration(v['OPS_ADMIN_USER'], v['OPS_ADMIN_PASSWORD'], v['OPS_PUBLIC_SCHEME'] == 'https', previous)
    identity = str(ROOT.resolve()) + ':' + name + ':' + v['OPS_PUBLIC_HOST'] + ':' + v['OPS_PUBLIC_PORT']
    config['cookie_name'] = 'opskit_session_' + hashlib.sha256(identity.encode()).hexdigest()[:12]
    save(path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    components = {'npm': {'identity': v['NPM_ADMIN_EMAIL'], 'secret': v['NPM_ADMIN_PASSWORD']}}
    if v.get('HUB_ONLY') != 'true':
        components.update(spug={'username': v['SPUG_ADMIN_USER'], 'password': v['SPUG_ADMIN_PASSWORD']},
                          dbx={'password': v['DBX_PASSWORD']})
    component_name = 'hub-components.json' if v.get('HUB_ONLY') == 'true' else 'components.json'
    save(path.parent / component_name, json.dumps(components, ensure_ascii=False, indent=2) + '\n')


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


def start_discovery(control=False):
    """宿主机导出清单，并可选处理两个固定服务的请求。"""
    directory = ROOT / '.workbench/discovery'
    command = [sys.executable, '-u', str(ROOT / 'portal/discovery.py'), '--output', str(directory / 'state.json')]
    if control:
        command.append('--control')
    run(command + ['--once'])
    if sys.platform != 'win32' and os.geteuid() == 0 and Path('/run/systemd/system').exists() and shutil.which('systemctl'):
        service = 'opskit-discovery-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:8] + '.service'
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        content = ('[Unit]\nDescription=OpsKit host service discovery\nAfter=docker.service network.target\n'
                   '[Service]\nType=simple\nExecStart=' + ' '.join(map(quote, command)) +
                   '\nEnvironment=' + quote('PATH=' + os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')) +
                   '\nRestart=always\nRestartSec=5\nTimeoutStopSec=3600\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n')
        save(Path('/etc/systemd/system') / service, content, private=False)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', service])
        run(['systemctl', 'restart', service])
        run(['systemctl', 'is-active', '--quiet', service])
        print(f'宿主机入口发现已启动并随系统启动：{service}')
        return
    pidfile = directory / 'discovery.pid'
    if pidfile.exists():
        from uninstall import stop_process
        stop_process(ROOT, directory, 'discovery.py')
    background = {'creationflags': 0x00000008 | 0x00000200} if sys.platform == 'win32' else {'start_new_session': True}
    with (directory / 'discovery.log').open('a', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **background)
    save(pidfile, str(process.pid) + '\n')
    print('宿主机入口发现已在后台启动；当前环境未配置系统自启，主机重启后需重跑安装脚本。')


@contextmanager
def installation_control():
    from uninstall import admin, service_file, stop_process
    from service_control import Controller, SERVICES
    unit = service_file(ROOT)
    if sys.platform.startswith('linux') and unit.exists():
        if str(ROOT / 'portal/discovery.py') not in unit.read_text(encoding='utf-8'):
            raise RuntimeError('发现服务已自定义，停止安装以免与升级恢复并发')
        admin('systemctl', 'stop', unit.name)
    stop_process(ROOT, ROOT / '.workbench/discovery', 'discovery.py')
    controller = Controller()
    with closing(sqlite3.connect(controller.directory / 'agent.lock', timeout=1)) as lock:
        try:
            lock.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError:
            raise RuntimeError('服务操作尚未结束，请等待 Spug 任务完成后重跑安装') from None
        recovered = {}
        for path in (controller.directory / 'recovery').glob('*.json'):
            state = json.loads(path.read_text(encoding='utf-8'))
            controller.recover(state, check_health=False)
            key = SERVICES[state['service']]
            recovered[key] = load_env(ROOT / '.env')[key]
        # 持锁直到 Compose 和路由配置完成，恢复写卷期间绝不启动业务容器。
        yield recovered


def clean_reinstall(config, configure_only=False, hub_only=False):
    if configure_only:
        raise ValueError('完全重新安装会清空运行数据，不能与 --configure-only 同时使用；请正常运行安装向导再选择第三项。')
    if config not in (ROOT / '.env', ROOT / '.workbench/hub.env'):
        raise ValueError('旧版配置不在当前模块内，请先使用旧版卸载入口清理，避免遗漏旧数据；未执行清空。')
    from uninstall import inventory, uninstall
    docker_ready()
    plan = inventory([ROOT], hub_only)
    if plan.get('orphans'):
        raise RuntimeError('存在无法直接备份的孤立卷，请先恢复原容器挂载后再清理：' + '、'.join(plan['orphans']))
    command = ['docker', 'compose']
    if hub_only:
        command += ['--env-file', str(config), '-f', 'hub.compose.yaml']
    declared = json.loads(run(command + ['config', '--format', 'json'], capture_output=True, text=True).stdout)
    names = {volume['name'] for volume in declared.get('volumes', {}).values()}
    existing = set(run(['docker', 'volume', 'ls', '--format', '{{.Name}}'], capture_output=True, text=True).stdout.splitlines())
    unknown = (names & existing) - {volume['name'] for volume in plan['volumes']}
    if unknown:
        raise RuntimeError('存在未能由当前容器确认归属的数据卷，已停止完全重装；请先恢复原容器挂载后再清理：' + '、'.join(sorted(unknown)))
    print('\n完全重新安装将清空当前安装范围内的密码、登录态、代理规则及全部数据。')
    print('  安装目录：' + str(ROOT) + ('（仅精简管理端）' if hub_only else '（管理模块，包括同目录的精简管理端）'))
    print('  容器：' + ('、'.join(c['Name'].lstrip('/') for c in plan['containers']) or '无'))
    print('  数据卷：' + ('、'.join(volume['name'] for volume in plan['volumes']) or '无'))
    print('源码、交付文档、离线镜像及其他 Docker 应用保留；备份内仍含原凭据，请妥善保管。')
    if input('确认完全重新安装请输入 yes；其他输入取消：').strip().lower() != 'yes':
        raise RuntimeError('已取消完全重新安装，未作更改。')
    if hub_only:
        backup = uninstall([ROOT], hub_only=True, purge=True, yes=True)
    else:
        with installation_control():
            backup = uninstall([ROOT], purge=True, yes=True)
    if backup is None:
        raise RuntimeError('已取消完全重新安装，未进入新配置向导。')
    print(f'原安装已清空，可恢复备份：{backup}；接下来配置全新安装，留空生成新密码。')


def install_hub(http_port, configure_only=False, offline=False, reconfigure=False, reuse=False):
    config = ROOT / '.workbench/hub.env'
    reconfigure = choose_configuration(config, reconfigure, reuse,
                                        reinstall=lambda: clean_reinstall(config, configure_only, hub_only=True))
    previous = load_env(config)
    http_port = int(previous.get('OPSKIT_HTTP_PORT', '80')) if http_port is None else http_port
    ui.step(1, 3, '精简管理端配置', '只运行 Hub 与 NPM；使用统一运维账号进入。')
    v = configure(previous, interactive=False)
    if sys.stdin.isatty() and (not previous or reconfigure):
        v['OPS_ADMIN_USER'] = ask('统一运维账号', v['OPS_ADMIN_USER'], lambda value: bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', value)))
        if not previous.get('OPS_ADMIN_PASSWORD'):
            v['OPS_ADMIN_PASSWORD'] = ask('统一运维密码', v['OPS_ADMIN_PASSWORD'], password, secret=True)
    if reconfigure:
        http_port = int(ask('Hub HTTP 端口', str(http_port), port))
        v['NPM_ADMIN_PORT'] = ask('NPM 初始化与恢复端口', v['NPM_ADMIN_PORT'], lambda s: port(s) and s != str(http_port))
    v.update(HUB_ONLY='true', OPS_PUBLIC_HOST='localhost', OPS_PUBLIC_SCHEME='http',
             OPS_PUBLIC_PORT=str(http_port), OPSKIT_HTTP_PORT=str(http_port),
             OPSKIT_BIND_IP='127.0.0.1', NPM_BIND_IP='127.0.0.1')
    validate(v)
    save(config, env_text(v))
    write_auth(v)
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
    ui.step(2, 3, '启动工作台与代理')
    docker_ready()
    command = ['docker', 'compose', '--env-file', str(config), '-f', 'hub.compose.yaml']
    run(command + ['config', '--quiet'])
    startup = compose_start_command(v, command, ['hub.compose.yaml'])
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
    from verify_ui import verify
    verify(v, hub_only=v.get('HUB_ONLY') == 'true')
    start_discovery()
    run(startup)
    from configure_proxy import initialize
    initialize(v)
    suffix = '' if http_port == 80 else ':' + str(http_port)
    print(f'应用工作台已启动：http://127.0.0.1{suffix}/')
    print(f'NPM 管理界面：http://127.0.0.1{suffix}/npm/；登录信息保存于 {config}')
    ui.step(3, 3, '完成')
    show_credentials(v, config, hub_only=True)


def main():
    parser = argparse.ArgumentParser(description='OpsKit 管理中心安装向导')
    parser.add_argument('--hub-only', action='store_true', help='只运行统一 Hub，不安装其他运维工具')
    parser.add_argument('--mode', choices=('development', 'production'), help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, help='NPM 的本机 HTTP 端口，沿用已保存值，新装默认 80')
    parser.add_argument('--configure-only', action='store_true', help='只生成配置，不启动容器')
    parser.add_argument('--offline', action='store_true', help='仅使用已导入的本地镜像')
    configuration = parser.add_mutually_exclusive_group()
    configuration.add_argument('--reconfigure', action='store_true', help='重新引导网络等配置，保留已有凭据')
    configuration.add_argument('--reuse-config', action='store_true', help='明确复用已有配置，跳过选择菜单（适用于自动化）')
    args = parser.parse_args()
    os.umask(0o077)
    ui.banner('管理中心安装向导')
    if args.hub_only or args.mode == 'development':
        if args.port is not None and not 1 <= args.port <= 65535:
            parser.error('Hub 端口须为 1～65535')
        install_hub(args.port, args.configure_only, args.offline, args.reconfigure, args.reuse_config)
        return
    source = ROOT / '.env'
    if not source.exists() and (ROOT.parent / '.env').exists():
        source = ROOT.parent / '.env'
        print('发现 v0.1 根目录配置，将保留原文件并迁移已有密码。')
    progress = ROOT / '.env.install-progress'
    draft = load_env(progress)
    if draft:
        print('发现未完成配置，已将上次确认的选择作为默认值；已有密码继续保留。')
    reconfigure = choose_configuration(source, args.reconfigure, args.reuse_config, draft,
                                        reinstall=lambda: clean_reinstall(source, args.configure_only))
    existing = load_env(source)
    existing.pop('HUB_ONLY', None)
    draft = load_env(progress)
    v = configure(existing, reconfigure, draft=draft,
                  checkpoint=lambda values: save(progress, env_text(values)))
    v['OPSKIT_CONTROL_OFFLINE'] = 'true' if args.offline else 'false'
    if args.configure_only:
        write_outputs(v)
        progress.unlink(missing_ok=True)
        ui.summary('配置已就绪', [('配置文件', ROOT / '.env'), ('下一步', '重新运行原安装命令，复用配置即可继续。')])
        return
    ui.step(5, 6, '准备并启动组件', '离线模式仅核对本地镜像。' if args.offline else '逐项准备镜像并启动服务，失败时保留配置以便重试。')
    docker_ready()
    with installation_control() as recovered:
        v.update(recovered)
        write_outputs(v)
        progress.unlink(missing_ok=True)
        print(f'配置已保存：{ROOT / ".env"}（已限制文件访问权限；服务启动并检查成功后回显登录信息）')
        print('已生成 collector.env（只含采集连接凭据）及 outer-nginx.conf.example。')
        run(['docker', 'compose', 'config', '--quiet'])
        startup = compose_start_command(v)
        prepare_images(v, ROOT / '.env', offline=args.offline)
        from verify_ui import verify
        verify(v, hub_only=v.get('HUB_ONLY') == 'true')
        run(startup)
        from configure_proxy import initialize
        initialize(v)
    start_discovery(control=True)
    from self_management import initialize as initialize_self
    initialize_self(v)
    ui.step(6, 6, '验收与完成', '检查统一登录、组件入口和日志采集权限。')
    from check import check
    check(v)
    print(f'\n应用工作台已启动：{public_url(v)}/')
    print(f'NPM：{public_url(v)}/npm/  Spug：{public_url(v)}/spug/')
    print('外层 Nginx、DNS 与防火墙需由其管理员按生成的示例配置。')
    print('下一步：复制 collector/ 到每台业务机，并只传递 collector.env 作为其 .env。')
    show_credentials(v, ROOT / '.env')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        ui.error(f'安装未完成：{exc}')
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        ui.error('安装已中止；已确认的选项已保存，重跑可继续。')
        sys.exit(1)

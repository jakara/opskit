#!/usr/bin/env python3
"""可独立复制到业务机的 Fluent Bit 安装向导。"""
import argparse
import base64
import os
from pathlib import Path
from pathlib import PureWindowsPath
import platform
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from common import ROOT, ask, docker_ready, env_text, host, load_env, password, port, run, save


def identifier(value):
    return bool(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', value))


def log_directory(value):
    if sys.platform == 'win32':
        path = PureWindowsPath(value)
        return (bool(re.match(r'^[A-Za-z]:[\\/]', value)) and path.is_absolute() and path != PureWindowsPath(path.anchor) and '..' not in path.parts
                and not any(c in value[2:] for c in (':', "'", '$', '\n', '\r')) and Path(value).is_dir())
    return value.startswith('/') and Path(value).resolve() != Path('/') and not any(c in value for c in (':', "'", '$', '\\', '\n', '\r')) and Path(value).is_dir()


def pattern(value):
    return bool(re.fullmatch(r'[A-Za-z0-9_*?./-]+', value)) and not value.startswith('/') and '..' not in value


def validate(v):
    checks = {'OPS_HOST': host, 'OPS_VHOST': host, 'OPS_PORT': port,
              'OPS_SCHEME': lambda s: s in ('http', 'https'),
              'LOG_AGENT_USER': identifier, 'LOG_AGENT_PASSWORD': password,
              'HOST_ID': identifier, 'PROJECT_ID': identifier, 'APP_ID': identifier,
              'APP_ENV': identifier, 'HOST_LOG_DIR': log_directory, 'LOG_FILE_PATTERN': pattern}
    for key, check in checks.items():
        if not check(v.get(key, '')) or v.get(key, '').startswith('REPLACE_'):
            raise ValueError(f'{key} 缺失或无效，请运行 ./install.sh --reconfigure')
    v['OPS_TLS'] = 'On' if v['OPS_SCHEME'] == 'https' else 'Off'


def configure(existing, reconfigure=False):
    v = dict(existing)
    if sys.platform == 'win32' and 'HOST_LOG_DIR' in v:
        v['HOST_LOG_DIR'] = v['HOST_LOG_DIR'].replace('\\', '/')
    required = ('HOST_ID', 'PROJECT_ID', 'APP_ID', 'APP_ENV', 'HOST_LOG_DIR', 'LOG_FILE_PATTERN',
                'OPS_HOST', 'OPS_VHOST', 'OPS_PORT', 'OPS_SCHEME', 'LOG_AGENT_USER', 'LOG_AGENT_PASSWORD')
    if reconfigure or any(key not in v for key in required):
        print('\n采集端配置：默认向管理机内网 HTTP 写入，不需要证书。')
        v['OPS_HOST'] = ask('管理机可连接的内网 IP 或域名', v.get('OPS_HOST', 'ops.example.internal'), host)
        v['OPS_SCHEME'] = ask('日志接入协议（直连内网用 http，经过外层 HTTPS 用 https）', v.get('OPS_SCHEME', 'http'), lambda s: s in ('http', 'https'))
        v['OPS_PORT'] = ask('日志接入端口', v.get('OPS_PORT', '443' if v['OPS_SCHEME'] == 'https' else '80'), port)
        v['OPS_VHOST'] = ask('NPM 代理主机的域名（与管理端一致）', v.get('OPS_VHOST', v['OPS_HOST']), host)
        for key, label, default in (
                ('HOST_ID', '主机标识', platform.node().split('.')[0]),
                ('PROJECT_ID', '项目标识', 'college-project'),
                ('APP_ID', '应用标识', 'academic-service'),
                ('APP_ENV', '运行环境', 'production'),
                ('LOG_AGENT_USER', '日志写入用户名', 'logagent')):
            v[key] = ask(label, v.get(key, default), identifier)
        default_directory = 'C:/logs/myapp' if sys.platform == 'win32' else '/var/log/myapp'
        v['HOST_LOG_DIR'] = ask('业务日志绝对目录（必须已存在）', v.get('HOST_LOG_DIR', default_directory), log_directory)
        if sys.platform == 'win32':
            v['HOST_LOG_DIR'] = v['HOST_LOG_DIR'].replace('\\', '/')
        v['LOG_FILE_PATTERN'] = ask('该目录中的日志文件匹配规则', v.get('LOG_FILE_PATTERN', '*.log'), pattern)
        if not password(v.get('LOG_AGENT_PASSWORD', '')) or v.get('LOG_AGENT_PASSWORD', '').startswith('REPLACE_'):
            v['LOG_AGENT_PASSWORD'] = ask('管理端发放的日志写入密码', '', password, secret=True)
        v['FLUENTBIT_IMAGE'] = ask('Fluent Bit 镜像地址（可填私有仓库）', v.get('FLUENTBIT_IMAGE', 'fluent/fluent-bit:5.1.4'),
                                  lambda s: bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]+', s)))
    v.setdefault('FLUENTBIT_IMAGE', 'fluent/fluent-bit:5.1.4')
    validate(v)
    return v


def check_connection(v):
    url = f'{v["OPS_SCHEME"]}://{v["OPS_HOST"]}:{v["OPS_PORT"]}/logs/insert/jsonline?_msg_field=log'
    token = base64.b64encode(f'{v["LOG_AGENT_USER"]}:{v["LOG_AGENT_PASSWORD"]}'.encode()).decode()
    req = Request(url, data=b'', headers={'Host': v['OPS_VHOST'], 'Authorization': 'Basic ' + token})
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f'管理端写入探测返回 HTTP {response.status}')
    except HTTPError as exc:
        raise RuntimeError(f'管理端写入探测返回 HTTP {exc.code}；请检查地址、Host 域名和采集凭据') from None
    print('通过：管理端日志写入连通性与凭据（空批次，不写入业务记录）。')


def main():
    parser = argparse.ArgumentParser(description='OpsKit Fluent Bit 采集端安装向导')
    parser.add_argument('--configure-only', action='store_true', help='只生成配置，不连接管理端或启动容器')
    parser.add_argument('--offline', action='store_true', help='使用已导入的本地镜像')
    parser.add_argument('--reconfigure', action='store_true', help='重新引导连接地址、标签和日志路径')
    args = parser.parse_args()
    os.umask(0o077)
    v = configure(load_env(ROOT / '.env'), args.reconfigure)
    save(ROOT / '.env', env_text(v))
    print(f'配置已保存：{ROOT / ".env"}（权限 600）')
    if args.configure_only:
        print('配置阶段完成。准备好 Docker 和管理端后重新运行 ./install.sh。')
        return
    docker_ready()
    run(['docker', 'compose', 'config', '--quiet'])
    check_connection(v)
    if not args.offline:
        run(['docker', 'compose', 'pull'])
    run(['docker', 'compose', 'up', '-d', '--pull', 'never'])
    time.sleep(3)
    state = run(['docker', 'compose', 'ps', '--all', '--format', 'json'], capture_output=True, text=True).stdout
    import json
    states = json.loads(state) if state.strip().startswith('[') else [json.loads(line) for line in state.splitlines() if line.strip()]
    if len(states) != 1 or states[0].get('State') != 'running':
        raise RuntimeError('Fluent Bit 未持续运行，请检查 docker compose logs fluent-bit')
    run(['docker', 'compose', 'ps'])
    print('采集端已启动。请追加一条业务日志，再到管理端按主机和项目查询，以确认文件读取链路。')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'安装未完成：{exc}', file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\n安装已中止，已保存配置可供重跑。', file=sys.stderr)
        sys.exit(1)

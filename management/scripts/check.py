#!/usr/bin/env python3
"""检查实际路由、登录边界和日志读写权限；失败返回非零。"""
import base64
import json
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

from common import ROOT, load_env, operator_headers, run


def request(v, path, credential=None, data=None):
    ip = v['OPSKIT_BIND_IP'] if v['OPSKIT_BIND_IP'] != '0.0.0.0' else '127.0.0.1'
    headers = {'Host': v['OPS_PUBLIC_HOST'], 'X-Forwarded-Proto': v['OPS_PUBLIC_SCHEME']}
    if credential:
        user_key, password_key = credential
        if user_key == 'OPS_ADMIN_USER':
            headers.update(operator_headers(v))
        else:
            encoded = base64.b64encode(f'{v[user_key]}:{v[password_key]}'.encode()).decode()
            headers['Authorization'] = 'Basic ' + encoded
    req = Request(f'http://{ip}:{v["OPSKIT_HTTP_PORT"]}{path}', data=data, headers=headers)
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=10) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, b''


def check_spug(v):
    operator = ('OPS_ADMIN_USER', 'OPS_ADMIN_PASSWORD')
    deadline = time.monotonic() + 180
    while True:
        try:
            code, page = request(v, '/spug/', operator)
        except (URLError, OSError):
            code, page = 0, b''
        if code == 200 and b'<title>Spug</title>' in page:
            print('通过：Spug 子路径入口')
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Spug /spug/ 未就绪，HTTP {code}')
        time.sleep(2)


def check(v):
    run(['docker', 'compose', 'config', '--quiet'])
    run(['docker', 'compose', 'exec', '-T', 'proxy-manager', 'nginx', '-t'])
    operator = ('OPS_ADMIN_USER', 'OPS_ADMIN_PASSWORD')
    collector = ('LOG_AGENT_USER', 'LOG_AGENT_PASSWORD')
    for path in ('/', '/hub/api/state', '/npm/', '/spug/', '/dbx/', '/logs/select/vmui/'):
        deadline = time.monotonic() + 180
        while True:
            try:
                code, _ = request(v, path, operator)
            except (URLError, OSError):
                code = 0
            if code == 200:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f'{path} 未就绪，HTTP {code}；请检查对应服务')
            time.sleep(2)
        if request(v, path)[0] != 401:
            raise RuntimeError(f'{path} 的未登录访问未被阻止')
        if request(v, path, collector)[0] != 401:
            raise RuntimeError(f'{path} 错误地允许了采集账户访问')
        print(f'通过：{path} 路由与访问隔离')
    _, homepage = request(v, '/', operator)
    if b'OpsKit' not in homepage or b'id="builtin-section"' not in homepage:
        raise RuntimeError('根路径没有返回 Hub 页面')
    deadline = time.monotonic() + 60
    while True:
        _, state = request(v, '/hub/api/state', operator)
        discovered = json.loads(state)
        if (discovered.get('api_version') == 1 and discovered.get('discovered_at') and
                not discovered.get('docker_error') and not discovered.get('http_error')):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Hub 尚未收到有效宿主机发现结果，请检查发现进程')
        time.sleep(2)
    check_spug(v)
    from configure_proxy import API
    target = v['OPSKIT_BIND_IP'] if v['OPSKIT_BIND_IP'] != '0.0.0.0' else '127.0.0.1'
    api = API(f'http://{target}:{v["OPSKIT_HTTP_PORT"]}/npm/api')
    api.headers = operator_headers(v)
    api.token = api.call('/tokens', {'identity': v['NPM_ADMIN_EMAIL'], 'secret': v['NPM_ADMIN_PASSWORD']})['token']
    if api.call('/users/me').get('email') != v['NPM_ADMIN_EMAIL']:
        raise RuntimeError('NPM 子路径登录与用户查询未通过')
    if request(v, '/npm/api/users/me')[0] != 401:
        raise RuntimeError('NPM API 未阻止缺少自身登录凭据的访问')
    if request(v, '/npm/api/users/me', operator)[0] != 401:
        raise RuntimeError('NPM API 错误地将入口账户当成 NPM 登录凭据')
    insert = '/logs/insert/jsonline?_stream_fields=project,host,app,env&_msg_field=log&_time_field=date'
    # 空批次验证写入权限，避免检查脚本向业务日志写入测试记录。
    for credential, expected in ((None, 401), (operator, 401), (collector, 200)):
        code, _ = request(v, insert, credential, data=b'')
        if code != expected:
            raise RuntimeError(f'日志写入权限检查失败：预期 {expected}，实际 {code}')
    if request(v, insert, collector)[0] != 403:
        raise RuntimeError('采集路径未正确限制为 POST')
    query = '/logs/select/logsql/query?' + urlencode({'query': '_time:5m', 'limit': '1'})
    if request(v, query, operator)[0] != 200:
        raise RuntimeError('VictoriaLogs 查询 API 检查失败')
    print('通过：日志查询 API、采集写入凭据及 POST 限制')
    run(['docker', 'compose', 'ps'])


if __name__ == '__main__':
    try:
        check(load_env(ROOT / '.env'))
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'检查未通过：{exc}', file=sys.stderr)
        sys.exit(1)

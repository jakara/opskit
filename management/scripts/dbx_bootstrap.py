#!/usr/bin/env python3
"""通过 DBX 0.6.40 API 预置 Spug 连接与默认只读防护，保留已有连接。"""
from http.cookiejar import CookieJar
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

from common import ROOT, load_env, operator_headers

CONNECTION_ID = 'opskit-spug'
CONNECTION_NAME = 'OpsKit · Spug MariaDB'
PROBE_SQL = 'SELECT 1 AS opskit_probe, DATABASE() AS current_database'
TABLES_SQL = ("SELECT TABLE_NAME AS table_name FROM information_schema.TABLES "
              "WHERE TABLE_SCHEMA = 'spug' AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, v):
        ip = v['OPSKIT_BIND_IP'] if v['OPSKIT_BIND_IP'] != '0.0.0.0' else '127.0.0.1'
        self.base = f'http://{ip}:{v["OPSKIT_HTTP_PORT"]}/dbx/api'
        self.headers = {'Host': v['OPS_PUBLIC_HOST'], 'X-Forwarded-Proto': v['OPS_PUBLIC_SCHEME'],
                        'Content-Type': 'application/json'}
        self.session = operator_headers(v)['Cookie']
        self.jar = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.jar), NoRedirect())

    def call(self, path, data=None):
        request = Request(self.base + path, headers=self.headers,
                          data=json.dumps(data).encode() if data is not None else None)
        # 先让 CookieJar 加入 DBX 自身 Cookie，再补入口 Cookie，二者缺一不可。
        self.jar.add_cookie_header(request)
        request.add_header('Cookie', '; '.join(filter(None, [request.get_header('Cookie'), self.session])))
        try:
            with self.opener.open(request, timeout=15) as response:
                result = json.load(response)
        except HTTPError as exc:
            exc.close()
            raise RuntimeError(f'DBX API {path} 返回 HTTP {exc.code}；请检查入口鉴权与 DBX 配置') from None
        except (URLError, OSError, ValueError):
            raise RuntimeError(f'DBX API {path} 无法访问或未返回有效 JSON') from None
        # 上游错误和连接清单可能含明文密码，只回显固定路径与状态。
        if isinstance(result, dict) and 'error' in result:
            raise RuntimeError(f'DBX API {path} 返回错误；请检查服务状态与连接配置')
        return result


def initialize(v):
    api = API(v)
    deadline = time.monotonic() + 180
    while True:
        try:
            auth = api.call('/auth/check')
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError('DBX 初始化未完成：鉴权接口在 180 秒内未就绪') from None
            time.sleep(2)
    if not isinstance(auth, dict) or auth.get('setup_required') or auth.get('required') is not True:
        raise RuntimeError('DBX 未启用预配置密码，请检查 DBX_PASSWORD 与容器配置')
    login = api.call('/auth/login', {'password': v['DBX_PASSWORD']})
    if not isinstance(login, dict) or login.get('ok') is not True:
        raise RuntimeError('DBX 登录未通过')
    profiles = api.call('/connection/list')
    if not isinstance(profiles, list) or any(not isinstance(p, dict) or
            not isinstance(p.get('id'), str) or not isinstance(p.get('name'), str) for p in profiles):
        raise RuntimeError('DBX 连接清单格式不正确')
    if not v.get('SPUG_DB_PASSWORD'):
        raise ValueError('SPUG_DB_PASSWORD 缺失，请检查 Spug 数据库配置')
    profile = {'id': CONNECTION_ID, 'name': CONNECTION_NAME, 'db_type': 'mysql',
               'driver_profile': 'mariadb', 'driver_label': 'MariaDB', 'host': 'spugdb', 'port': 3306,
               'username': 'spug', 'password': v['SPUG_DB_PASSWORD'], 'database': 'spug',
               'save_password': True, 'read_only': True}
    created = True
    for existing in profiles:
        if existing['id'] == CONNECTION_ID or existing['name'] == CONNECTION_NAME:
            fields = ('id', 'name', 'db_type', 'driver_profile', 'host', 'port', 'username',
                      'database', 'read_only', 'password')
            if not all(existing.get(key) == profile[key] for key in fields):
                print('已保留已有 DBX Spug 连接；未覆盖其配置或凭据，未重新验收人工连接。')
                return {'id': existing['id'], 'name': existing['name'], 'created': False, 'verified': False}
            profile, created = existing, False
            break
    if created:
        # shortcut: 固定版本接口没有 create-if-absent；安装器须串行执行，避免同时编辑同一连接。
        if api.call('/connection/save', {'configs': [profile]}) is not None:
            raise RuntimeError('DBX 保存连接未返回预期结果')
        tested = api.call('/connection/test-info', {'config': profile})
        if not isinstance(tested, dict) or not isinstance(tested.get('message'), str) or not tested['message']:
            raise RuntimeError('DBX Spug 连接测试未返回预期结果')
    connected = api.call('/connection/connect', {'config': profile})
    if not isinstance(connected, str) or not connected:
        raise RuntimeError('DBX Spug 连接未返回预期结果')
    query = {'connectionId': CONNECTION_ID, 'database': 'spug', 'maxRows': 1000, 'timeoutSecs': 10}
    probe = api.call('/query/execute', dict(query, sql=PROBE_SQL))
    probe_rows = probe.get('rows') if isinstance(probe, dict) else None
    # DBX MySQL 驱动会将 int 列传为字符串，只兼容精确的整数 1 和字符串 "1"。
    if (not isinstance(probe, dict) or probe.get('columns') != ['opskit_probe', 'current_database'] or
            not isinstance(probe_rows, list) or len(probe_rows) != 1 or
            not isinstance(probe_rows[0], list) or len(probe_rows[0]) != 2 or
            type(probe_rows[0][0]) not in (int, str) or str(probe_rows[0][0]) != '1' or probe_rows[0][1] != 'spug'):
        raise RuntimeError('DBX Spug 查询验收失败：SELECT 1 或当前数据库不正确')
    tables = api.call('/query/execute', dict(query, sql=TABLES_SQL))
    rows = tables.get('rows') if isinstance(tables, dict) else None
    if not isinstance(tables, dict) or tables.get('columns') != ['table_name'] or not isinstance(rows, list) or not rows or any(
            not isinstance(row, list) or len(row) != 1 or not isinstance(row[0], str) or not row[0] for row in rows):
        raise RuntimeError('DBX Spug 库表验收失败：未读取到已初始化的业务表')
    print('通过：DBX 已预置 Spug 连接，SELECT 1、当前数据库和库表读取均已验收；默认只读防误改可由管理员解锁。')
    return {'id': CONNECTION_ID, 'name': CONNECTION_NAME, 'created': created, 'verified': True,
            'read_only': True, 'database_user': 'spug', 'table_count': len(rows)}


if __name__ == '__main__':
    try:
        initialize(load_env(ROOT / '.env'))
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        print(f'DBX 初始化未完成：{exc}', file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""通过固定版本的 NPM API 初始化可在 WebUI 管理的配置。"""
import json
from datetime import datetime
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from common import ROOT, load_env, save

HUB_ROUTE = '''location /hub/ {
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header Authorization "";
    proxy_pass http://workbench:8090;
}'''


def upgrade_workbench(api, entry, reserved_domains=()):
    """迁移本包原始根入口和导航块，保留自定义代理配置。"""
    advanced = entry.get('advanced_config', '')
    pattern = r'location /hub/\s*\{\s*alias /opskit/portal/;\s*index index.html;\s*\}'
    updated, count = re.subn(pattern, lambda _: HUB_ROUTE, advanced, count=1)
    changes = {}
    if count:
        changes['advanced_config'] = updated
    if (entry.get('forward_host'), entry.get('forward_port')) == ('spug', 80) and (
            '# OpsKit 子路径配置' in advanced or count):
        changes.update(forward_host='workbench', forward_port=8090)
    owned_root = bool(changes.get('forward_host')) or (
        (entry.get('forward_host'), entry.get('forward_port')) == ('workbench', 8090) and
        '# OpsKit 子路径配置' in advanced)
    if owned_root:
        domains = list(entry.get('domain_names', []))
        domains.extend(name for name in ('localhost', '127.0.0.1') if name not in domains and name not in reserved_domains)
        if domains != entry.get('domain_names', []):
            changes['domain_names'] = domains
    if not changes:
        return
    backup = ROOT / '.backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f') / f'npm-proxy-{entry["id"]}.json'
    content = json.dumps(entry, ensure_ascii=False, indent=2)
    save(backup, content)
    if backup.read_text(encoding='utf-8') != content:
        raise RuntimeError('NPM 原配置备份校验失败')
    api.call('/nginx/proxy-hosts/' + str(entry['id']), changes, method='PUT')
    print(f'已升级工作台路由，其余代理配置保持不变；原配置备份：{backup}')


class API:
    def __init__(self, base):
        self.base = base
        self.token = None
        self.opener = build_opener(ProxyHandler({}))

    def call(self, path, data=None, method=None):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = Request(self.base + path, headers=headers,
                          data=json.dumps(data).encode() if data is not None else None, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            # 不回显响应内容，避免上游错误信息夹带请求凭据。
            raise RuntimeError(f'NPM API {path} 返回 HTTP {exc.code}；请检查管理员配置或容器日志') from None


def proxy_payload(v, operator_id, collector_id):
    headers = '''proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Scheme $x_forwarded_scheme;
        proxy_set_header X-Forwarded-Proto $x_forwarded_proto;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Authorization "";
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $http_connection;'''
    # 子路径写在 Advanced 中，避开 NPM Custom Locations 模板重置 HTTPS 协议头的问题。
    advanced = f'''# OpsKit 子路径配置；修改后请运行 scripts/check.sh。
auth_basic "OpsKit 运维访问";
auth_basic_user_file /data/access/{operator_id};
client_max_body_size 64m;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_buffering off;

location = /hub {{ return 302 /; }}
{HUB_ROUTE}
location = /dbx {{ return 308 /dbx/; }}
location ^~ /dbx/ {{
    {headers}
    proxy_pass http://dbx:4224;
}}
location ^~ /logs/insert/ {{
    auth_basic "OpsKit 日志写入";
    auth_basic_user_file /data/access/{collector_id};
    limit_except POST {{ deny all; }}
    client_max_body_size 16m;
    {headers}
    proxy_pass http://victorialogs:9428;
}}
location = /logs {{ return 308 /logs/select/vmui/; }}
location ^~ /logs/ {{
    {headers}
    proxy_pass http://victorialogs:9428;
}}
'''
    local = v.get('HUB_ONLY') == 'true'
    if local:
        # 仅绑定本机回环地址，与原本地 Hub 保持相同访问范围。
        advanced = '# OpsKit 本机 Hub\n' + HUB_ROUTE
    return {
        'domain_names': list(dict.fromkeys([v['OPS_PUBLIC_HOST'], 'localhost', '127.0.0.1'])),
        'forward_scheme': 'http', 'forward_host': 'workbench', 'forward_port': 8090,
        'certificate_id': 0, 'ssl_forced': False, 'caching_enabled': False,
        'block_exploits': False, 'allow_websocket_upgrade': True,
        'http2_support': False, 'hsts_enabled': False, 'hsts_subdomains': False,
        'trust_forwarded_proto': True, 'access_list_id': 0 if local else operator_id,
        'advanced_config': advanced, 'locations': [],
        'meta': {},
    }


def initialize(v):
    ip = v['NPM_BIND_IP'] if v['NPM_BIND_IP'] != '0.0.0.0' else '127.0.0.1'
    api = API(f'http://{ip}:{v["NPM_ADMIN_PORT"]}/api')
    print('等待 Nginx Proxy Manager 初始化（最多 4 分钟）……')
    deadline = time.monotonic() + 240
    while True:
        try:
            status = api.call('/')
            break
        except (RuntimeError, URLError, OSError):
            if time.monotonic() >= deadline:
                raise RuntimeError('NPM 启动超时；配置已保留，请检查容器状态后重跑') from None
            time.sleep(2)
    if status.get('setup') is False:
        api.call('/users', {
            'name': 'OpsKit 管理员', 'nickname': 'OpsKit', 'email': v['NPM_ADMIN_EMAIL'],
            'auth': {'type': 'password', 'secret': v['NPM_ADMIN_PASSWORD']},
        })
        print('已创建 NPM 初始管理员。')
    token = api.call('/tokens', {'identity': v['NPM_ADMIN_EMAIL'], 'secret': v['NPM_ADMIN_PASSWORD']})
    api.token = token['token']
    lists = api.call('/nginx/access-lists')
    ids = []
    for name, user_key, password_key in (
            ('OpsKit Operators', 'OPS_ADMIN_USER', 'OPS_ADMIN_PASSWORD'),
            ('OpsKit Collectors', 'LOG_AGENT_USER', 'LOG_AGENT_PASSWORD')):
        existing = next((item for item in lists if item['name'] == name), None)
        if existing is None:
            existing = api.call('/nginx/access-lists', {
                'name': name, 'satisfy_any': False, 'pass_auth': False,
                'items': [{'username': v[user_key], 'password': v[password_key]}], 'clients': [],
            })
        ids.append(existing['id'])
    hosts = api.call('/nginx/proxy-hosts')
    entry = next((item for item in hosts if v['OPS_PUBLIC_HOST'] in item['domain_names']), None)
    if entry is None:
        entry = api.call('/nginx/proxy-hosts', proxy_payload(v, *ids))
        print('已创建代理主机及子路径规则，可在 WebUI 编辑。')
    else:
        reserved = {domain for host in hosts if host['id'] != entry['id'] for domain in host['domain_names']}
        upgrade_workbench(api, entry, reserved)
        print('已保留 WebUI 中的现有代理主机和访问列表。')
    # 创建响应中的 meta 可能尚未反映配置生成结果，重新读取实际状态。
    entry = api.call('/nginx/proxy-hosts/' + str(entry['id']))
    if not entry.get('meta', {}).get('nginx_online'):
        raise RuntimeError('NPM 代理配置未上线；请在 WebUI 查看该主机的配置错误')


if __name__ == '__main__':
    try:
        initialize(load_env(ROOT / '.env'))
    except (KeyError, RuntimeError, OSError, ValueError) as exc:
        print(f'代理初始化未完成：{exc}', file=sys.stderr)
        sys.exit(1)

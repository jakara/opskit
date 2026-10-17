#!/usr/bin/env python3
"""统一应用工作台，支持宿主机直接发现或读取发现快照。"""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import shutil
import tempfile
import threading
from urllib.parse import urlsplit
import uuid

ASSETS = Path(__file__).resolve().parent
sys.path.insert(0, str(ASSETS))
from discovery import Discovery, SnapshotDiscovery

BUILTIN_IDS = ('spug', 'dbx', 'logs', 'npm')


def entry_values(data):
    values = {}
    for key, limit in (('name', 100), ('group', 100), ('url', 2048), ('description', 300)):
        value = data.get(key, '')
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError('入口字段过长或包含无效字符')
        values[key] = value.strip()
    if not values['name'] or not values['group']:
        raise ValueError('请填写入口名称和分组')
    url = values['url']
    parsed = urlsplit(url)
    relative = url.startswith('/') and not url.startswith('//')
    absolute = parsed.scheme in ('http', 'https') and bool(parsed.hostname) and not parsed.username and not parsed.password
    if not (relative or absolute) or '\\' in url or any(c.isspace() for c in url):
        raise ValueError('地址须为 http://、https:// 或以 / 开头的站内路径，不能包含登录凭据')
    return values


def default_entries():
    entries = [
        {'id': 'spug', 'name': '主机与发布 · Spug', 'group': '运维工具',
         'url': 'http://127.0.0.1:' + os.environ.get('SPUG_HTTP_PORT', '8081') + '/',
         'description': '主机纳管、SSH、部署发布与定时任务；独立端口访问'},
        {'id': 'dbx', 'name': '数据库 · DBX', 'group': '运维工具', 'url': '/dbx/', 'description': '连接业务数据库，查询与维护数据'},
        {'id': 'logs', 'name': '集中日志 · VictoriaLogs', 'group': '运维工具', 'url': '/logs/select/vmui/', 'description': '跨主机日志查询与故障排查'},
        {'id': 'npm', 'name': '代理配置 · NPM', 'group': '运维工具',
         'url': 'http://127.0.0.1:' + os.environ.get('NPM_ADMIN_PORT', '8181'),
         'description': '默认通过本机或 SSH 隧道访问；可编辑为已有的管理地址'},
    ]
    components = os.environ.get('OPSKIT_COMPONENTS', ','.join(BUILTIN_IDS)).split(',')
    return [entry for entry in entries if entry['id'] in components]


class Catalog:
    def __init__(self, directory, seed_tools=False):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / 'entries.json'
        self.lock = threading.RLock()
        if not self.path.exists():
            self._save(default_entries() if seed_tools else [])
        entries = self.read()
        original = json.dumps(entries, ensure_ascii=False)
        for entry in entries:
            if entry['id'] == 'spug' and entry['url'] == '/':
                entry['url'] = 'http://127.0.0.1:' + os.environ.get('SPUG_HTTP_PORT', '8081') + '/'
        if seed_tools:
            ids = {entry['id'] for entry in entries}
            entries.extend(entry for entry in default_entries() if entry['id'] not in ids)
        if json.dumps(entries, ensure_ascii=False) != original:
            self._save(entries)

    def read(self):
        with self.lock:
            if self.path.is_symlink():
                raise ValueError('工作台配置不能是软链接')
            entries = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(entries, list):
                raise ValueError('工作台入口配置格式错误')
            for entry in entries:
                entry_values(entry)
                if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', entry.get('id', '')):
                    raise ValueError('工作台入口标识错误')
            return entries

    def _save(self, entries):
        if self.path.is_symlink():
            raise ValueError('工作台配置不能是软链接')
        if self.path.exists():
            folder = self.directory / 'backups'
            folder.mkdir(mode=0o700, exist_ok=True)
            backup = folder / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
            shutil.copy2(self.path, backup)
            backup.chmod(0o600)
            if hashlib.sha256(self.path.read_bytes()).digest() != hashlib.sha256(backup.read_bytes()).digest():
                raise OSError('工作台配置备份校验失败')
        fd, temp = tempfile.mkstemp(dir=self.directory, prefix='.entries-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(entries, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def change(self, method, entry_id, data):
        with self.lock:
            entries = self.read()
            if method == 'POST':
                entries.append(dict(entry_values(data), id=uuid.uuid4().hex))
            else:
                index = next((i for i, item in enumerate(entries) if item['id'] == entry_id), None)
                if index is None:
                    raise LookupError('入口不存在，请刷新工作台')
                if method == 'DELETE':
                    if entry_id in BUILTIN_IDS:
                        raise ValueError('内置工具固定置顶，可编辑访问地址')
                    entries.pop(index)
                else:
                    entries[index] = dict(entry_values(data), id=entry_id)
            self._save(entries)
            return entries




class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, directory, discovery_file=None, seed_tools=False):
        self.local_only = address[0] == '127.0.0.1'
        self.catalog = Catalog(directory, seed_tools)
        self.discovery = SnapshotDiscovery(discovery_file) if discovery_file else Discovery()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def allowed(self, mutation=False):
        host = self.headers.get('Host', '')
        if self.server.local_only and urlsplit('http://' + host).hostname not in ('localhost', '127.0.0.1', '::1'):
            self.send_json(403, {'error': '当前工作台仅供本机访问'})
            return False
        origin = self.headers.get('Origin')
        if mutation and ((origin and urlsplit(origin).netloc != host) or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
            self.send_json(403, {'error': '拒绝跨站修改工作台配置'})
            return False
        return True

    def send_content(self, code, data, content_type):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, value):
        self.send_content(code, json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_GET(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path in ('/hub', '/hub/'):
            self.send_response(302)
            self.send_header('Location', '/')
            self.end_headers()
        elif path == '/healthz':
            self.send_json(200, {'ok': True})
        elif path == '/hub/api/state':
            try:
                self.send_json(200, dict(self.server.discovery.read(), api_version=1,
                                         entries=[dict(entry, builtin=entry['id'] in BUILTIN_IDS) for entry in self.server.catalog.read()],
                                         updated_at=datetime.now(timezone.utc).isoformat()))
            except (OSError, ValueError):
                self.send_json(500, {'error': '无法读取工作台配置，请检查数据文件及备份'})
        else:
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/hub/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/hub/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path not in files:
                self.send_json(404, {'error': '页面不存在'})
                return
            name, content_type = files[path]
            self.send_content(200, (ASSETS / name).read_bytes(), content_type)

    def mutate(self):
        if not self.allowed(mutation=True):
            return
        path = urlsplit(self.path).path
        match = re.fullmatch(r'/hub/api/entries(?:/([a-zA-Z0-9_-]{1,64}))?', path)
        if not match or (self.command == 'POST') == bool(match.group(1)):
            self.send_json(404, {'error': '入口操作不存在'})
            return
        try:
            data = {}
            if self.command != 'DELETE':
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('请使用 JSON 提交入口配置')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('入口配置须为对象')
            entries = self.server.catalog.change(self.command, match.group(1), data)
            self.send_json(200, {'entries': entries})
        except LookupError as exc:
            self.send_json(404, {'error': str(exc)})
        except (ValueError, UnicodeError) as exc:
            self.send_json(400, {'error': str(exc)})
        except OSError:
            self.send_json(500, {'error': '配置保存失败，原配置及备份已保留'})

    do_POST = mutate
    do_PUT = mutate
    do_DELETE = mutate


def serve(port, directory, bind='127.0.0.1', discovery_file=None, seed_tools=False):
    with WorkbenchServer((bind, port), directory, discovery_file, seed_tools) as server:
        print(f'应用工作台：http://{bind}:{server.server_port}/\n按 Ctrl+C 停止工作台。', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='OpsKit 统一应用工作台')
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=19080)
    parser.add_argument('--data', type=Path, default=ASSETS.parent / '.workbench/hub')
    parser.add_argument('--discovery-file', type=Path, help='宿主机只读发现快照；不填写时直接发现本机')
    parser.add_argument('--seed-tools', action='store_true', help='首次创建目录时登记 OpsKit 工具入口')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口须为 1～65535')
    serve(args.port, args.data, args.bind, args.discovery_file, args.seed_tools)

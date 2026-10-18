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
import stat
import tempfile
import threading
from urllib.parse import parse_qs, quote, urlsplit
import uuid
import zipfile

ASSETS = Path(__file__).resolve().parent
sys.path.insert(0, str(ASSETS))
from discovery import Discovery, SnapshotDiscovery
from auth import Sessions, component_target, destination, navigation, required
from component_auth import ComponentAuth, ComponentAuthError

BUILTIN_IDS = ('spug', 'dbx', 'logs', 'npm')
DOCUMENT_TYPES = {
    '.md': ('markdown', 'text/plain; charset=utf-8'),
    '.markdown': ('markdown', 'text/plain; charset=utf-8'),
    '.txt': ('text', 'text/plain; charset=utf-8'),
    '.pdf': ('pdf', 'application/pdf'),
    '.docx': ('word', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    '.doc': ('legacy-word', 'application/msword'),
    '.png': ('image', 'image/png'), '.jpg': ('image', 'image/jpeg'),
    '.jpeg': ('image', 'image/jpeg'), '.gif': ('image', 'image/gif'),
    '.webp': ('image', 'image/webp'), '.svg': ('image', 'image/svg+xml'),
}
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024
MAX_DOCUMENTS = 1000


class Documents:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()

    def parts(self, value):
        if not isinstance(value, str) or not value or len(value) > 1024 or '\\' in value or any(ord(c) < 32 for c in value):
            raise ValueError('文档路径无效')
        parts = value.split('/')
        if len(parts) > 12 or any(not part or part.startswith('.') for part in parts):
            raise ValueError('文档路径无效')
        if Path(parts[-1]).suffix.lower() not in DOCUMENT_TYPES:
            raise ValueError('不支持此文档格式')
        return parts

    def read(self, value):
        parts = self.parts(value)
        if self.directory.is_symlink():
            raise ValueError('文档目录不能是软链接')
        descriptors = []
        try:
            if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
                # Open each component without following links, including concurrent replacements.
                directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(directory)
                for part in parts[:-1]:
                    directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    descriptors.append(directory)
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            else:
                path = self.directory
                for part in parts:
                    path = path / part
                    if path.is_symlink():
                        raise ValueError('文档不能是软链接')
                if not path.resolve().is_relative_to(self.directory.resolve()):
                    raise ValueError('文档路径越界')
                descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_SIZE:
                raise ValueError('文档须为不超过 20 MiB 的普通文件')
            with os.fdopen(os.dup(descriptor), 'rb') as stream:
                content = stream.read(MAX_DOCUMENT_SIZE + 1)
            if len(content) > MAX_DOCUMENT_SIZE:
                raise ValueError('文档超过 20 MiB')
            if Path(value).suffix.lower() == '.docx':
                self.validate_word(content)
            return content
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def validate_word(content):
        import io
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                if (len(entries) > 2000 or len(names) != len(set(names)) or
                        sum(entry.file_size for entry in entries) > 100 * 1024 * 1024 or
                        '[Content_Types].xml' not in names or 'word/document.xml' not in names):
                    raise ValueError('Word 文件结构异常或展开后超过 100 MiB')
                for entry in entries:
                    if (entry.flag_bits & 1 or entry.filename.startswith('/') or '\\' in entry.filename or
                            '..' in entry.filename.split('/') or
                            entry.file_size > 1000 * max(entry.compress_size, 1)):
                        raise ValueError('Word 文件包含不安全或异常压缩的内容')
        except (zipfile.BadZipFile, OSError) as exc:
            raise ValueError('Word 文件损坏，请重新保存为 .docx') from exc

    def listing(self):
        if not self.directory.exists():
            return []
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError('文档目录无效')
        documents = []
        for parent, directories, names in os.walk(self.directory, followlinks=False):
            directories[:] = sorted(name for name in directories if not name.startswith('.') and not (Path(parent) / name).is_symlink())
            for name in sorted(names):
                path = Path(parent) / name
                relative = path.relative_to(self.directory).as_posix()
                try:
                    self.parts(relative)
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_SIZE:
                        continue
                except (OSError, ValueError):
                    continue
                group = {'delivery': '交付手册', 'project': '项目文档', 'builtin': '内置说明'}.get(relative.split('/')[0], '本地文档')
                title = path.stem
                if path.suffix.lower() in ('.md', '.markdown') and info.st_size <= 512 * 1024:
                    try:
                        text = self.read(relative).decode('utf-8-sig')[:32768]
                        heading = re.search(r'^ {0,3}#\s+(.+?)\s*#*\s*$', text, re.MULTILINE)
                        if heading:
                            title = heading.group(1)[:160]
                    except (OSError, UnicodeError, ValueError):
                        pass
                documents.append({'path': relative, 'title': title, 'group': group,
                                  'kind': DOCUMENT_TYPES[path.suffix.lower()][0], 'size': info.st_size})
                if len(documents) >= MAX_DOCUMENTS:
                    return documents
        order = {'交付手册': 0, '项目文档': 1, '内置说明': 2, '本地文档': 3}
        return sorted(documents, key=lambda item: (order[item['group']], item['path'].casefold()))


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
         'url': '/spug/',
         'description': '主机纳管、应用配置与内置服务健康巡检'},
        {'id': 'dbx', 'name': '数据库 · DBX', 'group': '运维工具', 'url': '/dbx/', 'description': '连接业务数据库，查询与维护数据'},
        {'id': 'logs', 'name': '集中日志 · VictoriaLogs', 'group': '运维工具', 'url': '/logs/select/vmui/', 'description': '跨主机日志查询与故障排查'},
        {'id': 'npm', 'name': '代理配置 · NPM', 'group': '运维工具',
         'url': '/npm/',
         'description': '统一入口的反向代理与访问配置'},
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
            if entry['id'] in ('npm', 'spug'):
                name = entry['id']
                ports = ({os.environ.get('SPUG_HTTP_PORT', '8081'), '8081', '8082'} if name == 'spug'
                         else {os.environ.get('NPM_ADMIN_PORT', '8181'), '8181'})
                old = {f'http://{host}:{port}' for host in ('localhost', '127.0.0.1') for port in ports}
                if entry['url'].rstrip('/') in old or (name == 'spug' and entry['url'] == '/'):
                    entry['url'] = '/' + name + '/'
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

    def __init__(self, address, directory, discovery_file=None, seed_tools=False, documents_dir=None, auth_file=None, component_auth_file=None):
        self.local_only = address[0] == '127.0.0.1'
        self.catalog = Catalog(directory, seed_tools)
        self.discovery = SnapshotDiscovery(discovery_file) if discovery_file else Discovery()
        self.documents = Documents(documents_dir if documents_dir is not None else ASSETS.parent / 'documents')
        self.sessions = Sessions(auth_file, directory) if auth_file else None
        if component_auth_file and not self.sessions:
            raise ValueError('组件代登录必须启用入口认证')
        self.components = ComponentAuth(component_auth_file) if component_auth_file else None
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
        path = urlsplit(self.path).path
        if self.server.sessions and path != '/healthz' and not path.startswith('/auth/') and not self.server.sessions.read(self.headers.get('Cookie')):
            required(self)
            return False
        return True

    def send_content(self, code, data, content_type, document=False, download=None, headers=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for name, value in (headers or {}).items():
            for item in value if isinstance(value, list) else [value]:
                self.send_header(name, item)
        policy = ("default-src 'none'; img-src 'self' data:; style-src 'none'; script-src 'none'; sandbox; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
                  if document else "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; worker-src 'self'; font-src 'self'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header('Content-Security-Policy', policy)
        if download:
            self.send_header('Content-Disposition', "attachment; filename=\"document" + Path(download).suffix.lower() + "\"; filename*=UTF-8''" + quote(Path(download).name, safe=''))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def send_json(self, code, value, headers=None):
        self.send_content(code, json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8', headers=headers)

    def authentication(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if not path.startswith('/auth/'):
            return False
        sessions = self.server.sessions
        cookie_header = self.headers.get('Cookie')
        if path == '/auth/required':
            target = destination(self.headers.get('X-OpsKit-Original-URI', self.path))
            component = urlsplit(target).path.split('/')[1]
            if (sessions and self.server.components and sessions.read(cookie_header) and
                    component in self.server.components.credentials and navigation(self, target)):
                url = '/auth/open?component=' + component + '&next=' + quote(component_target(component, target), safe='')
                self.send_content(302, b'', 'text/plain', headers={'Location': url})
            else:
                required(self, target)
        elif path in ('/auth/verify', '/auth/api/session') and self.command == 'GET':
            state = sessions.read(cookie_header) if sessions else {'authenticated': True, 'user': None}
            if path.endswith('/verify'):
                target = destination(self.headers.get('X-OpsKit-Original-URI', '/'))
                component = urlsplit(target).path.split('/')[1]
                if (state and self.server.components and component in self.server.components.credentials and
                        navigation(self, target, self.headers.get('X-OpsKit-Original-Method'))):
                    state = state if sessions.consume(cookie_header, component) else None
                self.send_content(204 if state else 401, b'', 'text/plain')
            else:
                self.send_json(200 if state else 401, state or {'authenticated': False})
        elif path in ('/auth/api/login', '/auth/api/logout', '/auth/api/component-login', '/auth/api/component-native') and self.command == 'POST':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('请使用登录表单提交')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('登录信息格式无效')
                if not sessions:
                    if path.startswith('/auth/api/component-'):
                        raise PermissionError('请先登录运维入口')
                    self.send_json(200, {'redirect': '/'})
                    return True
                if path.startswith('/auth/api/component-'):
                    if not sessions.read(cookie_header):
                        raise PermissionError('登录已过期，请重新登录')
                    component = data.get('component')
                    if not isinstance(component, str) or not self.server.components or component not in self.server.components.credentials:
                        raise ValueError('此组件未配置自动登录')
                    result = self.server.components.login(component) if path.endswith('component-login') else {'storage': {}, 'set_cookie': None}
                    if not sessions.read(cookie_header):
                        raise PermissionError('登录已过期，请重新登录')
                    if not sessions.grant(cookie_header, component):
                        raise PermissionError('登录已过期，请重新登录')
                    headers = {}
                    if result['set_cookie']:
                        headers['Set-Cookie'] = result['set_cookie'] + ('; Secure' if sessions.config['secure'] else '')
                    self.send_json(200, {'storage': result['storage'], 'redirect': component_target(component, data.get('next'))}, headers=headers)
                    return True
                elif path.endswith('/logout'):
                    cookie = sessions.logout(self.headers.get('Cookie'))
                    cookie = [cookie, 'dbx_session=; Path=/dbx; Max-Age=0; HttpOnly; SameSite=Lax' + ('; Secure' if sessions.config['secure'] else '')]
                    target = '/auth/login'
                else:
                    peer = self.headers.get('X-OpsKit-Client-IP', self.client_address[0])
                    cookie = sessions.login(data.get('username'), data.get('password'), peer)
                    target = destination(data.get('next', '/'))
                self.send_json(200, {'redirect': target}, headers={'Set-Cookie': cookie})
            except TimeoutError as exc:
                self.send_json(429, {'error': str(exc)}, headers={'Retry-After': '60'})
            except PermissionError as exc:
                self.send_json(401, {'error': str(exc)})
            except ComponentAuthError as exc:
                self.send_json(503, {'error': str(exc)})
            except (ValueError, UnicodeError) as exc:
                self.send_json(400, {'error': str(exc)})
        elif self.command == 'GET' and path in ('/auth/open', '/auth/component.js'):
            if not sessions or not self.server.components or not sessions.read(cookie_header):
                required(self)
            else:
                name = 'component.html' if path == '/auth/open' else 'component.js'
                self.send_content(200, (ASSETS / name).read_bytes(), ('text/html' if name.endswith('.html') else 'text/javascript') + '; charset=utf-8')
        elif self.command == 'GET' and path in ('/auth/login', '/auth/login.js', '/auth/login.css'):
            name = {'/auth/login': 'login.html', '/auth/login.js': 'login.js', '/auth/login.css': 'login.css'}[path]
            content_type = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css'}[name.rsplit('.', 1)[1]]
            self.send_content(200, (ASSETS / name).read_bytes(), content_type + '; charset=utf-8')
        else:
            self.send_json(404, {'error': '登录接口不存在'})
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.authentication():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in ('/hub', '/hub/'):
            self.send_response(302)
            self.send_header('Location', '/')
            self.end_headers()
        elif path == '/healthz':
            self.send_json(200, {'ok': True})
        elif path == '/hub/api/documents':
            try:
                self.send_json(200, {'documents': self.server.documents.listing()})
            except (OSError, ValueError):
                self.send_json(500, {'error': '无法读取文档目录，请检查目录权限和配置'})
        elif path == '/hub/api/documents/content':
            try:
                values = parse_qs(parsed.query, keep_blank_values=True)
                if len(values.get('path', [])) != 1:
                    raise ValueError('请指定一个文档路径')
                name = values['path'][0]
                content = self.server.documents.read(name)
                content_type = DOCUMENT_TYPES[Path(name).suffix.lower()][1]
                self.send_content(200, content, content_type, document=True,
                                  download=name if values.get('download') == ['1'] else None)
            except (OSError, KeyError):
                self.send_json(404, {'error': '文档不存在或无法读取'})
            except ValueError as exc:
                self.send_json(400, {'error': str(exc)})
        elif path == '/hub/api/state':
            try:
                self.send_json(200, dict(self.server.discovery.read(), api_version=1,
                                         entries=[dict(entry, builtin=entry['id'] in BUILTIN_IDS) for entry in self.server.catalog.read()],
                                         updated_at=datetime.now(timezone.utc).isoformat()))
            except (OSError, ValueError):
                self.send_json(500, {'error': '无法读取工作台配置，请检查数据文件及备份'})
        else:
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/hub/docs': ('index.html', 'text/html; charset=utf-8'),
                     '/hub/docs/': ('index.html', 'text/html; charset=utf-8'),
                     '/hub/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/hub/auth.js': ('auth.js', 'text/javascript; charset=utf-8'),
                     '/hub/documents.js': ('documents.js', 'text/javascript; charset=utf-8'),
                     '/hub/style.css': ('style.css', 'text/css; charset=utf-8')}
            for name in ('marked-18.1.1.umd.js', 'purify-3.4.17.min.js', 'mammoth-1.13.0.browser.min.js'):
                files['/hub/vendor/' + name] = ('vendor/' + name, 'text/javascript; charset=utf-8')
            pdf_prefix = '/hub/vendor/pdfjs-6.4.299/'
            if path.startswith(pdf_prefix):
                relative = path[len(pdf_prefix):]
                if re.fullmatch(r'(?:build/pdf(?:\.worker)?\.mjs|(?:cmaps|standard_fonts|wasm)/[A-Za-z0-9_.-]+\.(?:bcmap|pfb|ttf|wasm|mjs|js))', relative):
                    name = 'vendor/pdfjs-6.4.299/' + relative
                    candidate = ASSETS / name
                    if candidate.is_file() and not any(item.is_symlink() for item in (candidate, candidate.parent, candidate.parent.parent)):
                        suffix = candidate.suffix
                        content_type = {'.mjs': 'text/javascript; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.wasm': 'application/wasm', '.ttf': 'font/ttf'}.get(suffix, 'application/octet-stream')
                        files[path] = (name, content_type)
            if path not in files:
                self.send_json(404, {'error': '页面不存在'})
                return
            name, content_type = files[path]
            self.send_content(200, (ASSETS / name).read_bytes(), content_type)

    def do_HEAD(self):
        self.do_GET()

    def mutate(self):
        if not self.allowed(mutation=True):
            return
        if self.authentication():
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


def serve(port, directory, bind='127.0.0.1', discovery_file=None, seed_tools=False, documents_dir=None, auth_file=None, component_auth_file=None):
    with WorkbenchServer((bind, port), directory, discovery_file, seed_tools, documents_dir, auth_file, component_auth_file) as server:
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
    parser.add_argument('--documents-dir', type=Path, help='只读文档目录；默认 management/documents')
    parser.add_argument('--auth-file', type=Path, default=os.environ.get('OPSKIT_AUTH_FILE'), help='安装器生成的入口密码散列配置')
    parser.add_argument('--component-auth-file', type=Path, default=os.environ.get('OPSKIT_COMPONENT_AUTH_FILE'), help='安装器生成的组件代登录配置')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口须为 1～65535')
    if args.bind not in ('127.0.0.1', '::1') and not args.auth_file:
        parser.error('非回环监听必须提供 --auth-file')
    serve(args.port, args.data, args.bind, args.discovery_file, args.seed_tools, args.documents_dir, args.auth_file, args.component_auth_file)

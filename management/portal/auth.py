"""OpsKit 入口会话：密码散列、可撤销的服务端会话，不向浏览器下发组件密码。"""
from contextlib import closing
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

COOKIE = 'opskit_session'
LIFETIME = 12 * 3600
ITERATIONS = 600000


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def configuration(username, password, secure, previous=None):
    if previous and previous.get('username') == username and hmac.compare_digest(
            password_hash(password, previous['salt']), previous['password_hash']):
        return dict(previous, secure=secure)
    salt = secrets.token_hex(16)
    return {'username': username, 'salt': salt, 'password_hash': password_hash(password, salt), 'secure': secure}


def destination(value):
    if not isinstance(value, str) or len(value) > 4096:
        return '/'
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    if (not value.startswith('/') or decoded.startswith('//') or '\\' in decoded or
            any(ord(c) < 32 or ord(c) == 127 for c in decoded) or urlsplit(value).netloc):
        return '/'
    if urlsplit(value).path == '/auth/open':
        args = parse_qs(urlsplit(value).query)
        component = args.get('component', [''])[0]
        if component in ('spug', 'npm', 'dbx'):
            target = component_target(component, args.get('next', [''])[0])
            return '/auth/open?component=' + component + '&next=' + quote(target, safe='')
    if decoded.split('?', 1)[0].startswith(('/auth/', '/_opskit_auth')):
        return '/'
    return value


def component_target(component, value):
    default = {'spug': '/spug/home', 'npm': '/npm/', 'dbx': '/dbx/'}[component]
    if not isinstance(value, str) or not value.startswith('/' + component + '/'):
        return default
    # 不让 next 再进入桥接入口，避免递归；组件外路径也不允许。
    target = destination(value)
    return target if target.startswith('/' + component + '/') and urlsplit(target).path not in ('/spug/', '/spug/login', '/npm/login') else default


def navigation(handler, target, method=None):
    api = re.match(r'^/(?:hub/api|npm/api|spug/api|dbx/api|logs/(?:insert|select/logsql))(?:/|$)', urlsplit(target).path)
    return ((method or handler.command) == 'GET' and not api and
            handler.headers.get('Upgrade', '').lower() != 'websocket' and
            (handler.headers.get('Sec-Fetch-Dest') in ('document', 'iframe') or 'text/html' in handler.headers.get('Accept', '')))


class Sessions:
    def __init__(self, configuration_file, directory):
        path = Path(configuration_file)
        if path.is_symlink():
            raise ValueError('登录配置不能是软链接')
        self.config = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(self.config, dict):
            raise ValueError('登录配置无效，请重跑安装器')
        self.cookie_name = self.config.get('cookie_name', COOKIE)
        if (not isinstance(self.config, dict) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,31}', self.config.get('username', '')) or
                not re.fullmatch(r'[a-f0-9]{32}', self.config.get('salt', '')) or
                not re.fullmatch(r'[a-f0-9]{64}', self.config.get('password_hash', '')) or
                type(self.config.get('secure')) is not bool or
                not isinstance(self.cookie_name, str) or not re.fullmatch(r'opskit_session(?:_[a-f0-9]{12})?', self.cookie_name)):
            raise ValueError('登录配置无效，请重跑安装器')
        self.fingerprint = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        self.path = Path(directory) / 'sessions.sqlite3'
        if self.path.is_symlink():
            raise ValueError('会话文件不能是软链接')
        self.path.touch(mode=0o600, exist_ok=True)
        self.lock = threading.RLock()
        self.password_slots = threading.BoundedSemaphore(2)
        self.attempts = {}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL, fingerprint TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS grants (session TEXT, component TEXT, uses INTEGER, expires REAL, PRIMARY KEY(session, component))')
            db.execute('DELETE FROM sessions WHERE expires < ? OR fingerprint != ?', (time.time(), self.fingerprint))
            db.execute('DELETE FROM grants WHERE expires < ?', (time.time(),))

    def token(self, header):
        cookie = SimpleCookie()
        try:
            cookie.load(header or '')
        except Exception:
            return None
        value = cookie.get(self.cookie_name)
        if value and re.fullmatch(r'[A-Za-z0-9_-]{43}', value.value):
            return hashlib.sha256(value.value.encode()).hexdigest()
        return None

    def read(self, header):
        digest = self.token(header)
        if not digest:
            return None
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT expires FROM sessions WHERE digest=? AND fingerprint=? AND expires>?',
                             (digest, self.fingerprint, time.time())).fetchone()
        return {'authenticated': True, 'user': self.config['username'], 'expires_at': row[0]} if row else None

    def login(self, username, password, peer):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 128 or not 1 <= len(password) <= 128:
            raise ValueError('请填写有效的用户名和密码')
        # 密码验证限并发，但不占会话读锁；同代理其他人的错密不会锁住正确登录。
        if not self.password_slots.acquire(timeout=0.5):
            raise TimeoutError('登录请求较多，请稍后再试')
        try:
            correct = hmac.compare_digest(password_hash(password, self.config['salt']), self.config['password_hash'])
        finally:
            self.password_slots.release()
        now = time.time()
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if now - v[1] < 60}
            failures = self.attempts.get(peer, (0, now))
            if not correct or not hmac.compare_digest(username.encode(), self.config['username'].encode()):
                if failures[0] >= 5 or len(self.attempts) >= 1024:
                    raise TimeoutError('尝试次数过多，请稍后再试')
                self.attempts[peer] = (failures[0] + 1, failures[1])
                raise PermissionError('用户名或密码不正确')
            self.attempts.pop(peer, None)
            token = secrets.token_urlsafe(32)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('DELETE FROM sessions WHERE expires < ?', (now,))
                db.execute('INSERT INTO sessions VALUES (?, ?, ?)',
                           (hashlib.sha256(token.encode()).hexdigest(), now + LIFETIME, self.fingerprint))
            return self.cookie(token)

    def cookie(self, token=''):
        return f'{self.cookie_name}={token}; Path=/; Max-Age={LIFETIME if token else 0}; HttpOnly; SameSite=Lax' + ('; Secure' if self.config['secure'] else '')

    def grant(self, header, component):
        digest = self.token(header)
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            if not db.execute('SELECT 1 FROM sessions WHERE digest=? AND fingerprint=? AND expires>?',
                              (digest, self.fingerprint, time.time())).fetchone():
                return False
            now = time.time()
            db.execute('INSERT INTO grants VALUES (?, ?, 1, ?) ON CONFLICT(session, component) DO UPDATE SET uses=CASE WHEN grants.expires>? THEN MIN(uses+1,8) ELSE 1 END, expires=excluded.expires',
                       (digest, component, now + 30, now))
            return True

    def consume(self, header, component):
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute('UPDATE grants SET uses=uses-1 WHERE session=? AND component=? AND uses>0 AND expires>?',
                                (self.token(header), component, time.time()))
            return result.rowcount == 1

    def logout(self, header):
        digest = self.token(header)
        if digest:
            with self.lock, closing(sqlite3.connect(self.path)) as db, db:
                db.execute('DELETE FROM sessions WHERE digest=?', (digest,))
                db.execute('DELETE FROM grants WHERE session=?', (digest,))
        return self.cookie()


def required(handler, target=None):
    target = destination(target or handler.path)
    if navigation(handler, target):
        handler.send_content(302, b'', 'text/plain', headers={'Location': '/auth/login?next=' + quote(target, safe='')})
    else:
        handler.send_json(401, {'error': '登录已过期，请重新登录', 'login_url': '/auth/login?next=' + quote(target, safe='')})

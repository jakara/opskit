"""临时本机 HTTP 服务的站内认证边界；不使用本机真实账号或组件。"""
from contextlib import closing
import hashlib
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from test_installer import ROOT, module

portal = module('portal_auth_tests', ROOT / 'management/portal/server.py')
auth = __import__('auth')


class PortalAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'Fixture-password-ONLY-7219'
        cls.configuration = auth.configuration('admin', cls.password, False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / 'auth.json'
        self.config.write_text(json.dumps(self.configuration))
        self.data = self.root / 'data'
        self.data.mkdir(mode=0o700)
        self.server = portal.WorkbenchServer(('127.0.0.1', 0), self.data, auth_file=self.config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method, path, data=None, cookie=None, headers=None):
        headers = {'Content-Type': 'application/json', **(headers or {})}
        if cookie:
            headers['Cookie'] = cookie
        body = json.dumps(data) if data is not None else None
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self, **changes):
        values = dict({'username': 'admin', 'password': self.password, 'next': '/'}, **changes)
        status, headers, body = self.request('POST', '/auth/api/login', values)
        self.assertEqual(status, 200, body)
        return headers['Set-Cookie'], json.loads(body)

    def test_login_assets_are_public_and_expired_navigation_never_uses_basic_auth(self):
        for path in ('/auth/login?next=%2Fspug%2F', '/auth/login.js', '/auth/login.css'):
            status, headers, body = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertNotIn('WWW-Authenticate', headers)
            self.assertTrue(body)
        status, headers, body = self.request('GET', '/hub/api/state')
        self.assertEqual(status, 401)
        self.assertNotIn('WWW-Authenticate', headers)
        self.assertIn('/auth/login', json.loads(body)['login_url'])
        target = '/hub/docs?path=guide.md'
        status, headers, _ = self.request('GET', target, headers={'Accept': 'text/html'})
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/auth/login?next=' + quote(target, safe=''))
        self.assertNotIn('WWW-Authenticate', headers)

    def test_wrong_password_and_collector_account_never_receive_a_session(self):
        for username, password in (('admin', 'wrong-password'), ('collector', self.password)):
            status, headers, body = self.request('POST', '/auth/api/login', {'username': username, 'password': password})
            self.assertEqual(status, 401)
            self.assertNotIn('Set-Cookie', headers)
            self.assertNotIn('WWW-Authenticate', headers)
            self.assertNotIn(password, body.decode())
        self.assertEqual(self.request('GET', '/auth/api/session')[0], 401)

    def test_login_cookie_session_and_logout_follow_the_same_server_session(self):
        target = '/hub/docs?path=交付说明.md#安装'
        cookie, result = self.login(next=target)
        self.assertEqual(result['redirect'], target)
        for attribute in ('HttpOnly', 'SameSite=Lax', 'Path=/', 'Max-Age=43200'):
            self.assertIn(attribute, cookie)
        self.assertNotIn('; Secure', cookie)
        status, _, body = self.request('GET', '/auth/api/session', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['user'], 'admin')
        self.assertTrue(json.loads(body)['authenticated'])
        self.assertEqual(self.request('GET', '/', cookie=cookie)[0], 200)
        status, headers, body = self.request('POST', '/auth/api/logout', {}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['redirect'], '/auth/login')
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.request('GET', '/auth/api/session', cookie=cookie)[0], 401)

    def test_only_digest_is_persisted_and_sessions_survive_service_restart(self):
        cookie, _ = self.login()
        parsed = SimpleCookie()
        parsed.load(cookie)
        token = parsed[auth.COOKIE].value
        with closing(sqlite3.connect(self.server.sessions.path)) as database:
            digest, expires, fingerprint = database.execute('SELECT digest, expires, fingerprint FROM sessions').fetchone()
        self.assertEqual(digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(digest, token)
        self.assertNotIn(token.encode(), self.server.sessions.path.read_bytes())
        self.assertNotIn(self.password, self.config.read_text())
        self.assertEqual(self.server.sessions.path.stat().st_mode & 0o777, 0o600)
        self.server.sessions = auth.Sessions(self.config, self.data)
        self.assertEqual(self.request('GET', '/auth/api/session', cookie=cookie)[0], 200)

    def test_expired_sessions_and_changed_login_configuration_are_revoked(self):
        cookie, _ = self.login()
        with closing(sqlite3.connect(self.server.sessions.path)) as database, database:
            database.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.request('GET', '/auth/api/session', cookie=cookie)[0], 401)
        cookie, _ = self.login()
        replacement = auth.configuration('admin', 'Changed-fixture-password-9811', False)
        self.config.write_text(json.dumps(replacement))
        self.server.sessions = auth.Sessions(self.config, self.data)
        self.assertEqual(self.request('GET', '/auth/api/session', cookie=cookie)[0], 401)

    def test_cross_site_and_non_json_requests_cannot_create_or_delete_sessions(self):
        cookie, _ = self.login()
        for path in ('/auth/api/login', '/auth/api/logout'):
            for headers in ({'Origin': 'https://other.example'}, {'Sec-Fetch-Site': 'cross-site'}):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.request('POST', path, {}, cookie, headers)[0], 403)
            self.assertEqual(self.request('POST', path, {}, cookie, {'Content-Type': 'text/plain'})[0], 400)
            self.assertEqual(self.request('POST', path, [], cookie)[0], 400)
        self.assertEqual(self.request('GET', '/auth/api/session', cookie=cookie)[0], 200)

    def test_rate_limit_is_reported_without_authentication_prompt(self):
        for _ in range(5):
            self.assertEqual(self.request('POST', '/auth/api/login', {'username': 'admin', 'password': 'wrong'})[0], 401)
        status, headers, body = self.request('POST', '/auth/api/login', {'username': 'admin', 'password': 'wrong'})
        self.assertEqual(status, 429)
        self.assertEqual(headers['Retry-After'], '60')
        self.assertNotIn('WWW-Authenticate', headers)
        self.assertNotIn(self.password, body.decode())
        self.assertEqual(self.request('POST', '/auth/api/login', {'username': 'admin', 'password': self.password})[0], 200)

    def test_login_redirect_cannot_escape_site_or_reenter_auth_routes(self):
        for target in ('https://other.example', '//other.example', '/%2fother.example', '/%252fother.example',
                       '/\\other.example', '/%5cother.example', '/%0d%0aLocation:other', '/auth/login',
                       '/%61uth/api/logout', '/_opskit_auth', None, {'path': '/'}):
            with self.subTest(target=target):
                self.assertEqual(auth.destination(target), '/')
        cookie, result = self.login(next='//other.example')
        self.assertEqual(result['redirect'], '/')
        self.assertEqual(auth.destination('/spug/?task=1#history'), '/spug/?task=1#history')

    def test_https_configuration_sets_secure_cookie_and_keeps_existing_hash(self):
        secured = auth.configuration('admin', self.password, True, self.configuration)
        self.assertEqual(secured['password_hash'], self.configuration['password_hash'])
        self.assertEqual(secured['salt'], self.configuration['salt'])
        self.config.write_text(json.dumps(secured))
        self.server.sessions = auth.Sessions(self.config, self.data)
        cookie, _ = self.login()
        self.assertIn('; Secure', cookie)


if __name__ == '__main__':
    unittest.main()

"""隔离 HTTP 原生登录契约；不连接真实组件或修改宿主运行配置。"""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from test_installer import ROOT, module

auth = module('component_auth_test', ROOT / 'management/portal/component_auth.py')


class ComponentAuthTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'credentials.json'
        self.credentials = {'npm': {'identity': 'admin@example.internal', 'secret': 'npm-private-secret'},
                            'spug': {'username': 'admin', 'password': 'spug-private-secret'},
                            'dbx': {'password': 'dbx-private-secret'}}
        self.path.write_text(json.dumps(self.credentials))
        self.token = 'eyJhbGciOiJSUzI1NiJ9.eyJpZCI6MX0.signature'
        self.expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.dbx_cookie = 'dbx_session=12345678-1234-4123-8123-123456789abc; Path=/dbx; HttpOnly; SameSite=Lax'
        self.responses = {
            'npm': {'token': self.token, 'expires': self.expires, 'ignored': 'npm-private-secret'},
            'spug': {'error': '', 'data': {'id': 7, 'access_token': 'a' * 32, 'nickname': '内置管理员',
                     'is_supper': True, 'permissions': [], 'has_real_ip': False}},
            'dbx': {'ok': True},
        }
        self.cookies = [self.dbx_cookie]
        self.status = 200
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                component = self.path.strip('/')
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                owner.requests.append((self.path, data, dict(self.headers)))
                self.send_response(owner.status)
                self.send_header('Content-Type', 'application/json')
                if owner.status == 302:
                    self.send_header('Location', '/redirect-must-not-receive-credentials')
                if component == 'dbx':
                    for cookie in owner.cookies:
                        self.send_header('Set-Cookie', cookie)
                self.end_headers()
                result = owner.responses.get(component, {})
                self.wfile.write(result if isinstance(result, bytes) else json.dumps(result).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        urls = {component: f'http://127.0.0.1:{self.server.server_port}/{component}' for component in self.credentials}
        self.patches = patch.multiple(auth, _LOGIN_URLS=urls)
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.dns = patch.object(auth.socket, 'gethostbyname', return_value='172.30.0.2')
        self.resolve = self.dns.start()
        self.addCleanup(self.dns.stop)
        self.bridge = auth.ComponentAuth(self.path)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_native_payloads_and_exact_storage_contracts(self):
        npm = self.bridge.login('npm')
        self.assertEqual(npm, {'storage': {'authentications': json.dumps(
            [{'token': self.token, 'expires': self.expires}], separators=(',', ':'))}, 'set_cookie': None})
        spug = self.bridge.login('spug')
        self.assertEqual(spug, {'storage': {'id': '7', 'token': 'a' * 32, 'nickname': '内置管理员',
                         'is_supper': 'true', 'permissions': '[]', 'login_type': 'default'}, 'set_cookie': None})
        dbx = self.bridge.login('dbx')
        self.assertEqual(dbx, {'storage': {}, 'set_cookie': self.dbx_cookie})
        for component, (path, payload, headers) in zip(('npm', 'spug', 'dbx'), self.requests):
            expected = dict(self.credentials[component])
            if component == 'spug':
                expected['type'] = 'default'
                self.assertEqual(headers['X-Real-Ip'], '172.30.0.2')
                self.assertEqual(headers['X-Forwarded-For'], '172.30.0.2')
            self.assertEqual(payload, expected)
            self.assertEqual(path, '/' + component)
            self.assertNotIn('Authorization', headers)
        self.resolve.assert_called_once_with('proxy-manager')
        for secret in ('npm-private-secret', 'spug-private-secret', 'dbx-private-secret'):
            self.assertNotIn(secret, json.dumps([npm, spug, dbx]))

    def test_spug_normalizes_non_superuser_permissions_to_strings(self):
        self.responses['spug']['data'].update(is_supper=False, permissions=['host.view', 'schedule.exec'])
        storage = self.bridge.login('spug')['storage']
        self.assertEqual(storage['is_supper'], 'false')
        self.assertEqual(json.loads(storage['permissions']), ['host.view', 'schedule.exec'])
        self.assertTrue(all(isinstance(value, str) for value in storage.values()))

    def test_mfa_and_captcha_challenges_never_issue_storage(self):
        for component, result in (
                ('npm', {'requires_2fa': True, 'challenge_token': 'npm-private-secret'}),
                ('npm', dict(self.responses['npm'], requires_2fa=True)),
                ('spug', {'error': '', 'data': {'required_mfa': True}}),
                ('spug', {'error': '', 'data': {'required_captcha': True}})):
            with self.subTest(component=component, result=result):
                self.responses[component] = result
                with self.assertRaisesRegex(auth.ComponentAuthError, '要求额外登录验证') as caught:
                    self.bridge.login(component)
                self.assertNotIn('private-secret', str(caught.exception))

    def test_redirects_http_errors_and_invalid_json_do_not_leak_credentials(self):
        for component in self.credentials:
            for status in (302, 401, 500, 200):
                with self.subTest(component=component, status=status):
                    self.status = status
                    self.responses[component] = b'upstream echoes npm-private-secret spug-private-secret dbx-private-secret'
                    before = len(self.requests)
                    with self.assertRaises(auth.ComponentAuthError) as caught:
                        self.bridge.login(component)
                    self.assertEqual(len(self.requests), before + 1)
                    self.assertNotIn('private-secret', str(caught.exception))

    def test_dbx_filters_unrelated_cookies_and_rejects_unsafe_session_attributes(self):
        self.cookies = ['other=private-secret; Path=/', self.dbx_cookie]
        self.assertEqual(self.bridge.login('dbx')['set_cookie'], self.dbx_cookie)
        for cookies in ([], [self.dbx_cookie.replace('; HttpOnly', '')],
                        [self.dbx_cookie.replace('Path=/dbx', 'Path=/')],
                        [self.dbx_cookie + '; Domain=example.internal'],
                        [self.dbx_cookie.replace('12345678-1234-4123-8123-123456789abc', 'private-secret')],
                        [self.dbx_cookie, self.dbx_cookie]):
            with self.subTest(cookies=cookies):
                self.cookies = cookies
                with self.assertRaises(auth.ComponentAuthError):
                    self.bridge.login('dbx')

    def test_invalid_native_success_shapes_are_rejected(self):
        cases = [('npm', {'token': self.token, 'expires': '2000-01-01T00:00:00Z'}),
                 ('npm', {'token': 'npm-private-secret', 'expires': self.expires}),
                 ('spug', {'error': 'spug-private-secret', 'data': {}}),
                 ('spug', {'error': '', 'data': dict(self.responses['spug']['data'], is_supper='true')}),
                 ('dbx', {'ok': 'true'})]
        for component, result in cases:
            with self.subTest(component=component, result=result):
                self.responses[component] = result
                with self.assertRaises(auth.ComponentAuthError) as caught:
                    self.bridge.login(component)
                self.assertNotIn('private-secret', str(caught.exception))

    def test_unknown_or_missing_components_and_invalid_configuration_fail_before_http(self):
        for component in ('http://untrusted.example/login', '', [], None):
            with self.assertRaisesRegex(auth.ComponentAuthError, '不支持'):
                self.bridge.login(component)
        self.path.write_text('{}')
        with self.assertRaisesRegex(auth.ComponentAuthError, '未配置有效的 Spug'):
            auth.ComponentAuth(self.path).login('spug')
        for value in ('not json private-secret', '[]', '{"npm":{"url":"http://untrusted.example"}}'):
            self.path.write_text(value)
            with self.assertRaises(auth.ComponentAuthError) as caught:
                auth.ComponentAuth(self.path).login('npm')
            self.assertNotIn('private-secret', str(caught.exception))
        self.assertEqual(self.requests, [])

    def test_network_failure_uses_fixed_error_and_has_a_bounded_timeout(self):
        for failure in (URLError('npm-private-secret'), TimeoutError('npm-private-secret'),
                        auth.HTTPException('npm-private-secret')):
            with patch.object(self.bridge.opener, 'open', side_effect=failure) as request:
                with self.assertRaises(auth.ComponentAuthError) as caught:
                    self.bridge.login('npm')
                self.assertEqual(request.call_args.kwargs['timeout'], 15)
                self.assertNotIn('private-secret', str(caught.exception))
        self.resolve.side_effect = OSError('spug-private-secret')
        with self.assertRaisesRegex(auth.ComponentAuthError, '网关不可用') as caught:
            self.bridge.login('spug')
        self.assertNotIn('private-secret', str(caught.exception))
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()

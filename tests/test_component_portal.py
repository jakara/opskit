"""组件桥接的真实会话/授权与模拟原生登录；不监听端口或连接真实组件。"""
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.cookies import SimpleCookie
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, urlsplit

from test_installer import ROOT, module

portal = module('component_portal_tests', ROOT / 'management/portal/server.py')
auth = __import__('auth')


class ComponentPortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'Fixture-portal-password-4921'
        cls.config = auth.configuration('operator', cls.password, True)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        config = self.root / 'operator.json'
        config.write_text(json.dumps(self.config))
        self.sessions = auth.Sessions(config, self.root)
        credentials = self.root / 'components.json'
        credentials.write_text(json.dumps({
            'npm': {'identity': 'fixture@example.internal', 'secret': 'fixture-npm-secret'},
            'spug': {'username': 'admin', 'password': 'fixture-spug-secret'},
            'dbx': {'password': 'fixture-dbx-secret'},
        }))
        self.components = portal.ComponentAuth(credentials)
        self.components.login = Mock(return_value={'storage': {'authentications': 'FIXTURE_NATIVE_TOKEN'}, 'set_cookie': None})
        self.server = SimpleNamespace(local_only=False, sessions=self.sessions, components=self.components)

    @staticmethod
    def request_cookie(header):
        parsed = SimpleCookie()
        parsed.load(header)
        return '; '.join(name + '=' + value.value for name, value in parsed.items())

    def login(self):
        return self.request_cookie(self.sessions.login('operator', self.password, 'fixture-peer'))

    def request(self, method, path, data=None, cookie=None, headers=None):
        handler = portal.Handler.__new__(portal.Handler)
        handler.server = self.server
        handler.command, handler.path = method, path
        handler.client_address = ('127.0.0.1', 12345)
        handler.headers = Message()
        body = json.dumps(data).encode() if data is not None else b''
        for name, value in {'Host': 'ops.example.internal', 'Origin': 'https://ops.example.internal',
                            'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                            **(headers or {})}.items():
            handler.headers[name] = value
        if cookie:
            handler.headers['Cookie'] = cookie
        handler.rfile = io.BytesIO(body)
        response = SimpleNamespace(status=None, body=None, headers={})

        def content(status, data, content_type, **kwargs):
            response.status, response.body = status, data
            response.headers = kwargs.get('headers') or {}

        def json_response(status, data, headers=None):
            response.status, response.body, response.headers = status, data, headers or {}

        handler.send_content, handler.send_json = content, json_response
        if method == 'GET':
            handler.do_GET()
        else:
            handler.mutate()
        self.assertIsNotNone(response.status)
        return response

    def verify(self, cookie, target='/npm/', **headers):
        return self.request('GET', '/auth/verify', cookie=cookie, headers={
            'Accept': 'text/html', 'X-OpsKit-Original-URI': target,
            'X-OpsKit-Original-Method': 'GET', **headers,
        })

    def bridge(self, cookie, component='npm', **changes):
        return self.request('POST', '/auth/api/component-login',
                            dict({'component': component, 'next': '/'}, **changes), cookie)

    def test_anonymous_cross_site_and_non_json_requests_never_login_components(self):
        self.assertEqual(self.bridge(None).status, 401)
        cookie = self.login()
        for headers, status in (({'Origin': 'https://other.example'}, 403),
                                ({'Sec-Fetch-Site': 'cross-site'}, 403),
                                ({'Content-Type': 'text/plain'}, 400)):
            result = self.request('POST', '/auth/api/component-login', {'component': 'npm'}, cookie, headers)
            self.assertEqual(result.status, status)
        self.components.login.assert_not_called()
        self.assertFalse(self.sessions.consume(cookie, 'npm'))

    def test_arbitrary_urls_and_unconfigured_components_never_reach_adapter(self):
        cookie = self.login()
        for component in ('http://other.example/login', 'http://169.254.169.254/', 'victorialogs', '', [], {}, None):
            with self.subTest(component=component):
                self.assertEqual(self.bridge(cookie, component=component).status, 400)
        self.components.login.assert_not_called()

    def test_next_is_component_bound_and_browser_cannot_override_credentials_or_url(self):
        cookie = self.login()
        for target in ('https://other.example', '//other.example', '/spug/home', '/auth/open', '/%2fother.example'):
            with self.subTest(target=target):
                result = self.bridge(cookie, next=target, url='http://other.example', password='browser-password')
                self.assertEqual(result.status, 200)
                self.assertEqual(result.body['redirect'], '/npm/')
                self.assertEqual(self.components.login.call_args.args, ('npm',))

    def test_component_rejection_does_not_issue_grant_or_success_payload(self):
        cookie = self.login()
        self.components.login.side_effect = portal.ComponentAuthError('NPM 内置账号登录未通过')
        result = self.bridge(cookie)
        self.assertEqual(result.status, 503)
        self.assertNotIn('storage', result.body)
        self.assertNotIn('Set-Cookie', result.headers)
        self.assertFalse(self.sessions.consume(cookie, 'npm'))

    def test_document_grant_is_one_use_but_api_assets_and_websocket_need_only_session(self):
        cookie = self.login()
        self.assertEqual(self.verify(cookie).status, 401)
        self.assertEqual(self.bridge(cookie).status, 200)
        for target, headers in (('/npm/api/users/me', {}),
                                ('/npm/assets/app.js', {'Accept': '*/*', 'Sec-Fetch-Dest': 'script'}),
                                ('/spug/api/ws/notify/', {'Upgrade': 'websocket'})):
            self.assertEqual(self.verify(cookie, target, **headers).status, 204)
        self.assertEqual(self.verify(cookie).status, 204)
        self.assertEqual(self.verify(cookie).status, 401)

    def test_one_grant_cannot_authorize_two_parallel_navigations(self):
        cookie = self.login()
        self.assertEqual(self.bridge(cookie).status, 200)
        ready = threading.Barrier(8)

        def consume(_):
            ready.wait(timeout=5)
            return self.verify(cookie).status

        with ThreadPoolExecutor(max_workers=8) as workers:
            codes = list(workers.map(consume, range(8)))
        self.assertEqual(codes.count(204), 1)
        self.assertEqual(codes.count(401), 7)

    def test_two_completed_bridges_allow_exactly_two_navigations(self):
        cookie = self.login()
        self.assertEqual(self.bridge(cookie).status, 200)
        self.assertEqual(self.bridge(cookie).status, 200)
        self.assertEqual([self.verify(cookie).status for _ in range(3)], [204, 204, 401])

    def test_expired_unconsumed_grants_do_not_gain_fresh_uses(self):
        cookie = self.login()
        for _ in range(3):
            self.assertEqual(self.bridge(cookie).status, 200)
        with sqlite3.connect(self.sessions.path) as database, database:
            database.execute('UPDATE grants SET expires=0')
        self.assertEqual(self.bridge(cookie).status, 200)
        self.assertEqual([self.verify(cookie).status for _ in range(2)], [204, 401])

    def test_logout_revokes_session_and_component_grants_and_clears_dbx_cookie(self):
        cookie = self.login()
        self.assertEqual(self.bridge(cookie).status, 200)
        result = self.request('POST', '/auth/api/logout', {}, cookie)
        self.assertEqual(result.status, 200)
        self.assertIsNone(self.sessions.read(cookie))
        self.assertFalse(self.sessions.consume(cookie, 'npm'))
        self.assertEqual(self.verify(cookie).status, 401)
        cookies = result.headers['Set-Cookie']
        self.assertTrue(any(value.startswith('dbx_session=; Path=/dbx;') and 'Max-Age=0' in value for value in cookies))

    def test_logout_during_native_login_cannot_release_native_credentials(self):
        cookie = self.login()

        def interrupted(_):
            self.sessions.logout(cookie)
            return {'storage': {'authentications': 'FIXTURE_NATIVE_TOKEN'}, 'set_cookie': None}

        self.components.login.side_effect = interrupted
        result = self.bridge(cookie)
        self.assertEqual(result.status, 401)
        self.assertNotIn('storage', result.body)
        self.assertFalse(self.sessions.consume(cookie, 'npm'))

    def test_logout_between_recheck_and_grant_cannot_issue_native_credentials(self):
        cookie = self.login()
        grant = self.sessions.grant

        def interrupted(header, component):
            self.sessions.logout(header)
            return grant(header, component)

        with patch.object(self.sessions, 'grant', side_effect=interrupted):
            result = self.bridge(cookie)
        self.assertEqual(result.status, 401)
        self.assertNotIn('storage', result.body)
        self.assertFalse(self.sessions.consume(cookie, 'npm'))

    def test_https_dbx_bridge_preserves_native_cookie_and_adds_secure(self):
        cookie = self.login()
        native = 'dbx_session=12345678-1234-4123-8123-123456789abc; Path=/dbx; HttpOnly; SameSite=Lax'
        self.components.login.return_value = {'storage': {}, 'set_cookie': native}
        result = self.bridge(cookie, 'dbx')
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body['storage'], {})
        self.assertEqual(result.headers['Set-Cookie'], native + '; Secure')
        self.assertEqual(self.verify(cookie, '/dbx/').status, 204)
        self.assertEqual(self.verify(cookie, '/npm/').status, 401)

    def test_https_dbx_comment_does_not_suppress_secure_attribute(self):
        cookie = self.login()
        native = ('dbx_session=12345678-1234-4123-8123-123456789abc; '
                  'Path=/dbx; HttpOnly; SameSite=Lax; Comment=insecure')
        self.components.login.return_value = {'storage': {}, 'set_cookie': native}
        result = self.bridge(cookie, 'dbx')
        self.assertEqual(result.status, 200)
        parsed = SimpleCookie()
        parsed.load(result.headers['Set-Cookie'])
        self.assertTrue(parsed['dbx_session']['secure'])

    def test_login_bridge_navigation_and_hard_reload_form_a_closed_next_chain(self):
        target = '/spug/home?task=42'
        opening = '/auth/open?component=spug&next=' + quote(target, safe='')
        anonymous = self.request('GET', opening, headers={'Accept': 'text/html'})
        self.assertEqual(anonymous.status, 302)
        next_path = parse_qs(urlsplit(anonymous.headers['Location']).query)['next'][0]
        logged_in = self.request('POST', '/auth/api/login', {
            'username': 'operator', 'password': self.password, 'next': next_path,
        })
        self.assertEqual(logged_in.status, 200)
        self.assertEqual(logged_in.body['redirect'], opening)
        cookie = self.request_cookie(logged_in.headers['Set-Cookie'])
        self.components.login.return_value = {'storage': {'token': 'a' * 32}, 'set_cookie': None}
        self.assertEqual(self.bridge(cookie, 'spug', next=target).body['redirect'], target)
        self.assertEqual(self.verify(cookie, target).status, 204)
        self.assertEqual(self.verify(cookie, target).status, 401)
        required = self.request('GET', '/auth/required', cookie=cookie, headers={
            'Accept': 'text/html', 'X-OpsKit-Original-URI': target,
        })
        self.assertEqual(required.status, 302)
        self.assertEqual(required.headers['Location'], opening)


if __name__ == '__main__':
    unittest.main()

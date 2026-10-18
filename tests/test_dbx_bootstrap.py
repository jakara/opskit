"""隔离 HTTP 服务验证 DBX 播种、双层鉴权、保留人工配置和脱敏失败。"""
import base64
import contextlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import unittest
from unittest.mock import patch

from test_installer import ROOT, module

dbx = module('dbx_bootstrap', ROOT / 'management/scripts/dbx_bootstrap.py')


class DBXBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.values = {'OPSKIT_BIND_IP': '127.0.0.1', 'OPS_PUBLIC_HOST': 'ops.example.internal',
                       'OPS_PUBLIC_SCHEME': 'https', 'OPS_ADMIN_USER': 'operator',
                       'OPS_ADMIN_PASSWORD': 'operator-secret', 'DBX_PASSWORD': 'dbx-secret',
                       'SPUG_DB_PASSWORD': 'database-secret'}
        self.sentinel = {'id': 'user-database', 'name': '人工连接', 'password': 'saved-private-secret'}
        self.profiles = [copy.deepcopy(self.sentinel)]
        self.calls = []
        self.error_path = None
        self.error_status = 200
        self.bad_result = None
        self.probe_value = '1'
        self.not_ready = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_api()

            def do_POST(self):
                self.handle_api()

            def log_message(self, *_):
                pass

            def reply(self, value, status=200, cookie=False):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                if cookie:
                    self.send_header('Set-Cookie', 'dbx_session=fixture; Path=/dbx; HttpOnly; SameSite=Lax')
                self.end_headers()
                self.wfile.write(body)

            def handle_api(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                data = json.loads(body) if body else None
                path = self.path.removeprefix('/dbx/api')
                owner.calls.append((self.command, self.path, dict(self.headers), data))
                expected = 'Basic ' + base64.b64encode(b'operator:operator-secret').decode()
                if self.headers.get('Host') != owner.values['OPS_PUBLIC_HOST'] or self.headers.get('Authorization') != expected:
                    return self.reply({'error': 'bad proxy auth'}, 401)
                if path == '/auth/check' and owner.not_ready:
                    owner.not_ready -= 1
                    return self.reply({'error': 'starting'}, 502)
                if path == owner.error_path:
                    return self.reply({'error': 'database-secret saved-private-secret dbx-secret'}, owner.error_status)
                if path == '/auth/check':
                    return self.reply({'required': True, 'authenticated': False, 'setup_required': False})
                if path == '/auth/login':
                    if data != {'password': owner.values['DBX_PASSWORD']}:
                        return self.reply({'error': 'bad DBX auth'}, 401)
                    return self.reply({'ok': True}, cookie=True)
                if self.headers.get('Cookie') != 'dbx_session=fixture':
                    return self.reply({'error': 'missing DBX cookie'}, 401)
                if path == '/connection/list':
                    return self.reply(owner.profiles)
                if path == '/connection/save':
                    owner.profiles.extend(copy.deepcopy(data['configs']))
                    return self.reply(None)
                if path == '/connection/test-info':
                    return self.reply({'message': 'Connection successful', 'databaseInfo': {'currentDatabase': 'spug'}})
                if path == '/connection/connect':
                    return self.reply('Connected successfully')
                if path == '/query/execute':
                    if owner.bad_result is not None:
                        return self.reply(owner.bad_result)
                    if data['sql'] == dbx.PROBE_SQL:
                        return self.reply({'columns': ['opskit_probe', 'current_database'],
                                           'column_types': ['int', 'varchar'], 'rows': [[owner.probe_value, 'spug']]})
                    return self.reply({'columns': ['table_name'], 'rows': [['django_migrations'], ['users']]})
                self.reply({'error': 'unexpected route'}, 404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.values['OPSKIT_HTTP_PORT'] = str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_creates_readonly_connection_and_retains_existing_profiles_on_rerun(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            first = dbx.initialize(self.values)
            second = dbx.initialize(self.values)
        self.assertTrue(first['created'])
        self.assertTrue(first['verified'])
        self.assertEqual(first['table_count'], 2)
        self.assertFalse(second['created'])
        self.assertTrue(second['verified'])
        self.assertEqual(self.profiles[0], self.sentinel)
        saves = [call for call in self.calls if call[1].endswith('/connection/save')]
        self.assertEqual(len(saves), 1)
        self.assertEqual(set(saves[0][3]), {'configs'})
        profile = saves[0][3]['configs'][0]
        self.assertEqual(profile['id'], dbx.CONNECTION_ID)
        self.assertEqual((profile['db_type'], profile['driver_profile'], profile['host'], profile['database']),
                         ('mysql', 'mariadb', 'spugdb', 'spug'))
        self.assertEqual(profile['username'], 'spug')
        self.assertEqual(profile['password'], self.values['SPUG_DB_PASSWORD'])
        self.assertTrue(profile['read_only'])
        self.assertTrue(profile['save_password'])
        for method, path, headers, data in self.calls:
            self.assertTrue(path.startswith('/dbx/api/'))
            self.assertEqual(headers['X-Forwarded-Proto'], 'https')
            self.assertEqual(headers['Content-Type'], 'application/json')
            if path.endswith('/connection/test-info') or path.endswith('/connection/connect'):
                self.assertEqual(data, {'config': profile})
            if path.endswith('/query/execute'):
                self.assertEqual(data['connectionId'], dbx.CONNECTION_ID)
                self.assertEqual(data['database'], 'spug')
                self.assertLessEqual(data['timeoutSecs'], 10)
                self.assertEqual(method, 'POST')
        self.assertNotIn('victorialogs', str(self.calls))
        for secret in ('operator-secret', 'database-secret', 'saved-private-secret', 'dbx-secret'):
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(secret, json.dumps([first, second]))

    def test_fixed_id_or_same_name_preserves_user_edits_without_testing_them(self):
        for existing in ({'id': dbx.CONNECTION_ID, 'name': '用户已改名', 'host': 'private-host'},
                         {'id': 'user-choice', 'name': dbx.CONNECTION_NAME, 'read_only': False}):
            with self.subTest(existing=existing):
                self.calls.clear()
                self.profiles = [dict(existing, password='saved-private-secret')]
                before = copy.deepcopy(self.profiles)
                with contextlib.redirect_stdout(None):
                    summary = dbx.initialize(self.values)
                self.assertFalse(summary['created'])
                self.assertFalse(summary['verified'])
                self.assertEqual(self.profiles, before)
                self.assertEqual([call[1] for call in self.calls],
                                 ['/dbx/api/auth/check', '/dbx/api/auth/login', '/dbx/api/connection/list'])

    def test_recovers_failed_initialization_by_verifying_matching_saved_config(self):
        self.error_path = '/query/execute'
        with contextlib.redirect_stdout(None), self.assertRaises(RuntimeError):
            dbx.initialize(self.values)
        before = copy.deepcopy(self.profiles)
        self.error_path = None
        self.calls.clear()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            summary = dbx.initialize(self.values)
        self.assertFalse(summary['created'])
        self.assertTrue(summary['verified'])
        self.assertEqual(self.profiles, before)
        self.assertEqual([call[1] for call in self.calls], [
            '/dbx/api/auth/check', '/dbx/api/auth/login', '/dbx/api/connection/list',
            '/dbx/api/connection/connect', '/dbx/api/query/execute', '/dbx/api/query/execute'])
        connected = next(call[3] for call in self.calls if call[1].endswith('/connection/connect'))
        self.assertEqual(connected, {'config': before[-1]})
        for secret in ('database-secret', 'saved-private-secret', 'dbx-secret'):
            self.assertNotIn(secret, output.getvalue() + json.dumps(summary))

    def test_any_managed_field_change_preserves_profile_without_reverification(self):
        with contextlib.redirect_stdout(None):
            dbx.initialize(self.values)
        original = copy.deepcopy(self.profiles[-1])
        for field, changed in (('id', 'other-id'), ('name', '人工改名'), ('db_type', 'postgres'),
                               ('driver_profile', 'mysql'), ('host', 'private-host'), ('port', 3307),
                               ('username', 'other-user'), ('database', 'other-db'),
                               ('read_only', False), ('password', 'changed-private-secret')):
            with self.subTest(field=field):
                profile = dict(original, **{field: changed})
                self.profiles = [copy.deepcopy(profile)]
                self.calls.clear()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    summary = dbx.initialize(self.values)
                self.assertFalse(summary['created'])
                self.assertFalse(summary['verified'])
                self.assertEqual(self.profiles, [profile])
                self.assertEqual([call[1] for call in self.calls],
                                 ['/dbx/api/auth/check', '/dbx/api/auth/login', '/dbx/api/connection/list'])
                self.assertNotIn('changed-private-secret', output.getvalue() + json.dumps(summary))

    def test_rejects_api_error_json_and_http_failures_without_echoing_credentials(self):
        for path, status in (('/auth/login', 401), ('/connection/list', 200), ('/connection/save', 200),
                             ('/connection/test-info', 200), ('/connection/connect', 200), ('/query/execute', 200)):
            with self.subTest(path=path, status=status):
                self.profiles = [copy.deepcopy(self.sentinel)]
                self.error_path, self.error_status = path, status
                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaises(RuntimeError) as caught:
                    dbx.initialize(self.values)
                message = str(caught.exception) + output.getvalue()
                for secret in ('database-secret', 'saved-private-secret', 'dbx-secret'):
                    self.assertNotIn(secret, message)

    def test_rejects_wrong_probe_and_empty_spug_schema(self):
        for result in ({'columns': ['opskit_probe', 'current_database'], 'rows': [[1, 'other']]},
                       {'columns': ['opskit_probe', 'current_database'], 'rows': [[0, 'spug']]},
                       {'columns': ['error'], 'rows': [['database-secret']]},
                       {'columns': ['table_name'], 'rows': []}):
            with self.subTest(result=result):
                self.profiles = []
                # 第一个响应为正确探针时，第二个才用于验证空表清单。
                real_call = dbx.API.call
                def call(api, path, data=None):
                    if path == '/query/execute' and data['sql'] == dbx.TABLES_SQL:
                        return result
                    if path == '/query/execute' and result.get('columns') != ['table_name']:
                        return result
                    return real_call(api, path, data)
                with patch.object(dbx.API, 'call', call), contextlib.redirect_stdout(None), self.assertRaises(RuntimeError) as caught:
                    dbx.initialize(self.values)
                self.assertNotIn('database-secret', str(caught.exception))

    def test_mysql_probe_accepts_exact_numeric_or_string_one_only(self):
        for value in (1, '1', 0, '0', True, 1.0, '01', '1 '):
            with self.subTest(value=value, kind=type(value).__name__):
                self.profiles = []
                self.probe_value = value
                if type(value) in (int, str) and str(value) == '1':
                    with contextlib.redirect_stdout(None):
                        self.assertTrue(dbx.initialize(self.values)['verified'])
                else:
                    with contextlib.redirect_stdout(None), self.assertRaises(RuntimeError):
                        dbx.initialize(self.values)

    def test_waits_for_dbx_before_single_login(self):
        self.not_ready = 1
        with patch.object(dbx.time, 'sleep') as sleep, contextlib.redirect_stdout(None):
            dbx.initialize(self.values)
        sleep.assert_called_once_with(2)
        self.assertEqual(sum(call[1].endswith('/auth/login') for call in self.calls), 1)

    def test_all_interface_bind_uses_loopback(self):
        values = dict(self.values, OPSKIT_BIND_IP='0.0.0.0')
        with contextlib.redirect_stdout(None):
            dbx.initialize(values)
        self.assertEqual(self.profiles[-1]['username'], 'spug')


if __name__ == '__main__':
    unittest.main()

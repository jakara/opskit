"""工作台的持久化、输入边界、只读发现和代理迁移回归。"""
import contextlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import time
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_installer import ROOT, common, management, module, proxy

workbench = module('workbench', ROOT / 'management/portal/server.py')
discovery = __import__('discovery')


class WorkbenchTests(unittest.TestCase):
    def test_spug_check_uses_prefix_and_rejects_wrong_homepage(self):
        check = module('check_routes', ROOT / 'management/scripts/check.py')
        with patch.object(check, 'request', side_effect=[(200, b'OpsKit Hub'), (200, b'<title>Spug</title>')]) as request, patch.object(check.time, 'sleep'), contextlib.redirect_stdout(None):
            check.check_spug({'SPUG_HTTP_PORT': '8081', 'OPSKIT_HTTP_PORT': '80'})
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.args[0]['OPSKIT_HTTP_PORT'] == '80' and call.args[1] == '/spug/' for call in request.call_args_list))

    def test_url_validation(self):
        for url in ('javascript:alert(1)', '//evil.example', '/\\evil', 'https://user:password@example.com', 'http://[bad', '/has space'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                workbench.entry_values({'name': '系统', 'group': '业务系统', 'url': url})
        for url in ('/dbx/', 'http://127.0.0.1:4183/', 'https://example.com/system?q=1'):
            self.assertEqual(workbench.entry_values({'name': '系统', 'group': '业务系统', 'url': url})['url'], url)

    def test_catalog_backup_reload_and_failed_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = workbench.Catalog(tmp, seed_tools=True)
            original = catalog.path.read_bytes()
            entry = {'name': '心理健康', 'group': '业务系统', 'url': 'https://school.example/'}
            added = catalog.change('POST', None, entry)
            self.assertEqual(len(added), 5)
            self.assertEqual(next((Path(tmp) / 'backups').glob('*.json')).read_bytes(), original)
            self.assertEqual(workbench.Catalog(tmp, seed_tools=True).read(), added)
            catalog.change('PUT', added[-1]['id'], dict(entry, name='心理健康系统'))
            before = catalog.path.read_bytes()
            with patch.object(workbench.os, 'replace', side_effect=OSError('写入失败')), self.assertRaises(OSError):
                catalog.change('DELETE', added[-1]['id'], {})
            self.assertEqual(catalog.path.read_bytes(), before)
            catalog.change('DELETE', added[-1]['id'], {})
            self.assertEqual(len(catalog.read()), 4)
            self.assertEqual(catalog.path.stat().st_mode & 0o777, 0o600)

    def test_web_and_database_ports(self):
        container = {'state': 'running', 'ports': '127.0.0.1:4443->9000/tcp, 0.0.0.0:55432->5432/tcp, [::]:8443->443/tcp, 3000/tcp'}
        ports = discovery.container_ports(container)
        self.assertEqual([p['url'] for p in ports], ['http://127.0.0.1:4443/', '', 'https://127.0.0.1:8443/'])
        self.assertFalse(any(p['url'] for p in discovery.container_ports(dict(container, state='exited'))))

    def test_discovery_uses_read_only_commands_and_rejects_remote_context(self):
        item = {'id': 'abc', 'name': 'web', 'image': 'example/web', 'state': 'running', 'status': 'Up', 'project': 'demo', 'ports': '127.0.0.1:4183->8080/tcp'}
        results = [subprocess.CompletedProcess([], 0, 'unix:///var/run/docker.sock'), subprocess.CompletedProcess([], 0, json.dumps(item))]
        with patch.object(discovery.subprocess, 'run', side_effect=results) as run:
            containers, error = discovery.DockerDiscovery().read()
        self.assertIsNone(error)
        self.assertEqual(containers[0]['published_ports'][0]['url'], 'http://127.0.0.1:4183/')
        self.assertEqual(run.call_args_list[1].args[0][:4], ['docker', 'ps', '-a', '--format'])
        with patch.object(discovery.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ssh://remote')) as run:
            containers, error = discovery.DockerDiscovery().read()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(containers)
        self.assertIn('远程', error)
        with patch.dict(discovery.os.environ, {'DOCKER_HOST': 'tcp://remote.example:2376', 'DOCKER_CONTEXT': ''}), patch.object(discovery.subprocess, 'run') as run:
            containers, error = discovery.DockerDiscovery().read()
        run.assert_not_called()
        self.assertFalse(containers)
        self.assertIn('远程', error)

    def test_http_crud_shared_discovery_and_blocks_cross_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            with workbench.WorkbenchServer(('127.0.0.1', 0), tmp, seed_tools=True) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    def request(method, path, data=None, headers=None):
                        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                        body = json.dumps(data) if data is not None else None
                        connection.request(method, path, body, {'Content-Type': 'application/json', **(headers or {})})
                        response = connection.getresponse()
                        result = response.status, response.read()
                        connection.close()
                        return result
                    with patch.object(server.discovery, 'read', return_value={'containers': [], 'http_services': [{'name': '服务'}], 'discovered_at': 'now'}):
                        code, body = request('GET', '/hub/api/state')
                    self.assertEqual(code, 200)
                    self.assertEqual(json.loads(body)['http_services'], [{'name': '服务'}])
                    self.assertNotIn('mode', json.loads(body))
                    self.assertEqual(json.loads(body)['api_version'], 1)
                    self.assertEqual(request('GET', '/')[0], 200)
                    self.assertEqual(request('GET', '/hub/')[0], 302)
                    self.assertEqual(request('GET', '/hub/app.js')[0], 200)
                    self.assertEqual(sum(entry['builtin'] for entry in json.loads(body)['entries']), 4)
                    self.assertEqual(request('DELETE', '/hub/api/entries/spug')[0], 400)
                    self.assertEqual(request('GET', '/hub/server.py')[0], 404)
                    self.assertEqual(request('GET', '/hub/../entries.json')[0], 404)
                    entry = {'name': '教务系统', 'group': '业务系统', 'url': 'https://example.com'}
                    self.assertEqual(request('POST', '/hub/api/entries', entry, {'Origin': 'https://evil.example'})[0], 403)
                    self.assertEqual(len(server.catalog.read()), 4)
                    code, body = request('POST', '/hub/api/entries', entry)
                    self.assertEqual(code, 200)
                    entry_id = json.loads(body)['entries'][-1]['id']
                    self.assertEqual(request('PUT', '/hub/api/entries/' + entry_id, dict(entry, url='javascript:alert(1)'))[0], 400)
                    self.assertEqual(request('DELETE', '/hub/api/entries/' + entry_id)[0], 200)
                finally:
                    server.shutdown()
                    thread.join(timeout=3)

    def test_hub_cli_migrates_catalog_without_tool_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'management'
            shutil.copytree(ROOT / 'management', dest, ignore=shutil.ignore_patterns('.env', '.workbench', 'collector.env', '__pycache__', '.backups'))
            previous = dest / '.workbench/development'
            previous.mkdir(parents=True)
            saved = '[{"id":"saved","name":"已有系统","group":"收藏","url":"http://localhost:8000"}]'
            (previous / 'entries.json').write_text(saved)
            result = subprocess.run([str(dest / 'install.sh'), '--hub-only', '--configure-only'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((dest / '.env').exists())
            migrated = json.loads((dest / '.workbench/hub/entries.json').read_text())
            self.assertEqual(migrated[0], json.loads(saved)[0])
            self.assertEqual(migrated[-1]['id'], 'npm')
            self.assertTrue((dest / '.workbench/hub.env').exists())
            self.assertEqual((previous / 'entries.json').read_text(), saved)

    def test_local_listener_parsers_and_http_deduplication(self):
        mac = 'p123\ncPython\nn127.0.0.1:18765\nn[::1]:18766\ncDocker\nn*:4183\n'
        linux = 'LISTEN 0 4096 127.0.0.1:18765 0.0.0.0:* users:(("python",pid=123,fd=3))\nLISTEN 0 128 [::]:18766 [::]:*\n'
        self.assertEqual([x['port'] for x in discovery.parse_listeners(mac, 'lsof')], [18765, 18766, 4183])
        self.assertEqual([x['port'] for x in discovery.parse_listeners(linux, 'ss')], [18765, 18766])
        for address in ('evil.example:8080', '127.0.0.1:99999', 'bad', 'http://remote:80'):
            self.assertIsNone(discovery.listener(address))
        container = {'name': 'app', 'state': 'running', 'published_ports': [{'bind': '127.0.0.1', 'port': 4183, 'target': '23456/tcp', 'url': ''}]}
        with patch.object(discovery.sys, 'platform', 'darwin'), patch.object(discovery.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, mac)), patch.object(discovery, 'probe_http', side_effect=lambda x: dict(x, url='http://localhost:' + str(x['port']), status=200)):
            services, error = discovery.discover_http([container])
        self.assertIsNone(error)
        self.assertEqual(len(services), 2)
        self.assertTrue(container['published_ports'][0]['url'].endswith(':4183'))

    def test_http_probe_uses_head_without_following_redirects(self):
        calls = []
        class App(BaseHTTPRequestHandler):
            def do_HEAD(self):
                calls.append(('HEAD', self.path))
                self.send_response(302)
                self.send_header('Location', 'http://must-not-follow.invalid/')
                self.end_headers()
            def log_message(self, *_):
                pass
        with ThreadingHTTPServer(('127.0.0.1', 0), App) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                found = discovery.probe_http({'bind': '127.0.0.1', 'port': server.server_port, 'name': '应用'})
                self.assertEqual(found['status'], 302)
                self.assertEqual(calls, [('HEAD', '/')])
            finally:
                server.shutdown()
                thread.join(timeout=3)

    def test_snapshot_missing_stale_and_container_reads_same_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            snapshot = discovery.SnapshotDiscovery(path)
            self.assertIn('尚未收到', snapshot.read()['http_error'])
            expected = {'containers': [], 'http_services': [{'name': '本机服务', 'port': 8000}], 'docker_error': None, 'http_error': None, 'discovered_at': 'now'}
            discovery.write_snapshot(path, expected)
            self.assertEqual(snapshot.read(), expected)
            with workbench.WorkbenchServer(('127.0.0.1', 0), Path(tmp) / 'catalog', path) as server:
                self.assertEqual(server.discovery.read(), expected)
            os.utime(path, (time.time() - 100, time.time() - 100))
            self.assertIn('90 秒', snapshot.read()['docker_error'])
            self.assertEqual(snapshot.read()['http_services'], expected['http_services'])
            path.write_text('[]')
            self.assertIn('尚未收到', snapshot.read()['docker_error'])

    def test_linux_discovery_autostart_unit_uses_fixed_command(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(management, 'ROOT', Path(tmp)), patch.object(management.os, 'geteuid', return_value=0), patch.object(Path, 'exists', return_value=True), patch.object(management.shutil, 'which', return_value='/usr/bin/systemctl'), patch.object(management, 'run') as run, patch.object(management, 'save') as save, contextlib.redirect_stdout(None):
            management.start_discovery()
            self.assertEqual(run.call_args_list[0].args[0][-1], '--once')
            path, content = save.call_args.args[:2]
            self.assertTrue(path.name.startswith('opskit-discovery-'))
            self.assertIn('Restart=always', content)
            self.assertIn('WantedBy=multi-user.target', content)
            self.assertIn(str(Path(tmp) / 'portal/discovery.py'), content)
            self.assertNotIn('docker.sock', content)
            self.assertIn(['systemctl', 'enable', path.name], [call.args[0] for call in run.call_args_list])

    def test_root_proxy_and_builtin_catalog_migration(self):
        values = {'OPS_PUBLIC_HOST': 'ops.example.internal'}
        payload = proxy.proxy_payload(values, 1, 2)
        self.assertEqual((payload['forward_host'], payload['forward_port']), ('workbench', 8090))
        self.assertIn('127.0.0.1', payload['domain_names'])
        local = proxy.proxy_payload(dict(values, HUB_ONLY='true'), 1, 2)
        self.assertEqual(local['access_list_id'], 0)
        self.assertNotIn('dbx:4224', local['advanced_config'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'entries.json'
            old = [{'id': 'spug', 'name': '我的主机', 'group': '运维工具', 'url': '/'},
                   {'id': 'dbx', 'name': '数据库', 'group': '自定义分组', 'url': 'https://custom.example/db'}]
            path.write_text(json.dumps(old))
            catalog = workbench.Catalog(tmp, seed_tools=True)
            self.assertEqual(catalog.read()[0]['url'], '/spug/')
            self.assertEqual(catalog.read()[1], old[1])
            backup = next((Path(tmp) / 'backups').glob('*.json'))
            self.assertEqual(json.loads(backup.read_text()), old)
            self.assertEqual(len(catalog.read()), 4)

    def test_prefix_routes_separate_native_api_auth_and_preserve_custom_catalog(self):
        routes = proxy.console_routes()
        api = routes.split('location ^~ /npm/api/ {', 1)[1].split('\n}', 1)[0]
        self.assertIn('auth_basic off;', api)
        self.assertIn('return 401;', api)
        self.assertIn('location = /npm/api/tokens {', routes)
        self.assertIn('Authorization $http_authorization;', api)
        self.assertIn('http://127.0.0.1:81/api/;', api)
        self.assertIn('http://spug:80/;', routes)
        self.assertNotIn('spug:80', proxy.console_routes(True))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'entries.json'
            old = [{'id': 'spug', 'name': 'Spug', 'group': '工具', 'url': 'http://127.0.0.1:8082/'},
                   {'id': 'npm', 'name': 'NPM', 'group': '工具', 'url': 'https://custom.example/npm/'}]
            path.write_text(json.dumps(old))
            values = workbench.Catalog(tmp).read()
            self.assertEqual(values[0]['url'], '/spug/')
            self.assertEqual(values[1], old[1])

    def test_proxy_upgrade_preserves_custom_rules_and_backs_up(self):
        old = 'auth_basic "OpsKit";\nlocation /hub/ {\n    alias /opskit/portal/;\n    index index.html;\n}\nlocation /custom { return 204; }'
        entry = {'id': 7, 'advanced_config': old, 'domain_names': ['ops.example'], 'forward_host': 'spug', 'forward_port': 80}
        class API:
            calls = []
            def call(self, *args, **kwargs):
                self.calls.append((args, kwargs))
        api = API()
        with tempfile.TemporaryDirectory() as tmp, patch.object(proxy, 'ROOT', Path(tmp)), contextlib.redirect_stdout(None):
            proxy.upgrade_workbench(api, entry, reserved_domains=('localhost',))
            backup = next((Path(tmp) / '.backups').glob('*/npm-proxy-7.json'))
            self.assertEqual(json.loads(backup.read_text()), entry)
        args, kwargs = api.calls[0]
        self.assertEqual(kwargs['method'], 'PUT')
        self.assertEqual(args[1]['forward_host'], 'workbench')
        self.assertEqual(args[1]['forward_port'], 8090)
        self.assertIn('127.0.0.1', args[1]['domain_names'])
        self.assertNotIn('localhost', args[1]['domain_names'])
        updated = args[1]['advanced_config']
        self.assertIn('location /custom { return 204; }', updated)
        self.assertIn('proxy_pass http://workbench:8090;', updated)
        api.calls.clear()
        proxy.upgrade_workbench(api, dict(entry, advanced_config='location /hub/ { proxy_pass http://custom; }'))
        self.assertFalse(api.calls)


if __name__ == '__main__':
    unittest.main()

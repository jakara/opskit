"""在非 Windows 主机验证平台分支；不代表原生 Windows/NTFS 验收。"""
import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_installer import common, collector, management

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'management/portal'))
import discovery
import uninstall


class PlatformTests(unittest.TestCase):
    def test_windows_listener_json_and_read_only_query(self):
        record = dict(bind='::', port=9000, name='服务')
        self.assertEqual(discovery.parse_listeners(json.dumps(record), 'windows'), [record])
        self.assertEqual(discovery.parse_listeners('null', 'windows'), [])
        self.assertEqual(discovery.parse_listeners('\ufeff[]', 'windows'), [])
        bad = [dict(bind='remote.example', port=80), dict(bind='127.0.0.1', port=70000), None]
        self.assertEqual(discovery.parse_listeners(json.dumps(bad), 'windows'), [])
        with patch.object(discovery.sys, 'platform', 'win32'), patch.object(discovery.subprocess, 'run') as run, \
                patch.object(discovery, 'probe_http', side_effect=lambda item: dict(item, url='http://[::1]:9000/')):
            run.return_value = Mock(returncode=0, stdout=json.dumps([record]))
            services, warning = discovery.discover_http([])
        self.assertIsNone(warning)
        self.assertEqual(services[0]['name'], '服务')
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['powershell.exe', '-NoProfile'])
        script = base64.b64decode(command[-1]).decode('utf-16-le')
        self.assertIn('Get-NetTCPConnection -State Listen', script)
        self.assertNotIn('Stop-Process', script)

    def test_windows_discovery_failure_is_actionable(self):
        with patch.object(discovery.sys, 'platform', 'win32'), patch.object(discovery.subprocess, 'run', side_effect=OSError):
            self.assertIn('Get-NetTCPConnection', discovery.discover_http([])[1])

    def test_windows_background_stops_only_owned_positive_pid(self):
        for pid, owned in (('123', True), ('456', False), ('-1', False)):
            with self.subTest(pid=pid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                folder = root / '.workbench/discovery'
                folder.mkdir(parents=True)
                (folder / 'discovery.pid').write_text(pid, encoding='utf-8')
                command = f'{root / "portal/discovery.py"} --output {folder / "state.json"}' if owned else 'other-app'
                with patch.object(management, 'ROOT', root), patch.object(management.sys, 'platform', 'win32'), \
                        patch.object(management, 'run'), patch.object(management, 'save'), \
                        patch.object(uninstall, 'powershell', side_effect=[Mock(stdout=command), Mock(stdout='')]) as query, \
                        patch.object(management.os, 'kill') as kill, \
                        patch.object(management.subprocess, 'Popen', return_value=Mock(pid=789)) as launch, \
                        contextlib.redirect_stdout(io.StringIO()):
                    management.start_discovery()
                if owned:
                    kill.assert_called_once_with(123, 15)
                else:
                    kill.assert_not_called()
                if pid == '-1':
                    query.assert_not_called()
                kwargs = launch.call_args.kwargs
                self.assertEqual(kwargs['creationflags'], 0x208)
                self.assertNotIn('start_new_session', kwargs)
                self.assertEqual(kwargs['stdin'], management.subprocess.DEVNULL)

    def test_windows_log_paths_normalize_and_roundtrip(self):
        with patch.object(collector.sys, 'platform', 'win32'), patch.object(collector.Path, 'is_dir', return_value=True):
            for path in ('C:/logs/业务', r'D:\app logs\service'):
                self.assertTrue(collector.log_directory(path), path)
            for path in ('C:/', 'C:logs', '/', r'\\server\logs', 'C:/logs/../../', 'C:/logs:private', "C:/a'b", 'C:/$HOME'):
                self.assertFalse(collector.log_directory(path), path)
            values = dict(HOST_ID='win-app-01', PROJECT_ID='college', APP_ID='app', APP_ENV='production',
                          HOST_LOG_DIR=r'D:\app logs\业务', LOG_FILE_PATTERN='*.log', OPS_HOST='127.0.0.1',
                          OPS_VHOST='ops.internal', OPS_PORT='80', OPS_SCHEME='http',
                          LOG_AGENT_USER='logagent', LOG_AGENT_PASSWORD='abcdef1234567890')
            normalized = collector.configure(values)
        self.assertEqual(normalized['HOST_LOG_DIR'], 'D:/app logs/业务')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text(common.env_text(normalized), encoding='utf-8-sig')
            self.assertEqual(common.load_env(path)['HOST_LOG_DIR'], 'D:/app logs/业务')

    def test_private_windows_write_protects_before_credentials_and_acl_backup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(common.sys, 'platform', 'win32'), \
                patch.object(common, 'powershell') as ps, contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / '.env'
            common.save(path, '秘密一')
            common.save(path, '秘密二')
            common.save(path, '秘密二')
            self.assertEqual(path.read_text(encoding='utf-8'), '秘密二')
            self.assertEqual(next((Path(tmp) / '.backups').glob('*/.env')).read_text(encoding='utf-8'), '秘密一')
            self.assertTrue(any(call.kwargs['env']['OPSKIT_BACKUP_ACL'] == '1' for call in ps.call_args_list))
            script = ps.call_args.args[0]
            self.assertLess(script.index('ACL backup verification failed'), script.index('Set-Acl -LiteralPath $path'))
            self.assertIn('Private ACL verification failed', script)
            self.assertIn("'S-1-5-18', 'S-1-5-32-544'", script)


if __name__ == '__main__':
    unittest.main()

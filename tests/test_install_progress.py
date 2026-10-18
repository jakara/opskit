"""实际入口中断、逐项落盘与恢复默认值；所有配置均位于临时目录。"""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_installer import ROOT, common, management, module

options = module('install_options', ROOT / 'management/scripts/options.py')
location = module('progress_location', ROOT / 'management/scripts/location.py')


class InstallProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'opskit'
        self.root.mkdir()
        for name in ('management', 'collector'):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns(
                '.env', '.env.install-*', '.install-options.json', 'collector.env', '.workbench', '.backups', '__pycache__'))
        for name in ('install.sh', 'install.ps1'):
            shutil.copy2(ROOT / name, self.root / name)

    def tearDown(self):
        self.temp.cleanup()

    def run_install(self, module_name, answers, *flags):
        script = self.root / module_name / 'install.sh' if module_name else self.root / 'install.sh'
        return subprocess.run([str(script), '--configure-only', '--keep-location', *flags] if module_name else [str(script), *flags],
                              input=answers, text=True, capture_output=True)

    def test_management_remembers_each_answer_on_eof_and_resumes_defaults(self):
        first = self.run_install('management', 'school.example.internal\nhttp\n18080\n10.2.3.4\n18081\n')
        self.assertEqual(first.returncode, 1)
        root = self.root / 'management'
        progress = root / '.env.install-progress'
        saved = common.load_env(progress)
        self.assertEqual(saved, {'OPS_PUBLIC_HOST': 'school.example.internal', 'OPS_PUBLIC_SCHEME': 'http',
                                 'OPS_PUBLIC_PORT': '18080', 'OPSKIT_BIND_IP': '10.2.3.4', 'OPSKIT_HTTP_PORT': '18081'})
        self.assertEqual(progress.stat().st_mode & 0o777, 0o600)
        self.assertFalse((root / '.env').exists())
        second = self.run_install('management', '\n' * 35)
        self.assertEqual(second.returncode, 0, second.stderr)
        for key, value in saved.items():
            self.assertEqual(common.load_env(root / '.env')[key], value)
            self.assertIn('[' + value + ']', second.stdout)
        self.assertFalse(progress.exists())

    def test_reconfigure_draft_does_not_replace_formal_config_or_reset_passwords(self):
        first = self.run_install('management', '\n' * 35)
        self.assertEqual(first.returncode, 0, first.stderr)
        root = self.root / 'management'
        config = root / '.env'
        original, connection = config.read_bytes(), (root / 'collector.env').read_bytes()
        interrupted = self.run_install('management', 'new.example.internal\n', '--reconfigure')
        self.assertEqual(interrupted.returncode, 1)
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual((root / 'collector.env').read_bytes(), connection)
        self.assertEqual(common.load_env(root / '.env.install-progress')['OPS_PUBLIC_HOST'], 'new.example.internal')
        completed = self.run_install('management', '\n' * 35)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        updated = common.load_env(config)
        self.assertEqual(updated['OPS_PUBLIC_HOST'], 'new.example.internal')
        for key in management.PASSWORD_KEYS:
            self.assertEqual(updated[key], common.load_env(next((root / '.backups').glob('*/.env')))[key])
            self.assertNotIn(updated[key], completed.stdout + completed.stderr)
        self.assertIn(original, [p.read_bytes() for p in (root / '.backups').glob('*/.env')])

    def test_password_is_saved_before_next_prompt_and_never_printed(self):
        password = 'remember_password_1234!'
        interrupted = self.run_install('management', '\n' * 6 + password + '\n')
        self.assertEqual(interrupted.returncode, 1)
        root = self.root / 'management'
        self.assertEqual(common.load_env(root / '.env.install-progress')['OPS_ADMIN_PASSWORD'], password)
        completed = self.run_install('management', '\n' * 35)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(common.load_env(root / '.env')['OPS_ADMIN_PASSWORD'], password)
        self.assertNotIn(password, interrupted.stdout + interrupted.stderr + completed.stdout + completed.stderr)

    def test_unified_username_does_not_collide_with_automatic_collector_identity(self):
        result = self.run_install('management', '\n' * 5 + 'logagent\n')
        self.assertEqual(result.returncode, 1)
        values = common.load_env(self.root / 'management/.env.install-progress')
        self.assertEqual(values['OPS_ADMIN_USER'], 'logagent')
        self.assertEqual(values['LOG_AGENT_USER'], 'opskit-agent')
        self.assertNotIn('日志写入用户名', result.stdout)
        self.assertNotIn('已备份原配置', result.stdout)

    def test_collector_resumes_labels_directory_and_keeps_issued_credentials(self):
        root = self.root / 'collector'
        original = {'OPS_HOST': 'ops.internal', 'OPS_PORT': '80', 'OPS_SCHEME': 'http',
                    'OPS_VHOST': 'ops.internal', 'LOG_AGENT_USER': 'logagent', 'LOG_AGENT_PASSWORD': 'secret_password_1234'}
        (root / '.env').write_text(common.env_text(original))
        before = (root / '.env').read_bytes()
        first = self.run_install('collector', '\n' * 4 + 'customer-vm\nmy-project\nmy-service\nstaging\n\n', '--reuse-config')
        self.assertEqual(first.returncode, 1)
        self.assertEqual((root / '.env').read_bytes(), before)
        self.assertEqual(common.load_env(root / '.env.install-progress')['APP_ID'], 'my-service')
        logs = Path(self.temp.name) / '业务日志'
        logs.mkdir()
        second = self.run_install('collector', '\n' * 9 + str(logs) + '\n\n\n')
        self.assertEqual(second.returncode, 0, second.stderr)
        saved = common.load_env(root / '.env')
        self.assertEqual(saved['HOST_ID'], 'customer-vm')
        self.assertEqual(saved['APP_ENV'], 'staging')
        self.assertEqual(saved['HOST_LOG_DIR'], str(logs))
        self.assertEqual(saved['LOG_AGENT_PASSWORD'], original['LOG_AGENT_PASSWORD'])
        self.assertFalse((root / '.env.install-progress').exists())

    def test_root_module_choice_is_saved_before_configuration_and_defaults_next_time(self):
        first = self.run_install('', 'custom.internal\n', 'collector', '--configure-only', '--keep-location')
        self.assertEqual(first.returncode, 1)
        self.assertEqual(options.read(self.root)['module'], '2')
        second = self.run_install('', '\n', '--configure-only')
        self.assertEqual(second.returncode, 1)
        self.assertIn('回车采用 2', second.stdout)
        self.assertIn('[custom.internal]', second.stdout)

    def test_hub_port_defaults_to_saved_value_and_explicit_port_wins(self):
        for flags, expected in ((('--port', '19090'), '19090'), ((), '19090'), (('--port', '19091'), '19091')):
            result = self.run_install('management', '', '--hub-only', '--reuse-config', *flags)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(common.load_env(self.root / 'management/.workbench/hub.env')['OPSKIT_HTTP_PORT'], expected)
        path = self.root / 'management/.workbench/hub.env'
        password = common.load_env(path)['NPM_ADMIN_PASSWORD']
        reconfigured = self.run_install('management', '2\n19092\n8182\n', '--hub-only')
        self.assertEqual(reconfigured.returncode, 0, reconfigured.stderr)
        self.assertEqual(common.load_env(path)['OPSKIT_HTTP_PORT'], '19092')
        self.assertEqual(common.load_env(path)['NPM_ADMIN_PORT'], '8182')
        self.assertEqual(common.load_env(path)['NPM_ADMIN_PASSWORD'], password)
        self.assertNotIn(password, reconfigured.stdout + reconfigured.stderr)

    def test_location_uses_last_directory_and_explicit_choices_take_priority(self):
        source = self.root
        with contextlib.redirect_stdout(io.StringIO()):
            options.remember(source, directory=str(source), module='2')
            with patch.object(location.sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=AssertionError('不应重新询问当前位置')) as ask:
                self.assertEqual(location.choose(source, [])[0], source)
                ask.assert_not_called()
            target = Path(self.temp.name) / 'new-location'
            options.remember(source, directory=str(target))
            with patch.object(location.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value=''), \
                    patch.object(location, 'install_at', return_value=target) as install:
                self.assertEqual(location.choose(source, [])[0], target)
                self.assertEqual(install.call_args.args[:2], (source, str(target)))
            self.assertEqual(location.choose(source, ['--keep-location'])[0], source)

    def test_configure_only_does_not_reset_previous_installation_directory(self):
        previous = Path(self.temp.name) / 'previous-installation'
        with contextlib.redirect_stdout(io.StringIO()):
            options.remember(self.root, directory=str(previous))
        result = subprocess.run([str(self.root / 'install.sh'), 'management', '--configure-only'],
                                input='\n' * 35, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(options.read(self.root)['directory'], str(previous))

    def test_module_reuses_the_full_package_installation_directory(self):
        with contextlib.redirect_stdout(io.StringIO()):
            options.remember(self.root, directory=str(self.root))
            for name in ('management', 'collector'):
                source = self.root / name
                with patch.object(location.sys.stdin, 'isatty', return_value=True), \
                        patch('builtins.input', side_effect=AssertionError('不应搬移已安装模块')):
                    self.assertEqual(location.choose(source, [])[0], source)

    def test_invalid_option_records_are_not_silently_replaced(self):
        path = self.root / '.install-options.json'
        for values in ([], {'unknown': 'value'}, {'module': '99'}, {'directory': None}):
            with self.subTest(values=values):
                path.write_text(json.dumps(values))
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    options.remember(self.root, module='1')
                self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

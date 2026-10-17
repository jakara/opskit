"""不启动 Docker，验证安装器输入、凭据保留、配置备份与独立安装。"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


sys.path.insert(0, str(ROOT / 'management/scripts'))
common = module('common', ROOT / 'management/scripts/common.py')
sys.modules['common'] = common
management = module('management_install', ROOT / 'management/scripts/install.py')
proxy = module('configure_proxy', ROOT / 'management/scripts/configure_proxy.py')
collector = module('collector_install', ROOT / 'collector/scripts/install.py')


class InstallerTests(unittest.TestCase):
    def defaults(self):
        with patch.object(management, 'ask', side_effect=lambda label, default, validator, **kw: str(default)), contextlib.redirect_stdout(io.StringIO()):
            return management.configure({})

    def test_reconfigure_retains_secrets(self):
        first = self.defaults()
        with patch.object(management, 'ask', side_effect=lambda label, default, validator, **kw: str(default)), contextlib.redirect_stdout(io.StringIO()):
            second = management.configure(first, True)
        for key in management.PASSWORD_KEYS:
            self.assertEqual(first[key], second[key])
        self.assertGreaterEqual(len(set(first[key] for key in management.PASSWORD_KEYS)), 6)

    def test_legacy_port_migration_preserves_log_ingest(self):
        values = self.defaults()
        values.pop('SPUG_HTTP_PORT')
        values['OPSKIT_HTTP_PORT'] = '8080'
        with contextlib.redirect_stdout(None):
            migrated = management.configure(values, interactive=False)
        self.assertEqual(migrated['OPSKIT_HTTP_PORT'], '80')
        self.assertEqual(migrated['SPUG_HTTP_PORT'], '8081')
        self.assertEqual(migrated['OPSKIT_LEGACY_HTTP_PORT'], '8080')
        self.assertEqual(migrated['COMPOSE_FILE'], 'compose.yaml:legacy.compose.yaml')
        for key in management.PASSWORD_KEYS:
            self.assertEqual(migrated[key], values[key])

    def test_invalid_ports_and_credentials_rejected(self):
        first = self.defaults()
        with self.assertRaisesRegex(ValueError, '只允许绑定'):
            management.validate(dict(first, HUB_ONLY='true', OPSKIT_BIND_IP='0.0.0.0'))
        for key, value in (('OPS_PUBLIC_HOST', 'bad; touch /tmp/bad'), ('NPM_ADMIN_PORT', first['OPSKIT_HTTP_PORT']), ('SPUG_HTTP_PORT', first['OPSKIT_HTTP_PORT']),
                           ('LOG_AGENT_USER', first['OPS_ADMIN_USER']), ('DBX_PASSWORD', 'REPLACE_WITH_RANDOM')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                management.validate(dict(first, **{key: value}))

    def test_reconfigure_repairs_invalid_application_password(self):
        first = self.defaults()
        first['DBX_PASSWORD'] = ''
        with patch.object(management, 'ask', side_effect=lambda label, default, validator, **kw: str(default)), contextlib.redirect_stdout(io.StringIO()):
            repaired = management.configure(first, True)
        self.assertTrue(common.password(repaired['DBX_PASSWORD']))
        self.assertEqual(repaired['SPUG_DB_PASSWORD'], first['SPUG_DB_PASSWORD'])

    def test_env_is_data_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text(common.env_text({'PASSWORD': 'has#punctuation!123', 'LITERAL': '$(exit 93)'}))
            self.assertEqual(common.load_env(path)['LITERAL'], '$(exit 93)')
            self.assertEqual(common.load_env(path)['PASSWORD'], 'has#punctuation!123')

    def test_config_backup_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / '.env'
            common.save(path, '旧配置\n')
            common.save(path, '新配置\n')
            backups = list((Path(tmp) / '.backups').glob('*/.env'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), '旧配置\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.stat().st_mtime_ns
            common.save(path, '新配置\n')
            self.assertEqual(path.stat().st_mtime_ns, before)
            target = Path(tmp) / 'link'
            target.symlink_to(path)
            with self.assertRaises(ValueError):
                common.save(target, '覆盖')

    def test_standalone_management_cli_and_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'management'
            shutil.copytree(ROOT / 'management', dest, ignore=shutil.ignore_patterns('.env', 'collector.env', '__pycache__', '.backups'))
            first = subprocess.run([str(dest / 'install.sh'), '--configure-only'], input='\n' * 25, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            secrets = common.load_env(dest / '.env')
            for key in management.PASSWORD_KEYS:
                self.assertNotIn(secrets[key], first.stdout + first.stderr)
            connection = common.load_env(dest / 'collector.env')
            self.assertNotIn('NPM_ADMIN_PASSWORD', connection)
            self.assertNotIn('SPUG_DB_PASSWORD', connection)
            before = (dest / '.env').read_bytes()
            second = subprocess.run([str(dest / 'install.sh'), '--configure-only'], input='', text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(before, (dest / '.env').read_bytes())
            third = subprocess.run([str(dest / 'install.sh'), '--configure-only', '--reconfigure'], input='\n' * 20, text=True, capture_output=True)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(secrets, common.load_env(dest / '.env'))

    def test_management_reprompts_conflicting_log_username_before_passwords(self):
        for reconfigure in (False, True):
            with self.subTest(reconfigure=reconfigure), tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / 'management'
                shutil.copytree(ROOT / 'management', dest, ignore=shutil.ignore_patterns('.env', 'collector.env', '__pycache__', '.backups'))
                existing = self.defaults() if reconfigure else {}
                if existing:
                    (dest / '.env').write_text(common.env_text(existing))
                answers = [''] * 11 + ['', 'shareduser', 'shareduser', 'shareduser', 'logwriter']
                answers += [''] * (6 if reconfigure else 11)
                args = [str(dest / 'install.sh'), '--configure-only']
                if reconfigure:
                    args.append('--reconfigure')
                result = subprocess.run(args, input='\n'.join(answers) + '\n', text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count('运维访问和日志写入必须使用不同账户'), 2)
                self.assertEqual(result.stdout.count('日志写入用户名 ['), 3)
                self.assertLess(result.stdout.rindex('日志写入用户名 ['), result.stdout.index('密码要求：'))
                saved = common.load_env(dest / '.env')
                self.assertEqual(saved['OPS_ADMIN_USER'], 'shareduser')
                self.assertEqual(saved['LOG_AGENT_USER'], 'logwriter')
                if existing:
                    for key in management.PASSWORD_KEYS:
                        self.assertEqual(saved[key], existing[key])

    def test_standalone_collector_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'collector'
            logs = Path(tmp) / 'logs'
            logs.mkdir()
            shutil.copytree(ROOT / 'collector', dest, ignore=shutil.ignore_patterns('.env', '__pycache__', '.backups'))
            values = {'OPS_HOST': '127.0.0.1', 'OPS_PORT': '8080', 'OPS_VHOST': 'ops.example.internal',
                      'OPS_SCHEME': 'http', 'LOG_AGENT_USER': 'logagent', 'LOG_AGENT_PASSWORD': 'a1' * 24}
            (dest / '.env').write_text(common.env_text(values))
            answers = '\n' * 9 + str(logs) + '\n\n\n'
            result = subprocess.run([str(dest / 'install.sh'), '--configure-only'], input=answers, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = common.load_env(dest / '.env')
            self.assertEqual(data['HOST_LOG_DIR'], str(logs))
            self.assertEqual(data['OPS_TLS'], 'Off')
            self.assertEqual(data['LOG_AGENT_PASSWORD'], values['LOG_AGENT_PASSWORD'])
            repeat = subprocess.run([str(dest / 'install.sh'), '--configure-only'], input='', text=True, capture_output=True)
            self.assertEqual(repeat.returncode, 0, repeat.stderr)

    def test_log_mount_and_labels_validation(self):
        self.assertFalse(collector.log_directory('/'))
        self.assertFalse(collector.pattern('../private/*'))
        self.assertFalse(collector.identifier('bad label'))
        self.assertFalse(common.host('http://ops.example.internal'))

    def test_installers_select_supported_python_beside_system_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            old = folder / 'python3'
            old.write_text('#!/bin/sh\nexit 1\n')
            old.chmod(0o755)
            (folder / 'python3.9').symlink_to(sys.executable)
            # 仅保留测试解释器及包装脚本所需工具，避免碰巧使用其他新版 Python。
            for command in ('bash', 'dirname'):
                (folder / command).symlink_to(shutil.which(command))
            for name in ('management', 'collector'):
                result = subprocess.run([str(ROOT / name / 'install.sh'), '--help'],
                                        env=dict(os.environ, PATH=str(folder)), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--configure-only', result.stdout)

    def test_operations_scripts_without_config_or_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            bin_dir = dest / 'bin'
            bin_dir.mkdir()
            docker = bin_dir / 'docker'
            docker.write_text('''#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == compose && "$2" == exec ]]; then
  if [[ "${TEST_DUMP_FAIL:-0}" == 1 ]]; then exit 1; fi
  printf '备份测试记录\\n'
elif [[ "$1" == compose && "$2" == --env-file && "$3" == .env.example && "$4" == config && "$5" == --images ]]; then
  printf 'example.invalid/image:1\\n'
elif [[ "$1" == pull ]]; then
  exit 0
elif [[ "$1" == image && "$2" == save && "$3" == -o ]]; then
  printf '测试镜像' > "$4"
else
  exit 13
fi
''')
            docker.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
            for name in ('management', 'collector'):
                module_dir = dest / name
                shutil.copytree(ROOT / name, module_dir, ignore=shutil.ignore_patterns('.env', 'collector.env', '__pycache__', '.backups'))
                result = subprocess.run([str(module_dir / 'scripts/export-images.sh')], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((module_dir / 'offline-images/SHA256SUMS.txt').read_text().strip())
            backup = dest / 'management/scripts/backup-spug-sql.sh'
            success = subprocess.run([str(backup)], env=env, capture_output=True, text=True)
            self.assertEqual(success.returncode, 0, success.stderr)
            files = list((dest / 'management/backups').glob('*.sql'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(), '备份测试记录\n')
            failed = subprocess.run([str(backup)], env=dict(env, TEST_DUMP_FAIL='1'), capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(list((dest / 'management/backups').glob('*.partial')))


if __name__ == '__main__':
    unittest.main()

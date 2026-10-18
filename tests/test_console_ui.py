"""Rich 离线资源、账号收敛、静默备份以及原生终端密码隐藏。"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_installer import ROOT, common, management
from test_airgap_skill import packager


class ConsoleUITests(unittest.TestCase):
    def test_backup_is_silent_and_still_verified(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as output:
            path = Path(folder) / '.env.install-progress'
            common.save(path, 'old draft')
            common.save(path, 'new draft')
            backup = next(path.parent.glob('.backups/*/.env.install-progress'))
            self.assertEqual(backup.read_text(), 'old draft')
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.getvalue(), '')

    def test_only_human_account_is_prompted_and_native_credentials_are_preserved(self):
        with contextlib.redirect_stdout(io.StringIO()):
            old = management.configure({}, interactive=False)
            old.update(NPM_ADMIN_EMAIL='operator@example.internal', SPUG_ADMIN_USER='existingadmin')
            labels = []
            def answer(label, default, validator, **kwargs):
                labels.append(label)
                return str(default)
            with patch.object(management, 'ask', side_effect=answer):
                current = management.configure(old, reconfigure=True)
        self.assertEqual(current, old)
        self.assertIn('统一运维账号', labels)
        self.assertFalse(any('管理员' in label or '日志写入用户名' in label for label in labels))
        self.assertFalse(any('IMAGE' in label for label in labels))

    def test_wheels_are_pinned_verified_and_both_modules_are_self_contained(self):
        for module in ('management', 'collector'):
            folder = ROOT / module / 'scripts/vendor'
            manifest = json.loads((folder / 'manifest.json').read_text())
            self.assertEqual(len(manifest), 4)
            for item in manifest:
                self.assertEqual(hashlib.sha256((folder / item['filename']).read_bytes()).hexdigest(), item['sha256'])
            result = subprocess.run([sys.executable, '-S', '-B', str(ROOT / module / 'scripts/console_ui.py')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('精简管理端', result.stdout)
        self.assertEqual((ROOT / 'management/scripts/console_ui.py').read_bytes(), (ROOT / 'collector/scripts/console_ui.py').read_bytes())

    def test_export_copy_retains_wheels_and_tampering_stops_before_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder) / 'management'
            packager.copy_clean(ROOT / 'management', site, runtime_only=True)
            self.assertEqual(len(list((site / 'scripts/vendor').glob('*.whl'))), 4)
            wheel = next((site / 'scripts/vendor').glob('*.whl'))
            wheel.write_bytes(b'corrupt wheel')
            result = subprocess.run([sys.executable, '-S', '-B', 'scripts/install.py', '--configure-only'], cwd=site, input='', capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('校验失败', result.stderr)
            self.assertFalse((site / '.env').exists())
            self.assertNotIn('Traceback', result.stderr)
            (site / 'scripts/vendor/manifest.json').write_text('null')
            result = subprocess.run([sys.executable, '-S', '-B', 'scripts/install.py', '--configure-only'], cwd=site, input='', capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('校验失败', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    @unittest.skipUnless(sys.platform != 'win32', '原生 PTY 验证仅在 Unix 运行')
    def test_real_tty_has_rich_steps_and_never_echoes_password(self):
        import pty
        import select
        import termios
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder) / 'management'
            packager.copy_clean(ROOT / 'management', site, runtime_only=True)
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            self.addCleanup(os.close, slave)
            process = subprocess.Popen([sys.executable, '-S', '-B', 'scripts/install.py', '--configure-only'], cwd=site,
                                       stdin=slave, stdout=slave, stderr=slave, env=dict(os.environ, TERM='xterm-256color'))
            output, sent, defaults_sent = b'', False, False
            os.write(master, b'\n' * 6)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)
                if not sent and '统一运维密码'.encode() in output and not termios.tcgetattr(slave)[3] & termios.ECHO:
                    os.write(master, b'Private-test-password-9381\n')
                    sent = True
                if sent and not defaults_sent and '业务机连接管理机'.encode() in output:
                    os.write(master, b'\n\n\n')
                    defaults_sent = True
                if process.poll() is not None:
                    break
            if process.poll() is None:
                process.kill()
            process.wait()
            self.assertEqual(process.returncode, 0, output.decode(errors='replace'))
            self.assertTrue(sent)
            text = output.decode()
            self.assertIn('\x1b[', text)
            self.assertIn('统一登录', text)
            self.assertIn('高级选项', text)
            self.assertNotIn('Private-test-password-9381', text)
            self.assertEqual(common.load_env(site / '.env')['OPS_ADMIN_PASSWORD'], 'Private-test-password-9381')
            self.assertNotIn('已备份原配置', text)

"""在隔离目录验证安装包复制、位置重选和真实入口重启，不安装服务。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'management/scripts/location.py'
spec = importlib.util.spec_from_file_location('install_location', HELPER)
location = importlib.util.module_from_spec(spec)
spec.loader.exec_module(location)


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / '解压 包'
        self.source.mkdir()
        (self.source / 'install.sh').write_text('#!/bin/sh\nprintf "continued"\n')
        (self.source / 'install.sh').chmod(0o755)
        (self.source / '.env').write_text('TEST_PASSWORD=fixture')
        (self.source / 'empty').mkdir()
        self.destination = self.base / '安装 目录'

    def tearDown(self):
        self.temporary.cleanup()

    def test_both_modules_use_the_same_helper(self):
        self.assertEqual(HELPER.read_bytes(), (ROOT / 'collector/scripts/location.py').read_bytes())

    def test_windows_reparse_points_are_detected_without_python312(self):
        from types import SimpleNamespace
        info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
        with patch.object(Path, 'lstat', return_value=info):
            self.assertTrue(location.linked(self.base / 'junction'))

    def test_recommendations_match_platform_and_standalone_package(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            (source / 'management').mkdir()
            self.assertEqual([str(path) for path, _ in location.recommendations(source, 'Linux', '/users/engineer')],
                             ['/opt/opskit', '/srv/opskit', '/users/engineer/opskit'])
            self.assertEqual(location.recommendations(source, 'Darwin', '/users/engineer')[0][0],
                             Path('/users/engineer/Applications/opskit'))
            windows = location.recommendations(source, 'Windows', '/users/engineer',
                                               {'LOCALAPPDATA': '/local', 'ProgramData': '/shared'})
            self.assertEqual([str(path) for path, _ in windows], ['/local/opskit', '/users/engineer/opskit', '/shared/opskit'])
            (source / 'management').rmdir()
            (source / 'fluent-bit.conf').touch()
            self.assertTrue(str(location.recommendations(source, 'Linux')[0][0]).endswith('/opskit-collector'))

    def test_copy_preserves_content_secrets_modes_and_original(self):
        (self.source / '.git').mkdir()
        (self.source / '.git/private').write_text('not runtime')
        (self.source / 'dist').mkdir()
        (self.source / 'dist/old-release').write_text('not runtime')
        (self.source / 'offline-images').mkdir()
        (self.source / 'offline-images/component.tar').write_bytes(b'image-fixture')
        (self.source / '.workbench').mkdir()
        (self.source / '.workbench/hub.env').write_text('HUB_PASSWORD=fixture')
        self.destination.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            result = location.copy_package(self.source, self.destination)
        self.assertEqual(result, self.destination)
        self.assertEqual((result / '.env').read_bytes(), (self.source / '.env').read_bytes())
        self.assertEqual((result / 'offline-images/component.tar').read_bytes(), b'image-fixture')
        self.assertTrue((result / 'empty').is_dir())
        self.assertFalse((result / '.git').exists())
        self.assertFalse((result / 'dist').exists())
        if sys.platform != 'win32':
            self.assertEqual(stat.S_IMODE((result / 'install.sh').stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((result / '.env').stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((result / '.workbench/hub.env').stat().st_mode), 0o600)
        self.assertTrue((self.source / 'install.sh').is_file())
        self.assertEqual(location.copy_package(self.source, self.source), self.source)

    def test_existing_nonempty_or_nested_destinations_are_never_modified(self):
        self.destination.mkdir()
        marker = self.destination / '.env'
        marker.write_text('customer=keep')
        for target in (self.destination, self.source / 'child', self.source.parent, self.source / 'x/../child'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                location.copy_package(self.source, target)
        self.assertEqual(marker.read_text(), 'customer=keep')
        self.assertFalse((self.source / 'child').exists())

    @unittest.skipIf(sys.platform == 'win32', 'POSIX symlinks and FIFO fixture')
    def test_links_and_special_files_are_rejected_without_copying(self):
        linked = self.base / 'linked'
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '软链接'):
            location.copy_package(self.source, linked / 'target')
        (self.source / 'bad-link').symlink_to(self.source / '.env')
        with self.assertRaisesRegex(ValueError, '链接'):
            location.copy_package(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        (self.source / 'bad-link').unlink()
        os.mkfifo(self.source / 'pipe')
        with self.assertRaisesRegex(ValueError, '特殊文件'):
            location.copy_package(self.source, self.destination)

    def test_interactive_invalid_or_occupied_path_reprompts(self):
        occupied = self.base / 'occupied'
        occupied.mkdir()
        (occupied / 'keep.txt').write_text('preserve')
        with patch.object(sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', side_effect=['9', '5', str(occupied), '5', str(self.destination)]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result, args = location.choose(self.source, ['hub', '--port', '19090'])
        self.assertEqual(result, self.destination)
        self.assertEqual(args, ['hub', '--port', '19090'])
        self.assertIn('请重新选择', output.getvalue())
        self.assertEqual((occupied / 'keep.txt').read_text(), 'preserve')

    def test_noninteractive_configure_help_and_keep_do_not_copy(self):
        for arguments in ([], ['management', '--configure-only'], ['--help'], ['--keep-location']):
            with patch.object(sys.stdin, 'isatty', return_value=False), \
                 patch('builtins.input', side_effect=AssertionError('unexpected prompt')), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(location.choose(self.source, arguments)[0], self.source)
        for arguments in (['--install-dir'], ['--install-dir=a', '--install-dir=b'],
                          ['--install-dir=a', '--keep-location'], ['--install-dir=']):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()):
                location.choose(self.source, arguments)

    def test_copy_failure_never_continues_or_damages_source(self):
        with patch.object(shutil, 'copyfileobj', side_effect=OSError('disk full')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(OSError):
                location.copy_package(self.source, self.destination)
        self.assertEqual((self.source / '.env').read_text(), 'TEST_PASSWORD=fixture')

    def test_cancel_and_permission_failure_do_not_start_install(self):
        with patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='0'), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            location.choose(self.source, [])
        self.assertFalse(self.destination.exists())
        with patch.object(sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', side_effect=['1', '4']), \
             patch.object(location, 'copy_package', side_effect=PermissionError('denied')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(location.choose(self.source, [])[0], self.source)
        self.assertIn('请重新选择', output.getvalue())

    def test_corruption_after_copy_is_detected(self):
        original = location.inventory
        def corrupt(path):
            if path == self.destination:
                (path / 'install.sh').write_text('corrupt')
            return original(path)
        with patch.object(location, 'inventory', side_effect=corrupt), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(ValueError, '发生变化'):
            location.copy_package(self.source, self.destination)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX launch fixture')
    def test_helper_restarts_from_copy_and_preserves_arguments_and_exit_code(self):
        (self.source / 'install.sh').write_text('''#!/usr/bin/env bash
printf '%s\\n' "$PWD" "$OPSKIT_LOCATION_READY" "$@" > "$PWD/continued.txt"
exit 23
''')
        result = subprocess.run([sys.executable, '-B', str(HELPER), '--source', str(self.source), '--entry', 'install.sh',
                                 '--', '--install-dir', str(self.destination), 'hub', '--port', '19090'],
                                text=True, capture_output=True, cwd=self.base)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual((self.destination / 'continued.txt').read_text().splitlines(),
                         [str(self.destination), str(self.destination), 'hub', '--port', '19090'])
        self.assertFalse((self.source / 'continued.txt').exists())

    @unittest.skipIf(sys.platform == 'win32', '真实 POSIX 伪终端交互验收')
    def test_terminal_selection_copies_and_then_runs_new_entry(self):
        import pty
        import select
        import time
        (self.source / 'install.sh').write_text('#!/bin/sh\nprintf "%s" "$PWD" > "$PWD/interactive-result"\n')
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, '-B', str(HELPER), '--source', str(self.source),
                                    '--entry', 'install.sh', '--', 'management'],
                                   stdin=slave, stdout=slave, stderr=slave, cwd=self.base)
        os.close(slave)
        output, stage = b'', 0
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
                    if stage == 0 and '请选择 ['.encode() in output:
                        os.write(master, b'5\n')
                        stage = 1
                    if stage == 1 and '请输入安装目录：'.encode() in output:
                        os.write(master, str(self.destination).encode() + b'\n')
                        stage = 2
            self.assertEqual(process.wait(timeout=2), 0, output.decode(errors='replace'))
            self.assertEqual(stage, 2)
            self.assertEqual((self.destination / 'interactive-result').read_text(), str(self.destination))
            self.assertFalse((self.source / 'interactive-result').exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX shell simulates the PowerShell process boundary')
    def test_windows_helper_parameter_boundary_and_policy(self):
        (self.source / 'install.ps1').write_text('PowerShell fixture')
        commands = self.base / 'bin'
        commands.mkdir()
        fake = commands / 'powershell.exe'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$OPSKIT_LOCATION_READY/ps-args.txt"\n')
        fake.chmod(0o755)
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'])
        result = subprocess.run([sys.executable, '-B', str(HELPER), '--source', str(self.source), '--entry', 'install.ps1',
                                 '--', '--install-dir', str(self.destination), '-Module', 'hub', '-Offline'],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (self.destination / 'ps-args.txt').read_text().splitlines()
        self.assertEqual(args, ['-NoLogo', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-File',
                                str(self.destination / 'install.ps1'), '-Module', 'hub', '-Offline'])

    @unittest.skipIf(sys.platform == 'win32', 'Unix wrapper acceptance')
    def test_real_full_and_standalone_installer_configure_in_copy(self):
        # 配置生成在副本执行，不调用 Docker、不触及开发源码或本机服务。
        for standalone in (False, True):
            source = self.base / ('standalone' if standalone else 'full')
            source.mkdir()
            shutil.copytree(ROOT / 'management', source if standalone else source / 'management', dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '.env', 'collector.env', '.workbench', '.backups'))
            if not standalone:
                shutil.copy2(ROOT / 'install.sh', source / 'install.sh')
            target = self.base / (source.name + '-installed')
            command = ['bash', str(source / 'install.sh')]
            if not standalone:
                command.append('management')
            result = subprocess.run(command + ['--install-dir', str(target), '--configure-only'],
                                    input='\n' * 30, text=True, capture_output=True, cwd=self.base)
            self.assertEqual(result.returncode, 0, result.stderr)
            module = target if standalone else target / 'management'
            self.assertTrue((module / '.env').is_file())
            self.assertFalse((source if standalone else source / 'management').joinpath('.env').exists())
            self.assertEqual(result.stdout.count('当前安装包位置：'), 1)


if __name__ == '__main__':
    unittest.main()

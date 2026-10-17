"""依赖引导只使用模拟包管理器；真实闭包证据由收集器单独生成。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/opskit/scripts/collect_prerequisites.py'
BOOTSTRAP = ROOT / 'skills/opskit/assets/site/offline-bootstrap.sh'
spec = importlib.util.spec_from_file_location('airgap_prerequisites', SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class PrerequisiteCollectorTests(unittest.TestCase):
    def test_invalid_target_precedes_docker(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(collector, 'run') as run:
            with self.assertRaises(ValueError):
                collector.collect({'os': 'ubuntu', 'version': '8.9', 'arch': 'arm64'}, Path(tmp) / 'out')
            run.assert_not_called()

    def test_explicit_platform_clean_offline_container_and_hashes(self):
        commands = []

        def fake_run(command, log=None):
            commands.append(command)
            if command[1] == 'pull':
                return ''
            if command[1:3] == ['image', 'inspect']:
                return json.dumps([{'Id': 'sha256:fixture', 'Architecture': 'arm64', 'Os': 'linux'}])
            mount = command[command.index('--mount') + 1]
            stage = Path(mount.partition('src=')[2].partition(',dst=')[0])
            if '--network' not in command:
                (stage / 'rpms/repodata').mkdir(parents=True)
                (stage / 'keys').mkdir()
                (stage / 'rpms/python.rpm').write_bytes(b'fixture Python RPM')
                (stage / 'rpms/runtime.rpm').write_bytes(b'fixture runtime RPM')
                (stage / 'rpms/repodata/repomd.xml').write_text('fixture repository')
                (stage / 'keys/vendor.asc').write_text('fixture public key')
                (stage / 'python-packages.txt').write_text('python.rpm\n')
                result = 'mock collection; not real verification'
            else:
                self.assertEqual(command[command.index('--network') + 1], 'none')
                result = 'OPSKIT_PYTHON=3.11.13\nOPSKIT_DOCKER=Docker version fixture\nOPSKIT_COMPOSE=fixture\n'
            log.write_text(result)
            return result

        with tempfile.TemporaryDirectory() as tmp, patch.object(collector, 'run', side_effect=fake_run), patch.object(collector.shutil, 'which', return_value='/fixture/docker'):
            out = Path(tmp) / 'output'
            target = {'os': 'anolis', 'version': '8.9', 'arch': 'arm64'}
            manifest = collector.collect(target, out)
            self.assertEqual(manifest['target'], target)
            self.assertTrue(manifest['verification']['offline_install'])
            self.assertEqual([x['group'] for x in manifest['files']], ['python', 'runtime'])
            for item in manifest['files'] + manifest['keys'] + manifest['repository_files']:
                self.assertEqual(item['sha256'], collector.sha256(out / item['path']))
            subprocess.run(['bash', '-n', str(out / 'bootstrap.sh')], check=True)
            self.assertTrue((out / 'SHA256SUMS').is_file())
        runs = [cmd for cmd in commands if cmd[1] == 'run']
        self.assertEqual(len(runs), 2)
        for cmd in runs:
            self.assertEqual(cmd[cmd.index('--platform') + 1], 'linux/arm64')
            self.assertNotIn('/var/run/docker.sock', ' '.join(cmd))

    def test_failed_verification_never_publishes_output(self):
        def fake_run(command, log=None):
            if command[1] == 'pull':
                return ''
            if command[1:3] == ['image', 'inspect']:
                return json.dumps([{'Id': 'sha256:fixture', 'Architecture': 'amd64', 'Os': 'linux'}])
            if '--network' in command:
                raise RuntimeError('fixture: missing dependency')
            log.write_text('fixture')
            return ''
        with tempfile.TemporaryDirectory() as tmp, patch.object(collector, 'run', side_effect=fake_run), patch.object(collector.shutil, 'which', return_value='/fixture/docker'):
            out = Path(tmp) / 'output'
            with self.assertRaises(RuntimeError):
                collector.collect({'os': 'anolis', 'version': '8.9', 'arch': 'amd64'}, out)
            self.assertFalse(out.exists())
            self.assertFalse(list(Path(tmp).rglob('manifest.json')))


@unittest.skipUnless(shutil.which('bash') and shutil.which('sha256sum'), '需要 bash 和 sha256sum')
class OfflineBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bundle = self.directory / 'prerequisites'
        self.bin = self.directory / 'bin'
        (self.bundle / 'rpms/repodata').mkdir(parents=True)
        (self.bundle / 'keys').mkdir()
        self.bin.mkdir()
        (self.bin / 'sha256sum').symlink_to(shutil.which('sha256sum'))
        os_release = self.directory / 'os-release'
        os_release.write_text('ID=anolis\nVERSION_ID=8.9\n')
        # 仅测试副本读取隔离发行信息，生产脚本不接受环境变量覆盖 OS 身份。
        self.bootstrap = self.bundle / 'bootstrap.sh'
        self.bootstrap.write_text(BOOTSTRAP.read_text().replace('/etc/os-release', str(os_release)))
        self.bootstrap.chmod(0o755)
        (self.bundle / 'target.txt').write_text('anolis\n8.9\narm64\n')
        (self.bundle / 'manifest.json').write_text('{}')
        (self.bundle / 'keys/vendor.asc').write_text('fixture public key')
        (self.bundle / 'rpms/python.rpm').write_text('fixture RPM')
        (self.bundle / 'rpms/repodata/repomd.xml').write_text('fixture repository')
        self.log = self.directory / 'commands.log'
        self.installed = self.directory / 'installed'
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + '/usr/bin:/bin', COMMAND_LOG=str(self.log), INSTALLED=str(self.installed), PYTHON_MISSING='0', PODMAN='0')
        self.stub('uname', 'case "$1" in -s) echo Linux;; -m) echo aarch64;; esac')
        self.stub('id', 'echo 0')
        self.stub('python3.11', 'if [ "${PYTHON_MISSING:-0}" = 1 ] && [ ! -f "$INSTALLED" ]; then exit 1; fi; [ "${1:-}" = --version ] && echo "Python 3.11.13"; exit 0')
        for name in ('python3.9', 'python3.10', 'python3.12', 'python3.13', 'python3'):
            self.stub(name, 'exit 1')
        self.stub('docker', 'case "${1:-}" in --version) echo "Docker version fixture";; *) exit 0;; esac')
        self.stub('ss', 'exit 0')
        self.stub('ps', 'exit 0')
        self.stub('rpm', '[ "${PODMAN:-0}" = 1 ] && [ "$*" = "-q podman" ]')
        self.stub('rpmkeys', 'printf "rpmkeys %s\\n" "$*" >> "$COMMAND_LOG"')
        self.stub('dnf', 'printf "dnf %s\\n" "$*" >> "$COMMAND_LOG"; touch "$INSTALLED"')
        self.checksums()

    def stub(self, name, body):
        file = self.bin / name
        file.write_text('#!/bin/bash\n' + body + '\n')
        file.chmod(0o755)

    def checksums(self):
        files = sorted(p for p in self.bundle.rglob('*') if p.is_file() and p.name != 'SHA256SUMS')
        (self.bundle / 'SHA256SUMS').write_text(''.join('%s  %s\n' % (hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(self.bundle)) for p in files))

    def run_script(self, *args):
        return subprocess.run(['bash', str(self.bootstrap), '--root', str(self.bundle), *args], env=self.env, text=True, errors='replace', capture_output=True)

    def test_healthy_dependencies_are_reused_without_package_mutations(self):
        result = self.run_script('--check-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())

    def test_missing_python_is_reported_in_check_mode(self):
        self.env['PYTHON_MISSING'] = '1'
        result = self.run_script('--check-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('python3.11', result.stderr)
        self.assertFalse(self.log.exists())

    def test_python_install_uses_only_local_signed_repo(self):
        self.env['PYTHON_MISSING'] = '1'
        result = self.run_script('--python-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.log.read_text()
        self.assertIn('--disablerepo=*', command)
        self.assertIn('file://', command)
        self.assertIn('gpgcheck=1', command)
        self.assertIn('install python3.11', command)
        self.assertNotIn('allowerasing', command)
        self.assertNotIn('https:', command)

    def test_os_or_arch_mismatch_fails_before_install(self):
        (self.bundle / 'target.txt').write_text('anolis\n8.10\namd64\n')
        self.checksums()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('包要求', result.stderr)
        self.assertFalse(self.log.exists())

    def test_corrupt_rpm_fails_before_install(self):
        (self.bundle / 'rpms/python.rpm').write_text('corrupt')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('文件损坏', result.stderr)
        self.assertFalse(self.log.exists())

    def test_existing_non_docker_runtime_is_not_replaced(self):
        self.stub('docker', 'echo "podman version fixture"')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不会替换', result.stderr)
        self.assertFalse(self.log.exists())

    def test_missing_docker_with_podman_is_not_erased(self):
        (self.bin / 'docker').unlink()
        self.env['PODMAN'] = '1'
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Podman/runc', result.stderr)
        self.assertFalse(self.log.exists())

    def test_check_only_does_not_start_or_probe_daemon(self):
        self.stub('docker', 'case "${1:-}" in --version) echo "Docker version fixture";; compose) exit 0;; info) touch "$COMMAND_LOG"; exit 99;; esac')
        result = self.run_script('--check-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())

    def test_symbolic_links_are_rejected_before_install(self):
        (self.bundle / 'rpms/link.rpm').symlink_to(self.bundle / 'rpms/python.rpm')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('包含链接', result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()

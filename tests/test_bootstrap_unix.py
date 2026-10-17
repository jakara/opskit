"""基础依赖引导行为：所有包管理器和服务调用均为隔离的替身。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / 'management/scripts/bootstrap.sh'


class UnixBootstrapTests(unittest.TestCase):
    def bash(self, source, directory, *args):
        return subprocess.run(
            ['bash', '-euo', 'pipefail', '-c', '. "$1"; shift; ' + source,
             'test', str(BOOTSTRAP), *args],
            cwd=directory, text=True, capture_output=True,
            env=dict(os.environ, TEST_ROOT=str(directory)))

    def test_standalone_modules_share_valid_bootstrap(self):
        self.assertEqual(BOOTSTRAP.read_bytes(), (ROOT / 'collector/scripts/bootstrap.sh').read_bytes())
        for path in (BOOTSTRAP, ROOT / 'management/install.sh', ROOT / 'collector/install.sh',
                     ROOT / 'management/scripts/check.sh', ROOT / 'collector/scripts/check.sh'):
            check = subprocess.run(['bash', '-n', str(path)], text=True, capture_output=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_check_wrappers_use_parallel_python_and_do_not_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for command in ('bash', 'dirname'):
                (folder / command).symlink_to(shutil.which(command))
            (folder / 'python3').write_text('#!/bin/sh\nexit 1\n')
            supported = folder / 'python3.9'
            supported.write_text('#!/bin/sh\nif [ "$1" = -c ]; then exit 0; fi\nprintf "%s\\n" "$@"\n')
            for path in (folder / 'python3', supported):
                path.chmod(0o755)
            for name in ('management', 'collector'):
                result = subprocess.run([str(ROOT / name / 'scripts/check.sh')], text=True, capture_output=True,
                                        env=dict(os.environ, PATH=str(folder)))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'scripts/check.py')
            supported.unlink()
            result = subprocess.run([str(ROOT / 'management/scripts/check.sh')], text=True, capture_output=True,
                                    env=dict(os.environ, PATH=str(folder)))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('install.sh 自动补齐依赖', result.stderr)

    def test_configure_only_installs_missing_python_without_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
opskit_find_python() { [[ -f "$TEST_ROOT/python-ready" ]]; }
uname() { printf 'Linux\\n'; }
opskit_install_python_linux() { printf 'python\\n' >> calls; touch python-ready; }
opskit_prepare_docker() { printf 'docker\\n' >> calls; return 1; }
opskit_bootstrap --configure-only --reconfigure
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'calls').read_text(), 'python\n')

    def test_offline_missing_dependencies_never_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
opskit_find_python() { return 1; }
opskit_install_python_linux() { touch forbidden; }
opskit_bootstrap --offline
''', tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('离线模式缺少 Python', result.stderr)
            self.assertFalse((Path(tmp) / 'forbidden').exists())
            result = self.bash('''
opskit_docker_ready() { return 1; }
docker() { return 1; }
opskit_install_docker_linux() { touch forbidden; }
OPSKIT_OFFLINE=1
opskit_prepare_docker Linux
''', tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('离线模式缺少 Docker', result.stderr)
            self.assertFalse((Path(tmp) / 'forbidden').exists())

    def test_healthy_docker_is_reused_without_package_or_service_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
docker() { printf '%s\\n' "$*" >> calls; }
opskit_install_docker_linux() { touch forbidden; }
opskit_start_docker_linux() { touch forbidden; }
OPSKIT_OFFLINE=0
opskit_prepare_docker Linux
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'calls').read_text().splitlines(), ['compose version', 'info'])
            self.assertFalse((Path(tmp) / 'forbidden').exists())

    def test_anolis_python_is_installed_beside_platform_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
opskit_linux_release() { OPSKIT_FAMILY=rhel; }
opskit_package_manager() { OPSKIT_PACKAGE_MANAGER=yum; }
opskit_find_python() { [[ -f "$TEST_ROOT/python-ready" ]]; }
opskit_privileged() {
  printf '%s\\n' "$*" >> calls
  if [[ "$*" == 'yum -y install python39' ]]; then touch python-ready; return 0; fi
  return 1
}
opskit_install_python_linux
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'calls').read_text().splitlines(), [
                'yum -y install python3.11', 'yum -y install python3.9', 'yum -y install python39'])

    def test_rhel_repository_uses_major_version_and_verified_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            target = folder / 'opskit-docker-ce.repo'
            target.write_text('original repository\n')
            result = self.bash('''
opskit_privileged() { "$@"; }
VERSION_ID=8.9
opskit_rhel_docker_repo "$TEST_ROOT"
opskit_rhel_docker_repo "$TEST_ROOT"
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = target.read_text()
            self.assertIn('https://download.docker.com/linux/rhel/8/$basearch/stable', content)
            self.assertIn('gpgcheck=1', content)
            backups = list(folder.glob('opskit-docker-ce.repo.opskit-backup-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'original repository\n')
            target.unlink()
            target.symlink_to(backups[0])
            result = self.bash('opskit_privileged() { "$@"; }; VERSION_ID=8.9; opskit_rhel_docker_repo "$TEST_ROOT"', tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('拒绝覆盖软链接', result.stderr)
            self.assertEqual(backups[0].read_text(), 'original repository\n')

    def test_compose_only_does_not_replace_engine_or_remove_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
opskit_linux_release() { OPSKIT_FAMILY=rhel; }
opskit_package_manager() { OPSKIT_PACKAGE_MANAGER=dnf; }
opskit_rhel_docker_repo() { printf 'repository\\n' >> calls; }
opskit_privileged() {
  printf '%s\\n' "$*" >> calls
  [[ -f second-attempt ]] && return 0
  touch second-attempt
  return 1
}
opskit_install_docker_linux yes
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'calls').read_text().splitlines(), [
                'dnf -y install docker-compose-plugin', 'repository', 'dnf -y install docker-compose-plugin'])

    def test_existing_stopped_docker_starts_without_reinstallation(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
docker() { [[ "$*" == 'compose version' || -f started ]]; }
opskit_local_engine() { return 0; }
opskit_start_docker_linux() { printf 'start\\n' >> calls; touch started; }
opskit_install_docker_linux() { touch forbidden; }
opskit_sudo_rerun() { return 1; }
OPSKIT_OFFLINE=0
opskit_prepare_docker Linux
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'calls').read_text(), 'start\n')
            self.assertFalse((Path(tmp) / 'forbidden').exists())

    def test_missing_docker_installs_then_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
command() { if [[ "${1:-}" == '-v' && "${2:-}" == docker && ! -f installed ]]; then return 1; fi; builtin command "$@"; }
docker() { [[ -f installed && ( "$*" == 'compose version' || -f started ) ]]; }
opskit_install_docker_linux() { printf 'install %s\\n' "$1" >> calls; touch installed; }
opskit_start_docker_linux() { printf 'start\\n' >> calls; touch started; }
opskit_sudo_rerun() { return 1; }
OPSKIT_OFFLINE=0
opskit_prepare_docker Linux
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'calls').read_text().splitlines(), ['install no', 'start'])

    def test_unavailable_compatible_client_does_not_replace_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
opskit_local_engine() { return 1; }
docker() { [[ "$*" == 'compose version' ]]; }
opskit_install_docker_linux() { touch forbidden; }
OPSKIT_OFFLINE=0
opskit_prepare_docker Linux
''', tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('脚本不会替换远程、Podman', result.stderr)
            self.assertFalse((Path(tmp) / 'forbidden').exists())

    def test_mac_requires_desktop_readiness_before_claiming_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
docker() { return 1; }
opskit_desktop_installed() { return 0; }
open() { return 0; }
sleep() { return 0; }
opskit_prepare_brew() { touch forbidden; }
OPSKIT_OFFLINE=0
opskit_prepare_docker Darwin
''', tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('首次启动、许可确认及系统授权', result.stderr)
            self.assertFalse((Path(tmp) / 'forbidden').exists())

    def test_mac_missing_docker_uses_homebrew_cask(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
command() { if [[ "${1:-}" == '-v' && "${2:-}" == docker && ! -f installed ]]; then return 1; fi; builtin command "$@"; }
docker() { [[ -f installed ]]; }
opskit_desktop_installed() { return 0; }
opskit_prepare_brew() { printf 'homebrew\\n' >> calls; }
brew() { printf 'brew %s\\n' "$*" >> calls; touch installed; }
open() { printf 'open %s\\n' "$*" >> calls; }
sleep() { return 0; }
OPSKIT_OFFLINE=0
opskit_prepare_docker Darwin
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'calls').read_text().splitlines(), [
                'homebrew', 'brew install --cask docker-desktop', 'open -a Docker'])

    def test_linux_installs_missing_http_discovery_dependency(self):
        for family, expected in [('rhel', ['dnf -y install iproute']),
                                 ('debian', ['apt-get update', 'apt-get install -y iproute2'])]:
            with self.subTest(family=family), tempfile.TemporaryDirectory() as tmp:
                result = self.bash('''
command() { if [[ "${1:-}" == '-v' && "${2:-}" == ss ]]; then [[ -f installed ]]; else builtin command "$@"; fi; }
FAMILY="$1"
opskit_linux_release() { OPSKIT_FAMILY="$FAMILY"; }
opskit_package_manager() { OPSKIT_PACKAGE_MANAGER=dnf; }
opskit_privileged() { printf '%s\\n' "$*" >> calls; [[ "$*" == 'apt-get update' ]] || touch installed; }
OPSKIT_OFFLINE=0
OPSKIT_MODULE=management
opskit_prepare_discovery Linux
''', tmp, family)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((Path(tmp) / 'calls').read_text().splitlines(), expected)

    def test_mac_installs_missing_lsof_without_modifying_system_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.bash('''
command() { if [[ "${1:-}" == '-v' && "${2:-}" == lsof ]]; then [[ -f installed ]]; else builtin command "$@"; fi; }
opskit_prepare_brew() { printf 'homebrew\\n' >> calls; }
brew() { printf 'brew %s\\n' "$*" >> calls; touch installed; }
OPSKIT_OFFLINE=0
OPSKIT_MODULE=management
opskit_prepare_discovery Darwin
''', tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'calls').read_text().splitlines(), ['homebrew', 'brew install lsof'])

    def test_discovery_dependency_is_skipped_for_collector_and_offline_never_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = '''
command() { if [[ "${1:-}" == '-v' && "${2:-}" == ss ]]; then return 1; else builtin command "$@"; fi; }
opskit_linux_release() { touch forbidden; }
OPSKIT_OFFLINE=1
OPSKIT_MODULE="$1"
opskit_prepare_discovery Linux
'''
            result = self.bash(source, tmp, 'collector')
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.bash(source, tmp, 'management')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('离线模式缺少 ss', result.stderr)
            self.assertFalse((Path(tmp) / 'forbidden').exists())


if __name__ == '__main__':
    unittest.main()

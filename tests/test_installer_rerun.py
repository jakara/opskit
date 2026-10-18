"""续装选择、成功后凭据回显及已有容器重建；仅使用临时配置与 Docker 查询样本。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_installer import ROOT, common, management


class InstallerRerunTests(unittest.TestCase):
    def test_existing_configuration_requires_choice_and_explicit_flags_skip_it(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as output:
            config = Path(folder) / '.env'
            config.touch()
            with patch('builtins.input', side_effect=['invalid', '2']):
                self.assertTrue(common.choose_configuration(config))
            self.assertIn('输入无效', output.getvalue())
            with patch('builtins.input', return_value='1'):
                self.assertFalse(common.choose_configuration(config))
            with patch('builtins.input', side_effect=EOFError), self.assertRaises(EOFError):
                common.choose_configuration(config)
            with patch('builtins.input', side_effect=AssertionError('不得再次询问')):
                self.assertTrue(common.choose_configuration(config, reconfigure=True))
                self.assertFalse(common.choose_configuration(config, reuse=True))
                self.assertFalse(common.choose_configuration(config, draft={'OPS_PUBLIC_HOST': 'saved.internal'}))

    def test_running_containers_are_recreated_only_for_exact_directory_and_compose_files(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(common, 'ROOT', Path(folder)), \
                patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            owned = {'com.docker.compose.project.working_dir': str(root),
                     'com.docker.compose.project.config_files': str(root / 'compose.yaml')}
            for directory, files, running, allowed in (
                    (str(root), str(root / 'compose.yaml'), True, True),
                    (str(root), str(root / 'compose.yaml'), False, True),
                    (str(root / 'other'), str(root / 'compose.yaml'), True, False),
                    (str(root), str(root / 'hub.compose.yaml'), True, False)):
                labels = dict(owned, **{'com.docker.compose.project.working_dir': directory,
                                       'com.docker.compose.project.config_files': files})
                results = [SimpleNamespace(stdout='container-id\n'),
                           SimpleNamespace(stdout=json.dumps({'Labels': labels, 'Running': running}))]
                with patch.object(common, 'run', side_effect=results) as run:
                    if allowed:
                        command = common.compose_start_command({})
                        self.assertEqual('--force-recreate' in command, running)
                        self.assertIn('never', command)
                    else:
                        with self.assertRaisesRegex(RuntimeError, '同名 Compose 项目'):
                            common.compose_start_command({})
                    self.assertFalse(any('up' in call.args[0] for call in run.call_args_list))
            with patch.object(common, 'run', return_value=SimpleNamespace(stdout='')):
                self.assertNotIn('--force-recreate', common.compose_start_command({}))

    def test_hub_and_legacy_compose_files_use_the_actual_scope(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(common, 'ROOT', Path(folder)), \
                patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            for files in (['hub.compose.yaml'], ['compose.yaml', 'legacy.compose.yaml']):
                labels = {'com.docker.compose.project.working_dir': str(root),
                          'com.docker.compose.project.config_files': ','.join(str(root / f) for f in files)}
                with patch.object(common, 'run', side_effect=[SimpleNamespace(stdout='id'), SimpleNamespace(
                        stdout=json.dumps({'Labels': labels, 'Running': True}))]):
                    command = common.compose_start_command({'COMPOSE_FILE': os.pathsep.join(files)}, files=files)
                self.assertIn('--force-recreate', command)
            labels['com.docker.compose.project.config_files'] = str(root / 'compose.yaml')
            with patch.object(common, 'run', side_effect=[SimpleNamespace(stdout='id'), SimpleNamespace(
                    stdout=json.dumps({'Labels': labels, 'Running': True}))]):
                self.assertIn('--force-recreate', common.compose_start_command({'COMPOSE_FILE': os.pathsep.join(files)}))

    def test_credentials_only_appear_once_after_all_startup_checks_pass(self):
        with contextlib.redirect_stdout(io.StringIO()):
            values = management.configure({}, interactive=False)
        for failure in (None, 'pull', 'check'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                output = io.StringIO()
                events = []

                def prepare(*args, **kwargs):
                    self.assertNotIn(values['NPM_ADMIN_PASSWORD'], output.getvalue())
                    events.append('pull')
                    if failure == 'pull':
                        raise RuntimeError('pull failed')

                def check(v):
                    self.assertNotIn(v['NPM_ADMIN_PASSWORD'], output.getvalue())
                    events.append('check')
                    if failure == 'check':
                        raise RuntimeError('check failed')

                startup = ['docker', 'compose', 'up', '-d', '--pull', 'never', '--force-recreate']
                with patch.object(management, 'ROOT', Path(folder)), patch.object(management, 'configure', return_value=values), \
                        patch.object(management, 'docker_ready'), patch.object(management, 'start_discovery'), \
                        patch.object(management, 'installation_control', return_value=contextlib.nullcontext({})), \
                        patch.object(management, 'compose_start_command', return_value=startup), \
                        patch.object(management, 'prepare_images', side_effect=prepare), \
                        patch.object(management, 'run', side_effect=lambda args: events.append(args)), \
                        patch.dict(sys.modules, {'configure_proxy': SimpleNamespace(initialize=Mock()),
                                                 'check': SimpleNamespace(check=check),
                                                 'verify_ui': SimpleNamespace(verify=Mock()),
                                                 'self_management': SimpleNamespace(initialize=Mock())}), \
                        patch.object(sys, 'argv', ['install.py']), contextlib.redirect_stdout(output):
                    if failure:
                        with self.assertRaises(RuntimeError):
                            management.main()
                    else:
                        management.main()
                for key in management.PASSWORD_KEYS:
                    self.assertEqual(output.getvalue().count(values[key]), int(failure is None and key != 'SPUG_DB_PASSWORD'))
                if not failure:
                    self.assertLess(events.index('pull'), events.index(startup))
                    self.assertLess(events.index(startup), events.index('check'))

    def test_hub_only_displays_its_own_login(self):
        with contextlib.redirect_stdout(io.StringIO()):
            values = management.configure({}, interactive=False)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            management.show_credentials(values, ROOT / 'management/.workbench/hub.env', hub_only=True)
        self.assertEqual(output.getvalue().count(values['NPM_ADMIN_PASSWORD']), 1)
        for key in management.PASSWORD_KEYS[1:]:
            self.assertNotIn(values[key], output.getvalue())


class InstallationControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.values = management.configure({}, interactive=False)
        self.env = self.root / '.env'
        self.env.write_text(common.env_text(self.values))
        self.directory = self.root / '.workbench/control'
        self.recovery = self.directory / 'recovery/dbx.json'
        self.recovery.parent.mkdir(parents=True)
        self.recovery.write_text(json.dumps({'service': 'dbx', 'stage': 'applying'}))
        self.alias = 'example/dbx:opskit-rollback-test'
        self.events = []
        self.unit = self.root / 'opskit-discovery.service'
        self.unit.write_text('ExecStart=python3 ' + str(self.root / 'portal/discovery.py'))
        self.controller = SimpleNamespace(directory=self.directory, recover=Mock(side_effect=self.recover))
        self.uninstall = SimpleNamespace(
            service_file=lambda root: self.unit,
            admin=Mock(side_effect=lambda *args: self.events.append(('systemd-stop', args))),
            stop_process=Mock(side_effect=lambda *args: self.events.append(('process-stop', args))))
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(management, 'ROOT', self.root))
        self.patches.enter_context(patch.object(sys, 'platform', 'linux'))
        self.patches.enter_context(patch.dict(sys.modules, {
            'uninstall': self.uninstall,
            'service_control': SimpleNamespace(Controller=lambda: self.controller, SERVICES={'dbx': 'DBX_IMAGE'})}))

    def assert_locked(self):
        with contextlib.closing(sqlite3.connect(self.directory / 'agent.lock', timeout=0)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('BEGIN EXCLUSIVE')

    def assert_unlocked(self):
        with contextlib.closing(sqlite3.connect(self.directory / 'agent.lock', timeout=0)) as connection:
            connection.execute('BEGIN EXCLUSIVE')

    def recover(self, state, check_health):
        self.assertEqual(state['service'], 'dbx')
        self.assertFalse(check_health)
        self.assertEqual([event[0] for event in self.events], ['systemd-stop', 'process-stop'])
        self.assert_locked()
        self.events.append(('recover',))
        self.env.write_text(common.env_text(dict(self.values, DBX_IMAGE=self.alias)))
        self.recovery.unlink()

    def test_recovery_stops_discovery_before_taking_lock_and_yields_exact_restored_image(self):
        with management.installation_control() as recovered:
            self.assertEqual(recovered, {'DBX_IMAGE': self.alias})
            self.assert_locked()
        self.assert_unlocked()
        self.uninstall.admin.assert_called_once_with('systemctl', 'stop', self.unit.name)
        self.uninstall.stop_process.assert_called_once_with(self.root, self.root / '.workbench/discovery', 'discovery.py')
        self.assertEqual([event[0] for event in self.events], ['systemd-stop', 'process-stop', 'recover'])

    def test_complete_startup_keeps_lock_through_proxy_then_restarts_discovery(self):
        startup = ['docker', 'compose', 'up', '-d']
        def locked_event(name, values=None):
            self.assert_locked()
            if values is not None:
                self.assertEqual(values['DBX_IMAGE'], self.alias)
            self.events.append((name,))
        def run(args):
            locked_event('compose-up' if args == startup else 'compose-config')
        def start_discovery(**kwargs):
            self.assertEqual(kwargs, {'control': True})
            self.assert_unlocked()
            self.events.append(('discovery-start',))
        def initialize(values):
            self.assert_unlocked()
            self.assertEqual(self.events[-1][0], 'discovery-start')
            self.assertEqual(values['DBX_IMAGE'], self.alias)
            self.events.append(('self-management',))
        with patch.object(management, 'choose_configuration', return_value=False), \
                patch.object(management, 'configure', return_value=self.values.copy()), \
                patch.object(management, 'docker_ready'), \
                patch.object(management, 'write_outputs', side_effect=lambda values: locked_event('write-config', values)), \
                patch.object(management, 'compose_start_command', side_effect=lambda values: locked_event('startup-plan', values) or startup), \
                patch.object(management, 'prepare_images', side_effect=lambda values, *args, **kwargs: locked_event('prepare-images', values)), \
                patch.object(management, 'run', side_effect=run), \
                patch.object(management, 'start_discovery', side_effect=start_discovery), \
                patch.object(management, 'show_credentials'), \
                patch.dict(sys.modules, {
                    'configure_proxy': SimpleNamespace(initialize=lambda values: locked_event('proxy', values)),
                    'verify_ui': SimpleNamespace(verify=lambda values, **kwargs: locked_event('verify-ui', values)),
                    'self_management': SimpleNamespace(initialize=initialize),
                    'check': SimpleNamespace(check=lambda values: self.events.append(('check',)))}), \
                patch.object(sys, 'argv', ['install.py', '--reuse-config']), contextlib.redirect_stdout(io.StringIO()):
            management.main()
        self.assertEqual([event[0] for event in self.events], [
            'systemd-stop', 'process-stop', 'recover', 'write-config', 'compose-config', 'startup-plan',
            'prepare-images', 'verify-ui', 'compose-up', 'proxy', 'discovery-start', 'self-management', 'check'])

    def test_recovery_failure_blocks_configuration_startup_and_discovery_restart(self):
        original = self.env.read_bytes()
        self.controller.recover.side_effect = RuntimeError('recovery incomplete')
        with patch.object(management, 'choose_configuration', return_value=False), \
                patch.object(management, 'configure', return_value=self.values.copy()), \
                patch.object(management, 'docker_ready'), patch.object(management, 'write_outputs') as write, \
                patch.object(management, 'run') as run, patch.object(management, 'prepare_images') as prepare, \
                patch.object(management, 'start_discovery') as discovery, \
                patch.dict(sys.modules, {'configure_proxy': SimpleNamespace(initialize=Mock()),
                                         'self_management': SimpleNamespace(initialize=Mock())}), \
                patch.object(sys, 'argv', ['install.py', '--reuse-config']), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, 'recovery incomplete'):
            management.main()
        write.assert_not_called()
        run.assert_not_called()
        prepare.assert_not_called()
        discovery.assert_not_called()
        self.assertEqual(self.env.read_bytes(), original)
        self.assertTrue(self.recovery.exists())
        self.assert_unlocked()

    def test_busy_control_lock_never_recovers_or_enters_installation_body(self):
        entered = False
        with contextlib.closing(sqlite3.connect(self.directory / 'agent.lock', timeout=0)) as operation:
            operation.execute('BEGIN EXCLUSIVE')
            with self.assertRaisesRegex(RuntimeError, '尚未结束'):
                with management.installation_control():
                    entered = True
        self.assertFalse(entered)
        self.controller.recover.assert_not_called()
        self.assertTrue(self.recovery.exists())


if __name__ == '__main__':
    unittest.main()

"""续装选择、成功后凭据回显及已有容器重建；仅使用临时配置与 Docker 查询样本。"""
import contextlib
import io
import json
import os
from pathlib import Path
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
                        patch.object(management, 'compose_start_command', return_value=startup), \
                        patch.object(management, 'prepare_images', side_effect=prepare), \
                        patch.object(management, 'run', side_effect=lambda args: events.append(args)), \
                        patch.dict(sys.modules, {'configure_proxy': SimpleNamespace(initialize=Mock()),
                                                 'check': SimpleNamespace(check=check)}), \
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


if __name__ == '__main__':
    unittest.main()

"""受限服务操作的真实文件队列、所有权边界和升级恢复；不运行 Docker。"""
import ast
import contextlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_installer import ROOT, module

control = module('service_control_tests', ROOT / 'management/scripts/service_control.py')
client = module('control_client_tests', ROOT / 'management/scripts/control_client.py')


class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(control, 'ROOT', self.root).start()
        patch.dict(os.environ, {'DOCKER_HOST': 'unix:///test/docker.sock', 'DOCKER_CONTEXT': ''}).start()
        self.values = {'DBX_IMAGE': 'registry.example:5000/team/dbx:old',
                       'VICTORIALOGS_IMAGE': 'registry.example/team/logs:old',
                       'WORKBENCH_IMAGE': 'python:3.12-alpine', 'DBX_PASSWORD': 'PRIVATE_DBX_PASSWORD',
                       'OPS_ADMIN_PASSWORD': 'PRIVATE_ADMIN_PASSWORD'}
        self.env = self.root / '.env'
        self.env.write_text(control.env_text(self.values))
        self.controller = control.Controller(self.root / '.workbench/control')
        self.info = self.container('dbx')

    def container(self, service):
        return {'Id': service + '-container-id', 'Image': 'sha256:old-image',
                'reference': self.values.get(control.SERVICES.get(service), 'spug:4.1'),
                'Labels': {'com.docker.compose.project.working_dir': str(self.root),
                           'com.docker.compose.project.config_files': str(self.root / 'compose.yaml'),
                           'com.docker.compose.service': service},
                'Running': True, 'Mounts': [{'Type': 'volume', 'RW': True, 'Name': 'test_dbx', 'Destination': '/data'}]}

    def request(self, **changes):
        return dict({'id': 'a' * 32, 'action': 'start', 'service': 'dbx', 'created_at': time.time()}, **changes)

    def archive(self, folder, name='data.tar', member_name='db.sqlite', kind=tarfile.REGTYPE):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with tarfile.open(path, 'w') as stream:
            member = tarfile.TarInfo(member_name)
            member.type = kind
            if member.isfile():
                member.size = 8
                stream.addfile(member, io.BytesIO(b'old-data'))
            else:
                member.linkname = '../outside'
                stream.addfile(member)
        return path

    def backup(self, folder):
        archive = self.archive(folder)
        return [{'volume': 'test_dbx', 'archive': archive.name, 'sha256': control.digest(archive)}]

    def image_command(self, args, **kwargs):
        if args[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([{'Os': 'linux', 'Architecture': 'amd64'}]).encode()
        return b''

    def process_without_blocking(self, path):
        errors = []
        def run():
            try:
                self.controller.process(path)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), 'an invalid queue entry blocked the consumer')
        self.assertEqual(errors, [])

    def test_request_allowlist_shape_and_age_are_rejected_before_docker(self):
        invalid = [None, [], 'request', self.request(id='../outside'), self.request(action='exec'),
                   self.request(service='spug'), self.request(command='id'), self.request(created_at='now'),
                   self.request(created_at=time.time() - 301), self.request(created_at=time.time() + 30),
                   self.request(created_at=float('nan')), self.request(image='image:tag'),
                   self.request(action=['start']), self.request(service={'name': 'dbx'})]
        with patch.object(self.controller, 'owned') as owned:
            for request in invalid:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    self.controller.handle(request)
            owned.assert_not_called()
        self.assertFalse((self.root / '.backups/service-control').exists())

    def test_start_stop_restart_verify_the_resulting_state(self):
        for index, action in enumerate(('start', 'stop', 'restart')):
            expected = action != 'stop'
            with self.subTest(action=action), patch.object(self.controller, 'owned', side_effect=[self.info, dict(self.info, Running=expected)]), \
                    patch.object(self.controller, 'compose') as compose, patch.object(self.controller, 'healthy') as healthy:
                result = self.controller.handle(self.request(id=f'{index:032x}', action=action))
                self.assertTrue(result['ok'])
                self.assertEqual(result['running'], expected)
                compose.assert_called_once_with(action, 'dbx', timeout=90)
                self.assertEqual(healthy.call_count, int(expected))

    def test_wrong_final_state_fails_instead_of_reporting_success(self):
        with patch.object(self.controller, 'owned', return_value=dict(self.info, Running=True)), \
                patch.object(self.controller, 'compose'), self.assertRaisesRegex(RuntimeError, '状态'):
            self.controller.handle(self.request(action='stop'))

    def test_malformed_json_and_identifier_mismatch_do_not_poison_the_next_request(self):
        bad = ('{', 'null', '[]', 'true', '"text"', json.dumps(self.request(id='b' * 32)), 'x' * 4097)
        with patch.object(self.controller, 'handle', return_value={'ok': True}) as handle:
            for index, content in enumerate(bad):
                path = self.controller.directory / 'requests' / f'{index:032x}.json'
                path.write_text(content)
                self.process_without_blocking(path)
                result = json.loads((self.controller.directory / 'results' / path.name).read_text())
                self.assertFalse(result['ok'])
                self.assertEqual(result['id'], path.stem)
                self.assertFalse(path.exists())
            valid = self.request()
            path = self.controller.directory / 'requests' / (valid['id'] + '.json')
            path.write_text(json.dumps(valid))
            self.process_without_blocking(path)
            handle.assert_called_once_with(valid)
            self.assertTrue(json.loads((self.controller.directory / 'results' / path.name).read_text())['ok'])
        self.assertIsNone(self.controller.current)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires a POSIX named pipe')
    def test_fifo_and_symlink_requests_never_open_their_targets_or_block(self):
        target = self.root / 'private-file'
        target.write_text('PRIVATE_OUTSIDE_CONTENT')
        paths = [self.controller.directory / 'requests' / (character * 32 + '.json') for character in ('b', 'c', 'd')]
        os.mkfifo(paths[0])
        paths[1].symlink_to(target)
        paths[2].symlink_to(self.root / 'missing-target')
        with patch.object(self.controller, 'handle') as handle:
            for path in paths:
                self.process_without_blocking(path)
                result = json.loads((self.controller.directory / 'results' / path.name).read_text())
                self.assertFalse(result['ok'])
                self.assertNotIn('PRIVATE_OUTSIDE_CONTENT', json.dumps(result))
            handle.assert_not_called()
        self.assertEqual(target.read_text(), 'PRIVATE_OUTSIDE_CONTENT')

    def test_completed_request_is_not_replayed(self):
        request = self.request()
        path = self.controller.directory / 'requests' / (request['id'] + '.json')
        path.write_text(json.dumps(request))
        output = self.controller.directory / 'results' / path.name
        output.write_text('{"ok":true,"original":true}')
        with patch.object(self.controller, 'handle') as handle:
            self.controller.process(path)
            handle.assert_not_called()
        self.assertEqual(json.loads(output.read_text()), {'ok': True, 'original': True})
        self.assertFalse(path.exists())

    def test_only_local_docker_contexts_and_all_installation_labels_are_accepted(self):
        with patch.object(self.controller, 'compose', return_value=b'dbx-container-id') as compose, \
                patch.object(self.controller, 'command', return_value=json.dumps(self.info).encode()) as command:
            self.assertEqual(self.controller.owned('dbx'), self.info)
            for key, value in (('com.docker.compose.project.working_dir', '/other'),
                               ('com.docker.compose.project.config_files', str(self.root / 'other.yaml')),
                               ('com.docker.compose.service', 'victorialogs')):
                item = dict(self.info, Labels=dict(self.info['Labels'], **{key: value}))
                command.return_value = json.dumps(item).encode()
                with self.subTest(label=key), self.assertRaisesRegex(RuntimeError, '不属于'):
                    self.controller.owned('dbx')
            compose.reset_mock()
            with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://remote:2376', 'DOCKER_CONTEXT': ''}), \
                    self.assertRaisesRegex(RuntimeError, '本机'):
                self.controller.owned('dbx')
            compose.assert_not_called()
            command.return_value = b'ssh://remote'
            with patch.dict(os.environ, {'DOCKER_HOST': 'unix:///local.sock', 'DOCKER_CONTEXT': 'remote'}), \
                    self.assertRaisesRegex(RuntimeError, '本机'):
                self.controller.owned('dbx')

    def test_compose_file_allowlist_ignores_inherited_compose_overrides(self):
        self.values['COMPOSE_FILE'] = os.pathsep.join(('compose.yaml', 'legacy.compose.yaml'))
        self.env.write_text(control.env_text(self.values))
        with patch.dict(os.environ, {'COMPOSE_FILE': '/foreign.yaml', 'COMPOSE_PROJECT_NAME': 'foreign'}), \
                patch.object(self.controller, 'command', return_value=b'') as command:
            self.controller.compose('start', 'dbx')
        args = command.call_args.args[0]
        self.assertNotIn('/foreign.yaml', args)
        self.assertEqual(args[-2:], ['start', 'dbx'])
        self.assertFalse(any(key.startswith('COMPOSE_') for key in command.call_args.kwargs['env']))
        self.values['COMPOSE_FILE'] = '../foreign.yaml'
        self.env.write_text(control.env_text(self.values))
        with patch.object(self.controller, 'command') as command, self.assertRaises(RuntimeError):
            self.controller.compose('start', 'dbx')
        command.assert_not_called()

    def test_upgrade_rejects_foreign_repository_and_shell_text_before_docker(self):
        folder = self.root / 'upgrade'
        folder.mkdir()
        for image in ('foreign.example/dbx:new', self.values['DBX_IMAGE'] + ';touch /tmp/unwanted', ''):
            with self.subTest(image=image), patch.object(self.controller, 'command') as command, self.assertRaises(RuntimeError):
                self.controller.upgrade(self.request(action='upgrade', image=image), self.info, self.values.copy(), folder)
            command.assert_not_called()
        self.assertEqual(control.repository('registry.example:5000/team/dbx@sha256:abc'), 'registry.example:5000/team/dbx')

    def test_snapshot_verifies_archive_and_rejects_shared_volumes(self):
        source = self.archive(self.root / 'source')
        def command(args, **kwargs):
            if args[:2] == ['docker', 'ps']:
                return self.info['Id'].encode()
            kwargs['stdout'].write(source.read_bytes())
            return b''
        folder = self.root / 'snapshot'
        folder.mkdir()
        with patch.object(self.controller, 'command', side_effect=command):
            records = self.controller.snapshot(self.info, folder)
        self.assertEqual(len(records), 1)
        archive = folder / records[0]['archive']
        self.assertEqual(archive.read_bytes(), source.read_bytes())
        self.assertEqual(records[0]['sha256'], control.digest(archive))
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        shared = self.root / 'shared'
        shared.mkdir()
        with patch.object(self.controller, 'command', return_value=b'foreign-container') as run, \
                self.assertRaisesRegex(RuntimeError, '其他容器'):
            self.controller.snapshot(self.info, shared)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(list(shared.iterdir()), [])

    def test_offline_upgrade_uses_only_present_images_and_keeps_verified_backup(self):
        folder = self.root / 'upgrade'
        records = self.backup(folder)
        values = dict(self.values, OPSKIT_CONTROL_OFFLINE='true')
        image = 'registry.example:5000/team/dbx:new'
        original = self.env.read_bytes()
        with patch.object(self.controller, 'command', side_effect=self.image_command) as command, \
                patch.object(self.controller, 'snapshot', return_value=records), \
                patch.object(self.controller, 'compose') as compose, patch.object(self.controller, 'healthy'):
            result = self.controller.upgrade(self.request(action='upgrade', image=image), self.info, values, folder)
        self.assertTrue(result['ok'])
        self.assertFalse(any(call.args[0][:2] == ['docker', 'pull'] for call in command.call_args_list))
        self.assertEqual((folder / 'previous.env').read_bytes(), original)
        self.assertEqual(json.loads((folder / 'manifest.json').read_text())['volumes'], records)
        self.assertEqual(control.load_env(self.env)['DBX_IMAGE'], image)
        self.assertIn('--pull', compose.call_args_list[-1].args)
        self.assertIn('never', compose.call_args_list[-1].args)
        self.assertFalse((self.controller.directory / 'recovery/dbx.json').exists())

    def test_offline_missing_image_fails_before_stopping_or_changing_configuration(self):
        folder = self.root / 'upgrade'
        folder.mkdir()
        original = self.env.read_bytes()
        with patch.object(self.controller, 'command', side_effect=RuntimeError('missing local image')) as command, \
                patch.object(self.controller, 'compose') as compose, self.assertRaises(RuntimeError):
            self.controller.upgrade(self.request(action='upgrade', image='registry.example:5000/team/dbx:new'),
                                    self.info, dict(self.values, OPSKIT_CONTROL_OFFLINE='true'), folder)
        self.assertEqual(command.call_args.args[0][:3], ['docker', 'image', 'inspect'])
        compose.assert_not_called()
        self.assertEqual(self.env.read_bytes(), original)

    def test_upgrade_failure_restores_data_and_exact_old_image_before_unlocking(self):
        folder = self.root / 'upgrade'
        records = self.backup(folder)
        image = 'registry.example:5000/team/dbx:new'
        with patch.object(self.controller, 'command', side_effect=self.image_command) as command, \
                patch.object(self.controller, 'snapshot', return_value=records), patch.object(self.controller, 'owned', return_value=self.info), \
                patch.object(self.controller, 'compose') as compose, \
                patch.object(self.controller, 'healthy', side_effect=[RuntimeError('PRIVATE_UPGRADE_DETAIL'), None]), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '已恢复原服务') as raised:
            self.controller.upgrade(self.request(action='upgrade', image=image), self.info, self.values.copy(), folder)
        restored_image = control.load_env(self.env)['DBX_IMAGE']
        self.assertIn(':opskit-rollback-', restored_image)
        recreations = [call for call in compose.call_args_list if call.args[0] == 'up']
        self.assertEqual([call.kwargs['overrides']['DBX_IMAGE'] for call in recreations], [image, restored_image])
        self.assertTrue(any(call.args[0][:2] == ['docker', 'run'] and call.kwargs.get('stdin') for call in command.call_args_list))
        self.assertFalse((self.controller.directory / 'recovery/dbx.json').exists())
        self.assertNotIn('PRIVATE_UPGRADE_DETAIL', str(raised.exception))

    def test_failed_recovery_keeps_service_locked_and_preserves_backup(self):
        folder = self.root / 'upgrade'
        records = self.backup(folder)
        with patch.object(self.controller, 'command', side_effect=self.image_command), \
                patch.object(self.controller, 'snapshot', return_value=records), patch.object(self.controller, 'owned', return_value=self.info), \
                patch.object(self.controller, 'compose'), patch.object(self.controller, 'healthy', side_effect=RuntimeError('failure')), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '已锁定'):
            self.controller.upgrade(self.request(action='upgrade', image='registry.example:5000/team/dbx:new'), self.info, self.values.copy(), folder)
        self.assertTrue((self.controller.directory / 'recovery/dbx.json').exists())
        self.assertEqual(control.digest(folder / records[0]['archive']), records[0]['sha256'])
        with patch.object(self.controller, 'owned') as owned, self.assertRaisesRegex(RuntimeError, '已锁定'):
            self.controller.handle(self.request(id='b' * 32))
        owned.assert_not_called()

    def test_missing_container_can_recover_only_through_owned_peer(self):
        folder = self.root / 'upgrade'
        state = {'service': 'dbx', 'stage': 'applying', 'was_running': True,
                 'alias': 'registry.example:5000/team/dbx:rollback', 'helper': self.values['WORKBENCH_IMAGE'],
                 'folder': str(folder), 'volumes': self.backup(folder)}
        recovery = self.controller.directory / 'recovery/dbx.json'
        control.write_json(recovery, state)
        peer = self.container('spug')
        def command(args, **kwargs):
            if args[:2] == ['docker', 'inspect']:
                return json.dumps(peer).encode()
            return self.image_command(args, **kwargs)
        def compose(*args, **kwargs):
            return b'spug-container-id' if args == ('ps', '--all', '--quiet', 'spug') else b''
        with patch.object(self.controller, 'command', side_effect=command), \
                patch.object(self.controller, 'compose', side_effect=compose) as compose_mock, \
                patch.object(self.controller, 'healthy'), contextlib.redirect_stdout(io.StringIO()):
            self.controller.recover(state)
        self.assertFalse(recovery.exists())
        self.assertTrue(any(call.args[0] == 'up' for call in compose_mock.call_args_list))
        self.assertEqual(control.load_env(self.env)['DBX_IMAGE'], state['alias'])

    def test_missing_container_with_foreign_peer_keeps_recovery_locked(self):
        state = {'service': 'dbx', 'stage': 'stopping', 'was_running': True}
        recovery = self.controller.directory / 'recovery/dbx.json'
        control.write_json(recovery, state)
        peer = self.container('spug')
        peer['Labels']['com.docker.compose.project.working_dir'] = '/foreign'
        def compose(*args, **kwargs):
            return b'spug-container-id' if args == ('ps', '--all', '--quiet', 'spug') else b''
        with patch.object(self.controller, 'compose', side_effect=compose) as compose_mock, \
                patch.object(self.controller, 'command', return_value=json.dumps(peer).encode()), \
                self.assertRaisesRegex(RuntimeError, '不属于'):
            self.controller.recover(state)
        self.assertTrue(recovery.exists())
        self.assertFalse(any(call.args[0] in ('up', 'start') for call in compose_mock.call_args_list))

    def test_corrupted_backup_cannot_be_restored_and_keeps_recovery_marker(self):
        folder = self.root / 'upgrade'
        records = self.backup(folder)
        (folder / records[0]['archive']).write_bytes(b'corrupted')
        state = {'service': 'dbx', 'stage': 'applying', 'was_running': True, 'alias': 'old-image',
                 'folder': str(folder), 'volumes': records, 'helper': 'python:3.12-alpine'}
        recovery = self.controller.directory / 'recovery/dbx.json'
        control.write_json(recovery, state)
        with patch.object(self.controller, 'owned', return_value=self.info), patch.object(self.controller, 'command'), \
                patch.object(self.controller, 'compose') as compose, self.assertRaisesRegex(RuntimeError, '备份校验失败'):
            self.controller.recover(state)
        compose.assert_not_called()
        self.assertTrue(recovery.exists())

    def test_archive_validation_rejects_links_special_files_and_escaping_paths(self):
        namespace = {'PurePosixPath': PurePosixPath}
        function = next(node for node in ast.parse(control.RESTORE).body if isinstance(node, ast.FunctionDef) and node.name == 'safe')
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<restore-filter>', 'exec'), namespace)
        invalid = [('../outside', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE), ('folder\\child', tarfile.REGTYPE),
                   ('symlink', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE), ('pipe', tarfile.FIFOTYPE)]
        for index, (name, kind) in enumerate(invalid):
            path = self.archive(self.root / 'archives', name=f'{index}.tar', member_name=name, kind=kind)
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                control.validate_archive(path)
            member = tarfile.TarInfo(name)
            member.type = kind
            with self.subTest(restore_name=name), self.assertRaises(ValueError):
                namespace['safe'](member, '/restore')
        control.validate_archive(self.archive(self.root / 'archives', name='safe.tar'))

    def test_restore_helper_cleanup_never_removes_a_foreign_container(self):
        labels = {'opskit.restore.root': str(self.root), 'opskit.restore.volume': 'test_dbx'}
        with patch.object(self.controller, 'command', side_effect=[b'helper-id', b'{"opskit.restore.root":"/other"}']) as command, \
                self.assertRaisesRegex(RuntimeError, '其他容器'):
            self.controller.clean_helper('helper', labels)
        self.assertFalse(any(call.args[0][:2] == ['docker', 'rm'] for call in command.call_args_list))
        with patch.object(self.controller, 'command', side_effect=[b'helper-id', json.dumps(labels).encode(), b'', b'']) as command:
            self.controller.clean_helper('helper', labels)
        self.assertIn(['docker', 'rm', '-f', 'helper-id'], [call.args[0] for call in command.call_args_list])

    def test_docker_errors_and_private_diagnostics_redact_configured_passwords(self):
        self.controller.log = self.root / 'docker.jsonl'
        self.controller.log.touch(mode=0o600)
        detail = b'PRIVATE_DBX_PASSWORD and PRIVATE_ADMIN_PASSWORD: unauthorized'
        with patch.object(control.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=b'', stderr=detail)), \
                self.assertRaises(RuntimeError) as raised:
            self.controller.command(['docker', 'image', 'inspect', 'image:tag'])
        content = self.controller.log.read_text() + str(raised.exception)
        self.assertNotIn('PRIVATE_DBX_PASSWORD', content)
        self.assertNotIn('PRIVATE_ADMIN_PASSWORD', content)
        self.assertIn('[已隐藏]', content)


class ControlClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for name in ('requests', 'results', 'status'):
            (self.directory / name).mkdir()
        (self.directory / 'status/agent.json').write_text(json.dumps({'updated_at': time.time()}))
        self.identifier = 'c' * 32

    def test_allowlist_and_stale_agent_fail_before_creating_requests(self):
        for action, service in (('exec', 'dbx'), ('start', 'spug')):
            with self.subTest(action=action, service=service), self.assertRaises(ValueError):
                client.execute(action, service, directory=self.directory)
        (self.directory / 'status/agent.json').write_text(json.dumps({'updated_at': time.time() - 21}))
        with self.assertRaisesRegex(RuntimeError, '未运行'):
            client.execute('start', 'dbx', directory=self.directory)
        self.assertEqual(list((self.directory / 'requests').iterdir()), [])

    def test_client_waits_for_matching_real_result_and_returns_failure_code(self):
        for ok, code in ((True, 0), (False, 1)):
            def complete(_):
                requests = list((self.directory / 'requests').glob('*.json'))
                self.assertEqual(len(requests), 1)
                request = json.loads(requests[0].read_text())
                self.assertEqual(set(request), {'id', 'action', 'service', 'created_at', 'image'})
                (self.directory / 'results' / requests[0].name).write_text(json.dumps(dict(request, ok=ok)))
            result = self.directory / 'results' / (self.identifier + '.json')
            result.unlink(missing_ok=True)
            with patch.object(client, 'uuid4', return_value=SimpleNamespace(hex=self.identifier)), \
                    patch.object(client.time, 'sleep', side_effect=complete), contextlib.redirect_stdout(io.StringIO()):
                actual = client.execute('upgrade', 'dbx', 'example/dbx:new', directory=self.directory, timeout=5)
            self.assertEqual(actual, code)
            self.assertFalse(list((self.directory / 'requests').glob('*.tmp')))
            (self.directory / 'requests' / (self.identifier + '.json')).unlink()

    def test_client_rejects_mismatched_results_and_timeout_is_not_success(self):
        path = self.directory / 'results' / (self.identifier + '.json')
        path.write_text(json.dumps({'id': self.identifier, 'service': 'victorialogs', 'action': 'start', 'ok': True}))
        with patch.object(client, 'uuid4', return_value=SimpleNamespace(hex=self.identifier)), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '标识不符'):
            client.execute('start', 'dbx', directory=self.directory, timeout=1)
        path.unlink()
        (self.directory / 'requests' / (self.identifier + '.json')).unlink()
        with patch.object(client, 'uuid4', return_value=SimpleNamespace(hex=self.identifier)), \
                patch.object(client.time, 'monotonic', side_effect=[0, 2]), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '超时'):
            client.execute('start', 'dbx', directory=self.directory, timeout=1)


if __name__ == '__main__':
    unittest.main()

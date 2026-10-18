"""卸载边界、备份和失败保护；真实 Docker 备份另用隔离项目验收。"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_installer import module, ROOT

uninstall = module('opskit_uninstall', ROOT / 'management/scripts/uninstall.py')


class UninstallTests(unittest.TestCase):
    def container(self, root, project='opskit', cid='abc', compose='compose.yaml'):
        return {'Id': cid, 'Name': '/example', 'Labels': {
            'com.docker.compose.project': project,
            'com.docker.compose.project.working_dir': str(root),
            'com.docker.compose.project.config_files': str(root / compose)}, 'Mounts': []}

    def plan(self, root):
        return dict(containers=[self.container(root)], volumes=[], networks=['network-id'], remaining=[], orphans=[])

    def test_owner_requires_path_and_known_compose_file(self):
        root = ROOT / 'management'
        self.assertEqual(uninstall.owner(self.container(root), [root]), root)
        self.assertIsNone(uninstall.owner(self.container(ROOT / 'other'), [root]))
        self.assertIsNone(uninstall.owner(self.container(root, compose='other.yaml'), [root]))
        self.assertIsNone(uninstall.owner({'Name': 'opskit-hub-workbench-1'}, [root]))

    def test_remote_context_and_same_project_other_directory_are_rejected(self):
        root = ROOT / 'management'
        with patch.dict(uninstall.os.environ, {'DOCKER_HOST': 'ssh://remote', 'DOCKER_CONTEXT': ''}), \
                patch.object(uninstall, 'output') as output:
            with self.assertRaisesRegex(RuntimeError, '本机 Docker'):
                uninstall.inventory([root])
            output.assert_not_called()
        containers = [self.container(root), self.container(ROOT / 'other', cid='other')]
        with patch.dict(uninstall.os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': ''}), \
                patch.object(uninstall, 'output', side_effect=['linux', 'abc other', '\n'.join(map(json.dumps, containers))]):
            with self.assertRaisesRegex(RuntimeError, '同名 Compose'):
                uninstall.inventory([root])

    def test_dry_run_and_cancellation_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for dry, answer in ((True, 'yes'), (False, 'no')):
                with patch.object(uninstall, 'inventory', return_value=self.plan(root)), \
                        patch.object(uninstall, 'docker') as docker, patch('builtins.input', return_value=answer), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(uninstall.uninstall([root], purge=True, dry_run=dry))
                    docker.assert_not_called()
            self.assertFalse((root / '.uninstall-backups').exists())

    def test_network_attached_to_other_container_is_rejected(self):
        root=ROOT/'management'
        replies=['linux','abc',json.dumps(self.container(root)),'','net-id',json.dumps({'foreign-id':{}})]
        with patch.dict(uninstall.os.environ,{'DOCKER_HOST':'unix:///var/run/docker.sock','DOCKER_CONTEXT':''}),patch.object(uninstall,'output',side_effect=replies):
            with self.assertRaisesRegex(RuntimeError,'其他容器'):
                uninstall.inventory([root])

    def test_purge_verifies_local_files_and_volume_before_remove(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp) / 'management';root.mkdir()
            (root / '.env').write_text('private-password', encoding='utf-8')
            (root / '.workbench/hub').mkdir(parents=True)
            (root / '.workbench/hub/entries.json').write_text('["中文收藏"]', encoding='utf-8')
            plan = self.plan(root)
            plan['volumes'] = [dict(name='opskit-data', container='abc', destination='/data')]
            calls = []
            def docker(*args, **kwargs):
                calls.append(args)
                if args[0] == 'cp':
                    with tarfile.open(fileobj=kwargs['stdout'], mode='w|') as tar:
                        content = b'database-content'
                        member = tarfile.TarInfo('database.sqlite');member.size=len(content)
                        tar.addfile(member, io.BytesIO(content))
                if args[0] == 'rm':
                    self.assertTrue(list(root.glob('.uninstall-backups/*/manifest.json')))
            with patch.object(uninstall, 'inventory', return_value=plan), patch.object(uninstall, 'docker', side_effect=docker):
                backup = uninstall.uninstall([root], purge=True, yes=True)
            self.assertFalse((root / '.env').exists())
            self.assertFalse((root / '.workbench').exists())
            self.assertEqual((backup / 'local/management/.env').read_text(), 'private-password')
            self.assertEqual((backup / 'local/management/.env').stat().st_mode & 0o777, 0o600)
            manifest = json.loads((backup / 'manifest.json').read_text())
            for path, digest in manifest['sha256'].items():
                self.assertEqual(uninstall.digest(backup / path), digest)
            self.assertLess(next(i for i,c in enumerate(calls) if c[0]=='cp'), next(i for i,c in enumerate(calls) if c[0]=='rm'))
            self.assertIn(('volume', 'rm', 'opskit-data'), calls)

    def test_failed_volume_backup_never_removes_containers_or_data(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            (root / '.env').write_text('original')
            plan=self.plan(root);plan['volumes']=[dict(name='opskit-data', container='abc', destination='/data')]
            calls=[]
            def docker(*args, **kwargs):
                calls.append(args)
                if args[0]=='cp':
                    raise subprocess.CalledProcessError(1, ['docker', 'cp'])
            with patch.object(uninstall,'inventory',return_value=plan), patch.object(uninstall,'docker',side_effect=docker):
                with self.assertRaises(subprocess.CalledProcessError):
                    uninstall.uninstall([root],purge=True,yes=True)
            self.assertEqual((root/'.env').read_text(),'original')
            self.assertFalse(any(c[0] in ('rm','network','volume') for c in calls))

    def test_default_keeps_configuration_and_volumes(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);(root/'.env').write_text('original')
            plan=self.plan(root);plan['volumes']=[dict(name='data',container='abc',destination='/data')]
            with patch.object(uninstall,'inventory',return_value=plan),patch.object(uninstall,'docker') as docker:
                uninstall.uninstall([root],yes=True)
            self.assertEqual((root/'.env').read_text(),'original')
            self.assertFalse(any(c.args[0] in ('cp','volume') for c in docker.call_args_list))

    def test_stop_process_rejects_foreign_and_negative_pids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/'.workbench/discovery';directory.mkdir(parents=True)
            for pid in ('-1','123'):
                (directory/'discovery.pid').write_text(pid)
                with patch.object(uninstall,'process_command',return_value='unrelated-app'),patch.object(uninstall.os,'kill') as kill,contextlib.redirect_stdout(io.StringIO()):
                    uninstall.stop_process(root,directory,'discovery.py')
                    kill.assert_not_called()
            (directory/'discovery.pid').write_text('456')
            with patch.object(uninstall,'process_command',side_effect=[str(root/'portal/discovery.py')+' --output '+str(directory/'state.json'),'']),patch.object(uninstall.os,'kill') as kill:
                uninstall.stop_process(root,directory,'discovery.py')
                kill.assert_called_once_with(456,uninstall.signal.SIGTERM)

    def test_hub_only_preserves_full_management_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.env').touch();(root/'.workbench/discovery').mkdir(parents=True)
            (root/'.workbench/hub.env').touch()
            auth = root / '.workbench/auth'
            auth.mkdir()
            for name in ('operator.json', 'components.json', 'hub-operator.json', 'hub-components.json'):
                (auth / name).touch()
            paths=uninstall.local_paths(root,hub_only=True,discovery_in_use=True)
            self.assertEqual(paths,[root/'.workbench/hub.env', auth/'hub-operator.json', auth/'hub-components.json'])

    def test_standalone_modules_share_engine_and_help_installs_nothing(self):
        self.assertEqual((ROOT/'management/scripts/uninstall.py').read_bytes(),(ROOT/'collector/scripts/uninstall.py').read_bytes())
        for name in ('management','collector'):
            result=subprocess.run([str(ROOT/name/'uninstall.sh'),'--help'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--purge-data',result.stdout)


if __name__=='__main__':
    unittest.main()

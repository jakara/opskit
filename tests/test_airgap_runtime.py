"""现场运行时的隔离验收：不安装依赖、不操作本机 Docker 项目。"""
import contextlib
import copy
import importlib.util
import io
import json
import os
import argparse
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'skills/opskit/assets/site'
spec = importlib.util.spec_from_file_location('airgap_site', SOURCE / 'site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)
IMAGE_ID = 'sha256:' + 'a' * 64
IMAGE_CONFIG = {'architecture': 'amd64', 'os': 'linux', 'config': {'Cmd': ['serve']},
                'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + 'b' * 64]}}
CONFIG_BYTES = json.dumps(IMAGE_CONFIG).encode()
CONFIG_ID = 'sha256:' + site.hashlib.sha256(CONFIG_BYTES).hexdigest()
TARGET = {'os': 'anolis', 'version': '8.9', 'arch': 'amd64'}


class FakeRunner(site.Runner):
    commands = []
    bad_image = False
    classic_engine = False

    def run(self, args, **kwargs):
        self.commands.append(list(args))
        if args[:3] == ['docker', 'compose', 'version']:
            return 'Docker Compose version v2.35.0'
        if args[:2] == ['docker', 'info']:
            return json.dumps({'OSType': 'linux', 'Architecture': 'x86_64'})
        if args[:3] == ['docker', 'image', 'inspect']:
            actual_id = CONFIG_ID if self.classic_engine else IMAGE_ID
            return json.dumps([{'Id': 'bad' if self.bad_image else actual_id, 'Os': 'linux', 'Architecture': 'amd64',
                                'RootFS': {'Layers': IMAGE_CONFIG['rootfs']['diff_ids']}, 'Config': IMAGE_CONFIG['config']}])
        if args[:3] == ['docker', 'image', 'load']:
            return 'Loaded image ID: ' + (CONFIG_ID if self.classic_engine else IMAGE_ID) + '\n'
        if args[:2] == ['docker', 'compose']:
            if 'config' in args:
                return Path(args[args.index('-f') + 1]).read_text()
            return ''
        if args[0] == 'bash':
            return super().run(args, **kwargs)
        return ''


@unittest.skipIf(sys.platform == 'win32', '现场运行时仅支持 Linux，Windows 制包端不运行 POSIX 现场验收')
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'bundle'
        self.root.mkdir()
        (self.root / 'delivery').mkdir()
        (self.root / 'images').mkdir()
        app = self.root / 'application/web'
        (app / 'hooks').mkdir(parents=True)
        (app / 'compose.json').write_text(json.dumps({'services': {'app': {'image': 'opskit/example:one'}}}))
        (app / 'hooks/check.sh').write_text('set -eu\ntest -f "$OPSKIT_APP_DIR/compose.json"\n')
        (app / 'hooks/backup.sh').write_text('set -eu\ncp "$OPSKIT_ENV_FILE" "$OPSKIT_BACKUP_DIR/parameters.copy"\n')
        archive = self.root / 'images/one.tar'
        with tarfile.open(archive, 'w') as image:
            data = {'config.json': CONFIG_BYTES, 'manifest.json': json.dumps([{'Config': 'config.json', 'RepoTags': None, 'Layers': []}]).encode()}
            for name, content in data.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                image.addfile(member, io.BytesIO(content))
        self.role = {'id': 'web', 'name': '应用机', 'project_name': 'example-web',
                     'directory': 'application/web', 'compose': 'application/web/compose.json',
                     'parameters': [{'key': 'HTTP_PORT', 'type': 'port', 'label': '端口', 'required': True}],
                     'checks': [], 'hooks': {'check': 'application/web/hooks/check.sh', 'backup': 'application/web/hooks/backup.sh'},
                     'stateful': True, 'rollback': 'app-only', 'image_references': ['opskit/example:one']}
        self.manifest = {'schema_version': 1, 'artifact_id': 'example-1', 'application': {'id': 'example', 'version': '1.0'},
                         'target': TARGET, 'roles': [self.role], 'images': [{'reference': 'opskit/example:one', 'image_id': IMAGE_ID,
                         'platform': 'linux/amd64', 'archive': 'images/one.tar', 'sha256': site.digest(archive), 'config_digest': CONFIG_ID}]}
        self.state = self.base / 'state'
        self.env = self.base / 'values.env'
        self.env.write_text("HTTP_PORT='18088'\n")
        FakeRunner.commands, FakeRunner.bad_image, FakeRunner.classic_engine = [], False, False
        self.seal()

    def tearDown(self):
        self.temp.cleanup()

    def seal(self):
        (self.root / 'delivery/manifest.json').write_text(json.dumps(self.manifest))
        sums = self.root / 'SHA256SUMS'
        sums.write_text(''.join(site.digest(p) + '  ' + p.relative_to(self.root).as_posix() + '\n'
                                for p in sorted(self.root.rglob('*')) if p.is_file() and p != sums))

    def invoke(self, action, *extra):
        with patch.object(site, 'BUNDLE', self.root), patch.object(site, 'target_probe', return_value=TARGET), \
             patch.object(site.sys, 'platform', 'linux'), patch.object(site, 'Runner', FakeRunner), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return site.main(['--role', 'web', '--action', action, '--non-interactive', '--state-root', str(self.state),
                              '--env-file', str(self.env)] + list(extra))

    def test_checksum_rejects_tamper_extra_symlink_and_escape(self):
        site.verify_tree(self.root)
        source = self.root / 'application/web/compose.json'
        original = source.read_bytes()
        source.write_bytes(original + b' ')
        with self.assertRaises(site.SiteError):
            site.verify_tree(self.root)
        source.write_bytes(original)
        extra = self.root / 'unexpected'
        extra.write_text('not listed')
        with self.assertRaises(site.SiteError):
            site.verify_tree(self.root)
        extra.unlink()
        extra.symlink_to(source)
        with self.assertRaises(site.SiteError):
            site.verify_tree(self.root)
        extra.unlink()
        with self.assertRaises(site.SiteError):
            site.child(self.root, '../outside')

    def test_opskit_only_manifest_and_role_selection_without_dummy_application(self):
        self.manifest.update(kind='opskit', roles=[])
        self.seal()
        manifest = site.load_manifest(self.root)
        args = argparse.Namespace(action='install', role=None)
        with patch('builtins.input', return_value='1'), contextlib.redirect_stdout(io.StringIO()):
            site.choose(manifest, args)
        self.assertEqual(args.role, 'management')
        self.manifest['kind'] = 'application'
        self.seal()
        with self.assertRaisesRegex(site.SiteError, '缺少业务角色'):
            site.load_manifest(self.root)
        self.manifest.update(kind='opskit', roles=[self.role])
        self.seal()
        with self.assertRaisesRegex(site.SiteError, '不能包含业务角色'):
            site.load_manifest(self.root)

    def test_target_mismatch_precedes_state_and_docker(self):
        with patch.object(site, 'BUNDLE', self.root), patch.object(site, 'target_probe', return_value=dict(TARGET, version='8.10')):
            with self.assertRaises(site.SiteError):
                site.main(['--role', 'web', '--action', 'install', '--state-root', str(self.state)])
        self.assertFalse(self.state.exists())
        self.assertFalse(FakeRunner.commands)

    def test_noninteractive_missing_or_unknown_parameters_has_no_side_effect(self):
        for text in ('', "HTTP_PORT='70000'\n", "HTTP_PORT='8088'\nPASSWORD='never-print-this'\n"):
            self.env.write_text(text)
            with self.assertRaises(site.SiteError) as error:
                self.invoke('install')
            self.assertNotIn('never-print-this', str(error.exception))
            self.assertFalse(self.state.exists())
            self.assertFalse(FakeRunner.commands)

    def test_interactive_repompts_only_invalid_field(self):
        secret = {'key': 'PASSWORD', 'secret': True, 'required': True, 'type': 'text'}
        with patch('builtins.input', side_effect=['65536', '8089']), patch.object(site.getpass, 'getpass', return_value='private'), \
             contextlib.redirect_stdout(io.StringIO()):
            result = site.parameters({'parameters': self.role['parameters'] + [secret]}, {}, True)
        self.assertEqual(result, {'HTTP_PORT': '8089', 'PASSWORD': 'private'})

    def test_null_default_never_satisfies_required_parameter(self):
        with self.assertRaises(site.SiteError):
            site.parameters({'parameters': [{'key': 'TOKEN', 'type': 'text', 'required': True, 'default': None}]}, {}, False)

    def test_wrong_loaded_image_never_receives_delivery_alias(self):
        FakeRunner.bad_image = True
        with self.assertRaises(site.SiteError):
            self.invoke('install')
        self.assertFalse(any(c[:3] == ['docker', 'image', 'tag'] for c in FakeRunner.commands))
        self.assertFalse((self.state / 'example/web/current').exists())

    def test_classic_engine_config_id_is_bound_to_archive_and_preserved_for_rollback(self):
        FakeRunner.classic_engine = True
        self.invoke('install')
        self.manifest['artifact_id'] = 'example-2'
        self.seal()
        self.invoke('upgrade')
        self.invoke('rollback')
        state = site.read_json(self.state / 'example/web/state.json')
        self.assertEqual(state['images'][0]['loaded_image_id'], CONFIG_ID)

    def test_local_image_candidate_cannot_replace_actual_load_identity(self):
        base_run = FakeRunner.run
        def wrong_output(runner, args, **kwargs):
            if args[:3] == ['docker', 'image', 'load']:
                return 'Loaded image ID: sha256:' + 'f' * 64 + '\n'
            return base_run(runner, args, **kwargs)
        with patch.object(FakeRunner, 'run', wrong_output), self.assertRaises(site.SiteError):
            self.invoke('install')
        self.assertFalse(any(c[:3] == ['docker', 'image', 'tag'] for c in FakeRunner.commands))

    def test_existing_compose_project_is_never_adopted(self):
        base_run = FakeRunner.run
        def occupied(runner, args, **kwargs):
            if args[:3] == ['docker', 'container', 'ls']:
                return 'unrelated-existing-container\n'
            return base_run(runner, args, **kwargs)
        with patch.object(FakeRunner, 'run', occupied), self.assertRaises(site.SiteError):
            self.invoke('install')
        self.assertFalse(any(c[:3] == ['docker', 'image', 'load'] for c in FakeRunner.commands))
        self.assertFalse((self.state / 'example/web/current').exists())

    def test_unlabelled_same_name_volume_is_never_adopted(self):
        base_run = FakeRunner.run
        def occupied(runner, args, **kwargs):
            if args[:3] == ['docker', 'volume', 'ls'] and any(a.startswith('name=') for a in args):
                return 'example-web_data\n'
            return base_run(runner, args, **kwargs)
        with patch.object(FakeRunner, 'run', occupied), self.assertRaises(site.SiteError):
            self.invoke('install')
        self.assertFalse(any(c[:3] == ['docker', 'image', 'load'] for c in FakeRunner.commands))
        self.assertFalse((self.state / 'example/web/current').exists())

    def test_loaded_runtime_config_must_match_archive(self):
        base_run = FakeRunner.run
        def wrong_config(runner, args, **kwargs):
            output = base_run(runner, args, **kwargs)
            if args[:3] == ['docker', 'image', 'inspect']:
                value = json.loads(output)
                value[0]['Config']['Cmd'] = ['unexpected-command']
                return json.dumps(value)
            return output
        with patch.object(FakeRunner, 'run', wrong_config), self.assertRaises(site.SiteError):
            self.invoke('install')
        self.assertFalse(any(c[:3] == ['docker', 'image', 'tag'] for c in FakeRunner.commands))

    def test_install_upgrade_backup_and_app_only_rollback(self):
        self.invoke('install')
        directory = self.state / 'example/web'
        original = (directory / 'current/site.env').read_text()
        self.assertEqual((directory / 'current/site.env').stat().st_mode & 0o777, 0o600)
        self.invoke('backup')
        self.manifest['artifact_id'] = 'example-2'
        self.manifest['application']['version'] = '2.0'
        self.env.write_text("HTTP_PORT='18089'\n")
        self.seal()
        FakeRunner.commands = []
        self.invoke('upgrade')
        state = site.read_json(directory / 'state.json')
        self.assertEqual(state['previous']['artifact_id'], 'example-1')
        self.assertTrue(site.read_json(directory / 'result.json')['stateful_backup_passed'])
        commands = FakeRunner.commands
        backup_index = next(i for i, c in enumerate(commands) if c[0] == 'bash' and c[1].endswith('backup.sh'))
        import_index = next(i for i, c in enumerate(commands) if c[:3] == ['docker', 'image', 'load'])
        self.assertLess(backup_index, import_index)
        self.invoke('rollback')
        self.assertEqual((directory / 'current/site.env').read_text(), original)
        self.assertEqual(site.read_json(directory / 'state.json')['artifact_id'], 'example-1')
        ups = [c for c in FakeRunner.commands if c[:2] == ['docker', 'compose'] and 'up' in c]
        self.assertTrue(ups)
        self.assertTrue(all(c[-3:] == ['--pull', 'never', '--no-build'] for c in ups))
        self.assertFalse(any('pull' in c or 'build' in c for c in FakeRunner.commands))
        site.verify_tree(self.root)  # 包本身始终只读，不产生 .env/缓存。

    def test_failed_backup_does_not_replace_current_or_load_images(self):
        self.invoke('install')
        directory = self.state / 'example/web'
        (directory / 'current/hooks/backup.sh').write_text('exit 7\n')
        original = (directory / 'current/site.env').read_bytes()
        FakeRunner.commands = []
        with self.assertRaises(site.SiteError):
            self.invoke('upgrade')
        self.assertEqual((directory / 'current/site.env').read_bytes(), original)
        self.assertFalse(any(c[:3] == ['docker', 'image', 'load'] for c in FakeRunner.commands))
        self.assertFalse(site.read_json(directory / 'result.json')['passed'])

    def test_no_empty_backup_or_unsupported_rollback_success(self):
        self.invoke('install')
        directory = self.state / 'example/web'
        (directory / 'current/hooks/backup.sh').write_text('exit 0\n')
        with self.assertRaises(site.SiteError):
            self.invoke('backup')
        with self.assertRaises(site.SiteError):
            self.invoke('rollback')

    def test_restore_hook_uses_verified_preupgrade_backup(self):
        restore = self.root / 'application/web/hooks/restore.sh'
        restore.write_text('set -eu\ntest -s "$OPSKIT_BACKUP_DIR/parameters.copy"\ncp "$OPSKIT_BACKUP_DIR/parameters.copy" "$OPSKIT_APP_DIR/restored"\n')
        self.role['hooks']['restore'] = 'application/web/hooks/restore.sh'
        self.role['rollback'] = 'restore-hook'
        self.seal()
        self.invoke('install')
        self.manifest['artifact_id'] = 'example-2'
        self.seal()
        self.invoke('upgrade')
        self.invoke('rollback')
        self.assertTrue(any(c[0] == 'bash' and c[1].endswith('restore.sh') for c in FakeRunner.commands))

    def test_real_http_check_fails_without_business_response(self):
        runner = FakeRunner(self.root, 'isolated-check', self.root / 'test.log')
        with self.assertRaises(site.SiteError):
            site.check_business(runner, {'checks': [{'url': 'http://127.0.0.1:1/', 'attempts': 1, 'timeout': 0.1}]}, self.root, {}, 'check')

    def add_documents(self, text='# 现场操作第一版\n', app_id='example'):
        docs = self.root / 'opskit/management/documents'
        manual = docs / 'project' / app_id / 'docs/manual.md'
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_text(text, encoding='utf-8')
        self.manifest['documents'] = [{'path': manual.relative_to(self.root).as_posix(), 'group': 'project',
                                       'source': 'docs/manual.md', 'bytes': manual.stat().st_size, 'sha256': site.digest(manual)}]
        return docs

    def test_first_builtin_install_copies_documents_to_stable_management(self):
        self.add_documents()
        directory = self.state / 'example/management'
        directory.mkdir(parents=True)
        self.manifest['opskit_path'] = 'opskit'
        self.manifest['opskit_images'] = {}
        args = argparse.Namespace(action='install', non_interactive=True, env_file=str(self.env))
        runner = FakeRunner(directory, 'isolated-management', directory / 'install.log')
        with patch.object(site, 'BUNDLE', self.root), patch.object(runner, 'run', return_value=''), \
                patch.object(site, 'check_builtin'), patch.object(site, 'import_images'):
            site.builtin(self.manifest, 'management', args, directory, runner, [])
            config = directory / 'opskit/management/.env'
            original = config.read_bytes()
            site.builtin(self.manifest, 'management', args, directory, runner, [])
        self.assertEqual(config.read_bytes(), original)
        library = directory / 'opskit/management/documents'
        self.assertEqual((library / 'project/example/docs/manual.md').read_text(), '# 现场操作第一版\n')
        self.assertEqual(site.read_json(library / '.opskit-sources.json')['applications']['example']['artifact_id'], 'example-1')

    def test_new_bundle_document_sync_keeps_config_other_apps_and_manual_files(self):
        self.add_documents()
        directory = self.state / 'example/management'
        library = directory / 'opskit/management/documents'
        library.mkdir(parents=True)
        config = library.parent / '.env'
        config.write_text('DATABASE_PASSWORD=local-only\n')
        (library / '手工说明.md').write_text('现场手写\n')
        original_inode = library.stat().st_ino
        with patch.object(site, 'BUNDLE', self.root), contextlib.redirect_stdout(io.StringIO()):
            initial = site.sync_documents(self.manifest, directory)
            other = copy.deepcopy(self.manifest)
            other['application']['id'] = 'another-app'
            other['artifact_id'] = 'other-1'
            other_path = self.root / 'opskit/management/documents/project/another-app/manual.md'
            other_path.parent.mkdir(parents=True)
            other_path.write_text('其他应用资料')
            other['documents'] = [{'path': other_path.relative_to(self.root).as_posix(), 'group': 'project',
                                   'source': 'manual.md', 'bytes': other_path.stat().st_size, 'sha256': site.digest(other_path)}]
            site.sync_documents(other, directory)
            self.manifest['artifact_id'] = 'example-2'
            self.manifest['application']['version'] = '2.0'
            self.add_documents('# 现场操作第二版\n')
            updated = site.sync_documents(self.manifest, directory)
            again = site.sync_documents(self.manifest, directory)
        self.assertFalse(again['documents_changed'])
        self.assertEqual(library.stat().st_ino, original_inode)
        self.assertEqual(config.read_text(), 'DATABASE_PASSWORD=local-only\n')
        self.assertEqual((library / '手工说明.md').read_text(), '现场手写\n')
        self.assertEqual((library / 'project/another-app/manual.md').read_text(), '其他应用资料')
        self.assertEqual((library / 'project/example/docs/manual.md').read_text(), '# 现场操作第二版\n')
        origin = site.read_json(library / '.opskit-sources.json')['applications']['example']
        self.assertEqual(origin['version'], '2.0')
        self.assertEqual(origin['documents'][0]['source'], 'docs/manual.md')
        backup = Path(updated['documents_backup'])
        site.verify_tree(backup)
        self.assertEqual((backup / 'library/project/example/docs/manual.md').read_text(), '# 现场操作第一版\n')
        self.assertTrue(Path(initial['documents_backup']).is_dir())

    def test_document_sync_rejects_unowned_conflicts_local_edits_and_tamper(self):
        self.add_documents()
        directory = self.state / 'example/management'
        library = directory / 'opskit/management/documents'
        manual = library / 'project/example/docs/manual.md'
        manual.parent.mkdir(parents=True)
        manual.write_text('本地未登记资料')
        with patch.object(site, 'BUNDLE', self.root), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(site.SiteError, '无归属'):
                site.sync_documents(self.manifest, directory)
            self.assertEqual(manual.read_text(), '本地未登记资料')
            manual.unlink()
            site.sync_documents(self.manifest, directory)
            manual.write_text('本地已编辑资料')
            with self.assertRaisesRegex(site.SiteError, '本地编辑'):
                site.sync_documents(self.manifest, directory)
            self.assertEqual(manual.read_text(), '本地已编辑资料')
            source = self.root / self.manifest['documents'][0]['path']
            source.write_text('发行包已损坏')
            with self.assertRaisesRegex(site.SiteError, '清单校验'):
                site.sync_documents(self.manifest, directory)

    def test_sync_documents_cli_accepts_shared_library_without_using_docker(self):
        self.add_documents()
        library = self.base / 'shared-management/documents'
        library.mkdir(parents=True)
        self.seal()
        with patch.object(site, 'BUNDLE', self.root), patch.object(site, 'target_probe', return_value=TARGET), \
                patch.object(site.sys, 'platform', 'linux'), patch.object(site, 'Runner', FakeRunner), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            site.main(['--role', 'management', '--action', 'sync-docs', '--non-interactive',
                       '--state-root', str(self.state), '--documents-dir', str(library)])
        self.assertFalse(FakeRunner.commands)
        self.assertTrue((library / 'project/example/docs/manual.md').is_file())
        result = site.read_json(self.state / 'example/management/result.json')
        self.assertTrue(result['passed'])
        self.assertEqual(result['action'], 'sync-docs')

    def test_document_library_symlink_metadata_is_rejected_before_write(self):
        self.add_documents()
        directory = self.state / 'example/management'
        library = self.base / 'shared-documents'
        library.mkdir()
        target = self.base / 'untouched'
        target.write_text('unchanged')
        (library / '.opskit-documents.lock').symlink_to(target)
        with patch.object(site, 'BUNDLE', self.root), self.assertRaisesRegex(site.SiteError, '软链接'):
            site.sync_documents(self.manifest, directory, library)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_shell_rejects_tamper_before_running_bootstrap(self):
        (self.root / 'prerequisites').mkdir()
        marker = Path(self.temp.name) / 'bootstrap-ran'
        (self.root / 'prerequisites/bootstrap.sh').write_text('touch ' + str(marker) + '\n')
        (self.root / 'delivery/site.py').write_text('print("must not execute")\n')
        (self.root / 'install.sh').write_bytes((SOURCE / 'install.sh').read_bytes())
        self.seal()
        (self.root / 'delivery/site.py').write_text('tampered\n')
        result = subprocess.run(['bash', str(self.root / 'install.sh'), '--action', 'install'], capture_output=True)
        # 本机 macOS 会更早拒绝；Linux 必须在调用任何包内脚本前拒绝校验失败。
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()

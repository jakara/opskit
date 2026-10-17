import copy
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
import zipfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('airgap_builder', ROOT / 'skills/opskit/scripts/build_bundle.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def configuration():
    return {'schema_version': 1, 'application': {'id': 'sample', 'name': '示例系统', 'version': '1.0.0'},
            'target': {'os': 'anolis', 'version': '8.9', 'arch': 'amd64'},
            'roles': [{'id': 'web', 'name': '网站', 'project_name': 'sample-web', 'compose': 'deploy/compose.yaml',
                       'images': {}, 'parameters': [{'key': 'HTTP_PORT', 'type': 'port', 'default': '8088', 'required': True}],
                       'files': [], 'checks': [{'url': 'http://127.0.0.1:${HTTP_PORT}/', 'status': 200}],
                       'stateful': False, 'rollback': 'app-only', 'hooks': {}}]}


def compose():
    return {'name': 'sample', 'services': {'web': {'image': 'nginx:alpine', 'ports': [{'target': 80, 'published': '${HTTP_PORT}'}]}}}


class AirgapBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'deploy').mkdir()
        (self.repo / 'deploy/compose.yaml').write_text('services: {}\n')

    def tearDown(self):
        self.temporary.cleanup()

    def render(self, document=None, role=None):
        destination = self.root / 'rendered'
        destination.mkdir(exist_ok=True)
        with patch.object(builder, 'compose_document', return_value=copy.deepcopy(document or compose())):
            return builder.role_document(self.repo, role or configuration()['roles'][0], destination)

    def prereqs(self):
        directory = self.root / 'prereqs'
        directory.mkdir()
        files, keys = [], []
        for filename, group in [('rpms/docker.rpm', 'runtime'), ('rpms/python.rpm', 'python')]:
            path = directory / filename
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'fixture-rpm-not-for-shipment')
            files.append({'path': filename, 'group': group, 'sha256': builder.digest(path)})
        key = directory / 'keys/key.asc'
        key.parent.mkdir()
        key.write_text('fixture-signing-key')
        keys.append({'path': 'keys/key.asc', 'sha256': builder.digest(key)})
        for filename in ('bootstrap.sh', 'target.txt', 'python-packages.txt', 'verification/offline-install.log'):
            path = directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('unit-test evidence fixture, never a real acceptance\n')
        builder.write_json(directory / 'manifest.json', {'schema_version': 1, 'target': configuration()['target'], 'files': files, 'keys': keys,
            'verification': {'offline_install': True, 'network': 'none', 'container_image': 'fixture', 'image_id': 'fixture', 'platform': 'linux/amd64',
                             'python_version': 'fixture', 'docker_version': 'fixture', 'compose_version': 'fixture',
                             'commands': ['fixture'], 'log': 'verification/offline-install.log'}})
        builder.checksums(directory)
        return directory

    def acceptance(self, manifest):
        return {'schema_version': 1, 'artifact_id': manifest['artifact_id'], 'target': manifest['target'],
                'network': 'disabled', 'method': 'anolis-container', 'roles': [
                    {'id': r['id'], 'action': 'install', 'passed': True, 'checks_passed': True, 'stateful_backup_passed': r['stateful']} for r in manifest['roles']],
                'opskit': {'management': True, 'collector': True}, 'prerequisites': {'verified': True}}

    def image_archive(self, command, **kwargs):
        self.assertEqual(command[:4], ['docker', 'image', 'save', '--output'])
        with tarfile.open(command[4], 'w') as stream:
            for name, value in [('config.json', {'os': 'linux', 'architecture': 'amd64', 'rootfs': {'diff_ids': []}, 'config': {}}),
                                ('manifest.json', [{'Config': 'config.json', 'RepoTags': None, 'Layers': []}])]:
                payload = json.dumps(value).encode('utf-8')
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                stream.addfile(info, io.BytesIO(payload))
        return ''

    def test_inspect_is_read_only_and_skips_runtime_and_links(self):
        (self.repo / 'Dockerfile').write_text('FROM busybox\n')
        (self.repo / '.env').write_text('PASSWORD=must-not-read\n')
        (self.repo / 'node_modules').mkdir()
        (self.repo / 'node_modules/Dockerfile').write_text('private')
        (self.repo / 'linked').symlink_to(self.repo / 'node_modules', target_is_directory=True)
        result = builder.inspect_repo(self.repo)
        self.assertEqual(result['compose_files'], ['deploy/compose.yaml'])
        self.assertEqual(result['dockerfiles'], ['Dockerfile'])
        self.assertEqual((self.repo / '.env').read_text(), 'PASSWORD=must-not-read\n')

    def test_paths_reject_links_absolute_and_traversal(self):
        (self.repo / 'linked').symlink_to(self.repo / 'deploy')
        for value in ('../secret', '/secret', 'C:/secret', 'deploy/../secret', 'linked/compose.yaml', 'a\\b'):
            with self.subTest(value=value), self.assertRaises(builder.BundleError):
                builder.safe_path(self.repo, value)

    def test_windows_shell_source_is_normalized_for_linux(self):
        source, target = self.root / 'windows.sh', self.root / 'linux.sh'
        source.write_bytes(b'\xef\xbb\xbf#!/bin/bash\r\nset -eu\r\n')
        builder.copy_deployment_file(source, target)
        self.assertEqual(target.read_bytes(), b'#!/bin/bash\nset -eu\n')
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_config_rejects_missing_backup_and_secret_defaults(self):
        config = configuration()
        config['roles'][0]['stateful'] = True
        with self.assertRaisesRegex(builder.BundleError, 'backup'):
            builder.validate_config(config)
        config = configuration()
        config['roles'][0]['parameters'].append({'key': 'DB_PASSWORD', 'secret': True, 'default': 'actual-password'})
        with self.assertRaisesRegex(builder.BundleError, '秘密参数'):
            builder.validate_config(config)

    def test_config_requires_real_check_and_explicit_rollback(self):
        config = configuration()
        config['roles'][0]['checks'] = []
        with self.assertRaisesRegex(builder.BundleError, 'checks'):
            builder.validate_config(config)
        config['roles'][0]['hooks'] = {'check': 'check.sh'}
        config['roles'][0]['rollback'] = 'restore-hook'
        with self.assertRaisesRegex(builder.BundleError, 'restore'):
            builder.validate_config(config)

    def test_empty_roles_are_exclusive_to_opskit_kind(self):
        config = configuration()
        builder.validate_config(config)
        config['kind'] = 'application'
        builder.validate_config(config)
        config['roles'] = []
        for kind in ('application', 'unknown', None):
            config['kind'] = kind
            with self.subTest(kind=kind), self.assertRaises(builder.BundleError):
                builder.validate_config(config)
        config['kind'] = 'opskit'
        builder.validate_config(config)
        config['roles'] = configuration()['roles']
        with self.assertRaisesRegex(builder.BundleError, '必须为空'):
            builder.validate_config(config)
        for overrides in ({'UNKNOWN_IMAGE': 'registry/app:1'}, {'SPUG_IMAGE': ''}, {'SPUG_IMAGE': '${IMAGE}'}, ['SPUG_IMAGE']):
            config = configuration()
            config['opskit_images'] = overrides
            with self.subTest(overrides=overrides), self.assertRaisesRegex(builder.BundleError, 'opskit_images'):
                builder.validate_config(config)

    def test_build_opskit_cli_needs_no_business_repo_and_forwards_platform_and_sources(self):
        captured = {}
        def stage(repo, config_path, output, source, prerequisites, use_local):
            captured.update(config=builder.read_json(config_path), repo=Path(repo), output=output, source=source,
                            prerequisites=prerequisites, use_local=use_local)
            self.assertEqual(builder.validate_config(captured['config']), captured['config'])
            return Path(output) / 'staged'
        with patch.object(builder, 'build', side_effect=stage), patch('sys.stdout', new_callable=io.StringIO):
            result = builder.main(['build-opskit', '--os', 'anolis', '--version', '8.10', '--arch', 'arm64',
                                   '--output', str(self.root / 'out'), '--opskit-source', str(ROOT),
                                   '--prerequisites-dir', str(self.root / 'prereqs'), '--use-local-images',
                                   '--kit-version', '1.2.3', '--image', 'SPUG_IMAGE=registry.example/spug:4.1',
                                   '--image', 'DBX_IMAGE=registry.example/dbx:0.6.40'])
        self.assertEqual(result, 0)
        self.assertEqual(captured['config']['kind'], 'opskit')
        self.assertEqual(captured['config']['roles'], [])
        self.assertEqual(captured['config']['application']['version'], '1.2.3')
        self.assertEqual(captured['config']['target'], {'os': 'anolis', 'version': '8.10', 'arch': 'arm64'})
        self.assertEqual(captured['config']['opskit_images'], {'SPUG_IMAGE': 'registry.example/spug:4.1', 'DBX_IMAGE': 'registry.example/dbx:0.6.40'})
        self.assertEqual(captured['source'], ROOT)
        self.assertTrue(captured['use_local'])
        self.assertFalse(captured['repo'].exists())

    def test_build_opskit_version_and_inputs_fail_before_docker(self):
        source = self.root / 'opskit-source'
        source.mkdir()
        (source / 'README.md').write_text('# 没有版本号\n')
        with patch.object(builder, 'run') as runner, patch.object(builder, 'build') as stage:
            with self.assertRaisesRegex(builder.BundleError, 'kit-version'):
                builder.build_opskit(configuration()['target'], self.root / 'out', source=source)
            with self.assertRaisesRegex(builder.BundleError, '目标'):
                builder.build_opskit({'os': 'windows', 'version': '11', 'arch': 'amd64'}, self.root / 'out', source=source, kit_version='1.0.0')
            with self.assertRaisesRegex(builder.BundleError, 'opskit_images'):
                builder.build_opskit(configuration()['target'], self.root / 'out', source=source, kit_version='1.0.0', images={'UNKNOWN_IMAGE': 'app:1'})
            runner.assert_not_called()
            stage.assert_not_called()

    def test_compose_does_not_read_dotenv_and_keeps_variables(self):
        with patch.object(builder, 'run', return_value=json.dumps(compose())) as runner:
            result = builder.compose_document(self.repo, 'deploy/compose.yaml')
        arguments = runner.call_args[0][0]
        for option in ('--no-interpolate', '--no-env-resolution', '--no-path-resolution'):
            self.assertIn(option, arguments)
        self.assertIn('--env-file', arguments)
        self.assertEqual(result['services']['web']['ports'][0]['published'], '${HTTP_PORT}')

    def test_render_uses_site_project_namespace_for_implicit_resources(self):
        role = configuration()['roles'][0]
        with patch.object(builder, 'compose_document', return_value=compose()) as reader:
            builder.role_document(self.repo, role, self.root / 'rendered')
        self.assertEqual(reader.call_args.args[2], role['project_name'])
        for resources in ({'networks': {'default': {'external': True, 'name': 'shared'}}},
                          {'volumes': {'db': {'name': 'developer_db'}}}):
            document = compose()
            document.update(resources)
            with self.subTest(resources=resources), self.assertRaises(builder.BundleError):
                self.render(document)

    def test_build_must_be_prebuilt_and_rewrites_only_published_image(self):
        document = compose()
        document['services']['web']['build'] = {'context': '.'}
        with self.assertRaisesRegex(builder.BundleError, '先按目标平台构建'):
            self.render(document)
        role = configuration()['roles'][0]
        role['images'] = {'web': 'sample:web-v1'}
        rendered, output, images = self.render(document, role)
        self.assertNotIn('build', rendered['services']['web'])
        self.assertEqual(images, {'web': 'sample:web-v1'})
        self.assertEqual(output['compose'], 'application/web/compose.json')

    def test_named_volumes_and_writable_bind_are_not_stateless(self):
        for volume in ({'type': 'volume', 'source': 'db', 'target': '/data'}, {'type': 'bind', 'source': '${DATA_DIR}', 'target': '/data'}):
            document = compose()
            document['services']['web']['volumes'] = [volume]
            with self.subTest(volume=volume), self.assertRaisesRegex(builder.BundleError, 'stateful'):
                self.render(document)

    def test_only_explicit_files_are_copied_and_mounts_are_rewritten(self):
        (self.repo / 'deploy/nginx.conf').write_text('server { listen 80; }\n')
        role = configuration()['roles'][0]
        role['files'] = [{'source': 'deploy/nginx.conf', 'target': 'config/nginx.conf'}]
        document = compose()
        document['services']['web']['volumes'] = [{'type': 'bind', 'source': './nginx.conf', 'target': '/etc/nginx/nginx.conf', 'read_only': True}]
        rendered, _, _ = self.render(document, role)
        self.assertEqual(rendered['services']['web']['volumes'][0]['source'], './config/nginx.conf')
        self.assertEqual((self.root / 'rendered/config/nginx.conf').read_text(), 'server { listen 80; }\n')

    def test_compose_rejects_development_environment_and_secrets(self):
        cases = [{'env_file': [{'path': '.env'}]}, {'environment': {'DB_PASSWORD': 'production-secret'}},
                 {'environment': {'INHERITED': None}}, {'container_name': 'my-database'}]
        for extra in cases:
            document = compose()
            document['services']['web'].update(extra)
            with self.subTest(extra=extra), self.assertRaises(builder.BundleError):
                self.render(document)

    def test_compose_requires_all_referenced_parameters(self):
        document = compose()
        document['services']['web']['environment'] = {'SETTING': '${UNDECLARED}'}
        with self.assertRaisesRegex(builder.BundleError, 'UNDECLARED'):
            self.render(document)

    def test_empty_hook_cannot_pass_for_acceptance(self):
        hook = self.repo / 'check.sh'
        for contents in ('#!/bin/bash\nset -euo pipefail\nexit 0\n', '# todo\necho ok\n', 'true\n'):
            hook.write_text(contents)
            with self.assertRaisesRegex(builder.BundleError, '空操作'):
                builder.validate_hook(hook)
        hook.write_text('curl --fail http://127.0.0.1:8088/\n')
        builder.validate_hook(hook)

    def test_sensitive_explicit_files_are_rejected(self):
        for filename in ('.env', 'config/.env.production', 'logs/error.log', 'keys/server.key', '.git/config', 'db.sql'):
            with self.subTest(filename=filename), self.assertRaises(builder.BundleError):
                builder.reject_sensitive_path(filename)
        file = self.repo / 'config.yaml'
        file.write_text('DB_PASSWORD: this-is-actual-secret\n')
        with self.assertRaisesRegex(builder.BundleError, '明文凭据'):
            builder.check_text_secrets(file)

    def test_document_directories_preserve_resources_and_deduplicate(self):
        docs = self.repo / 'docs'
        (docs / 'images').mkdir(parents=True)
        (docs / '手册.md').write_text('# 安装\n![拓扑](images/topology.svg)\n', encoding='utf-8')
        (docs / 'images/topology.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (docs / '.DS_Store').write_bytes(b'ignored')
        destination = self.root / 'library'
        records = builder.project_documents(self.repo, ['docs', 'docs/手册.md'], destination, 'sample')
        self.assertEqual(len(records), 2)
        manual = destination / 'project/sample/docs/手册.md'
        self.assertIn('(images/topology.svg)', manual.read_text())
        self.assertTrue((manual.parent / 'images/topology.svg').is_file())
        for record in records:
            self.assertEqual(record['group'], 'project')
            target = destination / record['path'].removeprefix(builder.DOCUMENT_ROOT + '/')
            self.assertEqual(record['sha256'], builder.digest(target))
            self.assertEqual(record['bytes'], target.stat().st_size)

    def test_documents_reject_unsupported_sensitive_and_linked_resources(self):
        docs = self.repo / 'docs'
        docs.mkdir()
        (docs / 'safe.md').write_text('安装说明\n', encoding='utf-8')
        for filename, contents, message in [('old.doc', b'legacy', '.doc'), ('file.exe', b'binary', '格式'),
                                             ('secret.md', b'DB_PASSWORD=real-secret\n', '明文凭据'),
                                             ('.hidden.md', b'private notes', '文档路径')]:
            path = docs / filename
            path.write_bytes(contents)
            with self.subTest(filename=filename), self.assertRaisesRegex(builder.BundleError, message):
                builder.project_documents(self.repo, ['docs'], self.root / 'library', 'sample')
            path.unlink()
        (docs / 'linked.md').symlink_to(docs / 'safe.md')
        with self.assertRaisesRegex(builder.BundleError, '软链接'):
            builder.project_documents(self.repo, ['docs'], self.root / 'library', 'sample')
        for source in ('../outside.md', '.git', '.env', 'logs'):
            with self.subTest(source=source), self.assertRaises(builder.BundleError):
                builder.project_documents(self.repo, [source], self.root / 'library', 'sample')
        (docs / 'linked.md').unlink()
        nested = docs.joinpath(*(['nested'] * 10), 'manual.md')
        nested.parent.mkdir(parents=True)
        nested.write_text('too deeply nested for Hub')
        with self.assertRaisesRegex(builder.BundleError, '层级'):
            builder.project_documents(self.repo, ['docs'], self.root / 'library', 'sample')

    def test_word_documents_must_be_valid_bounded_and_without_plain_credentials(self):
        path = self.repo / 'manual.docx'
        def word(text='使用说明'):
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('[Content_Types].xml', '<Types/>')
                archive.writestr('word/document.xml', '<document><p><r><t>%s</t></r></p></document>' % text)
        word()
        builder.validate_document(path)
        word('x' * 2000000)
        with self.assertRaisesRegex(builder.BundleError, '压缩比例'):
            builder.validate_document(path)
        word('DB_PASSWORD=real-secret')
        with self.assertRaisesRegex(builder.BundleError, '明文凭据'):
            builder.validate_document(path)
        path.write_bytes(b'not-a-docx')
        with self.assertRaisesRegex(builder.BundleError, '损坏'):
            builder.validate_document(path)
        word()
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('../escape.xml', '<document/>')
        with self.assertRaisesRegex(builder.BundleError, '越界'):
            builder.validate_document(path)
        word()
        with zipfile.ZipFile(path, 'a') as archive:
            for number in range(2000):
                archive.writestr('word/resource%s.xml' % number, '<resource/>')
        with self.assertRaisesRegex(builder.BundleError, '2000'):
            builder.validate_document(path)
        huge = self.repo / 'huge.pdf'
        with huge.open('wb') as stream:
            stream.truncate(21 * 1024 * 1024)
        with self.assertRaisesRegex(builder.BundleError, '20 MiB'):
            builder.validate_document(huge)

    def test_documents_configuration_requires_explicit_relative_string_list(self):
        for selections in ('docs', [42], ['.'], ['/docs'], ['docs/../private']):
            config = configuration()
            config['documents'] = selections
            with self.subTest(selections=selections), self.assertRaises(builder.BundleError):
                builder.validate_config(config)

    def test_auto_and_builtin_manuals_are_included_without_project_documents(self):
        bundle = self.root / 'auto-documents'
        (bundle / 'docs').mkdir(parents=True)
        for name in ('现场操作.md', 'Spug操作.md'):
            (bundle / 'docs' / name).write_text('# 现场说明\n[管理端](../opskit/management/README.md)\n', encoding='utf-8')
        manifest = {'application': configuration()['application']}
        builder.library_documents(bundle, ROOT, manifest, [])
        self.assertEqual(len(manifest['documents']), 7)
        self.assertEqual({record['group'] for record in manifest['documents']}, {'builtin', 'delivery'})
        builder.checksums(bundle)
        builder.verify_checksums(bundle)

    def test_resolve_registry_image_pulls_digest_and_never_tags(self):
        image_id, manifest_digest = 'sha256:' + '1' * 64, 'sha256:' + '2' * 64
        responses = [json.dumps({'manifests': [{'digest': manifest_digest, 'platform': {'os': 'linux', 'architecture': 'amd64'}}]}), '',
                     json.dumps([{'Id': image_id, 'Os': 'linux', 'Architecture': 'amd64'}])]
        with patch.object(builder, 'run', side_effect=responses) as runner:
            result = builder.resolve_image('registry.example:5000/app:current', 'linux/amd64')
        self.assertEqual(result, image_id)
        self.assertEqual(runner.call_args_list[1][0][0], ['docker', 'pull', '--platform', 'linux/amd64', 'registry.example:5000/app@' + manifest_digest])
        self.assertFalse(any('tag' in c[0][0] for c in runner.call_args_list))

    def test_local_mode_still_rejects_wrong_architecture(self):
        with patch.object(builder, 'run', return_value=json.dumps([{'Id': 'sha256:' + '1' * 64, 'Os': 'linux', 'Architecture': 'arm64'}])):
            with self.assertRaisesRegex(builder.BundleError, '架构不匹配'):
                builder.resolve_image('local:image', 'linux/amd64', use_local=True)

    def test_resolve_single_manifest_uses_json_descriptor_digest(self):
        image_id, manifest_digest = 'sha256:' + '1' * 64, 'sha256:' + '2' * 64
        responses = [json.dumps({'schemaVersion': 2, 'config': {'digest': image_id}}),
                     json.dumps({'digest': manifest_digest}), '', json.dumps([{'Id': image_id, 'Os': 'linux', 'Architecture': 'amd64'}])]
        with patch.object(builder, 'run', side_effect=responses) as runner:
            self.assertEqual(builder.resolve_image('example/app:v1', 'linux/amd64'), image_id)
        self.assertEqual(runner.call_args_list[1][0][0][-1], '{{json .Manifest}}')
        self.assertEqual(runner.call_args_list[2][0][0][-1], 'example/app@' + manifest_digest)

    def test_dependency_bundle_requires_evidence_and_exact_hashes(self):
        directory = self.prereqs()
        builder.verify_prerequisites(directory, configuration()['target'])
        (directory / 'rpms/docker.rpm').write_text('corrupted')
        with self.assertRaisesRegex(builder.BundleError, '校验失败'):
            builder.verify_prerequisites(directory, configuration()['target'])

    def test_dependency_bool_without_real_evidence_is_rejected(self):
        directory = self.prereqs()
        manifest = builder.read_json(directory / 'manifest.json')
        manifest['verification'] = {'offline_install': True}
        builder.write_json(directory / 'manifest.json', manifest)
        builder.checksums(directory)
        with self.assertRaisesRegex(builder.BundleError, '证据'):
            builder.verify_prerequisites(directory, configuration()['target'])

    def test_checksum_rejects_extra_and_linked_files(self):
        directory = self.root / 'bundle'
        directory.mkdir()
        (directory / 'file.txt').write_text('hello')
        builder.checksums(directory)
        builder.verify_checksums(directory)
        (directory / '.env').write_text('secret')
        with self.assertRaisesRegex(builder.BundleError, '集合'):
            builder.verify_checksums(directory)
        (directory / '.env').unlink()
        (directory / 'link').symlink_to(directory / 'file.txt')
        with self.assertRaises(builder.BundleError):
            builder.checksums(directory)

    def test_seal_acceptance_requires_every_role_and_real_opskit(self):
        manifest = {'artifact_id': 'id', 'target': configuration()['target'], 'roles': configuration()['roles']}
        report = self.acceptance(manifest)
        builder.validate_acceptance(manifest, report)
        for mutation in ('target', 'checks', 'opskit', 'network', 'duplicate'):
            modified = copy.deepcopy(report)
            if mutation == 'target':
                modified['target']['arch'] = 'arm64'
            elif mutation == 'checks':
                modified['roles'][0]['checks_passed'] = False
            elif mutation == 'opskit':
                modified['opskit']['collector'] = False
            elif mutation == 'network':
                modified['network'] = 'enabled'
            else:
                modified['roles'] *= 2
            with self.subTest(mutation=mutation), self.assertRaises(builder.BundleError):
                builder.validate_acceptance(manifest, modified)

    def test_stateful_acceptance_needs_successful_backup(self):
        manifest = {'artifact_id': 'id', 'target': configuration()['target'], 'roles': configuration()['roles']}
        manifest['roles'][0]['stateful'] = True
        report = self.acceptance(manifest)
        report['roles'][0]['stateful_backup_passed'] = False
        with self.assertRaisesRegex(builder.BundleError, '备份'):
            builder.validate_acceptance(manifest, report)

    def test_opskit_copy_excludes_credentials_runtime_dist_and_skill(self):
        source, destination = self.root / 'opskit', self.root / 'snapshot'
        for filename in ('management/compose.yaml', 'collector/compose.yaml', 'management/.env',
                         'management/.env.example', 'management/.workbench/token.json', 'management/scripts/install.py',
                         'management/.backups/config.py', 'management/.document-backups/client.md', 'management/documents/project/old-customer.md',
                         'management/portal/vendor/LICENSE.txt', 'skills/private.py', 'dist/secret.py', 'README.md'):
            path = source / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture')
        builder.copy_opskit(source, destination)
        files = sorted(p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file())
        self.assertEqual(files, ['README.md', 'collector/compose.yaml', 'management/.env.example', 'management/compose.yaml',
                                 'management/portal/vendor/LICENSE.txt', 'management/scripts/install.py'])

    def test_build_fails_before_docker_without_prerequisites(self):
        path = self.repo / 'opskit-delivery.json'
        builder.write_json(path, configuration())
        with patch.object(builder, 'opskit_source', return_value=ROOT), patch.object(builder, 'run') as runner:
            with self.assertRaisesRegex(builder.BundleError, 'prerequisites-dir'):
                builder.build(self.repo, path, self.root / 'out')
        runner.assert_not_called()

    def test_stage_and_seal_roundtrip_is_gated_and_deduplicates_image_archives(self):
        # Image save and prereqs here are explicit fixtures, not release evidence.
        prerequisite_dir = self.prereqs()
        path = self.repo / 'opskit-delivery.json'
        config = configuration()
        config['documents'] = ['guide.md']
        (self.repo / 'guide.md').write_text('# 客户部署\n', encoding='utf-8')
        builder.write_json(path, config)
        assets = self.root / 'assets/site'
        assets.mkdir(parents=True)
        (assets / 'site.py').write_text('print("fixture")\n')
        (assets / 'install.sh').write_text('#!/bin/bash\necho fixture\n')
        fixture_script = self.root / 'scripts/build_bundle.py'
        fixture_script.parent.mkdir()
        fixture_script.write_text('# fixture location only\n')
        image_id = 'sha256:' + '1' * 64
        with patch.object(builder, 'compose_document', return_value=compose()), \
                patch.object(builder, 'image_defaults', return_value={key: 'local:fixture' for key in builder.IMAGE_KEYS['management'] + builder.IMAGE_KEYS['collector']}), \
                patch.object(builder, 'resolve_image', return_value=image_id), patch.object(builder, 'run', side_effect=self.image_archive) as runner, \
                patch.object(builder, '__file__', str(fixture_script)):
            staged = builder.build(self.repo, path, self.root / 'out', ROOT, prerequisite_dir, True)
        self.assertEqual(runner.call_count, 1)
        builder.verify_checksums(staged)
        manifest = builder.read_json(staged / 'delivery/manifest.json')
        self.assertFalse(manifest['verification']['target_smoke'])
        self.assertEqual({record['group'] for record in manifest['documents']}, {'builtin', 'delivery', 'project'})
        self.assertEqual(len(manifest['documents']), 8)
        manual = staged / 'opskit/management/documents/delivery/sample/现场操作.md'
        self.assertIn('../../builtin/sample/management/README.md', manual.read_text())
        self.assertIn('--action sync-docs', manual.read_text())
        for record in manifest['documents']:
            self.assertEqual(builder.digest(staged / record['path']), record['sha256'])
        self.assertEqual(len(manifest['images']), 8)
        self.assertEqual(len({i['reference'] for i in manifest['images']}), 8)
        self.assertEqual(len(list((staged / 'images').glob('*.tar'))), 1)
        self.assertEqual(builder.read_json(staged / 'opskit/management/image-defaults.json')['NPM_IMAGE'], manifest['opskit_images']['NPM_IMAGE'])
        report_file = self.root / 'acceptance.json'
        report = self.acceptance(manifest)
        report['opskit']['collector'] = False
        builder.write_json(report_file, report)
        with self.assertRaises(builder.BundleError):
            builder.seal(staged, report_file, self.root / 'release')
        self.assertFalse(list((self.root / 'release').glob('*.tar.gz')) if (self.root / 'release').exists() else False)
        report['opskit']['collector'] = True
        builder.write_json(report_file, report)
        archive = builder.seal(staged, report_file, self.root / 'release')
        self.assertTrue(archive.is_file())
        self.assertFalse(builder.read_json(staged / 'delivery/manifest.json')['verification']['target_smoke'])
        with tarfile.open(archive) as stream:
            sealed = json.load(stream.extractfile(staged.name + '/delivery/manifest.json'))
            self.assertTrue(sealed['verification']['target_smoke'])
            self.assertEqual(sealed['verification']['status'], 'verified')
            self.assertEqual(sealed['documents'], manifest['documents'])
            self.assertEqual(stream.extractfile(staged.name + '/opskit/management/documents/project/sample/guide.md').read(), b'# \xe5\xae\xa2\xe6\x88\xb7\xe9\x83\xa8\xe7\xbd\xb2\n')
            self.assertFalse(any(m.issym() or m.islnk() or m.name.startswith('/') for m in stream.getmembers()))
            self.assertEqual(stream.getmember(staged.name + '/install.sh').mode, 0o755)
            self.assertTrue(all(m.uid == 0 and m.gid == 0 and not m.uname and not m.gname for m in stream.getmembers()))

    def test_opskit_only_stage_contains_seven_images_and_seal_still_requires_runtime_acceptance(self):
        # These archives, RPMs and acceptance are unit fixtures, never deployment evidence.
        prerequisite_dir = self.prereqs()
        defaults = {key: 'local:' + key.lower() for key in builder.IMAGE_KEYS['management'] + builder.IMAGE_KEYS['collector']}
        with patch.object(builder, 'image_defaults', return_value=defaults), \
                patch.object(builder, 'resolve_image', return_value='sha256:' + '1' * 64) as resolver, \
                patch.object(builder, 'run', side_effect=self.image_archive) as runner, patch.object(builder, 'role_document') as role_builder:
            staged = builder.build_opskit(configuration()['target'], self.root / 'out', ROOT, prerequisite_dir, True)
        role_builder.assert_not_called()
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(resolver.call_count, 7)
        self.assertTrue(all(call.args[1:] == ('linux/amd64', True) for call in resolver.call_args_list))
        builder.verify_checksums(staged)
        manifest = builder.read_json(staged / 'delivery/manifest.json')
        self.assertEqual(manifest['kind'], 'opskit')
        self.assertEqual(manifest['roles'], [])
        self.assertEqual(manifest['application']['id'], 'opskit')
        self.assertIn('v' + manifest['application']['version'], (ROOT / 'README.md').read_text().splitlines()[0])
        self.assertFalse((staged / 'application').exists())
        self.assertEqual(manifest['target'], configuration()['target'])
        self.assertEqual(manifest['prerequisites'], 'prerequisites/manifest.json')
        self.assertEqual(set(manifest['opskit_images']), set(defaults))
        self.assertEqual(len(manifest['images']), 7)
        self.assertEqual(set(manifest['opskit_roles']), {'management', 'collector', 'hub'})
        self.assertEqual({record['group'] for record in manifest['documents']}, {'builtin', 'delivery'})
        self.assertFalse(manifest['verification']['target_smoke'])
        manual = (staged / 'docs/现场操作.md').read_text()
        spug = (staged / 'docs/Spug操作.md').read_text()
        for command in ('--role management --action install', '--role collector --action install', '--role hub --action install', '--action sync-docs'):
            self.assertIn(command, manual)
        self.assertIn('不包含业务应用', manual)
        self.assertNotIn('## 业务角色', manual)
        self.assertIn('测试连接', spug)
        self.assertIn('不升级组件或迁移数据库', spug)
        self.assertNotIn('--action upgrade', spug)
        for record in manifest['documents']:
            self.assertEqual(builder.digest(staged / record['path']), record['sha256'])
        report = self.acceptance(manifest)
        builder.validate_acceptance(manifest, report)
        for mutation in ('missing-management', 'missing-collector', 'missing-prerequisites', 'missing-image', 'missing-module', 'business-kind', 'fake-role'):
            changed_manifest, changed_report = copy.deepcopy(manifest), copy.deepcopy(report)
            if mutation.startswith('missing-') and mutation.split('-', 1)[1] in ('management', 'collector'):
                changed_report['opskit'].pop(mutation.split('-', 1)[1])
            elif mutation == 'missing-prerequisites':
                changed_report.pop('prerequisites')
            elif mutation == 'missing-image':
                changed_manifest['images'].pop()
            elif mutation == 'missing-module':
                changed_manifest['opskit_roles'].pop('collector')
            elif mutation == 'business-kind':
                changed_manifest.pop('kind')
            else:
                changed_manifest['roles'] = configuration()['roles']
            with self.subTest(mutation=mutation), self.assertRaises(builder.BundleError):
                builder.validate_acceptance(changed_manifest, changed_report)
        report_file = self.root / 'acceptance-opskit.json'
        rejected = copy.deepcopy(report)
        rejected['opskit']['collector'] = False
        builder.write_json(report_file, rejected)
        with self.assertRaises(builder.BundleError):
            builder.seal(staged, report_file, self.root / 'release')
        builder.write_json(report_file, report)
        archive = builder.seal(staged, report_file, self.root / 'release')
        with tarfile.open(archive) as stream:
            sealed = json.load(stream.extractfile(staged.name + '/delivery/manifest.json'))
            self.assertEqual(sealed['kind'], 'opskit')
            self.assertEqual(sealed['roles'], [])
            self.assertEqual(len(sealed['images']), 7)
            self.assertTrue(sealed['verification']['target_smoke'])
        with self.assertRaisesRegex(builder.BundleError, '已存在'):
            builder.seal(staged, report_file, self.root / 'release')
        with patch.object(builder, 'run') as docker:
            with self.assertRaisesRegex(builder.BundleError, '输出已存在'):
                builder.build_opskit(configuration()['target'], self.root / 'out', ROOT, prerequisite_dir, True)
            docker.assert_not_called()


if __name__ == '__main__':
    unittest.main()

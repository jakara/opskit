"""分发包独立性和基础安装器镜像默认值的实际集成检查。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


packager = module('airgap_packager', ROOT / 'skills/package_skill.py')
common = module('airgap_common', ROOT / 'management/scripts/common.py')


class SkillDistributionTests(unittest.TestCase):
    def test_clean_runtime_excludes_credentials_data_and_unknown_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            for name in ('.env', '.env.production', '.env.install-progress', '.install-options.json', 'client.json', 'users.db', 'archive.tar',
                         'compose.yaml', '.env.example', 'install.sh'):
                (source / name).write_text('fixture')
            (source / '.workbench').mkdir()
            (source / '.workbench/credentials.py').write_text('fixture')
            (source / 'documents').mkdir()
            (source / 'documents/customer.md').write_text('不应进入通用Skill的本机资料')
            destination = Path(directory) / 'clean'
            packager.copy_clean(source, destination, runtime_only=True)
            self.assertEqual({p.name for p in destination.iterdir()}, {'compose.yaml', '.env.example', 'install.sh'})

    def test_stage_is_self_contained_and_records_actual_source(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = packager.stage(Path(directory) / 'opskit')
            runtime = stage / 'assets/opskit'
            provenance = json.loads((runtime / 'OPSKIT_SOURCE.json').read_text())
            self.assertIn('management/scripts/install.py', provenance['sha256'])
            self.assertTrue((runtime / 'management/documents/builtin/management/README.md').is_file())
            self.assertTrue(provenance['documentation_sha256'])
            self.assertFalse(any(key.startswith('management/documents/') for key in provenance['sha256']))
            self.assertEqual((runtime / 'management/scripts/common.py').read_bytes(),
                             (ROOT / 'management/scripts/common.py').read_bytes())
            result = subprocess.run([sys.executable, '-B', str(stage / 'scripts/build_bundle.py'), 'inspect',
                                     '--repo', str(stage / 'assets/example')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('deploy/compose.yaml', json.loads(result.stdout)['compose_files'])
            output = Path(directory) / 'direct'
            exported = subprocess.run([sys.executable, '-B', str(stage / 'scripts/export_opskit.py'),
                                       '--output', str(output)], capture_output=True, text=True,
                                      env={**os.environ, 'PATH': str(Path(directory) / 'no-tools')})
            self.assertEqual(exported.returncode, 0, exported.stderr)
            with tarfile.open(output / 'opskit-direct.tar.gz') as archive:
                files = {member.name.removeprefix('opskit/'): archive.extractfile(member).read()
                         for member in archive.getmembers() if member.isfile()}
                self.assertEqual(archive.getmember('opskit/install.sh').mode, 0o755)
            files.pop('SHA256SUMS')
            self.assertEqual(files, {p.relative_to(runtime).as_posix(): p.read_bytes()
                                     for p in runtime.rglob('*') if p.is_file()})
            self.assertFalse(any(p.name == '.env' or '__pycache__' in p.parts for p in stage.rglob('*')))
            with patch.object(sys, 'dont_write_bytecode', True):
                builder = module('detached_airgap_builder', stage / 'scripts/build_bundle.py')
            self.assertEqual(builder.opskit_source(), runtime)
            with self.assertRaisesRegex(ValueError, '已存在'):
                packager.stage(stage)

    def test_runtime_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'private.py').symlink_to(ROOT / 'install.sh')
            with self.assertRaisesRegex(ValueError, '软链接'):
                packager.copy_clean(source, Path(directory) / 'clean', runtime_only=True)

    def test_default_image_file_cannot_change_non_image_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(common, 'ROOT', Path(directory)):
            defaults = {'TEST_IMAGE': 'official/app:1'}
            target = Path(directory) / 'image-defaults.json'
            target.write_text(json.dumps({'TEST_IMAGE': 'opskit/example:unique'}))
            self.assertEqual(common.image_defaults(defaults)['TEST_IMAGE'], 'opskit/example:unique')
            self.assertEqual(defaults['TEST_IMAGE'], 'official/app:1')
            for values in ({'OPS_ADMIN_PASSWORD': 'private'}, {'TEST_IMAGE': 'app:1\ncommand'}, []):
                target.write_text(json.dumps(values))
                with self.assertRaises(ValueError):
                    common.image_defaults(defaults)

    def test_management_cli_uses_locked_images_without_disclosing_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'management'
            packager.copy_clean(ROOT / 'management', destination, runtime_only=True)
            keys = ('WORKBENCH_IMAGE', 'NPM_IMAGE', 'SPUG_IMAGE', 'MARIADB_IMAGE', 'DBX_IMAGE', 'VICTORIALOGS_IMAGE')
            images = {key: 'opskit/example-' + key.lower() + ':locked' for key in keys}
            (destination / 'image-defaults.json').write_text(json.dumps(images))
            result = subprocess.run([str(destination / 'install.sh'), '--configure-only'],
                                    input='\n' * 30, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = common.load_env(destination / '.env')
            self.assertEqual({key: values[key] for key in keys}, images)
            self.assertNotIn(values['SPUG_DB_PASSWORD'], result.stdout + result.stderr)
            repeated = subprocess.run([str(destination / 'install.sh'), '--configure-only', '--reuse-config'],
                                      input='', text=True, capture_output=True)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(values, common.load_env(destination / '.env'))


if __name__ == '__main__':
    unittest.main()

"""验证交付字节、精确镜像身份及离线摘要回退，不连接真实 Docker。"""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
module_spec = importlib.util.spec_from_file_location('verify_ui', ROOT / 'management/scripts/verify_ui.py')
ui = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(ui)


class UIAssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.assets = Path(self.directory.name)
        self.versions = json.loads((ROOT / 'management/ui/versions.json').read_text())
        self.manifests = {}
        self.values = {'NPM_IMAGE': 'fixture/npm', 'SPUG_IMAGE': 'fixture/spug'}
        self.metadata = {}
        for name, spec in self.versions.items():
            folder = self.assets / name
            folder.mkdir()
            data = f'{name} prefixed static page'.encode()
            (folder / 'index.html').write_bytes(data)
            self.manifests[name] = {'index.html': ui.sha256(data)}
            self.metadata[name] = {'RepoDigests': ['fixture@' + spec['image_index_digest']]}
            if name == 'npm':
                self.metadata[name]['Config'] = {'Env': ['NPM_BUILD_VERSION=' + spec['version'],
                                                         'NPM_BUILD_COMMIT=' + spec['revision']]}
            else:
                self.metadata[name]['Config'] = {'Labels': {'org.opencontainers.image.version': spec['version'],
                                                            'org.opencontainers.image.revision': spec['revision']}}
        self.save()
        self.asset_patch = patch.object(ui, 'ASSETS', self.assets)
        self.asset_patch.start()
        self.addCleanup(self.asset_patch.stop)

    def save(self):
        (self.assets / 'versions.json').write_text(json.dumps(self.versions))
        (self.assets / 'static-hashes.json').write_text(json.dumps(self.manifests))

    def inspect(self, *args):
        self.assertEqual(args[:2], ('image', 'inspect'))
        return json.dumps([self.metadata[args[2].split('/')[-1]]]).encode()

    def verify(self, hub_only=False):
        with redirect_stdout(io.StringIO()):
            ui.verify(self.values, hub_only=hub_only)

    def test_known_digest_never_starts_a_container(self):
        with patch.object(ui, 'docker', side_effect=self.inspect) as docker:
            self.verify()
        self.assertEqual(docker.call_count, 2)

    def test_changed_or_extra_static_file_rejected_before_docker(self):
        for kind in ('changed', 'extra'):
            with self.subTest(kind=kind), patch.object(ui, 'docker') as docker:
                target = self.assets / 'npm' / ('index.html' if kind == 'changed' else 'unexpected.js')
                original = target.read_bytes() if target.exists() else None
                target.write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, '前端'):
                    self.verify()
                docker.assert_not_called()
                if original is None:
                    target.unlink()
                else:
                    target.write_bytes(original)

    def test_unknown_revision_rejected_even_with_a_known_digest(self):
        self.metadata['npm']['Config']['Env'][1] = 'NPM_BUILD_COMMIT=unknown'
        with patch.object(ui, 'docker', side_effect=self.inspect), self.assertRaisesRegex(RuntimeError, 'revision'):
            self.verify()

    def test_offline_image_uses_readonly_disconnected_original_index(self):
        self.metadata['npm']['RepoDigests'] = []
        original = b'original upstream index'
        self.versions['npm']['upstream_index_sha256'] = ui.sha256(original)
        self.save()

        def execute(*args):
            if args[0] == 'image':
                return self.inspect(*args)
            self.assertEqual(args, ('run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'cat',
                                    'fixture/npm', '/app/frontend/index.html'))
            return original

        with patch.object(ui, 'docker', side_effect=execute) as docker:
            self.verify()
        self.assertEqual(docker.call_count, 3)

    def test_unknown_digest_and_index_rejected(self):
        self.metadata['npm']['RepoDigests'] = []
        with patch.object(ui, 'docker', side_effect=[json.dumps([self.metadata['npm']]).encode(), b'unknown']), \
                self.assertRaisesRegex(RuntimeError, '原始前端'):
            self.verify()

    def test_hub_only_does_not_require_spug_assets_or_image(self):
        (self.assets / 'spug/index.html').unlink()
        del self.values['SPUG_IMAGE']
        with patch.object(ui, 'docker', side_effect=self.inspect) as docker:
            self.verify(hub_only=True)
        self.assertEqual(docker.call_count, 1)

    def test_real_shipped_assets_match_manifest_without_build_material(self):
        manifests = json.loads((ROOT / 'management/ui/static-hashes.json').read_text())
        for name, entries in manifests.items():
            folder = ROOT / 'management/ui' / name
            actual = {str(path.relative_to(folder)) for path in folder.rglob('*') if path.is_file()}
            self.assertEqual(actual, set(entries))
            for relative, digest in entries.items():
                self.assertEqual(ui.sha256((folder / relative).read_bytes()), digest, relative)
                self.assertFalse(relative.endswith('.map'), relative)
                self.assertNotIn('node_modules', Path(relative).parts)
                self.assertNotIn('test', Path(relative).parts)


if __name__ == '__main__':
    unittest.main()

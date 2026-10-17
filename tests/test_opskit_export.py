"""Round-trip and failure checks for the standalone OpsKit exporter."""
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/opskit/scripts'))
import export_opskit as exporter


class ExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'snapshot'
        self.output = self.root / 'output'
        self.files = ('README.md', 'install.sh', 'install.cmd', 'install.ps1',
                      'management/compose.yaml', 'collector/compose.yaml',
                      'management/.env.example', 'management/portal/vendor/pdf.mjs',
                      'management/documents/builtin/README.md')
        hashes = {}
        for name in self.files:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture\n')
            hashes[name] = exporter.digest(path)
        self.provenance = {'base_commit': 'a' * 40, 'working_tree_changes': False,
                           'sha256': {k: v for k, v in hashes.items() if '/documents/' not in k},
                           'documentation_sha256': {k: v for k, v in hashes.items() if '/documents/' in k}}
        (self.source / 'OPSKIT_SOURCE.json').write_text(json.dumps(self.provenance))
        source_patch = patch.object(exporter, 'opskit_source', return_value=self.source)
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def test_roundtrip_keeps_only_registered_runtime_and_documents(self):
        for name in ('management/.env', 'management/documents/customer.md', 'business.py'):
            (self.source / name).write_text('must not ship')
        archive = exporter.export_opskit(self.output)
        unpacked = self.root / 'unpacked'
        with tarfile.open(archive) as stream:
            for member in stream.getmembers():
                self.assertTrue(member.isfile() or member.isdir())
                self.assertFalse(PurePosixPath(member.name).is_absolute())
                self.assertNotIn('..', PurePosixPath(member.name).parts)
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
            self.assertEqual(stream.getmember('opskit/install.sh').mode, 0o755)
            stream.extractall(unpacked)
        package = unpacked / 'opskit'
        exporter.verify_checksums(package)
        actual = {p.relative_to(package).as_posix() for p in package.rglob('*') if p.is_file()}
        self.assertEqual(actual, set(self.files) | {'OPSKIT_SOURCE.json', 'SHA256SUMS'})
        for name in self.files:
            self.assertEqual((package / name).read_bytes(), (self.source / name).read_bytes())
        checksum = Path(str(archive) + '.sha256').read_text()
        self.assertEqual(checksum, '%s  %s\n' % (exporter.digest(archive), archive.name))
        self.assertFalse(any(p.name.startswith('.opskit-direct-') for p in self.output.iterdir()))

    def test_corrupt_snapshot_publishes_nothing(self):
        (self.source / 'README.md').write_text('changed')
        with self.assertRaisesRegex(exporter.BundleError, '快照校验失败'):
            exporter.export_opskit(self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_archive_or_orphan_checksum_is_never_overwritten(self):
        for name in ('opskit-direct.tar.gz', 'opskit-direct.tar.gz.sha256'):
            with self.subTest(name=name):
                self.output.mkdir(exist_ok=True)
                existing = self.output / name
                existing.write_bytes(b'original')
                with self.assertRaisesRegex(exporter.BundleError, '拒绝覆盖'):
                    exporter.export_opskit(self.output)
                self.assertEqual(existing.read_bytes(), b'original')
                existing.unlink()

    def test_registered_symlink_is_rejected(self):
        path = self.source / 'README.md'
        path.unlink()
        path.symlink_to(self.source / 'install.sh')
        with self.assertRaisesRegex(exporter.BundleError, '软链接'):
            exporter.export_opskit(self.output)
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == '__main__':
    unittest.main()

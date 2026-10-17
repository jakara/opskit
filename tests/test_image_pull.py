"""重放镜像源拒绝、网络错误与离线缺镜像；不操作本机 Docker。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_installer import common, management


OLD_SPUG = 'openspug/spug:4.1'
NEW_SPUG = 'ghcr.io/openspug/spug:4.1'
ALLOWLIST = 'error from registry: 这镜像不在白名单. this image is not in the allowlist.'


class ImagePullTests(unittest.TestCase):
    def prepare(self, images, failures=None, offline=False, locked=False, overrides=None, literal=False,
                config_name='.env'):
        failures = {key: list(value) for key, value in (failures or {}).items()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / config_name
            config.parent.mkdir(parents=True, exist_ok=True)
            values = dict(images, OPS_ADMIN_PASSWORD='private-value-not-in-output')
            config.write_text(common.env_text(values))
            if locked:
                (root / 'image-defaults.json').write_text(json.dumps(images))
            calls = []

            def docker(args, **kwargs):
                calls.append(args)
                if args[-3:] == ['config', '--format', 'json']:
                    resolved = images if literal else common.load_env(config)
                    document = {'services': {key: {'image': resolved[key]} for key in images}}
                    return subprocess.CompletedProcess(args, 0, json.dumps(document))
                image = args[-1]
                errors = failures.get(image, [])
                if errors:
                    raise subprocess.CalledProcessError(1, args, output=errors.pop(0))
                return subprocess.CompletedProcess(args, 0, '')

            output, error = io.StringIO(), None
            with patch.object(common, 'ROOT', root), patch.object(common.subprocess, 'run', side_effect=docker), \
                    patch.object(common.time, 'sleep') as sleep, patch.dict(os.environ, overrides or {}, clear=True), \
                    contextlib.redirect_stdout(output):
                try:
                    common.prepare_images(values, config, offline=offline)
                except RuntimeError as exc:
                    error = str(exc)
            self.assertNotIn('private-value-not-in-output', output.getvalue() + (error or ''))
            return common.load_env(config), calls, error, sleep.call_count, output.getvalue()

    def test_management_allowlist_recovers_without_interrupting_other_images(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            values = management.configure({}, interactive=False)
            values['SPUG_IMAGE'] = OLD_SPUG
            initial = common.env_text(values)
            (root / '.env').write_text(initial)
            calls = []

            def docker(args, **kwargs):
                calls.append(args)
                if args == ['docker', 'compose', 'pull'] or args == ['docker', 'pull', OLD_SPUG]:
                    raise subprocess.CalledProcessError(1, args, output=ALLOWLIST)
                if args[-3:] == ['config', '--format', 'json']:
                    saved = common.load_env(root / '.env')
                    return subprocess.CompletedProcess(args, 0, json.dumps({'services': {
                        'spug': {'image': saved['SPUG_IMAGE']},
                        'spugdb': {'image': saved['MARIADB_IMAGE']},
                        'dbx': {'image': saved['DBX_IMAGE']}}}))
                return subprocess.CompletedProcess(args, 0, '')

            initialize, check = Mock(), Mock()
            with patch.object(management, 'ROOT', root), patch.object(common, 'ROOT', root), \
                    patch.object(management, 'configure', return_value=values), \
                    patch.object(management, 'docker_ready'), \
                    patch.object(management, 'start_discovery', side_effect=lambda: calls.append(['discovery'])), \
                    patch.object(common.subprocess, 'run', side_effect=docker), \
                    patch.dict(sys.modules, {'configure_proxy': SimpleNamespace(initialize=initialize),
                                             'check': SimpleNamespace(check=check)}), \
                    patch.object(sys, 'argv', ['install.py']), patch.dict(os.environ, {}, clear=True):
                management.main()
            saved = common.load_env(root / '.env')
            self.assertEqual(saved['SPUG_IMAGE'], NEW_SPUG)
            for key in management.PASSWORD_KEYS:
                self.assertEqual(saved[key], values[key])
            self.assertIn(initial, [p.read_text() for p in (root / '.backups').glob('*/.env')])
            self.assertIn(['docker', 'pull', values['MARIADB_IMAGE']], calls)
            self.assertIn(['docker', 'pull', values['DBX_IMAGE']], calls)
            self.assertGreater(calls.index(['discovery']), calls.index(['docker', 'pull', values['DBX_IMAGE']]))
            initialize.assert_called_once()
            check.assert_called_once()

    def test_transient_network_failure_retries_once(self):
        image = 'mariadb:10.11'
        _, calls, error, sleeps, _ = self.prepare({'MARIADB_IMAGE': image}, {image: ['i/o timeout']})
        self.assertIsNone(error)
        self.assertEqual(calls.count(['docker', 'pull', image]), 2)
        self.assertEqual(sleeps, 1)

    def test_failures_are_aggregated_and_other_images_still_download(self):
        images = {'A_IMAGE': 'private.example/a:1', 'B_IMAGE': 'private.example/b:1', 'MARIADB_IMAGE': 'mariadb:10.11'}
        _, calls, error, sleeps, _ = self.prepare(images, {
            images['A_IMAGE']: [ALLOWLIST], images['B_IMAGE']: ['unauthorized: authentication required']})
        self.assertIn('白名单', error)
        self.assertIn('docker login', error)
        self.assertIn(images['A_IMAGE'], error)
        self.assertIn(images['B_IMAGE'], error)
        self.assertIn(['docker', 'pull', images['MARIADB_IMAGE']], calls)
        self.assertNotIn('returned non-zero exit status', error)
        self.assertEqual(sleeps, 0)

    def test_custom_digest_locked_and_environment_overrides_are_not_rewritten(self):
        for image, locked, overrides in (
                ('private.example/spug:4.1', False, {}),
                ('openspug/spug@sha256:' + 'a' * 64, False, {}),
                (OLD_SPUG, True, {}), (OLD_SPUG, False, {'SPUG_IMAGE': OLD_SPUG})):
            with self.subTest(image=image, locked=locked, overrides=overrides):
                saved, calls, error, _, _ = self.prepare({'SPUG_IMAGE': image}, {image: [ALLOWLIST]},
                                                        locked=locked, overrides=overrides)
                self.assertEqual(saved['SPUG_IMAGE'], image)
                self.assertIsNotNone(error)
                self.assertEqual([c for c in calls if c[:2] == ['docker', 'pull']], [['docker', 'pull', image]])

    def test_storage_and_architecture_failures_do_not_change_registry(self):
        for output in ('no space left on device', 'no matching manifest for linux/arm64/v8'):
            with self.subTest(output=output):
                saved, calls, error, _, _ = self.prepare({'SPUG_IMAGE': OLD_SPUG}, {OLD_SPUG: [output]})
                self.assertIsNotNone(error)
                self.assertEqual(saved['SPUG_IMAGE'], OLD_SPUG)
                self.assertNotIn(['docker', 'pull', NEW_SPUG], calls)

    def test_both_official_sources_fail_without_changing_configuration(self):
        saved, calls, error, sleeps, _ = self.prepare({'SPUG_IMAGE': OLD_SPUG}, {
            OLD_SPUG: [ALLOWLIST], NEW_SPUG: ['i/o timeout', 'i/o timeout']})
        self.assertEqual(saved['SPUG_IMAGE'], OLD_SPUG)
        self.assertIn(OLD_SPUG, error)
        self.assertIn(NEW_SPUG, error)
        self.assertEqual(calls.count(['docker', 'pull', NEW_SPUG]), 2)
        self.assertEqual(sleeps, 1)

    def test_dbx_uses_fixed_version_and_official_sources_work_both_ways(self):
        for key, source, expected in (
                ('DBX_IMAGE', 't8y2/dbx:0.6.40', 'docker.cnb.cool/dbxio.com/dbx:0.6.40'),
                ('SPUG_IMAGE', NEW_SPUG, OLD_SPUG)):
            with self.subTest(source=source):
                saved, calls, error, _, _ = self.prepare({key: source}, {source: [ALLOWLIST]})
                self.assertIsNone(error)
                self.assertEqual(saved[key], expected)
                self.assertIn(['docker', 'pull', expected], calls)
                self.assertNotIn('latest', str(calls))

    def test_offline_checks_all_missing_images_and_never_pulls(self):
        images = {'SPUG_IMAGE': OLD_SPUG, 'DBX_IMAGE': 't8y2/dbx:0.6.40'}
        saved, calls, error, sleeps, _ = self.prepare(images, {image: ['No such image'] for image in images.values()}, offline=True)
        self.assertIn('docker load', error)
        for key, image in images.items():
            self.assertEqual(saved[key], image)
            self.assertIn(image, error)
            self.assertIn(['docker', 'image', 'inspect', image], calls)
        self.assertFalse(any('pull' in c for c in calls))
        self.assertEqual(sleeps, 0)
        _, calls, error, _, _ = self.prepare(images, offline=True)
        self.assertIsNone(error)
        self.assertFalse(any('pull' in c for c in calls))

    def test_compose_override_cannot_start_an_unprepared_image(self):
        _, _, error, _, _ = self.prepare({'SPUG_IMAGE': OLD_SPUG}, {OLD_SPUG: [ALLOWLIST]}, literal=True)
        self.assertIn('Compose 实际镜像', error)

    def test_hub_failure_resume_uses_standalone_scope(self):
        _, _, error, _, _ = self.prepare({'NPM_IMAGE': 'jc21/nginx-proxy-manager:2.16.0'},
                                         {'jc21/nginx-proxy-manager:2.16.0': [ALLOWLIST],
                                          'm.daocloud.io/docker.io/jc21/nginx-proxy-manager:2.16.0': [ALLOWLIST]},
                                         config_name='.workbench/hub.env')
        self.assertIn('./install.sh --hub-only', error)

    def test_registered_domestic_sources_fallback_without_changing_versions(self):
        for key, upstream in (
                ('WORKBENCH_IMAGE', 'library/python:3.12-alpine'),
                ('NPM_IMAGE', 'jc21/nginx-proxy-manager:2.16.0'),
                ('MARIADB_IMAGE', 'library/mariadb:10.11'),
                ('VICTORIALOGS_IMAGE', 'victoriametrics/victoria-logs:v1.53.0'),
                ('FLUENTBIT_IMAGE', 'fluent/fluent-bit:5.1.4')):
            official = upstream.removeprefix('library/')
            domestic = 'm.daocloud.io/docker.io/' + upstream
            with self.subTest(key=key):
                saved, calls, error, _, _ = self.prepare({key: official}, {official: [ALLOWLIST]})
                self.assertIsNone(error)
                self.assertEqual(saved[key], domestic)
                self.assertIn(['docker', 'pull', domestic], calls)
                saved, calls, error, _, _ = self.prepare({key: domestic}, {domestic: [ALLOWLIST]})
                self.assertIsNone(error)
                self.assertEqual(saved[key], official)
                saved, calls, error, _, _ = self.prepare({key: domestic}, {domestic: [ALLOWLIST]}, locked=True)
                self.assertIsNotNone(error)
                self.assertNotIn(['docker', 'pull', official], calls)

    def test_unresolved_images_prevent_management_background_and_startup(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            values = management.configure({}, interactive=False)
            with patch.object(management, 'ROOT', root), patch.object(management, 'configure', return_value=values), \
                    patch.object(management, 'docker_ready'), patch.object(management, 'run') as run, \
                    patch.object(management, 'prepare_images', side_effect=RuntimeError('镜像缺失')), \
                    patch.object(management, 'start_discovery') as discovery, patch.object(sys, 'argv', ['install.py']):
                with self.assertRaisesRegex(RuntimeError, '镜像缺失'):
                    management.main()
            discovery.assert_not_called()
            self.assertFalse(any('up' in c.args[0] for c in run.call_args_list))


if __name__ == '__main__':
    unittest.main()

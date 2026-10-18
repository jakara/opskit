"""完全重装的确认、归属检查和清空后配置重读；不删除真实 Docker 资源。"""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_installer import common, management


class CleanReinstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(management, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.plan = {'containers': [{'Name': '/current-opskit'}], 'volumes': [{'name': 'owned-data'}]}
        self.engine = SimpleNamespace(inventory=Mock(return_value=self.plan), uninstall=Mock(return_value=self.root / 'backup'))
        self.run = Mock(side_effect=[SimpleNamespace(stdout=json.dumps({'volumes': {'data': {'name': 'owned-data'}}})),
                                     SimpleNamespace(stdout='owned-data\n')])

    def reset(self, answer='yes', hub_only=False):
        with patch.dict(sys.modules, {'uninstall': self.engine}), patch.object(management, 'docker_ready'), \
                patch.object(management, 'run', self.run), patch('builtins.input', return_value=answer), \
                patch.object(management, 'installation_control', return_value=contextlib.nullcontext()) as guard:
            management.clean_reinstall(self.root / ('.workbench/hub.env' if hub_only else '.env'), hub_only=hub_only)
            return guard

    def test_confirmation_is_required_before_stopping_or_removing_anything(self):
        with self.assertRaisesRegex(RuntimeError, '已取消'):
            self.reset('no')
        self.engine.uninstall.assert_not_called()
        self.run.side_effect = [SimpleNamespace(stdout=json.dumps({'volumes': {'data': {'name': 'owned-data'}}})), SimpleNamespace(stdout='owned-data\n')]
        guard = self.reset()
        guard.assert_called_once()
        self.engine.uninstall.assert_called_once_with([self.root], purge=True, yes=True)

    def test_hub_reset_uses_its_own_scope_and_does_not_stop_full_control(self):
        guard = self.reset(hub_only=True)
        guard.assert_not_called()
        self.engine.uninstall.assert_called_once_with([self.root], hub_only=True, purge=True, yes=True)
        self.assertIn('hub.compose.yaml', self.run.call_args_list[0].args[0])

    def test_unknown_existing_volume_and_backup_failure_block_reinstall(self):
        self.plan['orphans'] = ['orphan-data']
        with self.assertRaisesRegex(RuntimeError, '孤立卷'):
            self.reset()
        self.run.assert_not_called()
        self.plan['orphans'] = []
        self.plan['volumes'] = []
        with self.assertRaisesRegex(RuntimeError, '未能由当前容器确认归属'):
            self.reset()
        self.engine.uninstall.assert_not_called()
        self.plan['volumes'] = [{'name': 'owned-data'}]
        self.run.side_effect = [SimpleNamespace(stdout=json.dumps({'volumes': {'data': {'name': 'owned-data'}}})), SimpleNamespace(stdout='owned-data\n')]
        self.engine.uninstall.side_effect = RuntimeError('备份核验失败')
        with self.assertRaisesRegex(RuntimeError, '备份核验失败'):
            self.reset()

    def test_configure_only_and_legacy_source_cannot_clear_data(self):
        with patch.object(management, 'docker_ready') as docker:
            with self.assertRaisesRegex(ValueError, 'configure-only'):
                management.clean_reinstall(self.root / '.env', configure_only=True)
            with self.assertRaisesRegex(ValueError, '旧版配置'):
                management.clean_reinstall(self.root.parent / '.env')
            docker.assert_not_called()

    def test_third_choice_works_with_saved_draft_and_propagates_failure(self):
        config = self.root / '.env'
        config.touch()
        reset = Mock()
        with patch('builtins.input', return_value='3'):
            self.assertTrue(common.choose_configuration(config, draft={'OPS_PUBLIC_HOST': 'old.internal'}, reinstall=reset))
        reset.assert_called_once()
        reset.side_effect = RuntimeError('不能清空')
        with patch('builtins.input', return_value='3'), self.assertRaisesRegex(RuntimeError, '不能清空'):
            common.choose_configuration(config, reinstall=reset)

    def test_main_does_not_reuse_old_values_or_draft_after_reset(self):
        old = management.configure({}, interactive=False)
        old['OPS_PUBLIC_HOST'] = 'old.internal'
        config = self.root / '.env'
        config.write_text(common.env_text(old))
        progress = self.root / '.env.install-progress'
        progress.write_text(common.env_text(dict(old, OPS_PUBLIC_HOST='draft.internal')))
        def clear(*args):
            config.unlink()
            progress.unlink()
        original = management.configure
        fresh = {}
        def configure(existing, reconfigure, **kwargs):
            self.assertEqual(existing, {})
            self.assertEqual(kwargs['draft'], {})
            self.assertTrue(reconfigure)
            fresh.update(original(existing, reconfigure, interactive=False, **kwargs))
            raise EOFError()
        with patch.object(sys, 'argv', ['install.py']), patch('builtins.input', return_value='3'), \
                patch.object(management, 'clean_reinstall', side_effect=clear), \
                patch.object(management, 'configure', side_effect=configure), self.assertRaises(EOFError):
            management.main()
        for key in management.PASSWORD_KEYS:
            self.assertNotEqual(old[key], fresh[key])
        self.assertEqual(fresh['OPS_PUBLIC_HOST'], 'ops.example.internal')

    def test_hub_rereads_config_and_creates_fresh_credentials(self):
        old = management.configure({}, interactive=False)
        old['OPSKIT_HTTP_PORT'] = '19390'
        config = self.root / '.workbench/hub.env'
        config.parent.mkdir()
        config.write_text(common.env_text(old))
        with patch('builtins.input', side_effect=['3', '', '']), \
                patch.object(management, 'clean_reinstall', side_effect=lambda *args, **kwargs: config.unlink()), \
                patch.object(management, 'docker_ready', side_effect=EOFError), self.assertRaises(EOFError):
            management.install_hub(None)
        fresh = common.load_env(config)
        self.assertEqual(fresh['OPSKIT_HTTP_PORT'], '80')
        for key in ('NPM_ADMIN_PASSWORD', 'OPS_ADMIN_PASSWORD'):
            self.assertNotEqual(fresh[key], old[key])

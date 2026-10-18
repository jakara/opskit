"""自管理初始化顺序、失败脱敏和凭据不进入命令参数。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_installer import ROOT, module

self_management = module('self_management', ROOT / 'management/scripts/self_management.py')


class SelfManagementTests(unittest.TestCase):
    def test_spug_input_is_stdin_and_errors_are_sanitized(self):
        for code in (0, 1):
            with patch.object(self_management.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=code, stdout='{"created": 1}', stderr='private-secret')) as run:
                if code:
                    with self.assertRaises(RuntimeError) as raised:
                        self_management.spug_run('script', {'PASSWORD': 'private-secret'})
                    self.assertNotIn('private-secret', str(raised.exception))
                else:
                    self.assertEqual(self_management.spug_run('script', {'PASSWORD': 'private-secret'}), {'created': 1})
                self.assertNotIn('private-secret', str(run.call_args.args))
                self.assertIn('private-secret', run.call_args.kwargs['input'])

    def test_health_failure_prevents_claiming_initialization_complete(self):
        import dbx_bootstrap
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scripts').mkdir()
            (root / 'scripts/spug_bootstrap.py').write_text('bootstrap')
            supervisor = '\n'.join(name + ' RUNNING' for name in ('spug-monitor', 'spug-scheduler', 'spug-worker'))
            with patch.object(self_management, 'ROOT', root), patch.object(self_management.subprocess, 'run', return_value=SimpleNamespace(stdout=supervisor)), \
                    patch.object(self_management, 'spug_run', side_effect=[{'tasks': [7], 'conflicts': []}, {'healthy': False, 'services': [{'service': 'dbx', 'healthy': False}]}]), \
                    patch.object(self_management.time, 'monotonic', side_effect=[0, 0, 100]), \
                    patch.object(dbx_bootstrap, 'initialize') as initialize, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, '健康巡检'):
                    self_management.initialize({'OPS_PUBLIC_SCHEME': 'http', 'OPS_PUBLIC_HOST': 'localhost', 'OPS_PUBLIC_PORT': '80', 'SPUG_ADMIN_USER': 'admin'})
                initialize.assert_not_called()
                self.assertFalse((root / '.workbench/self-management.json').exists())


if __name__ == '__main__':
    unittest.main()

"""Windows entry-point contracts; real PowerShell flow checks live beside this file."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WindowsBootstrapTests(unittest.TestCase):
    def test_independent_modules_keep_identical_bootstrap(self):
        management = ROOT / 'management/scripts/bootstrap.ps1'
        collector = ROOT / 'collector/scripts/bootstrap.ps1'
        self.assertEqual(management.read_bytes(), collector.read_bytes())
        for path in [ROOT / 'install.ps1', ROOT / 'management/install.ps1',
                     ROOT / 'collector/install.ps1', management, collector]:
            self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'), path)

    def test_windows_launchers_keep_security_policy_local(self):
        for folder in [ROOT, ROOT / 'management', ROOT / 'collector']:
            launcher = (folder / 'install.cmd').read_text()
            self.assertIn('"%~dp0install.ps1" %*', launcher)
            self.assertIn('RemoteSigned', launcher)
            self.assertNotIn('Bypass', launcher)
        bootstrap = (ROOT / 'management/scripts/bootstrap.ps1').read_text(encoding='utf-8-sig')
        self.assertNotIn('Set-ExecutionPolicy', bootstrap)
        self.assertNotIn('--accept-license', bootstrap)
        self.assertIn("$Id -eq 'Docker.DockerDesktop'", bootstrap)
        self.assertIn("'--interactive'", bootstrap)

    def test_windows_switches_reach_existing_python_installers(self):
        for module in ['management', 'collector']:
            text = (ROOT / module / 'install.ps1').read_text(encoding='utf-8-sig')
            for flag in ['--configure-only', '--offline', '--reconfigure']:
                self.assertIn(flag, text)
            self.assertIn('Invoke-OpsKitInstaller', text)
        management = (ROOT / 'management/install.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("'--hub-only', '--port'", management)


if __name__ == '__main__':
    unittest.main()

"""Contracts for Windows uninstall entry points; PowerShell flow tests are separate."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WindowsUninstallTests(unittest.TestCase):
    def test_launchers_preserve_arguments_and_exit_status(self):
        for folder in [ROOT, ROOT / 'management', ROOT / 'collector']:
            text = (folder / 'uninstall.cmd').read_text()
            self.assertIn('"%~dp0uninstall.ps1" %*', text)
            self.assertIn('RemoteSigned', text)
            self.assertIn('exit /b %errorlevel%', text)
            self.assertNotIn('Bypass', text)
            self.assertTrue((folder / 'uninstall.ps1').read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_uninstall_only_reuses_existing_python(self):
        for module in ['management', 'collector']:
            text = (ROOT / module / 'uninstall.ps1').read_text(encoding='utf-8-sig')
            self.assertIn('Find-OpsKitPython', text)
            self.assertNotIn('Get-OpsKitPython', text)
            self.assertNotIn('Initialize-OpsKitDocker', text)
            self.assertNotIn('Install-OpsKitPackage', text)
            self.assertNotIn('Set-ExecutionPolicy', text)
            self.assertIn('finally', text)
            self.assertIn('$env:PYTHONUTF8 = $oldUtf8', text)
            self.assertIn('$env:PYTHONIOENCODING = $oldIoEncoding', text)
            self.assertIn('$env:Path = $oldPath', text)
            self.assertIn('$code = $LASTEXITCODE', text)
            self.assertIn('exit $code', text)
            for flag in ['--yes', '--purge-data', '--dry-run']:
                self.assertIn(flag, text)

    def test_root_routes_all_explicit_scopes(self):
        text = (ROOT / 'uninstall.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("[string]$Module = 'all'", text)
        self.assertIn("'hub'", text)
        self.assertIn('-HubOnly', text)
        self.assertIn('-All', text)
        self.assertIn('exit $LASTEXITCODE', text)
        self.assertIn('collector/uninstall.ps1', text)
        management = (ROOT / 'management/uninstall.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("'--all'", management)
        self.assertIn("'--hub-only'", management)


if __name__ == '__main__':
    unittest.main()

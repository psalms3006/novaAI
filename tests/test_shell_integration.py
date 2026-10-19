import unittest
from unittest.mock import patch, MagicMock
from actions.open_app import execute as open_app_execute
from actions.computer_settings import _winget
import subprocess

class TestShellIntegration(unittest.TestCase):
    def test_open_app_integration(self):
        # Test safe execution through open_app
        with patch('os.startfile') as mock_start:
            result = open_app_execute({"app_name": "notepad"})
            self.assertEqual(result, "Opened notepad.")
            mock_start.assert_called_with("notepad")

    @patch('actions.computer_settings.shutil.which', return_value="C:\\Windows\\System32\\winget.exe")
    def test_computer_settings_winget_integration(self, mock_which):
        from core.shell_manager import ShellResult
        # Test computer_settings _winget integration with ShellManager
        with patch('actions.computer_settings.execute_safe') as mock_execute:
            # Use real ShellResult object
            mock_execute.return_value = ShellResult(0, "test output", "")
            
            code, out = _winget("list")
            self.assertEqual(code, 0)
            self.assertEqual(out, "test output")
            mock_execute.assert_called()

    @patch('actions.computer_settings.shutil.which', return_value=None)
    def test_computer_settings_winget_missing(self, mock_which):
        # Test behavior when winget is missing
        with self.assertRaises(FileNotFoundError):
            _winget("list")

if __name__ == "__main__":
    unittest.main()

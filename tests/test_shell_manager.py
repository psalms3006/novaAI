import unittest
from core.shell_manager import execute_safe

class TestShellManager(unittest.TestCase):
    def test_safe_execution(self):
        # A safe, standard command that should exist
        result = execute_safe(["python", "--version"])
        self.assertTrue(result.is_success())
        self.assertIn("Python", result.stdout)

    def test_sanitization(self):
        # Should fail due to shell metacharacters
        result = execute_safe(["echo", "hello; rm -rf /"])
        self.assertFalse(result.is_success())
        self.assertIn("Potentially dangerous", result.error)

    def test_missing_executable(self):
        # Should fail with a clear error
        result = execute_safe(["nonexistentcommand12345"])
        self.assertFalse(result.is_success())
        self.assertIn("Executable not found", result.error)

if __name__ == "__main__":
    unittest.main()

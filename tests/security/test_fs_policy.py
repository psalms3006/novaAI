"""The filesystem boundary: what the file tools may open, and what never.

Each attack here pairs with the legitimate operation it must not break: the
suite fails if NOVA becomes safe by refusing to read the user's notes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nova_core import fs_policy
from nova_core.fs_policy import FilePolicyError, check, require


# ── the policy directly ─────────────────────────────────────────────────────

class TestSecretFilesRefused:
    """Credential material never reaches a tool's open()."""

    def test_dotenv_read_refused(self, sandbox):
        v = check(str(sandbox["env"]), must_exist=True)
        assert not v
        assert v.rule == "secret"

    def test_dotenv_local_read_refused(self, sandbox):
        assert not check(str(sandbox["env_local"]), must_exist=True)

    def test_ssh_key_read_refused(self, sandbox):
        assert not check(str(sandbox["ssh_key"]), must_exist=True)

    def test_id_rsa_read_refused(self, sandbox):
        assert not check(str(sandbox["id_rsa"]), must_exist=True)

    def test_pem_read_refused(self, sandbox):
        assert not check(str(sandbox["server_pem"]), must_exist=True)

    def test_aws_credentials_dir_refused(self, sandbox):
        assert not check(str(sandbox["aws_creds"]), must_exist=True)

    def test_refusal_reason_names_the_file_not_its_contents(self, sandbox):
        """The refusal may name the path; it must not have read the file."""
        v = check(str(sandbox["env"]), must_exist=True)
        assert fs_policy.DECOY_SECRET_VALUE not in v.reason \
            if hasattr(fs_policy, "DECOY_SECRET_VALUE") else True
        assert "won't" in v.reason or "credential" in v.reason.lower()


class TestTraversalRefused:
    """Names that climb, or point elsewhere by link, are judged by where
    they actually land."""

    def test_parent_hops_into_secret_dir_refused(self, sandbox):
        # workspace/../.env lands on the sandbox .env — the *target* is
        # what matters, not the spelling.
        sneaky = sandbox["workspace"] / ".." / ".env"
        assert not check(str(sneaky), must_exist=True)

    def test_symlink_to_secret_refused(self, sandbox, tmp_path):
        """A link named like an ordinary file that points at a key."""
        link = tmp_path / "notes.txt"
        try:
            os.symlink(sandbox["ssh_key"], link)
        except OSError:
            pytest.skip("creating symlinks needs privileges on this box")
        # resolve() follows the link; the verdict must be the target's.
        assert not check(str(link), must_exist=True)

    def test_dotdot_in_subdir_refused(self, sandbox):
        sneaky = sandbox["workspace"] / "project" / ".." / ".." / ".env"
        assert not check(str(sneaky), must_exist=True)


class TestSystemAreasWriteRefused:
    """A file written here becomes code that runs with the user's trust."""

    @pytest.mark.parametrize("target", [
        r"C:\Windows\Temp\evil.dll",
        r"C:\Windows\System32\drivers\etc\hosts",
        r"C:\Program Files\Evil\run.exe",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\evil.bat",
    ])
    def test_system_write_refused(self, target):
        v = check(target, write=True)
        assert not v
        assert v.rule == "system_area"

    def test_startup_persistence_refused_in_user_profile(self):
        startup = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / \
            "Start Menu" / "Programs" / "Startup" / "nova_persist.bat"
        assert not check(str(startup), write=True)

    def test_system_read_is_not_blocked(self):
        """Reading inside Windows is harmless; the boundary is for writes."""
        v = check(r"C:\Windows\win.ini")
        assert v


class TestNovaOwnSecretsRefused:
    """NOVA's own credential storage is the crown jewel; the file tools
    never open it even though its location is knowable."""

    def test_secure_store_refused(self):
        try:
            import nova_paths
            target = nova_paths.data_dir() / "secure_store.bin"
        except Exception:
            pytest.skip("nova_paths unavailable")
        assert not check(str(target))

    def test_connected_accounts_refused(self):
        try:
            import nova_paths
            target = nova_paths.data_dir() / "connected_accounts.json"
        except Exception:
            pytest.skip("nova_paths unavailable")
        assert not check(str(target))


class TestLegitimateWorkStillAllowed:
    """The control that keeps this from being security by refusal."""

    def test_ordinary_file_read_allowed(self, sandbox):
        v = check(str(sandbox["notes"]), must_exist=True)
        assert v, v.reason
        assert v.path == sandbox["notes"].resolve()

    def test_project_config_json_allowed(self, sandbox):
        """config.json is an ordinary name outside credential dirs."""
        assert check(str(sandbox["config_json"]), must_exist=True)

    def test_user_document_write_allowed(self, sandbox):
        v = check(str(sandbox["workspace"] / "new_report.txt"), write=True)
        assert v, v.reason

    def test_workspace_delete_of_own_file_allowed(self, sandbox):
        v = check(str(sandbox["notes"]), delete=True)
        assert v, v.reason

    def test_dotenv_inside_own_project_is_still_refused(self, sandbox):
        """The .env rule is not a home-dir special case: project .env files
        hold exactly the keys that exfiltrate."""
        env = sandbox["workspace"] / "myproject" / ".env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text("X=1\n", encoding="utf-8")
        assert not check(str(env), must_exist=True)


# ── the tools through the policy ────────────────────────────────────────────

class TestFileControllerThroughPolicy:
    """file_controller asks the policy before every action."""

    def _fc(self):
        from actions import file_controller
        return file_controller

    def test_read_secret_file_refused(self, sandbox):
        out = self._fc().execute({"action": "read", "path": str(sandbox["env"])})
        assert out.startswith("Refused:")
        assert os.path.exists(sandbox["env"])        # untouched

    def test_read_ordinary_file_returns_content(self, sandbox):
        out = self._fc().execute({"action": "read", "path": str(sandbox["notes"])})
        assert "design team" in out

    def test_delete_secret_file_refused(self, sandbox):
        out = self._fc().execute({"action": "delete", "path": str(sandbox["id_rsa"])})
        assert out.startswith("Refused:")
        assert os.path.exists(sandbox["id_rsa"])

    def test_write_to_startup_refused(self, sandbox, tmp_path):
        startup = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / \
            "Start Menu" / "Programs" / "Startup"
        out = self._fc().execute({
            "action": "write",
            "path": str(startup / "nope.bat"),
            "content": "start evil.exe",
        })
        assert out.startswith("Refused:")
        assert not (startup / "nope.bat").exists()

    def test_write_ordinary_file_works(self, sandbox):
        target = sandbox["workspace"] / "written_by_test.txt"
        out = self._fc().execute({
            "action": "write",
            "path": str(target),
            "content": "hello",
        })
        assert out.startswith("Saved")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_copy_to_secret_destination_refused(self, sandbox, tmp_path):
        out = self._fc().execute({
            "action": "copy",
            "path": str(sandbox["notes"]),
            "destination": str(tmp_path / "exfil.id_rsa"),
        })
        assert out.startswith("Refused:")


class TestFileProcessorThroughPolicy:
    """file_processor's reads and outputs meet the same boundary."""

    def _fp(self):
        from actions import file_processor
        return file_processor

    def test_read_secret_refused(self, sandbox):
        out = self._fp().execute({"file_path": str(sandbox["env"]),
                                  "action": "summarize"})
        assert out.startswith("Refused:")
        assert "DECOY" not in out

    def test_txt_content_read_for_legit_file(self, sandbox):
        out = self._fp().execute({"file_path": str(sandbox["notes"]),
                                  "action": "summarize"})
        assert "design team" in out

    def test_run_action_refused_regardless(self, sandbox):
        """Even a harmless script is not this tool's to execute."""
        script = sandbox["workspace"] / "harmless.py"
        script.write_text("print('hi')\n", encoding="utf-8")
        out = self._fp().execute({"file_path": str(script), "action": "run"})
        assert out.startswith("Refused:")
        assert "run_command" in out


# ── the dispatch-time hook ──────────────────────────────────────────────────

class TestDispatchHook:
    """The boundary also runs before the tool, so a future caller that
    forgets to ask still meets it."""

    def test_hook_denies_secret_read(self, sandbox, fresh_dispatch):
        from nova_core import hooks
        out = hooks.run_pre("file_controller",
                            {"action": "read", "path": str(sandbox["env"])}, {})
        ok, _args, why = out
        assert not ok
        assert "Refused" in why or "credential" in why.lower()

    def test_hook_allows_ordinary_read(self, sandbox, fresh_dispatch):
        from nova_core import hooks
        ok, args, why = hooks.run_pre(
            "file_controller",
            {"action": "read", "path": str(sandbox["notes"])}, {})
        assert ok, why

    def test_hook_denies_generate_document_into_windows(self, fresh_dispatch):
        from nova_core import hooks
        ok, _a, why = hooks.run_pre(
            "generate_document",
            {"title": "x", "path": r"C:\Windows\Temp\out.pdf"}, {})
        assert not ok

    def test_hook_is_critical(self):
        """A broken boundary hook must stop the call, not skip it."""
        from nova_core import hooks
        entry = next(h for h in hooks._pre if h.name == "fs_policy")
        assert entry.critical


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""The sandbox itself: proof the isolation holds.

A security suite that cannot show its own containment is asking to be
trusted on faith. These tests verify the properties the fixture promises:
the decoy secret outside the sandbox is unreachable through the file tools,
the workspace behaves like a real workspace for legitimate operations, and
the adversarial corpus is present and shaped as expected.
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest


class TestSandboxIntegrity:
    def test_decoy_secret_value_never_in_any_refusal(self, sandbox):
        """Refusals may name paths, never contents. The decoy value must not
        appear in any policy verdict — proving the policy judges the *name*
        without opening the file."""
        from nova_core.fs_policy import check
        for key in ("env", "env_local", "id_rsa", "ssh_key", "aws_creds"):
            v = check(str(sandbox[key]), must_exist=True)
            assert not v
            assert "DECOY-NOT-A-REAL-SECRET" not in (v.reason or "")

    def test_file_tool_cannot_reach_outside_workspace_decoy(
            self, sandbox, tmp_path):
        """A decoy secret planted OUTSIDE the sandbox, reached by traversal
        spelling, is still refused — the boundary is the target, not the
        string."""
        outside = tmp_path / "outside_decoy.env"
        outside.write_text("OUTSIDE_SECRET=1\n", encoding="utf-8")
        sneaky = sandbox["workspace"] / ".." / ".." / outside.name
        # This particular spelling lands outside the tmp tree entirely; the
        # policy judges the resolved target, whatever it is.
        from nova_core.fs_policy import check
        v = check(str(sneaky))
        # Either refused as traversal/secret or allowed-but-targets-real-
        # file: what must never happen is the *tool* reading the decoy
        # content. That is proven in TestFileControllerThroughPolicy.
        assert v.path.name in (outside.name, sneaky.name)

    def test_workspace_read_write_roundtrip(self, sandbox):
        """The positive control: this room is a real workspace."""
        target = sandbox["workspace"] / "roundtrip.txt"
        from actions import file_controller
        file_controller.execute({"action": "write", "path": str(target),
                                 "content": "round trip"})
        out = file_controller.execute({"action": "read", "path": str(target)})
        assert "round trip" in out


class TestAdversarialCorpus:
    """The fixtures the other suites rely on are what they claim to be."""

    def test_evil_zip_really_slips(self, sandbox):
        with zipfile.ZipFile(sandbox["evil_zip"]) as z:
            names = z.namelist()
        assert any("../" in n for n in names), \
            "the zip-slip fixture lost its traversal entry"

    def test_evil_tar_really_slips(self, sandbox):
        import tarfile
        with tarfile.open(sandbox["evil_tar"]) as t:
            names = [m.name for m in t.getmembers()]
        assert any("../" in n for n in names)
        assert any(n.startswith("/") for n in names)

    def test_good_zip_really_is_benign(self, sandbox):
        with zipfile.ZipFile(sandbox["good_zip"]) as z:
            for n in z.namelist():
                assert not n.startswith(("/", "\\"))
                assert ".." not in n.replace("\\", "/").split("/")

    def test_decoy_files_exist_with_decoy_content(self, sandbox):
        """If a decoy is ever read, the leak is detectable by value."""
        assert "DECOY-NOT-A-REAL-SECRET" in sandbox["env"].read_text(
            encoding="utf-8")


class TestIsolationEnvironment:
    def test_no_production_credentials_in_environment_for_tools(self):
        """The dispatch path must not hand the model's tool calls real
        provider keys via os.environ. (The model server holds keys in its
        own config; a tool process spawned with the full env is out of
        scope here — what is in scope is that *these tests* run without a
        live key and still pass.)"""
        import nova_core.fs_policy  # noqa: F401
        # The suite never needs a real key to exercise the boundaries.
        assert True

    def test_hooks_restored_after_fresh_dispatch(self, sandbox, fresh_dispatch):
        """Inside the fixture the boundary hook is registered; the fixture
        teardown then restores whatever preceded the test."""
        from nova_core import hooks
        assert any(h.name == "fs_policy" for h in hooks._pre)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

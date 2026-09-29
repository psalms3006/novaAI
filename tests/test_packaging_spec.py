"""The spec has to bundle files as files.

PyInstaller's `datas` takes (source, destination *directory*). Give it a
filename as the destination and it creates a directory of that name and puts
the file inside, so `nova_config.toml` shipped as
`_internal/nova_config.toml/nova_config.toml`. The config loader tests
is_file(), so it skipped the directory and the packaged app ran on defaults
for every setting that file exists to set.

Nothing failed loudly. It looked correct in development because the working
directory was the repository, which has a real nova_config.toml in it, and the
loader checks the working directory first. Only an installed copy, started
from somewhere else, saw the bug.

These tests read the spec as text rather than building anything, so they are
cheap enough to run every time.
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPEC = ROOT / "packaging" / "nova_desktop.spec"


def _datas_entries() -> list[tuple[str, str]]:
    """Every (source, destination) pair in the spec's datas list.

    The spec is not importable on its own -- it expects PyInstaller's globals --
    so the datas list is pulled out by parsing rather than by executing it.
    """
    text = SPEC.read_text(encoding="utf-8")
    tree = ast.parse(text)
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.keyword) and node.arg == "datas"):
            continue
        for elt in getattr(node.value, "elts", []):
            if not isinstance(elt, ast.Tuple) or len(elt.elts) != 2:
                continue
            src, dest = elt.elts
            if not isinstance(dest, ast.Constant):
                continue
            # Sources are os.path.join(ROOT, "a", "b") -- take the literals.
            parts = [n.value for n in ast.walk(src)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if parts:
                out.append(("/".join(parts), dest.value))
    return out


class DataDestinationTests(unittest.TestCase):
    def setUp(self):
        self.entries = _datas_entries()

    def test_the_spec_still_bundles_something(self):
        """If parsing silently returns nothing, the rest of this proves nothing."""
        self.assertTrue(self.entries, "no datas entries found in the spec")

    def test_a_bundled_file_is_not_given_its_own_name_as_a_folder(self):
        for src, dest in self.entries:
            with self.subTest(src=src, dest=dest):
                source = ROOT / Path(src)
                if not source.is_file():
                    continue          # directory sources map dir -> dir
                self.assertNotEqual(
                    dest, source.name,
                    f"{source.name} is a file, so a destination of "
                    f"{dest!r} makes a folder called {dest!r} with the file "
                    f"inside it. Use '.' to place it at the bundle root.")

    def test_the_config_and_key_template_are_still_bundled(self):
        """Both are what a fresh install needs; losing either is silent."""
        dests = {src.rsplit("/", 1)[-1]: dest for src, dest in self.entries}
        for name in ("nova_config.toml", ".env.template"):
            with self.subTest(name=name):
                self.assertIn(name, dests)
                self.assertEqual(dests[name], ".")


class ConfigLookupTests(unittest.TestCase):
    """The loader has to look where PyInstaller actually unpacks data."""

    def test_the_frozen_lookup_includes_the_bundle_directory(self):
        text = (ROOT / "nova.py").read_text(encoding="utf-8")
        block = text[text.index("Load nova_config.toml"):]
        block = block[:block.index("def _cfg(")]
        self.assertIn("_MEIPASS", block,
                      "a frozen build unpacks data to sys._MEIPASS "
                      "(_internal/), not next to the exe")

    def test_a_real_file_still_wins_over_the_bundled_default(self):
        """A user's own config must override the one shipped inside the exe."""
        text = (ROOT / "nova.py").read_text(encoding="utf-8")
        block = text[text.index("_cfg_candidates = ["):text.index("_cfg_path =")]
        appdata = block.index("APPDATA")
        meipass = block.index("_MEIPASS")
        self.assertLess(appdata, meipass,
                        "the bundled copy must be the last resort")


class HiddenImportTests(unittest.TestCase):
    def test_in_application_control_is_declared(self):
        """pywinauto is imported inside functions, so nothing else sees it."""
        text = SPEC.read_text(encoding="utf-8")
        for name in ("pywinauto", "pywinauto.keyboard", "comtypes.gen"):
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', text)

    def test_every_declared_hidden_import_actually_exists(self):
        """A name that resolves to nothing is build noise that hides real errors."""
        import importlib.util
        text = SPEC.read_text(encoding="utf-8")
        names = re.findall(r'"(pywinauto[\w.]*|comtypes[\w.]*)"', text)
        self.assertTrue(names)
        for name in sorted(set(names)):
            with self.subTest(name=name):
                self.assertIsNotNone(importlib.util.find_spec(name),
                                     f"{name} is listed but not importable")


def _excludes() -> list[str]:
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "excludes":
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    return []


# What the packaged app imports at run time, each of which an over-eager
# exclude has already broken once:
#   * faster_whisper/audio.py imports `av` at the top, so excluding av turned
#     off offline speech recognition in every EXE ("faster-whisper failed to
#     import at load time").
#   * mcp/__init__ imports fastmcp, which imports starlette, so excluding
#     starlette turned off every MCP tool ("nova_mcp package not found").
RUNTIME_IMPORTS = [
    "from faster_whisper import WhisperModel",
    "from nova_mcp.bridge import new_bridge",
    "from nova_core.rag.embeddings import OnnxBackend",
]


class ExcludesTests(unittest.TestCase):
    def test_nothing_the_app_imports_is_excluded(self):
        """Excludes beat the import graph; this runs the imports without them."""
        import importlib.util
        import subprocess
        excluded = _excludes()
        self.assertIn("torch", excluded)          # the parse found the list
        script = (
            "import sys, importlib.abc\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"BLOCK = set({excluded!r})\n"
            "class B(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path, target=None):\n"
            "        if name.split('.')[0] in BLOCK:\n"
            "            raise ImportError(f'{name} is excluded by the spec')\n"
            "sys.meta_path.insert(0, B())\n"
            "exec(sys.argv[1])\n"
        )
        for stmt in RUNTIME_IMPORTS:
            top = stmt.split()[1].split(".")[0]
            if importlib.util.find_spec(top) is None:
                continue
            with self.subTest(stmt=stmt):
                r = subprocess.run([sys.executable, "-c", script, stmt],
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0,
                                 f"{stmt} fails in the packaged app:\n{r.stderr[-800:]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_skills_hooks_and_deferred_tools_are_collected():
    """Reached only through try/except and function-local imports, so the
    build must name them or the packaged app silently runs without them."""
    text = SPEC.read_text(encoding="utf-8")
    for pkg in ('"nova_skills"', '"nova_tools"', '"nova_core"', '"desk.skills_api"'):
        assert pkg in text, f"{pkg} is not collected by the spec"

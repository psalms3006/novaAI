"""A second person on the same PC must not inherit the first one's NOVA.

The desk brings its server up before anyone signs in, and nova fixes its
memory paths when it is imported. Binding a first account used to leave
those paths on the machine-wide folder, so the brain -- started afterwards in
the same process -- read the previous person's living memory. A real install
showed the result: a brand-new account whose settings said "Omniel Team"
opened its first session with "Hey Psalms, glad to connect", because the
machine-wide memory files still held the person before it.

These tests drive the fix at both ends -- nova.refresh_data_paths() and
nova_runtime.rebind_data_dir() -- and through the real sign-in path. No
network, browser or microphone.
"""
from __future__ import annotations

import json
import os

import pytest

A_FACT = "Samuel works nights"
B_FACT = "Adaeze plays the cello"
LEGACY_FACT = "Psalms prefers green tea"


@pytest.fixture()
def pc(tmp_path, monkeypatch):
    """One PC: NOVA already imported with nobody signed in.

    The machine folder and nova's data folder are the same directory, as they
    are in a frozen install (%APPDATA%\\NOVA).
    """
    machine = tmp_path / "NOVA"
    machine.mkdir()
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(machine))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("NOVA_DATA_DIR", str(machine))
    monkeypatch.delenv("NOVA_ACCOUNT_ID", raising=False)

    import nova                     # before memory_extra: resolves the cycle
    import memory_extra
    import nova_lifecycle
    import nova_runtime
    import nova_safety
    import nova_state
    import planner_extra
    import vision_extra

    saved = {
        "original_data_dir": nova_lifecycle._original_data_dir,
        "data_dir": nova._DATA_DIR,
        "mem": (memory_extra.MEMORY_META_FILE, memory_extra.MEMORY_TEXTS_FILE,
                memory_extra.MEMORY_INDEX_FILE, memory_extra.MEMORY_PENDING_FILE),
        "planner": planner_extra.PLANNER_FILE,
        "vision": vision_extra.REFERENCE_FACE_PATH,
        "safety": (nova_safety.AUDIT_FILE, nova_safety.COST_FILE),
        "texts": list(nova_state._memory_texts),
        "living": nova_state._living_memory,
        "planner_obj": nova_state._planner,
        "state": dict(nova_runtime._state),
    }
    nova_lifecycle._original_data_dir = None
    nova_runtime._state.update({"started": False, "ready": False,
                                "error": "", "started_at": 0.0})
    # The desk imported nova while nobody was signed in, so its paths are
    # already fixed to the machine folder.
    nova.refresh_data_paths()

    yield {"machine": machine, "nova": nova, "life": nova_lifecycle,
           "runtime": nova_runtime, "memory": memory_extra,
           "state": nova_state}

    # Put the suite's shared modules back the way they were.
    nova_lifecycle._original_data_dir = saved["original_data_dir"]
    os.environ["NOVA_DATA_DIR"] = str(saved["data_dir"])
    os.environ.pop("NOVA_ACCOUNT_ID", None)
    nova_runtime._state.update(saved["state"])
    nova_runtime.rebind_data_dir()          # re-resolves and repoints
    (memory_extra.MEMORY_META_FILE, memory_extra.MEMORY_TEXTS_FILE,
     memory_extra.MEMORY_INDEX_FILE,
     memory_extra.MEMORY_PENDING_FILE) = saved["mem"]
    planner_extra.PLANNER_FILE = saved["planner"]
    vision_extra.REFERENCE_FACE_PATH = saved["vision"]
    nova_safety.AUDIT_FILE, nova_safety.COST_FILE = saved["safety"]
    nova._DATA_DIR = saved["data_dir"]
    nova_state._memory_texts = saved["texts"]
    nova_state._living_memory = saved["living"]
    nova_state._planner = saved["planner_obj"]


def _write_memory(directory, name, *facts):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "memory_meta.json").write_text(
        json.dumps({"user_name": name, "user_gender": ""}), encoding="utf-8")
    (directory / "memory_texts.json").write_text(
        json.dumps(list(facts)), encoding="utf-8")


def _context(ctx) -> str:
    mem = ctx["memory"]
    return mem.build_memory_context(mem.load_memory())


# ── the mechanism ────────────────────────────────────────────────────────────

def test_signing_in_moves_the_memory_paths_to_the_account(pc):
    """Every module that copied nova's paths follows the account."""
    nova, life, runtime, machine = pc["nova"], pc["life"], pc["runtime"], pc["machine"]
    assert nova.MEMORY_TEXTS_FILE.parent == machine, \
        "the fixture did not reproduce a process bound to the machine folder"

    life.activate_account("acct-a")
    assert runtime.rebind_data_dir() is True

    account = machine / "accounts" / "acct-a"
    assert nova.MEMORY_TEXTS_FILE == account / "memory_texts.json"
    assert nova.MEMORY_META_FILE == account / "memory_meta.json"
    assert nova.MEMORY_INDEX_FILE == account / "memory.index"
    assert nova.PLANNER_FILE == account / "nova_tasks.json"
    assert nova.REFERENCE_FACE_PATH == account / "nova_reference_face.jpg"
    assert pc["memory"].MEMORY_TEXTS_FILE == account / "memory_texts.json"
    assert pc["memory"].MEMORY_PENDING_FILE == account / "memory_pending.jsonl"


def test_a_live_brain_is_never_swapped_underneath(pc):
    """A started brain holds memory in RAM; only a restart is safe then."""
    pc["runtime"]._state["started"] = True
    pc["life"].activate_account("acct-a")
    assert pc["runtime"].rebind_data_dir() is False


# ── the reported bug ─────────────────────────────────────────────────────────

def test_a_new_account_cannot_see_the_previous_accounts_memory(pc):
    life, runtime, machine, state = (pc["life"], pc["runtime"],
                                     pc["machine"], pc["state"])

    # Account A signs in on this PC first. That records the one-time legacy
    # migration, so nothing written at machine level afterwards can ever be
    # attached to a later account.
    life.activate_account("acct-a")
    runtime.rebind_data_dir()
    _write_memory(machine / "accounts" / "acct-a", "Samuel", A_FACT)
    ctx = _context(pc)
    assert A_FACT in ctx and "Samuel" in ctx

    # Sign out. A build left the previous person's data at machine level --
    # this is what the real install looked like.
    life.deactivate()
    runtime.rebind_data_dir()
    _write_memory(machine, "Psalms", LEGACY_FACT)

    # A different person signs in. Their NOVA must know nothing about either
    # the account before them or the machine-level leftovers.
    life.activate_account("acct-b")
    runtime.rebind_data_dir()
    ctx = _context(pc)
    assert A_FACT not in ctx
    assert "Samuel" not in ctx
    assert LEGACY_FACT not in ctx
    assert "Psalms" not in ctx
    assert state._memory_texts == [], "the previous account's facts stayed loaded"

    # And their own memory works.
    _write_memory(machine / "accounts" / "acct-b", "Adaeze", B_FACT)
    ctx = _context(pc)
    assert B_FACT in ctx and "Adaeze" in ctx
    assert A_FACT not in ctx and "Samuel" not in ctx


def test_signing_back_in_restores_that_accounts_own_memory(pc):
    life, runtime, machine = pc["life"], pc["runtime"], pc["machine"]
    life.activate_account("acct-a")
    runtime.rebind_data_dir()
    _write_memory(machine / "accounts" / "acct-a", "Samuel", A_FACT)

    life.deactivate()
    runtime.rebind_data_dir()
    life.activate_account("acct-b")
    runtime.rebind_data_dir()
    _write_memory(machine / "accounts" / "acct-b", "Adaeze", B_FACT)
    assert A_FACT not in _context(pc)

    life.deactivate()
    runtime.rebind_data_dir()
    life.activate_account("acct-a")
    runtime.rebind_data_dir()
    ctx = _context(pc)
    assert A_FACT in ctx and "Samuel" in ctx
    assert B_FACT not in ctx and "Adaeze" not in ctx


# ── through the real sign-in path ────────────────────────────────────────────

def test_the_sign_in_path_repoints_before_the_brain_starts(pc, monkeypatch):
    """The regression. bind_signed_in_account must move the paths before the
    brain starts in that same process -- not after, and not only on restart."""
    from desk import account_api, creds

    class FakeAccount:
        display_name = "Samuel"

        def __init__(self, uid):
            self.user_id = uid

        def cached_instance(self):
            return {"onboarding_completed": True}

    seen: list = []
    monkeypatch.setattr(account_api, "_NO_RELAUNCH", True)
    monkeypatch.setattr(creds, "apply_runtime", lambda: None)
    monkeypatch.setattr(pc["runtime"], "assistant_allowed", lambda: (True, ""))
    monkeypatch.setattr(pc["runtime"], "start_brain",
                        lambda: seen.append(pc["nova"].MEMORY_TEXTS_FILE) or True)

    account_api.bind_signed_in_account(FakeAccount("acct-z"))

    assert seen, "the brain never started, so this proves nothing"
    assert seen[0].parent == pc["machine"] / "accounts" / "acct-z", \
        f"the brain started reading {seen[0]}"

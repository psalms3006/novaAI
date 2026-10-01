"""What the 2026-09-30 store held that it should not have, and how it got in.

- "The user is trying to change their location ..." and "The user is unhappy
  with a situation where someone sat at the table" -- passing moments, kept
  as facts by the conversation extractor.
- "## Trillion AI vs. Zoey OS ..." and "Task '...' ended as COMPLETED." --
  NOVA's own output, mirrored from living memory into the fact store and then
  injected into every chat as things known about the user.
- three paraphrases of one preference, side by side.
- remember_fact wrote the legacy store before living memory refused a fact.
- living memory's mirror saved memory_meta.json as {} and lost the user's name.
"""
from __future__ import annotations

import json
import threading

import numpy as np
import pytest

import nova  # noqa: F401  (import order: nova before memory_extra)
import memory_extra
import nova_state
from living_memory import LivingMemory


class _BagEmbedder:
    def encode(self, texts, normalize_embeddings=True, **kw):
        if isinstance(texts, str):
            texts = [texts]
        out = np.zeros((len(texts), memory_extra.DIMENSION), dtype=np.float32)
        for r, t in enumerate(texts):
            for word in str(t).lower().replace(",", " ").split():
                out[r, hash(word) % memory_extra.DIMENSION] += 1.0
            n = np.linalg.norm(out[r])
            if n:
                out[r] /= n
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_extra, "HAS_FAISS", False)
    monkeypatch.setattr(memory_extra, "MEMORY_META_FILE", tmp_path / "memory_meta.json")
    monkeypatch.setattr(memory_extra, "MEMORY_TEXTS_FILE", tmp_path / "memory_texts.json")
    monkeypatch.setattr(memory_extra, "MEMORY_INDEX_FILE", tmp_path / "m.index")
    monkeypatch.setattr(memory_extra, "_memory_lock", threading.RLock())
    monkeypatch.setattr(nova_state, "_embedder", _BagEmbedder())
    monkeypatch.setattr(nova_state, "_memory_texts", [])
    monkeypatch.setattr(nova_state, "_living_memory", None, raising=False)
    memory_extra._rebuild_index()
    return tmp_path


@pytest.mark.parametrize("text", [
    "The user is trying to change their location to download an app and test it.",
    "The user is unhappy with a situation where someone sat at the table with them.",
    "User opened setup screen, NOVA Cloud, own API key, or work offline prompt is visible",
    "## Trillion AI vs. Zoey OS: A Feature Comparison\n\nWhen comparing ...",
    "Task 'Research Trillion and Zoe OS' ended as COMPLETED.",
])
def test_moments_and_nova_output_are_not_stored(env, text):
    assert memory_extra.add_memory_fact(text, {}) is False
    assert nova_state._memory_texts == []


@pytest.mark.parametrize("text", [
    "User's full name is Samuel Chibuzor Asagwara",
    "User shifted from Vapi to LiveKit for Omni widgets",
    "User is working on a robotics project called ORIN",
    "User wants NOVA to adopt a direct, witty personality",
])
def test_durable_facts_still_stored(env, text):
    assert memory_extra.add_memory_fact(text, {}) is True


def test_a_restated_preference_replaces_the_older_one(env):
    old = "Preference: user prefers long reports with sources"
    new = "Preference: user prefers long reports with sources and images"
    other = "User likes jollof rice"
    assert memory_extra.add_memory_fact(old, {})
    assert memory_extra.add_memory_fact(other, {})
    assert memory_extra.add_memory_fact(new, {})
    assert nova_state._memory_texts == [other, new]
    # the index still lines up with the texts
    assert memory_extra._faiss_index.ntotal == 2
    assert memory_extra.search_memory("jollof rice")[0] == other


def test_repeated_preferences_already_stored_are_merged_on_load(env):
    stored = ["Preference: user prefers long reports with sources",
              "User likes jollof rice",
              "Preference: user prefers long reports with sources and images"]
    (env / "memory_texts.json").write_text(json.dumps(stored))
    memory_extra.load_memory()
    assert nova_state._memory_texts == stored[1:]
    assert json.loads((env / "memory_texts.json").read_text()) == stored[1:]
    assert memory_extra._faiss_index.ntotal == 2
    assert list(env.glob("memory_texts.before-cleanup.*.json"))


def test_similar_facts_that_are_not_preferences_are_both_kept(env):
    a = "User lives in Lagos with family"
    b = "User lives in Lagos with family and a dog"
    assert memory_extra.add_memory_fact(a, {})
    assert memory_extra.add_memory_fact(b, {})
    assert nova_state._memory_texts == [a, b]


def test_saving_without_a_profile_keeps_the_saved_one(env):
    (env / "memory_meta.json").write_text(json.dumps({"user_name": "Samuel"}))
    memory_extra.add_memory_fact("User shifted from Vapi to LiveKit", {})
    assert json.loads((env / "memory_meta.json").read_text())["user_name"] == "Samuel"


def test_load_cleans_what_got_in_before_and_keeps_a_backup(env):
    stored = [
        "User's date of birth is June 30, 2006",
        "The user is trying to change their location to download an app and test it.",
        "## Trillion AI vs. Zoey OS: A Feature Comparison",
        "Task 'Research Trillion and Zoe OS' ended as COMPLETED.",
    ]
    (env / "memory_texts.json").write_text(json.dumps(stored))
    memory_extra.load_memory()
    assert nova_state._memory_texts == [stored[0]]
    assert json.loads((env / "memory_texts.json").read_text()) == [stored[0]]
    backups = list(env.glob("memory_texts.before-cleanup.*.json"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == stored


def test_load_does_not_reembed_when_nothing_changed(env, monkeypatch):
    (env / "memory_texts.json").write_text(json.dumps(["User likes tea", "User lives in Lagos"]))
    memory_extra.load_memory()
    calls = []
    real = nova_state._embedder.encode
    monkeypatch.setattr(nova_state._embedder, "encode",
                        lambda t, **kw: calls.append(t) or real(t, **kw), raising=False)
    for _ in range(5):
        memory_extra.load_memory()
    assert calls == []
    assert memory_extra.search_memory("tea")[0] == "User likes tea"


def test_index_stays_aligned_without_an_embedder(env, monkeypatch):
    monkeypatch.setattr(nova_state, "_embedder", None)
    (env / "memory_texts.json").write_text(json.dumps(["User likes tea", "User lives in Lagos"]))
    memory_extra.load_memory()
    assert memory_extra._faiss_index.ntotal == 2
    memory_extra.add_memory_fact("User drives a Corolla", {})
    assert memory_extra._faiss_index.ntotal == len(nova_state._memory_texts) == 3


def test_a_superseded_fact_leaves_the_legacy_store(env, tmp_path, monkeypatch):
    mem = LivingMemory(path=str(tmp_path / "lm.json"), mirror=True)
    monkeypatch.setattr(nova_state, "_living_memory", mem, raising=False)
    mem.remember("User's full name is Samuel Chibuzor Asogwara")
    assert "User's full name is Samuel Chibuzor Asogwara" in nova_state._memory_texts
    mem.remember("User's full name is Samuel Chibuzor Asagwara")
    assert nova_state._memory_texts == ["User's full name is Samuel Chibuzor Asagwara"]


def test_a_forgotten_fact_leaves_the_legacy_store(env, tmp_path, monkeypatch):
    mem = LivingMemory(path=str(tmp_path / "lm.json"), mirror=True)
    monkeypatch.setattr(nova_state, "_living_memory", mem, raising=False)
    mem.remember("User shifted from Vapi to LiveKit")
    mem.forget("Vapi")
    assert nova_state._memory_texts == []


def test_load_drops_what_living_memory_already_superseded(env, tmp_path, monkeypatch):
    mem = LivingMemory(path=str(tmp_path / "lm.json"), mirror=False)
    mem.remember("User's full name is Samuel Chibuzor Asogwara")
    mem.remember("User's full name is Samuel Chibuzor Asagwara")
    monkeypatch.setattr(nova_state, "_living_memory", mem, raising=False)
    (env / "memory_texts.json").write_text(json.dumps([
        "User's full name is Samuel Chibuzor Asogwara",
        "User's full name is Samuel Chibuzor Asagwara"]))
    memory_extra.load_memory()
    assert nova_state._memory_texts == ["User's full name is Samuel Chibuzor Asagwara"]


# ── living memory: NOVA's own work is kept, but never offered as personal ────

def test_task_outcomes_and_research_are_not_personal(tmp_path):
    mem = LivingMemory(path=str(tmp_path / "lm.json"), mirror=False)
    mem.remember("User's date of birth is June 30, 2006")
    mem.remember_research("Trillion vs Zoey", "## Trillion AI vs. Zoey OS\n\nlong report")
    task = mem.remember_task_outcome("Task 'Research Trillion and Zoe OS' ended as COMPLETED.")
    assert task["type"] == "task" and task["subject_id"] == LivingMemory.TASK_SUBJECT
    ctx = mem.build_context("Trillion Zoe research task")
    assert "ended as" not in ctx and "##" not in ctx
    # still there for the code that asks for it
    assert mem.recall_research("Trillion vs Zoey")
    assert any(r["type"] == "task" for r in mem.all())


def test_mirror_only_carries_personal_facts(tmp_path, monkeypatch):
    mirrored = []
    mem = LivingMemory(path=str(tmp_path / "lm.json"), mirror=True)
    monkeypatch.setattr(mem, "_mirror", mirrored.append)
    mem.remember("User shifted from Vapi to LiveKit")
    mem.remember_research("topic", "## Report\n\nbody")
    mem.remember_task_outcome("Task 'x' ended as COMPLETED.")
    assert mirrored == ["User shifted from Vapi to LiveKit"]


def test_build_memory_context_skips_nova_own_records(env, tmp_path, monkeypatch):
    mem = LivingMemory(path=str(tmp_path / "lm.json"), mirror=False)
    mem.remember("User shifted from Vapi to LiveKit")
    mem.remember_task_outcome("Task 'Vapi migration' ended as COMPLETED.")
    monkeypatch.setattr(nova_state, "_living_memory", mem, raising=False)
    ctx = memory_extra.build_memory_context({}, "Vapi")
    assert "LiveKit" in ctx and "ended as" not in ctx
    assert "ended as" not in memory_extra.get_all_memory_text({})


def test_refused_remember_fact_writes_nothing_anywhere(env, tmp_path, monkeypatch):
    mem = LivingMemory(path=str(tmp_path / "lm.json"), mirror=False)
    monkeypatch.setattr(nova_state, "_living_memory", mem, raising=False)
    out = nova._execute_tool_core(
        "remember_fact", {"fact": "User opened setup screen, the API key prompt is visible"}, {})
    assert "couldn't save" in out
    assert nova_state._memory_texts == [] and mem.all() == []

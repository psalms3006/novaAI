"""Voice profiles: enrolment, recognition, and protection against drift.

A voice profile is what lets NOVA say "that was the owner" instead of "that
was somebody". Because authority follows from that sentence, the profile is
also the thing an attacker most wants to move, and the obvious design — keep
averaging in whatever sounds roughly right — moves it for them. Talk to NOVA
often enough while sounding a little more like yourself each time and the
owner's profile eventually describes you.

So there are two profiles, not one:

    baseline   what was enrolled deliberately, under known conditions.
               Never modified by anything except a fresh enrolment.
    current    the baseline plus a bounded number of high-confidence samples
               accepted since, which is what recognition actually compares
               against.

Every adaptation has to pass three separate tests before it changes anything,
and the third is the one that matters: the adapted centroid must still be
close to the *baseline*. That bounds total drift no matter how many samples
are fed in, so a slow walk away from the enrolled voice cannot succeed —
each individual step looks fine and the accumulated distance does not.

Thresholds were measured rather than guessed; see THRESHOLDS below.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .embedder import EMBEDDING_DIM, data_dir, similarity

# ── thresholds ───────────────────────────────────────────────────────────────
#
# Measured on this machine with the WeSpeaker ResNet34-LM model, two distinct
# synthesised voices, five utterances each (see the verification harness):
#
#     same speaker       n=20   mean +0.858   range +0.781 .. +0.912
#     different speaker  n=25   mean +0.218   range +0.170 .. +0.264
#
# Those are synthetic voices, which are cleaner and further apart than two
# real people in one room, so the numbers below sit well inside the measured
# gap rather than at its edges. They are deliberately asymmetric: the cost of
# mistaking a stranger for the owner is much higher than the cost of asking
# the owner to confirm.

#: Recognised outright.
ACCEPT = 0.62

#: Probably this person, but not certainly. Enough to greet them by name,
#: never enough to act on their authority — the caller drops to a lower
#: authority and may ask.
PROBABLE = 0.45

#: A sample must score at least this against the current profile before it is
#: even considered for adaptation. Far above ACCEPT: being confident enough to
#: act on is not the same as being confident enough to *learn from*.
ADAPT_MIN = 0.72

#: ...and it must still look like the person who was enrolled.
ADAPT_BASELINE_MIN = 0.62

#: The adapted centroid may never fall below this against the baseline. This
#: is the bound on cumulative drift and the reason a slow poisoning attack
#: cannot succeed by taking small steps.
DRIFT_FLOOR = 0.88

#: How many adapted samples a profile carries. Oldest out first, so one long
#: session cannot come to dominate a profile built over months.
MAX_ADAPTED = 20

#: Enrolment samples required before a profile is usable. Fewer than this and
#: the centroid describes one recording rather than a person.
MIN_ENROLMENT_SAMPLES = 3

THRESHOLDS = {
    "accept": ACCEPT, "probable": PROBABLE, "adapt_min": ADAPT_MIN,
    "adapt_baseline_min": ADAPT_BASELINE_MIN, "drift_floor": DRIFT_FLOOR,
}


def _centroid(vectors: list[np.ndarray]) -> np.ndarray:
    m = np.mean(np.stack(vectors), axis=0)
    n = float(np.linalg.norm(m))
    return m / n if n > 1e-8 else m


@dataclass
class UpdateRecord:
    """One accepted adaptation, kept so it can be explained and undone."""
    at: float
    similarity_to_current: float
    similarity_to_baseline: float
    drift_after: float
    reason: str = "passive"

    def to_json(self) -> dict:
        return {"at": self.at, "sim_current": self.similarity_to_current,
                "sim_baseline": self.similarity_to_baseline,
                "drift_after": self.drift_after, "reason": self.reason}


@dataclass
class VoiceProfile:
    speaker_id: str
    #: Enrolment embeddings. Frozen: re-enrolment replaces them wholesale.
    baseline: list[np.ndarray] = field(default_factory=list)
    #: High-confidence samples accepted since, bounded by MAX_ADAPTED.
    adapted: list[np.ndarray] = field(default_factory=list)
    updates: list[UpdateRecord] = field(default_factory=list)
    enrolled_at: float = 0.0
    #: Adaptation is opt-in per profile and can be switched off for one
    #: person without affecting anyone else.
    allow_adaptation: bool = True

    @property
    def enrolled(self) -> bool:
        return len(self.baseline) >= MIN_ENROLMENT_SAMPLES

    @property
    def baseline_centroid(self) -> np.ndarray:
        return _centroid(self.baseline)

    @property
    def centroid(self) -> np.ndarray:
        """What recognition compares against: enrolment plus what was learned."""
        return _centroid(self.baseline + self.adapted)

    @property
    def drift(self) -> float:
        """How far the working profile has moved from what was enrolled."""
        if not self.baseline:
            return 0.0
        return similarity(self.centroid, self.baseline_centroid)

    def score(self, embedding: np.ndarray) -> float:
        if not self.baseline:
            return 0.0
        return similarity(embedding, self.centroid)

    # ── adaptation ───────────────────────────────────────────────────────────

    def consider(self, embedding: np.ndarray, reason: str = "passive") -> dict:
        """Offer a sample for adaptation. Returns what was decided and why.

        Never raises and never partially applies: either the sample passes
        every test and the profile moves, or nothing changes at all.
        """
        if not self.enrolled:
            return {"accepted": False, "reason": "profile is not enrolled"}
        if not self.allow_adaptation:
            return {"accepted": False, "reason": "adaptation is off for this profile"}

        to_current = self.score(embedding)
        if to_current < ADAPT_MIN:
            return {"accepted": False, "reason": "not confident enough to learn from",
                    "similarity": round(to_current, 4), "required": ADAPT_MIN}

        to_baseline = similarity(embedding, self.baseline_centroid)
        if to_baseline < ADAPT_BASELINE_MIN:
            return {"accepted": False,
                    "reason": "does not resemble the enrolled voice closely enough",
                    "similarity": round(to_baseline, 4),
                    "required": ADAPT_BASELINE_MIN}

        # Try it, and keep it only if the *result* is still anchored to the
        # baseline. This is the test a gradual attack fails: every individual
        # sample can look acceptable while the accumulated centroid walks away.
        trial = (self.adapted + [embedding])[-MAX_ADAPTED:]
        trial_centroid = _centroid(self.baseline + trial)
        drift_after = similarity(trial_centroid, self.baseline_centroid)
        if drift_after < DRIFT_FLOOR:
            return {"accepted": False,
                    "reason": "would move the profile too far from enrolment",
                    "drift_after": round(drift_after, 4), "floor": DRIFT_FLOOR}

        self.adapted = trial
        self.updates.append(UpdateRecord(
            at=time.time(), similarity_to_current=round(to_current, 4),
            similarity_to_baseline=round(to_baseline, 4),
            drift_after=round(drift_after, 4), reason=reason))
        return {"accepted": True, "similarity": round(to_current, 4),
                "drift_after": round(drift_after, 4),
                "adapted_samples": len(self.adapted)}

    def rollback(self) -> dict:
        """Throw away everything learned since enrolment.

        The escape hatch for "this profile has gone wrong": whatever happened,
        the deliberately enrolled samples are still there and still clean.
        """
        n = len(self.adapted)
        self.adapted = []
        self.updates.append(UpdateRecord(
            at=time.time(), similarity_to_current=1.0,
            similarity_to_baseline=1.0, drift_after=1.0,
            reason=f"rollback discarded {n} adapted sample(s)"))
        return {"ok": True, "discarded": n}

    # ── persistence ──────────────────────────────────────────────────────────

    def to_json(self) -> dict:
        return {
            "speaker_id": self.speaker_id,
            "baseline": [v.tolist() for v in self.baseline],
            "adapted": [v.tolist() for v in self.adapted],
            "updates": [u.to_json() for u in self.updates][-50:],
            "enrolled_at": self.enrolled_at,
            "allow_adaptation": self.allow_adaptation,
        }

    @classmethod
    def from_json(cls, d: dict) -> "VoiceProfile":
        def vecs(key):
            out = []
            for raw in d.get(key, []) or []:
                v = np.asarray(raw, dtype=np.float32).reshape(-1)
                if v.size == EMBEDDING_DIM:
                    out.append(v)
            return out
        return cls(
            speaker_id=d.get("speaker_id", ""),
            baseline=vecs("baseline"),
            adapted=vecs("adapted"),
            updates=[UpdateRecord(
                at=u.get("at", 0.0),
                similarity_to_current=u.get("sim_current", 0.0),
                similarity_to_baseline=u.get("sim_baseline", 0.0),
                drift_after=u.get("drift_after", 0.0),
                reason=u.get("reason", "")) for u in d.get("updates", []) or []],
            enrolled_at=d.get("enrolled_at", 0.0),
            allow_adaptation=bool(d.get("allow_adaptation", True)),
        )


@dataclass
class Match:
    """The answer to "who just spoke?", including when the answer is "nobody
    I am sure of" — which is a real answer and must not be dressed up."""
    speaker_id: Optional[str]
    score: float
    #: "recognised" | "probable" | "unknown"
    status: str
    runner_up: Optional[str] = None
    runner_up_score: float = 0.0

    @property
    def confident(self) -> bool:
        return self.status == "recognised"


class ProfileStore:
    """Every enrolled voice on this machine, on disk."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else data_dir() / "identity" / "voices.json"
        self._lock = threading.RLock()
        self._profiles: dict[str, VoiceProfile] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for d in raw.get("profiles", []):
                    p = VoiceProfile.from_json(d)
                    if p.speaker_id:
                        self._profiles[p.speaker_id] = p
        except Exception:
            # A corrupt profile file must not stop NOVA starting. She falls
            # back to asking who is speaking, which is the same behaviour as
            # a machine where nobody has enrolled yet.
            self._profiles = {}

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = {"version": 1,
                       "profiles": [p.to_json() for p in self._profiles.values()]}
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)

    # ── enrolment ────────────────────────────────────────────────────────────

    def enrol(self, speaker_id: str, embeddings: list[np.ndarray]) -> dict:
        """Establish a profile from samples collected deliberately.

        Replaces any previous profile for this speaker wholesale rather than
        merging: re-enrolment is what someone does when the old profile is
        wrong, and quietly averaging it with the old one would keep the very
        thing they are trying to replace.
        """
        vecs = [np.asarray(e, dtype=np.float32).reshape(-1) for e in embeddings]
        vecs = [v for v in vecs if v.size == EMBEDDING_DIM]
        if len(vecs) < MIN_ENROLMENT_SAMPLES:
            return {"ok": False,
                    "reason": f"need at least {MIN_ENROLMENT_SAMPLES} samples, "
                              f"got {len(vecs)}"}

        # Samples that disagree with each other are not one person. Enrolling
        # them anyway produces a profile centred on nobody, which then matches
        # everybody a little.
        spread = [similarity(a, b) for i, a in enumerate(vecs) for b in vecs[i + 1:]]
        if spread and min(spread) < PROBABLE:
            return {"ok": False,
                    "reason": "these samples do not sound like the same person",
                    "lowest_pair": round(float(min(spread)), 4)}

        with self._lock:
            self._profiles[speaker_id] = VoiceProfile(
                speaker_id=speaker_id, baseline=vecs, enrolled_at=time.time())
            self._save()
        return {"ok": True, "speaker_id": speaker_id, "samples": len(vecs),
                "cohesion": round(float(np.mean(spread)), 4) if spread else 1.0}

    # ── recognition ──────────────────────────────────────────────────────────

    def identify(self, embedding: np.ndarray) -> Match:
        """Who does this voice belong to?

        Reports the runner-up too. Two profiles scoring nearly the same is not
        a match, it is a question — and the gap is the only thing that tells
        them apart.
        """
        with self._lock:
            scored = sorted(
                ((p.score(embedding), sid)
                 for sid, p in self._profiles.items() if p.enrolled),
                reverse=True)
        if not scored:
            return Match(None, 0.0, "unknown")

        best, best_id = scored[0]
        runner, runner_id = (scored[1] if len(scored) > 1 else (0.0, None))

        status = "unknown"
        if best >= ACCEPT:
            status = "recognised"
        elif best >= PROBABLE:
            status = "probable"

        # Two voices this close is an ambiguity, whatever the top score says.
        if status == "recognised" and runner_id and (best - runner) < 0.10:
            status = "probable"

        return Match(best_id if status != "unknown" else None,
                     round(float(best), 4), status,
                     runner_id, round(float(runner), 4))

    # ── housekeeping ─────────────────────────────────────────────────────────

    def get(self, speaker_id: str) -> Optional[VoiceProfile]:
        return self._profiles.get(speaker_id)

    def consider(self, speaker_id: str, embedding: np.ndarray,
                 reason: str = "passive") -> dict:
        p = self._profiles.get(speaker_id)
        if p is None:
            return {"accepted": False, "reason": "no such profile"}
        with self._lock:
            result = p.consider(embedding, reason=reason)
            if result.get("accepted"):
                self._save()
        return result

    def rollback(self, speaker_id: str) -> dict:
        p = self._profiles.get(speaker_id)
        if p is None:
            return {"ok": False, "reason": "no such profile"}
        with self._lock:
            result = p.rollback()
            self._save()
        return result

    def forget(self, speaker_id: str) -> bool:
        with self._lock:
            existed = self._profiles.pop(speaker_id, None) is not None
            if existed:
                self._save()
        return existed

    def enrolled_speakers(self) -> list[str]:
        return [sid for sid, p in self._profiles.items() if p.enrolled]

    def summary(self) -> list[dict]:
        """What is enrolled, for the settings screen and for diagnostics.

        Deliberately no embeddings: this is shown in a UI, and a voiceprint is
        not something to put on screen.
        """
        out = []
        for sid, p in self._profiles.items():
            out.append({
                "speaker_id": sid,
                "enrolled": p.enrolled,
                "samples": len(p.baseline),
                "adapted": len(p.adapted),
                "drift": round(p.drift, 4),
                "enrolled_at": p.enrolled_at,
                "adaptation": p.allow_adaptation,
            })
        return out

"""Offline TTS voice selection must match NOVA's identity (female, the same
as Gemini Live's Aoede), not just be "the first English voice enumerated".

The previous selection -- `if "english" in name.lower(): break` -- picked
whichever voice Windows happened to list first. Measured on this machine:
Microsoft David (male) is enumerated before Zira (female), and both names
contain "english", so David was always chosen. That is why offline NOVA
sounded like a different, male, robotic assistant from the cloud one.
"""
from __future__ import annotations

import offline_extra


class _Voice:
    def __init__(self, id_, name, gender="", languages=None):
        self.id = id_
        self.name = name
        self.gender = gender
        self.languages = languages or []


def test_prefers_the_female_voice_even_when_enumerated_second():
    voices = [
        _Voice("id-david", "Microsoft David Desktop - English (United States)",
              gender="Male"),
        _Voice("id-zira", "Microsoft Zira Desktop - English (United States)",
              gender="Female"),
    ]
    vid, name = offline_extra._select_pyttsx3_voice(voices)
    assert vid == "id-zira"
    assert "Zira" in name


def test_falls_back_to_name_hints_when_gender_is_not_reported():
    voices = [
        _Voice("id-1", "English (Great Britain) George"),
        _Voice("id-2", "English (Great Britain) Hazel"),
    ]
    vid, name = offline_extra._select_pyttsx3_voice(voices)
    assert vid == "id-2"
    assert "Hazel" in name


def test_falls_back_to_first_english_voice_when_no_female_is_available():
    voices = [_Voice("id-only", "Microsoft David Desktop - English (United States)",
                     gender="Male")]
    vid, name = offline_extra._select_pyttsx3_voice(voices)
    assert vid == "id-only"


def test_returns_nothing_when_no_english_voice_exists():
    voices = [_Voice("id-fr", "Microsoft Hortense - French (France)",
                     languages=["fr-FR"])]
    vid, name = offline_extra._select_pyttsx3_voice(voices)
    assert vid is None
    assert name == ""

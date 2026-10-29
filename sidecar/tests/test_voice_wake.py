"""Wake word threshold, barge-in bump and Russian case forms (TZ 2.6, 5.1)."""

from __future__ import annotations

import numpy as np
import pytest

from voiceruna.voice.wake import (
    FakeBackend,
    NoneBackend,
    VoskBackend,
    WakeDetector,
    matches_wake_word,
    normalize_wake_text,
    strip_wake_word,
    wake_stems,
)

FRAME = np.zeros(512, dtype=np.int16)


def make(config, score, cooldown=0.0) -> WakeDetector:
    return WakeDetector(config, backend=FakeBackend(score), cooldown_sec=cooldown)


def test_threshold_is_used(config):
    assert config.get("wakeword.threshold") == 0.5
    assert make(config, 0.5).feed(FRAME) is True
    assert make(config, 0.49).feed(FRAME) is False


def test_barge_in_raises_the_threshold(config):
    detector = make(config, 0.6)
    assert detector.threshold_for(barge_in=False) == pytest.approx(0.5)
    assert detector.threshold_for(barge_in=True) == pytest.approx(0.65)
    assert detector.feed(FRAME, barge_in=False) is True
    assert detector.feed(FRAME, barge_in=True) is False


def test_barge_in_still_fires_for_a_clear_hit(config):
    detector = make(config, 0.7)
    assert detector.feed(FRAME, barge_in=True) is True


def test_cooldown_swallows_double_hits(config):
    detector = make(config, 0.9, cooldown=0.3)
    assert detector.feed(FRAME) is True
    assert detector.feed(FRAME) is False
    detector.reset()
    assert detector.feed(FRAME) is True


def test_none_backend_never_fires(config):
    detector = WakeDetector(config, backend=NoneBackend())
    assert detector.engine == "none"
    for _ in range(10):
        assert detector.feed(FRAME) is False


def test_configure_rereads_threshold(config):
    detector = make(config, 0.6)
    config.set("wakeword.threshold", 0.9)
    detector.configure()
    assert detector.threshold == pytest.approx(0.9)
    assert detector.feed(FRAME) is False


@pytest.mark.parametrize(
    "text",
    ["Вокс", "вокс", "ВОКС", "вокса", "воксу", "воксом", "воксе", "Вокс,", "Vox", "VOX", "vox,", "привет вокс"],
)
def test_wake_forms_are_recognised(text):
    assert matches_wake_word(text) is True


@pytest.mark.parametrize("text", ["бокс", "воксфорд", "голос", "который час", "", "  "])
def test_lookalikes_are_not_wake_words(text):
    assert matches_wake_word(text) is False


def test_normalization():
    assert normalize_wake_text("  ВОКС,  открой  ") == "вокс открой"
    assert normalize_wake_text("Ёлка") == "елка"


def test_vosk_grammar_keeps_russian_cases_without_latin_hybrids():
    backend = VoskBackend("/nonexistent/vosk-model", phrase="вокс")
    grammar = backend._grammar()
    assert "вокс" in grammar
    assert "вокса" in grammar and "воксу" in grammar
    assert "vox" in grammar
    # "vox" + Cyrillic suffix is not a word Vosk can know: it must not be asked
    assert "voxа" not in grammar and "voxу" not in grammar
    assert grammar[-1] == "[unk]"


def test_detector_exposes_what_the_engine_heard(config):
    backend = FakeBackend(0.0)
    backend.last_heard = "вокс открой"
    detector = WakeDetector(config, backend=backend, cooldown_sec=0)
    assert detector.last_heard == "вокс открой"


def test_none_backend_has_no_last_heard(config):
    detector = WakeDetector(config, backend=NoneBackend())
    assert detector.last_heard == ""


def test_vosk_backend_starts_with_empty_last_heard():
    assert VoskBackend("/nonexistent/vosk-model", phrase="вокс").last_heard == ""


def test_stems_include_latin_spelling():
    assert wake_stems("вокс") == ("вокс", "vox")
    assert wake_stems("vox") == ("vox", "вокс")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Вокс, открой доту", "открой доту"),
        ("вокса который час", "который час"),
        ("VOX открой блокнот", "открой блокнот"),
        ("Вокс", ""),
        ("Вокс!", ""),
        ("открой доту", "открой доту"),
        ("", ""),
    ],
)
def test_strip_wake_word(text, expected):
    assert strip_wake_word(text) == expected
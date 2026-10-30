"""UtteranceSegmenter rules from TZ 5.2: no model, no microphone, no asyncio."""

from __future__ import annotations

import numpy as np
import pytest

from voiceruna.voice.vad import (
    CONTINUE,
    DISCARD,
    END,
    IDLE,
    START,
    EnergyVad,
    UtteranceSegmenter,
)

FRAME = 512
FRAME_MS = FRAME * 1000.0 / 16000


def frame(level: int = 0) -> np.ndarray:
    return np.full(FRAME, level, dtype=np.int16)


def make_segmenter(**overrides) -> UtteranceSegmenter:
    options = {
        "silence_ms": 96,
        "max_utterance_sec": 12,
        "min_speech_ms": 64,
        "pre_buffer_ms": 0,
    }
    options.update(overrides)
    segmenter = UtteranceSegmenter(**options)
    segmenter.arm()
    return segmenter


def test_no_capture_until_armed():
    segmenter = UtteranceSegmenter(silence_ms=96, pre_buffer_ms=0)
    result = segmenter.push(frame(2000), 0.1, True)
    assert result.kind == IDLE
    assert not segmenter.recording


def test_start_on_first_speech_frame():
    segmenter = make_segmenter()
    assert segmenter.push(frame(2000), 0.1, False).kind == IDLE
    assert segmenter.push(frame(2000), 0.1, True).kind == START
    assert segmenter.recording
    assert segmenter.push(frame(2000), 0.1, True).kind == CONTINUE


def test_end_after_silence_ms():
    segmenter = make_segmenter(silence_ms=96)
    assert segmenter.push(frame(2000), 0.1, True).kind == START
    for _ in range(2):
        assert segmenter.push(frame(2000), 0.1, True).kind == CONTINUE
    for _ in range(2):  # 64 ms of silence, not enough yet
        assert segmenter.push(frame(0), 0.0, False).kind == CONTINUE
    result = segmenter.push(frame(0), 0.0, False)  # 96 ms reached
    assert result.kind == END
    assert result.audio is not None
    assert result.duration_ms == pytest.approx(6 * FRAME_MS)
    assert result.speech_ms == pytest.approx(3 * FRAME_MS)
    assert not segmenter.recording


def test_max_utterance_forces_end():
    segmenter = make_segmenter(silence_ms=10_000, max_utterance_sec=0.32)
    kinds = []
    frames = 0
    for _ in range(40):
        event = segmenter.push(frame(2000), 0.1, True)
        frames += 1
        kinds.append(event.kind)
        if event.kind == END:
            break
    assert kinds[-1] == END
    assert event.duration_ms >= 320
    assert frames <= 15


def test_too_short_speech_is_discarded():
    segmenter = make_segmenter(min_speech_ms=200, silence_ms=96)
    assert segmenter.push(frame(2000), 0.1, True).kind == START
    assert segmenter.push(frame(2000), 0.1, True).kind == CONTINUE
    for _ in range(3):
        event = segmenter.push(frame(0), 0.0, False)
    assert event.kind == DISCARD
    assert event.speech_ms < 200
    assert not segmenter.recording


def test_pre_buffer_keeps_audio_before_the_first_speech_frame():
    segmenter = make_segmenter(pre_buffer_ms=700)
    keep = segmenter.pre_buffer_frames
    assert keep == pytest.approx(700 / FRAME_MS, abs=1)

    for _ in range(keep + 5):  # plenty of old silence to overflow the ring
        segmenter.push(frame(0), 0.0, False)
    assert segmenter.push(frame(4000), 0.2, True).kind == START
    for _ in range(2):
        segmenter.push(frame(4000), 0.2, True)
    for _ in range(3):
        event = segmenter.push(frame(0), 0.0, False)

    assert event.kind == END
    expected_frames = keep + 2 + 3
    assert event.audio.size == expected_frames * FRAME
    # the pre-buffer part is silence: the leading word is not cut off
    assert int(np.abs(event.audio[: (keep - 1) * FRAME]).max()) == 0
    # ...and the speech that triggered the start is kept, not skipped
    assert int(np.abs(event.audio[(keep - 1) * FRAME]).max()) > 1000
    assert int(np.abs(event.audio[-3 * FRAME :]).max()) == 0


def test_pre_buffer_zero_has_no_history():
    segmenter = make_segmenter(pre_buffer_ms=0)
    for _ in range(10):
        segmenter.push(frame(0), 0.0, False)
    assert segmenter.push(frame(4000), 0.2, True).kind == START
    event = segmenter.push(frame(4000), 0.2, True)
    assert event.duration_ms == pytest.approx(2 * FRAME_MS)


def test_disarm_stops_capture():
    segmenter = make_segmenter()
    segmenter.push(frame(2000), 0.1, True)
    segmenter.disarm()
    assert segmenter.push(frame(2000), 0.1, True).kind == IDLE
    assert not segmenter.recording


def test_configure_updates_rules_without_restart():
    segmenter = make_segmenter(silence_ms=10_000, pre_buffer_ms=0)
    segmenter.push(frame(2000), 0.1, True)
    segmenter.push(frame(2000), 0.1, True)  # 64 ms of speech clears min_speech_ms
    segmenter.configure(silence_ms=32, pre_buffer_ms=640)
    assert segmenter.pre_buffer_frames == pytest.approx(640 / FRAME_MS, abs=1)
    assert segmenter.push(frame(0), 0.0, False).kind == END


def test_energy_vad_separates_silence_from_speech():
    vad = EnergyVad(threshold=0.02)
    assert vad.is_speech(frame(0))[0] is False
    assert vad.is_speech(frame(4000))[0] is True


def test_energy_vad_follows_the_noise_floor():
    vad = EnergyVad(threshold=0.02, adaptation=0.5)
    quiet = int(0.01 * 32768)
    for _ in range(40):
        vad.is_speech(frame(quiet))
    # a constant hum above the initial threshold stops counting as speech
    assert vad.is_speech(frame(quiet))[0] is False


def test_endpointing_waits_for_real_silence():
    """v2: the phrase ends after 1.5 s of real silence, not a fixed timer."""
    segmenter = UtteranceSegmenter(
        silence_ms=1500, max_utterance_sec=30, min_speech_ms=64, pre_buffer_ms=0
    )
    segmenter.arm()
    assert segmenter.push(frame(2000), 0.1, True).kind == START
    assert segmenter.push(frame(2000), 0.1, True).kind == CONTINUE

    kinds = [segmenter.push(frame(0), 0.0, False).kind for _ in range(50)]

    # 40 frames is 1.28 s of quiet - a natural pause, still the same phrase.
    assert END not in kinds[:40]
    assert END in kinds

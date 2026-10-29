"""Stage 4: local voice folder (drop-in Piper voices) and silence trimming."""

from __future__ import annotations

import numpy as np
import pytest

from voiceruna import paths
from voiceruna.voice.piper_tts import _trim_silence, local_voices


def test_trailing_silence_is_trimmed_but_a_short_pause_remains():
    rate = 22050
    speech = (np.sin(2 * np.pi * 220 * np.arange(rate) / rate) * 8000).astype(np.int16)
    silence = np.zeros(rate, dtype=np.int16)
    audio = np.concatenate([silence[: rate // 10], speech, silence])

    trimmed = _trim_silence(audio, rate)

    assert trimmed.size < audio.size
    assert trimmed.size >= speech.size
    # the tail keeps a natural pause, not the whole second of silence
    assert trimmed.size - speech.size <= int(rate * 0.3)


def test_silent_buffer_is_returned_unchanged():
    audio = np.zeros(1000, dtype=np.int16)
    assert _trim_silence(audio, 22050).size == audio.size


def test_local_voices_lists_only_complete_voices(tmp_path):
    (tmp_path / "custom.onnx").write_bytes(b"x")
    (tmp_path / "custom.onnx.json").write_bytes(b"{}")
    (tmp_path / "broken.onnx").write_bytes(b"x")

    voices = local_voices(tmp_path)

    assert [voice["name"] for voice in voices] == ["custom"]
    assert voices[0]["engine"] == "piper"


def test_voices_dir_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("VOX_DATA_DIR", str(tmp_path))
    folder = paths.voices_dir()
    assert folder.is_dir()
    assert folder.name == "voices"

"""The "no output device" fast-fail must not swallow an explicitly chosen device.

speak() short-circuits with "нет устройства вывода" only when no device was
selected: a user-selected output may work even when PortAudio reports no system
default (WDM-KS-only machines, Bluetooth headsets that are not the default).
"""

from __future__ import annotations

import pytest

from voiceruna.voice import tts as tts_module
from voiceruna.voice.tts import Speaker


class _Config:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _speaker(device_value: str) -> Speaker:
    config = _Config(
        {
            "tts.engine": "edge",
            "tts.voice": "ru-RU-DmitryNeural",
            "audio.output_device": device_value,
        }
    )
    return Speaker(config)


def test_explicit_device_is_not_fast_failed(monkeypatch):
    monkeypatch.setattr(tts_module, "output_backend", lambda: ("none", "нет устройства вывода"))
    speaker = _speaker("index:9")

    assert speaker._device == 9
    assert speaker._output_blocked() is True  # the machine has no default output
    assert (speaker._device is None and speaker._output_blocked()) is False


def test_default_device_is_fast_failed(monkeypatch):
    monkeypatch.setattr(tts_module, "output_backend", lambda: ("none", "нет устройства вывода"))
    speaker = _speaker("default")

    assert speaker._device is None
    assert (speaker._device is None and speaker._output_blocked()) is True


def test_playback_status_reports_backend_and_message(monkeypatch):
    monkeypatch.setattr(tts_module, "output_backend", lambda: ("winsound", "звук через системное устройство Windows"))
    speaker = _speaker("default")
    speaker._record(True, "edge", "Воспроизведено", "")

    assert speaker.last_result()["ok"] is True
    assert speaker.last_result()["message"] == "Воспроизведено"

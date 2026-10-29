"""Engine dispatch: the local neural voice is preferred in "auto" when ready."""

from __future__ import annotations

import numpy as np
import pytest

from voiceruna.voice import tts as tts_module
from voiceruna.voice.tts import Speaker


class _Config:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class _Piper:
    """Fake local voice: reports readiness, synthesizes silent PCM."""

    def __init__(self, ok: bool = True, fail: bool = False) -> None:
        self.ok = ok
        self.fail = fail
        self.calls = 0

    def availability(self):
        return (self.ok, "локальный голос готов" if self.ok else "голос не скачан")

    def synthesize(self, text):
        self.calls += 1
        if self.fail:
            raise RuntimeError("piper broke")
        return np.zeros(2205, dtype=np.int16), 22050

    def warm_up(self):
        pass

    def close(self):
        pass


class _Player:
    def __init__(self) -> None:
        self.played = 0

    def play(self, mp3: bytes) -> None:
        self.played += 1

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Wave:
    def play_wav(self, wav, *, wait=True) -> None:
        raise AssertionError("в тесте не должно доходить до winsound")

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def _speaker(piper, engine: str = "auto", synth=None) -> Speaker:
    config = _Config({"tts.engine": engine, "tts.voice": "ru-RU-DmitryNeural", "audio.output_device": "default"})
    return Speaker(config, player=_Player(), win_wave=None) if False else Speaker(
        config, synthesize=synth or _synth_ok, player=_Player(), piper=piper
    )


async def _synth_ok(text: str, voice: str, rate: str) -> bytes:
    return b"mp3-bytes"


@pytest.mark.asyncio
async def test_auto_prefers_the_local_voice(monkeypatch):
    played = []

    class _Output:
        def __init__(self, device=None) -> None:
            pass

        def play(self, samples, sample_rate) -> None:
            played.append((len(samples), sample_rate))

    piper = _Piper()
    speaker = _speaker(piper)
    monkeypatch.setattr(tts_module, "AudioOutput", _Output)

    ok = await speaker.speak("Привет")

    assert ok is True
    assert piper.calls == 1, "в режиме auto локальный голос должен быть первым"
    assert played and played[0][1] == 22050
    assert speaker.last_result()["engine"] == "piper"
    assert "Piper" in speaker.last_result()["message"]


@pytest.mark.asyncio
async def test_auto_falls_back_to_edge_when_the_local_voice_is_missing(monkeypatch):
    piper = _Piper(ok=False)
    speaker = _speaker(piper)
    monkeypatch.setattr(tts_module, "output_backend", lambda: ("portaudio", "ok"))
    monkeypatch.setattr(tts_module, "edge_available", lambda: True)

    ok = await speaker.speak("Привет")

    assert ok is True
    assert piper.calls == 0
    assert speaker.last_result()["engine"] == "edge"


@pytest.mark.asyncio
async def test_piper_engine_does_not_fall_back(monkeypatch):
    piper = _Piper(ok=True, fail=True)
    speaker = _speaker(piper, engine="piper")

    ok = await speaker.speak("Привет")

    assert ok is False
    assert speaker.last_result()["engine"] == "piper"
    assert "Piper" in speaker.last_result()["message"]


def test_unknown_local_engine_names_do_not_crash_configuration():
    speaker = Speaker(_Config({"tts.engine": "silero"}), player=_Player(), synthesize=_synth_ok)
    assert speaker.engine_mode == "piper"


def test_local_voice_status_never_loads_the_model():
    status = Speaker(_Config({"tts.engine": "auto"}), player=_Player(), piper=_Piper(ok=False)).local_voice_status()
    assert status["available"] is False
    assert "голос" in status["message"]

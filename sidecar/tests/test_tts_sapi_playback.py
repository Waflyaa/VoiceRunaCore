"""SAPI WAV playback must not depend on winsound alone.

On a machine where waveOut has no devices (Bluetooth headsets exposing only
WDM-KS/MME) winsound fails, yet PortAudio can play. The SAPI path therefore
tries PortAudio first, then winsound, and reports the real reason when both fail.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from voiceruna.voice import tts as tts_module
from voiceruna.voice.audio import AudioUnavailable
from voiceruna.voice.tts import Speaker, _decode_wav


class _Config:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _wav_bytes(seconds: float = 0.2, rate: int = 22050) -> bytes:
    samples = (np.sin(np.linspace(0, 30, int(rate * seconds))) * 8000).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return buffer.getvalue()


def test_decode_wav_reads_rate_and_samples():
    samples, rate = _decode_wav(_wav_bytes())
    assert rate == 22050
    assert samples.dtype == np.int16
    assert samples.size > 1000


def test_sapi_wav_plays_through_portaudio_when_winsound_is_dead(monkeypatch):
    played: list[tuple[int, int]] = []

    class _FakeOutput:
        def __init__(self, device=None) -> None:
            self.device = device

        def play(self, samples, sample_rate) -> None:
            played.append((len(samples), sample_rate))

    class _DeadWave:
        def play_wav(self, wav, *, wait=True) -> None:
            raise AudioUnavailable("Cannot play asynchronously from memory")

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(tts_module, "AudioOutput", _FakeOutput)
    speaker = Speaker(_Config({"tts.engine": "sapi", "audio.output_device": "default"}))
    speaker._wave_player = _DeadWave()

    speaker._play_wav(_wav_bytes())

    assert played and played[0][1] == 22050


def test_both_playback_paths_report_both_reasons(monkeypatch):
    class _BrokenOutput:
        def __init__(self, device=None) -> None:
            pass

        def play(self, samples, sample_rate) -> None:
            raise AudioUnavailable("Blocking API not supported yet")

    class _DeadWave:
        def play_wav(self, wav, *, wait=True) -> None:
            raise AudioUnavailable("Cannot play asynchronously from memory")

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(tts_module, "AudioOutput", _BrokenOutput)
    speaker = Speaker(_Config({"tts.engine": "sapi"}))
    speaker._wave_player = _DeadWave()

    with pytest.raises(AudioUnavailable) as excinfo:
        speaker._play_wav(_wav_bytes())

    message = str(excinfo.value)
    assert "PortAudio" in message and "winsound" in message


def test_edge_cooldown_is_skipped_for_injected_synthesizers():
    tts_module.reset_edge_cooldown()
    speaker = Speaker(_Config({"tts.engine": "auto"}), synthesize=_noop_synth)
    speaker._note_edge(False)
    assert speaker._edge_available() is True, "инъекция синтезатора всегда доступна"


def test_edge_cooldown_is_shared_between_speaker_instances():
    tts_module.reset_edge_cooldown()
    main = Speaker(_Config({"tts.engine": "auto"}))
    probe = Speaker(_Config({"tts.engine": "auto"}))
    assert main._edge_available() is True and probe._edge_available() is True

    # a failed probe (the "Проверить звук" button) must teach the main speaker
    probe._note_edge(False)
    assert probe._edge_available() is False
    assert main._edge_available() is False

    main._note_edge(True)
    assert main._edge_available() is True
    tts_module.reset_edge_cooldown()


async def _noop_synth(text: str, voice: str, rate: str) -> bytes:
    return b"mp3"


def test_edge_cooldown_is_shared_between_speaker_instances():
    tts_module.reset_edge_cooldown()
    main = Speaker(_Config({"tts.engine": "auto"}))
    probe = Speaker(_Config({"tts.engine": "auto"}))
    assert main._edge_available() is True
    assert probe._edge_available() is True

    # a failed "Проверить звук" probe must teach the window's speaker too
    probe._note_edge(False)
    assert probe._edge_available() is False
    assert main._edge_available() is False

    main._note_edge(True)
    assert main._edge_available() is True
    tts_module.reset_edge_cooldown()


@pytest.mark.asyncio
async def test_skipped_edge_with_failing_sapi_reports_a_message(monkeypatch):
    """A cooldown-skipped edge-tts must never crash with an unbound variable."""

    class _NoSapi:
        def available(self) -> bool:
            return False

    class _NoPiper:
        """Hermetic: the machine may have a real local voice downloaded."""

        def availability(self):
            return False, "локальный голос не скачан"

        def synthesize(self, text):
            raise AssertionError("piper must not be used in this test")

        def close(self):
            pass

    monkeypatch.setattr(tts_module, "edge_available", lambda: False)
    monkeypatch.setattr(tts_module, "SapiSpeaker", _NoSapi)
    tts_module.reset_edge_cooldown()

    speaker = Speaker(
        _Config({"tts.engine": "auto", "audio.output_device": "default"}),
        piper=_NoPiper(),
    )
    ok = await speaker.speak("Проверка")

    assert ok is False
    result = speaker.last_result()
    assert "SAPI" in result["message"]
    assert "detail" not in result["message"]
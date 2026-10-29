"""TTS: truncation, sentence splitting, failure policy and voice cache (TZ 5.4)."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

import numpy as np

from voiceruna.voice import tts as tts_module
from voiceruna.voice.audio import AudioUnavailable
from voiceruna.voice.tts import Mp3Player, Speaker, split_sentences, truncate_text, voice_label


async def until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


class FakePlayer:
    """Collects MP3 payloads; optional blocking play for the barge-in test."""

    def __init__(self, block: bool = False, error: BaseException | None = None) -> None:
        self.played: list[bytes] = []
        self.stopped = 0
        self.closed = 0
        self.block = block
        self.error = error
        self._release = threading.Event()

    def play(self, mp3: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.played.append(mp3)
        if self.block:
            self._release.wait(5)

    def stop(self) -> None:
        self.stopped += 1
        self._release.set()

    def close(self) -> None:
        self.closed += 1


class FakeSapi:
    """Offline SAPI stand-in: renders a fixed WAV without PowerShell."""

    def __init__(self, *, available: bool = True, wav: bytes = b"RIFF____WAVE", voice: str = "Microsoft Irina Desktop",
                 error: BaseException | None = None) -> None:
        self._available = available
        self.wav = wav
        self.voice = voice
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self._available

    def preferred_voice(self) -> str:
        return self.voice

    def synthesize(self, text: str, voice: str = "") -> bytes:
        self.calls.append((text, voice))
        if self.error is not None:
            raise self.error
        return self.wav


class FakeWavePlayer:
    """Collects WAV payloads played through winsound."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.played: list[bytes] = []
        self.stopped = 0
        self.closed = 0
        self.error = error

    def play_wav(self, wav: bytes, *, wait: bool = True) -> None:
        if self.error is not None:
            raise self.error
        self.played.append(wav)

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1


def make_speaker(config, data_dir, *, synth=None, player=None, block=False, sapi=None, wave=None):
    calls: list[tuple[str, str, str]] = []
    if synth is None:
        async def synth(text: str, voice: str, rate: str) -> bytes:
            calls.append((text, voice, rate))
            return b"mp3"
    player = player or FakePlayer(block=block)
    speaker = Speaker(
        config,
        synthesize=synth,
        player=player,
        voices_path=data_dir / "voices.json",
        # Tests never touch PowerShell or winsound: SAPI is off unless a test
        # injects its own fake explicitly.
        sapi=sapi if sapi is not None else FakeSapi(available=False),
        wave_player=wave if wave is not None else FakeWavePlayer(),
    )
    return speaker, player, calls


# --------------------------------------------------------------------------- text
def test_max_chars_zero_means_no_truncation():
    text = "Очень длинный ответ, который никто не собирается обрезать."
    assert truncate_text(text, 0) == text
    assert truncate_text(text, -5) == text


def test_truncation_respects_max_chars():
    text = "Открываю Dota 2, подожди немного"
    result = truncate_text(text, 12)
    assert len(result) <= 12
    assert result.startswith("Открываю")


def test_short_text_is_not_truncated():
    assert truncate_text("Сейчас 14:32", 40) == "Сейчас 14:32"


def test_split_sentences():
    assert split_sentences("Привет. Как дела? Хорошо!") == ["Привет.", "Как дела?", "Хорошо!"]
    assert split_sentences("") == []
    assert split_sentences("Первая строка\nВторая строка") == ["Первая строка", "Вторая строка"]


def test_split_long_sentence_keeps_chunks_bounded():
    text = "слово " * 200
    pieces = split_sentences(text.strip(), max_len=100)
    assert len(pieces) > 1
    assert all(len(piece) <= 100 for piece in pieces)


def test_voice_label():
    assert voice_label("ru-RU-DmitryNeural", "Male") == "Дмитрий (мужской)"
    assert voice_label("ru-RU-SvetlanaNeural", "Female") == "Светлана (женский)"
    assert voice_label("xx-YY-UnknownNeural", "") == "Unknown"


# --------------------------------------------------------------------------- speak
async def test_speak_plays_each_sentence_with_config_voice(config, data_dir):
    config.set("tts.voice", "ru-RU-SvetlanaNeural")
    config.set("tts.rate", "+15%")
    speaker, player, calls = make_speaker(config, data_dir)
    assert await speaker.speak("Привет. Как дела?") is True
    assert [call[0] for call in calls] == ["Привет.", "Как дела?"]
    assert all(call[1] == "ru-RU-SvetlanaNeural" for call in calls)
    assert all(call[2] == "+15%" for call in calls)
    assert player.played == [b"mp3", b"mp3"]


async def test_max_chars_is_applied_before_synthesis(config, data_dir):
    config.set("tts.max_chars", 5)
    speaker, _player, calls = make_speaker(config, data_dir)
    await speaker.speak("Открываю Dota 2, подожди")
    assert len(calls) == 1
    assert len(calls[0][0]) <= 6


async def test_synthesis_failure_does_not_raise(config, data_dir):
    async def synth(text: str, voice: str, rate: str) -> bytes:
        raise RuntimeError("no network")

    speaker, player, _calls = make_speaker(config, data_dir, synth=synth)
    assert await speaker.speak("Привет.") is False
    assert player.played == []


async def test_empty_text_is_a_no_op(config, data_dir):
    speaker, player, calls = make_speaker(config, data_dir)
    assert await speaker.speak("   ") is True
    assert calls == []
    assert player.played == []


async def test_stop_interrupts_playback(config, data_dir):
    speaker, player, _calls = make_speaker(config, data_dir, block=True)
    task = asyncio.create_task(speaker.speak("Привет. Пока."))
    assert await until(lambda: player.played)
    speaker.stop()
    await asyncio.wait_for(task, 2.0)
    assert player.stopped >= 1


async def test_preview_uses_the_requested_voice(config, data_dir):
    speaker, player, calls = make_speaker(config, data_dir)
    ok, message = await speaker.preview("ru-RU-DmitryNeural", "Проверка голоса")
    assert ok is True
    assert "edge-tts" in message and "ru-RU-DmitryNeural" in message
    assert calls[0][1] == "ru-RU-DmitryNeural"
    assert player.played


async def test_preview_reports_missing_output_device(config, data_dir):
    player = FakePlayer(error=AudioUnavailable("нет устройства вывода (PortAudio: WDM-KS)"))
    speaker, _player, _calls = make_speaker(config, data_dir, player=player, sapi=FakeSapi(available=False))
    ok, message = await speaker.preview("ru-RU-DmitryNeural", "Проверка звука")
    assert ok is False
    assert message == "нет устройства вывода"
    assert speaker.last_result()["ok"] is False


# --------------------------------------------------------------------------- voices
async def test_cached_voices_are_returned_without_network(config, data_dir):
    cache = data_dir / "voices.json"
    cached = [{"engine": "edge", "name": "ru-RU-TestNeural", "label": "Тест"}]
    cache.write_text(json.dumps(cached), encoding="utf-8")
    speaker, _player, _calls = make_speaker(config, data_dir)
    assert await speaker.list_voices() == cached


async def test_list_voices_filters_russian_and_caches(config, data_dir, monkeypatch):
    import edge_tts

    async def fake_list() -> list[dict]:
        return [
            {"ShortName": "ru-RU-DmitryNeural", "Gender": "Male", "Locale": "ru-RU"},
            {"ShortName": "en-US-GuyNeural", "Gender": "Male", "Locale": "en-US"},
        ]

    monkeypatch.setattr(edge_tts, "list_voices", fake_list)
    speaker, _player, _calls = make_speaker(config, data_dir)
    voices = await speaker.list_voices()
    assert [voice["name"] for voice in voices] == ["ru-RU-DmitryNeural"]
    assert voices[0]["label"] == "Дмитрий (мужской)"
    stored = json.loads((data_dir / "voices.json").read_text(encoding="utf-8"))
    assert stored[0]["name"] == "ru-RU-DmitryNeural"


async def test_list_voices_falls_back_to_config_offline(config, data_dir, monkeypatch):
    import edge_tts

    async def boom() -> list[dict]:
        raise RuntimeError("offline")

    monkeypatch.setattr(edge_tts, "list_voices", boom)
    speaker, _player, _calls = make_speaker(config, data_dir)
    voices = await speaker.list_voices()
    assert {voice["name"] for voice in voices} == {
        "ru-RU-DmitryNeural",
        "ru-RU-SvetlanaNeural",
        "ru-RU-DariyaNeural",
    }

# --------------------------------------------------------------------------- mp3 player chain
class FakeOutput:
    """PortAudio output stand-in."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.played: list[tuple[int, int]] = []
        self.stopped = 0

    def play(self, samples, sample_rate: int = 24000) -> None:
        if self.error is not None:
            raise self.error
        self.played.append((len(samples), sample_rate))

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.stopped += 1


class FakeMci:
    played: list[bytes] = []
    error: BaseException | None = None

    def __init__(self) -> None:
        return None

    def play(self, mp3: bytes) -> None:
        if FakeMci.error is not None:
            raise FakeMci.error
        FakeMci.played.append(mp3)

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_mp3_player_uses_portaudio_first(monkeypatch):
    monkeypatch.setattr(tts_module, "decode_mp3", lambda mp3: (np.zeros(2400, dtype=np.int16), 24000))
    output = FakeOutput()
    wave = FakeWavePlayer()
    player = Mp3Player(output=output, wave_player=wave, allow_mci=False)
    player.play(b"mp3")
    assert output.played == [(2400, 24000)]
    assert wave.played == []


def test_mp3_player_falls_back_to_winsound(monkeypatch):
    monkeypatch.setattr(tts_module, "decode_mp3", lambda mp3: (np.zeros(2400, dtype=np.int16), 24000))
    output = FakeOutput(AudioUnavailable("Blocking API not supported yet"))
    wave = FakeWavePlayer()
    player = Mp3Player(output=output, wave_player=wave, allow_mci=False, device=3)
    player.play(b"mp3")
    assert output.played == []
    assert len(wave.played) == 1
    assert wave.played[0][:4] == b"RIFF"
    assert player.device == 3


def test_mp3_player_falls_back_to_mci(monkeypatch):
    def no_decoder(mp3):
        raise tts_module._DecodeUnavailable("miniaudio is not installed")

    monkeypatch.setattr(tts_module, "decode_mp3", no_decoder)
    monkeypatch.setattr(tts_module, "_MciPlayer", FakeMci)
    FakeMci.played.clear()
    player = Mp3Player(output=FakeOutput(AudioUnavailable("no device")), wave_player=FakeWavePlayer())
    player.play(b"mp3-bytes")
    assert FakeMci.played == [b"mp3-bytes"]


def test_mp3_player_raises_when_nothing_can_play(monkeypatch):
    monkeypatch.setattr(tts_module, "decode_mp3", lambda mp3: (np.zeros(16, dtype=np.int16), 24000))
    FakeMci.error = AudioUnavailable("MCI \u043e\u0448\u0438\u0431\u043a\u0430 263")
    try:
        player = Mp3Player(
            output=FakeOutput(AudioUnavailable("no device")),
            wave_player=FakeWavePlayer(AudioUnavailable("winsound \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d")),
            allow_mci=True,
        )
        with pytest.raises(AudioUnavailable) as info:
            player.play(b"mp3")
    finally:
        FakeMci.error = None
    message = str(info.value)
    assert "\u043d\u0435\u0442 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430 \u0432\u044b\u0432\u043e\u0434\u0430" in message
    assert "PortAudio" in message and "winsound" in message and "MCI" in message


# --------------------------------------------------------------------------- SAPI fallback
async def test_edge_failure_falls_back_to_sapi(config, data_dir):
    assert config.get("tts.engine") == "auto"

    async def failing_edge(text: str, voice: str, rate: str) -> bytes:
        raise RuntimeError("No audio was received")

    wave = FakeWavePlayer()
    sapi = FakeSapi()
    speaker, player, _calls = make_speaker(config, data_dir, synth=failing_edge, sapi=sapi, wave=wave)
    assert await speaker.speak("\u041f\u0440\u0438\u0432\u0435\u0442.") is True
    assert player.played == []
    assert wave.played == [sapi.wav]
    assert sapi.calls and sapi.calls[0][1] == "Microsoft Irina Desktop"
    result = speaker.last_result()
    assert result["ok"] is True
    assert result["engine"] == "sapi"
    assert "SAPI" in result["message"] and "Microsoft Irina Desktop" in result["message"]


async def test_edge_is_retried_before_sapi(config, data_dir):
    attempts: list[str] = []

    async def flaky_edge(text: str, voice: str, rate: str) -> bytes:
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError("No audio was received")
        return b"mp3"

    sapi = FakeSapi()
    speaker, player, _calls = make_speaker(config, data_dir, synth=flaky_edge, sapi=sapi)
    assert await speaker.speak("\u041f\u0440\u0438\u0432\u0435\u0442.") is True
    assert player.played == [b"mp3"]
    assert sapi.calls == []  # the repeat succeeded, SAPI was not needed


async def test_engine_edge_has_no_sapi_fallback(config, data_dir):
    config.set("tts.engine", "edge")

    async def failing_edge(text: str, voice: str, rate: str) -> bytes:
        raise RuntimeError("offline")

    sapi = FakeSapi()
    speaker, _player, _calls = make_speaker(config, data_dir, synth=failing_edge, sapi=sapi)
    assert await speaker.speak("\u041f\u0440\u0438\u0432\u0435\u0442.") is False
    assert sapi.calls == []
    result = speaker.last_result()
    assert result["ok"] is False
    assert result["engine"] == "edge"
    assert result["message"].startswith("edge-tts:")


async def test_engine_sapi_skips_edge_entirely(config, data_dir):
    config.set("tts.engine", "sapi")
    wave = FakeWavePlayer()
    sapi = FakeSapi()
    speaker, player, calls = make_speaker(config, data_dir, sapi=sapi, wave=wave)
    assert await speaker.speak("\u041f\u0440\u0438\u0432\u0435\u0442.") is True
    assert calls == []  # edge synthesis was never asked
    assert player.played == []
    assert wave.played == [sapi.wav]
    assert speaker.last_result()["engine"] == "sapi"


async def test_sapi_synthesis_failure_is_reported(config, data_dir):
    async def failing_edge(text: str, voice: str, rate: str) -> bytes:
        raise RuntimeError("offline")

    sapi = FakeSapi(error=RuntimeError("SAPI: no voice"))
    speaker, _player, _calls = make_speaker(config, data_dir, synth=failing_edge, sapi=sapi)
    assert await speaker.speak("\u041f\u0440\u0438\u0432\u0435\u0442.") is False
    message = speaker.last_result()["message"]
    assert "edge-tts" in message and "SAPI" in message


async def test_sapi_playback_without_device_is_reported(config, data_dir):
    async def failing_edge(text: str, voice: str, rate: str) -> bytes:
        raise RuntimeError("offline")

    wave = FakeWavePlayer(error=AudioUnavailable("winsound \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d"))
    speaker, _player, _calls = make_speaker(config, data_dir, synth=failing_edge, sapi=FakeSapi(), wave=wave)
    assert await speaker.speak("\u041f\u0440\u0438\u0432\u0435\u0442.") is False
    assert "\u043d\u0435\u0442 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430 \u0432\u044b\u0432\u043e\u0434\u0430" in speaker.last_result()["message"]


async def test_preview_through_sapi_reports_the_voice(config, data_dir):
    config.set("tts.engine", "sapi")
    sapi = FakeSapi()
    speaker, _player, _calls = make_speaker(config, data_dir, sapi=sapi)
    ok, message = await speaker.preview("ru-RU-DmitryNeural", "\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430")
    assert ok is True
    assert message == "\u0412\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0434\u0435\u043d\u043e \u0447\u0435\u0440\u0435\u0437 SAPI (Microsoft Irina Desktop)"
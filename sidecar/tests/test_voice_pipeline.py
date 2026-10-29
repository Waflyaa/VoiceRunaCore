"""VoicePipeline end-to-end with fake mic/VAD/wake/STT/TTS.

No microphone, no models, no network: every hardware or engine dependency is
swapped through VoicePipeline.use_components().
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

import time

from voiceruna.state import ERROR, IDLE, LISTENING, PAUSED, SPEAKING, StateManager
from voiceruna.voice import audio as voice_audio
from voiceruna.voice import pipeline as pipeline_module
from voiceruna.voice.audio import AudioUnavailable, FrameQueue
from voiceruna.voice.pipeline import VoicePipeline

FRAME = 512
SILENCE = np.zeros(FRAME, dtype=np.int16)
SPEECH = np.full(FRAME, 6000, dtype=np.int16)


# --------------------------------------------------------------------------- fakes
class FakeMic(FrameQueue):
    """Test microphone: the test pushes frames, the pipeline reads them."""

    def __init__(self) -> None:
        super().__init__(maxsize=64)
        self.started = 0
        self.stopped = 0
        self._closed_flag = False

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    @property
    def active(self) -> bool:
        return self.started > self.stopped and not self._closed_flag

    def close(self) -> None:
        self._closed_flag = True
        super().close()


class FailingMic:
    """A machine without any input device."""

    def __init__(self) -> None:
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        raise AudioUnavailable("no input device")

    def stop(self) -> None:
        return None

    def read(self, timeout: float | None = 0.1):
        return None

    @property
    def active(self) -> bool:
        return False

    def close(self) -> None:
        return None


class FakeVad:
    def __init__(self, peak: int = 100) -> None:
        self.peak = peak

    def is_speech(self, frame) -> tuple[bool, float]:
        array = np.asarray(frame)
        level = int(np.abs(array).max()) if array.size else 0
        return level > self.peak, 1.0 if level > self.peak else 0.0

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeWake:
    """Fires once when next_hit is armed; records the barge-in flag."""

    def __init__(self) -> None:
        self.next_hit = False
        self.calls = 0
        self.barge_calls = 0

    def feed(self, frame, barge_in: bool = False) -> bool:
        self.calls += 1
        if barge_in:
            self.barge_calls += 1
        hit, self.next_hit = self.next_hit, False
        return hit

    def configure(self, config) -> None:
        return None

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeStt:
    def __init__(self, text: str | None = "открой блокнот", *, error=None, availability=None, details=None) -> None:
        self.text = text
        self.error = error
        self.calls = 0
        self.received: list[np.ndarray] = []
        self._availability = availability
        self._details = details

    def transcribe_details(self, audio, sample_rate=16000):
        self.received.append(np.asarray(audio))
        self.calls += 1
        if self.error is not None:
            raise self.error
        base = {"text": self.text or "", "confidence": None, "segments": 1,
                "processing_s": 0.05, "language": "ru", "peak_dbfs": None, "normalized": False}
        if self._details:
            base.update(self._details)
        return base

    def transcribe(self, audio) -> str | None:
        self.calls += 1
        self.received.append(np.asarray(audio))
        if self.error is not None:
            raise self.error
        return self.text

    def availability(self) -> tuple[bool, str]:
        return self._availability if self._availability is not None else (True, "")

    def maybe_unload(self, now: float | None = None) -> bool:
        return False

    def configure(self, config) -> None:
        return None


class FakeSpeaker:
    def __init__(self, block: bool = True) -> None:
        self.spoken: list[str] = []
        self.stopped = 0
        self.block = block
        self._release = asyncio.Event()

    def configure(self, config) -> None:
        return None

    async def speak(self, text: str) -> bool:
        self.spoken.append(text)
        if not self.block:
            return True
        self._release.clear()
        await self._release.wait()
        return True

    def stop(self) -> None:
        self.stopped += 1
        self._release.set()

    async def preview(self, voice: str, text: str = "") -> tuple[bool, str]:
        self.spoken.append(text)
        return True, "Воспроизведено через edge-tts"

    async def list_voices(self) -> list[dict]:
        return [{"engine": "edge", "name": "ru-RU-DmitryNeural", "label": "Дмитрий"}]

    def close(self) -> None:
        return None


class Rig:
    """Everything one pipeline test needs, built in one place."""

    def __init__(self, config, events, stt_text="открой блокнот", block=True, stt_details=None,
                 wake_mode: str = "vosk") -> None:
        # Most pipeline tests exercise the Vosk path; the vad_stt mode has its
        # own tests below and gets the mode explicitly.
        config.set("wakeword.engine", wake_mode, persist=False)
        self.state = StateManager()
        self.seen_states: list[str] = []
        self.state.subscribe(self.seen_states.append)
        self.spoken: list[str] = []
        self.events = events

        async def on_utterance(text: str) -> None:
            self.spoken.append(text)

        self.pipeline = VoicePipeline(config=config, state=self.state, emit=events, on_utterance=on_utterance)
        self.mic = FakeMic()
        self.vad = FakeVad()
        self.wake = FakeWake()
        self.stt = FakeStt(stt_text, details=stt_details)
        self.tts = FakeSpeaker(block=block)
        self.pipeline.use_components(
            mic=self.mic,
            vad=self.vad,
            wake=self.wake,
            stt=self.stt,
            tts=self.tts,
        )

    def types(self) -> list[str]:
        return [event.type for event in self.events.collected]


async def until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


async def push(mic: FakeMic, frames, delay: float = 0.02) -> None:
    for item in frames:
        mic.push(item)
        await asyncio.sleep(delay)


def quiet_config(config) -> None:
    config.set("vad.silence_ms", 96)
    config.set("vad.max_utterance_sec", 12)
    config.set("vad.pre_buffer_ms", 64)
    # The wake detector keeps its own short segmenter.
    config.set("vad.wake_silence_ms", 96)
    config.set("vad.wake_max_sec", 3.5)
    config.set("vad.wake_pre_buffer_ms", 64)
    config.set("wakeword.listen_timeout_sec", 1)


async def say_command(rig: Rig, *, wake: bool = True, speech_frames: int = 8, silence_frames: int = 4) -> None:
    if wake:
        rig.wake.next_hit = True
        await push(rig.mic, [SILENCE])
        assert await until(lambda: rig.state.state == LISTENING)
    await push(rig.mic, [SPEECH] * speech_frames)
    await push(rig.mic, [SILENCE] * silence_frames)


# --------------------------------------------------------------------------- tests
async def test_start_without_microphone_degrades(config, events):
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(mic=FailingMic())
    await rig.pipeline.start()
    try:
        assert rig.state.state == ERROR
        assert any(
            event.type == "error" and config.get("errors.mic_phrase") in event.data["message"]
            for event in events.collected
        )
        # the rest of the assistant keeps working
        await rig.pipeline.speak("привет")
        assert rig.tts.spoken == ["привет"]
    finally:
        await rig.pipeline.stop()


async def test_full_cycle_wake_transcript_utterance(config, events):
    quiet_config(config)
    rig = Rig(config, events)
    await rig.pipeline.start()
    try:
        assert rig.state.state == IDLE
        await push(rig.mic, [SILENCE, SILENCE])
        await say_command(rig)
        assert await until(lambda: rig.spoken == ["открой блокнот"])
        assert "transcript" in rig.types()
        transcript = [event for event in events.collected if event.type == "transcript"][0]
        assert transcript.data == {"text": "открой блокнот", "source": "voice"}
        # the runtime owns thinking; the pipeline must not fight for it
        assert "thinking" not in rig.seen_states
    finally:
        await rig.pipeline.stop()


async def test_listen_timeout_returns_to_idle(config, events):
    quiet_config(config)
    config.set("wakeword.listen_timeout_sec", 1)
    rig = Rig(config, events)
    await rig.pipeline.start()
    try:
        rig.wake.next_hit = True
        await push(rig.mic, [SILENCE])
        assert await until(lambda: rig.state.state == LISTENING)
        assert await until(lambda: rig.state.state == IDLE, timeout=2.5)
        assert rig.stt.calls == 0
    finally:
        await rig.pipeline.stop()


async def test_listen_now_skips_the_wake_word(config, events):
    quiet_config(config)
    rig = Rig(config, events)
    await rig.pipeline.start()
    try:
        await rig.pipeline.listen_now()
        assert rig.state.state == LISTENING
        await say_command(rig, wake=False)
        assert await until(lambda: rig.spoken == ["открой блокнот"])
    finally:
        await rig.pipeline.stop()


async def test_empty_transcript_asks_to_repeat(config, events):
    quiet_config(config)
    rig = Rig(config, events, stt_text=None, block=False)
    await rig.pipeline.start()
    try:
        await say_command(rig)
        phrase = config.get("errors.stt_failed_phrase")
        assert await until(lambda: rig.tts.spoken == [phrase])
        assert await until(lambda: rig.state.state == IDLE)
        assert any(event.type == "error" and event.data["kind"] == "stt" for event in events.collected)
    finally:
        await rig.pipeline.stop()


async def test_barge_in_stops_speaking_and_starts_listening(config, events):
    quiet_config(config)
    rig = Rig(config, events)
    await rig.pipeline.start()
    try:
        rig.wake.next_hit = True
        await push(rig.mic, [SILENCE])
        assert await until(lambda: rig.state.state == LISTENING)

        speaking = asyncio.create_task(rig.pipeline.speak("Большой ответ"))
        assert await until(lambda: rig.state.state == SPEAKING)
        assert rig.tts.spoken == ["Большой ответ"]

        rig.wake.next_hit = True
        await push(rig.mic, [SILENCE])
        assert await until(lambda: rig.tts.stopped > 0)
        assert await until(lambda: rig.state.state == LISTENING)
        assert rig.wake.barge_calls > 0
        await asyncio.wait_for(speaking, 2.0)
    finally:
        await rig.pipeline.stop()


async def test_pause_and_resume(config, events):
    quiet_config(config)
    rig = Rig(config, events)
    await rig.pipeline.start()
    try:
        await rig.pipeline.pause()
        assert rig.state.state == PAUSED
        assert rig.mic.stopped >= 1
        rig.wake.next_hit = True
        rig.mic.clear()
        await asyncio.sleep(0.1)
        assert rig.state.state == PAUSED
        await rig.pipeline.resume()
        assert rig.state.state == IDLE
        assert rig.mic.started >= 2
    finally:
        await rig.pipeline.stop()


async def test_stop_is_idempotent_and_speak_after_stop_is_safe(config, events):
    quiet_config(config)
    rig = Rig(config, events, block=False)
    await rig.pipeline.start()
    await rig.pipeline.stop()
    await rig.pipeline.stop()
    await rig.pipeline.speak("после остановки")
    await rig.pipeline.listen_now()
    assert rig.tts.spoken == []


async def test_apply_config_before_start_does_not_raise(config, events):
    rig = Rig(config, events)
    rig.pipeline.apply_config()
    config.set("tts.voice", "ru-RU-SvetlanaNeural")
    rig.pipeline.apply_config()
    assert rig.state.state == IDLE


async def test_disarmed_phrase_is_discarded_silently(config, events):
    quiet_config(config)
    config.set("vad.silence_ms", 32)
    rig = Rig(config, events)
    await rig.pipeline.start()
    try:
        rig.wake.next_hit = True
        await push(rig.mic, [SILENCE])
        assert await until(lambda: rig.state.state == LISTENING)
        await push(rig.mic, [SPEECH])
        await push(rig.mic, [SILENCE, SILENCE])
        assert await until(lambda: rig.state.state == IDLE)
        assert rig.stt.calls == 0
        assert rig.tts.spoken == []
    finally:
        await rig.pipeline.stop()


async def test_list_voices_and_preview_are_exposed(config, events):
    rig = Rig(config, events, block=False)
    await rig.pipeline.start()
    try:
        voices = await rig.pipeline.list_voices()
        assert voices[0]["name"] == "ru-RU-DmitryNeural"
        ok, message = await rig.pipeline.preview("ru-RU-DmitryNeural", "проверка")
        assert ok is True
        assert "edge-tts" in message
        assert "проверка" in rig.tts.spoken
    finally:
        await rig.pipeline.stop()

async def test_only_one_phrase_per_wake(config, events):
    """After the end of a phrase the segmenter must not start another one."""
    quiet_config(config)
    rig = Rig(config, events, block=False)
    await rig.pipeline.start()
    try:
        await say_command(rig)
        assert await until(lambda: rig.spoken == ["открой блокнот"])
        await push(rig.mic, [SPEECH] * 4 + [SILENCE] * 4)
        await asyncio.sleep(0.2)
        assert rig.spoken == ["открой блокнот"]
        assert rig.stt.calls == 1
    finally:
        await rig.pipeline.stop()

# --------------------------------------------------------------------------- audio devices
class FakeOutputProbe:
    """Stands in for the temporary Speaker used by test_output."""

    def __init__(self, result: tuple[bool, str] = (True, "Воспроизведено через edge-tts"), error=None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.closed = 0

    async def preview(self, voice: str, text: str = "") -> tuple[bool, str]:
        self.calls.append((voice, text))
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        self.closed += 1


class FakeInputProbe:
    """Stands in for the temporary microphone used by test_input."""

    def __init__(self, frames, *, start_error: Exception | None = None) -> None:
        self.frames = list(frames)
        self.start_error = start_error
        self.pos = 0
        self.started = 0
        self.closed = 0

    def start(self) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    def read(self, timeout: float | None = 0.1):
        if self.pos < len(self.frames):
            frame = self.frames[self.pos]
            self.pos += 1
            return frame
        time.sleep(0.01)
        return None

    def close(self) -> None:
        self.closed += 1


async def test_test_input_reports_silence(config, events):
    rig = Rig(config, events, block=False)
    probe = FakeInputProbe([SILENCE])
    rig.pipeline.use_components(input_probe=lambda device: probe)
    ok, message, level = await rig.pipeline.test_input(seconds=0.2)
    assert ok is True
    assert message == "тишина"
    assert level == pytest.approx(0.0)
    assert probe.started == 1 and probe.closed == 1


async def test_test_input_reports_signal_level(config, events):
    rig = Rig(config, events, block=False)
    loud = np.full(FRAME, 6000, dtype=np.int16)
    probe = FakeInputProbe([loud, loud])
    rig.pipeline.use_components(input_probe=lambda device: probe)
    ok, message, level = await rig.pipeline.test_input(seconds=0.2)
    assert ok is True
    assert level > 0.01
    assert "уровень" in message


async def test_test_input_without_data(config, events):
    rig = Rig(config, events, block=False)
    probe = FakeInputProbe([])
    rig.pipeline.use_components(input_probe=lambda device: probe)
    ok, message, level = await rig.pipeline.test_input(seconds=0.2)
    assert ok is False
    assert "не прислал данных" in message
    assert level == 0.0


async def test_test_input_microphone_unavailable(config, events):
    rig = Rig(config, events, block=False)
    probe = FakeInputProbe([], start_error=AudioUnavailable("no device"))
    rig.pipeline.use_components(input_probe=lambda device: probe)
    ok, message, level = await rig.pipeline.test_input(seconds=0.2)
    assert ok is False
    assert message == "микрофон недоступен"
    assert level == 0.0


async def test_test_output_reports_the_device(config, events):
    rig = Rig(config, events, block=False)
    probe = FakeOutputProbe((True, "Воспроизведено через edge-tts (ru-RU-DmitryNeural)"))
    rig.pipeline.use_components(output_probe=lambda device: probe)
    ok, message = await rig.pipeline.test_output()
    assert ok is True
    assert "Воспроизведено" in message
    assert "устройство" in message
    assert probe.closed == 1


async def test_test_output_failure_is_reported(config, events):
    rig = Rig(config, events, block=False)
    probe = FakeOutputProbe((False, "нет устройства вывода"))
    rig.pipeline.use_components(output_probe=lambda device: probe)
    ok, message = await rig.pipeline.test_output()
    assert ok is False
    assert message == "нет устройства вывода"


async def test_test_output_survives_a_broken_probe(config, events):
    rig = Rig(config, events, block=False)
    probe = FakeOutputProbe(error=RuntimeError("boom"))
    rig.pipeline.use_components(output_probe=lambda device: probe)
    ok, message = await rig.pipeline.test_output()
    assert ok is False
    assert "не удалась" in message


def test_devices_and_playback_status(monkeypatch, config, events):
    rig = Rig(config, events, block=False)
    monkeypatch.setattr(
        pipeline_module,
        "list_devices",
        lambda: {
            "input": [{"index": 1, "name": "Микрофон", "channels": 2, "hostapi": "MME", "default": True}],
            "output": [],
        },
    )
    monkeypatch.setattr(pipeline_module, "output_backend", lambda: ("none", "нет устройства вывода: колонки или наушники не подключены"))
    assert rig.pipeline.playback_status() == {
        "backend": "none",
        "ok": False,
        "message": "нет устройства вывода: колонки или наушники не подключены",
    }
    devices = asyncio.run(rig.pipeline.list_devices())
    assert devices["input"][0]["name"] == "Микрофон"


def test_apply_config_rebuilds_the_player_on_output_change(config, events):
    state = StateManager()
    async def on_utterance(text: str) -> None:
        return None
    pipe = VoicePipeline(config=config, state=state, emit=events, on_utterance=on_utterance)
    previous = pipe._speaker
    config.set("audio.output_device", "index:2")
    pipe.apply_config()
    assert pipe._speaker is not previous
    assert pipe._speaker._player.device == 2  # index:2 resolved into the new player


def test_apply_config_keeps_an_injected_speaker(config, events):
    rig = Rig(config, events, block=False)
    previous = rig.pipeline._speaker
    config.set("audio.output_device", "index:3")
    rig.pipeline.apply_config()
    assert rig.pipeline._speaker is previous

# --------------------------------------------------------------------------- STT self-test
class FakeSapiProbe:
    """Offline SAPI stand-in for the STT self-test."""

    def __init__(self, *, available: bool = True, wav: bytes | None = None,
                 error: Exception | None = None, voice: str = "Microsoft Irina Desktop") -> None:
        self._available = available
        self.wav = wav if wav is not None else voice_audio.to_wav_bytes(np.zeros(2205, dtype=np.int16), 22050)
        self.error = error
        self.voice = voice
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


def sapi_pipeline(rig: Rig, probe: FakeSapiProbe) -> None:
    rig.pipeline.use_components(sapi_probe=lambda: probe)


async def test_test_stt_uses_the_same_transcriber_and_the_sapi_phrase(config, events):
    rig = Rig(config, events, block=False)
    one_second = voice_audio.to_wav_bytes(np.zeros(22050, dtype=np.int16), 22050)
    probe = FakeSapiProbe(wav=one_second)
    sapi_pipeline(rig, probe)

    ok, message, recognized = await rig.pipeline.test_stt()

    assert ok is True
    assert recognized == "открой блокнот"
    assert "открой блокнот" in message
    # the phrase really went through SAPI and then through the loop's own STT
    assert probe.calls and probe.calls[0][0] == "Открой блокнот"
    assert probe.calls[0][1] == "Microsoft Irina Desktop"
    assert rig.stt.calls == 1
    audio = rig.stt.received[0]
    assert audio.dtype == np.int16
    assert audio.size == pytest.approx(16000, abs=2)  # 1 s resampled to 16 kHz


async def test_test_stt_without_offline_synthesis(config, events):
    rig = Rig(config, events, block=False)
    sapi_pipeline(rig, FakeSapiProbe(available=False))
    ok, message, recognized = await rig.pipeline.test_stt()
    assert ok is False
    assert recognized == ""
    assert message.startswith("нет офлайн-синтеза для самотеста")
    assert rig.stt.calls == 0


async def test_test_stt_reports_synthesis_failure(config, events):
    rig = Rig(config, events, block=False)
    sapi_pipeline(rig, FakeSapiProbe(error=RuntimeError("SAPI: no voice")))
    ok, message, _recognized = await rig.pipeline.test_stt()
    assert ok is False
    assert "нет офлайн-синтеза" in message and "RuntimeError" in message


async def test_test_stt_reports_a_crashing_transcriber(config, events):
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(stt=FakeStt(error=RuntimeError("cublas missing")))
    sapi_pipeline(rig, FakeSapiProbe())

    ok, message, recognized = await rig.pipeline.test_stt()

    assert ok is False
    assert recognized == ""
    assert "cublas missing" in message and "RuntimeError" in message
    assert rig.pipeline.diagnostics()["stt"]["error"] and "cublas missing" in rig.pipeline.diagnostics()["stt"]["error"]


async def test_test_stt_reports_an_empty_result(config, events):
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(stt=FakeStt(None))
    sapi_pipeline(rig, FakeSapiProbe())
    ok, message, recognized = await rig.pipeline.test_stt()
    assert ok is False
    assert recognized == ""
    assert "пустую строку" in message


async def test_stt_failure_is_visible_in_the_events(config, events):
    quiet_config(config)
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(stt=FakeStt(error=RuntimeError("CUDA driver missing")))
    await rig.pipeline.start()
    try:
        await say_command(rig)
        phrase = config.get("errors.stt_failed_phrase")
        assert await until(lambda: rig.tts.spoken == [phrase])
        errors = [event for event in events.collected if event.type == "error" and event.data["kind"] == "stt"]
        assert errors, "ошибка STT должна доходить до окна"
        assert any("CUDA driver missing" in event.data["message"] for event in errors)
        assert any("RuntimeError" in event.data["message"] for event in errors)
    finally:
        await rig.pipeline.stop()


async def test_start_reports_an_unavailable_model_once(config, events):
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(
        stt=FakeStt(availability=(False, "распознавание речи: модель whisper-small не скачана"))
    )
    await rig.pipeline.start()
    try:
        errors = [event for event in events.collected if event.type == "error" and event.data["kind"] == "stt"]
        assert len(errors) == 1
        assert "не скачана" in errors[0].data["message"]
        ready = rig.pipeline.diagnostics()["stt"]
        assert ready["ready"] is False
        assert "не скачана" in ready["error"]
    finally:
        await rig.pipeline.stop()


# --------------------------------------------------------------------------- diagnostics
def test_diagnostics_before_start_is_safe_and_complete(config, events):
    rig = Rig(config, events, block=False)
    info = rig.pipeline.diagnostics()

    assert info["state"] == "idle"
    # extra diagnostic keys are fine, the documented ones must be there
    assert set({"ok": False, "device": "default", "error": None}.items()) <= set(info["mic"].items())
    assert set({"type": "FakeVad", "ready": True}.items()) <= set(info["vad"].items())
    assert set({"engine": "none", "ready": False}.items()) <= set(info["wake"].items())
    assert info["stt"]["ready"] is True
    assert info["stt"]["loaded"] is False
    assert info["tts"]["backend"] in {"portaudio", "winsound", "none"}
    assert info["voices"]["count"] == 0
    assert info["models_dir"]


async def test_diagnostics_after_start_reflects_the_running_pipeline(config, events):
    quiet_config(config)
    rig = Rig(config, events, block=False)
    await rig.pipeline.start()
    try:
        info = rig.pipeline.diagnostics()
        assert info["state"] == "idle"
        assert info["mic"]["ok"] is True
        assert info["stt"]["ready"] is True
        assert info["stt"]["loaded"] is False  # diagnostics never loads whisper
    finally:
        await rig.pipeline.stop()
    info = rig.pipeline.diagnostics()
    assert info["mic"]["ok"] is False


async def test_diagnostics_reports_the_microphone_error(config, events):
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(mic=FailingMic())
    await rig.pipeline.start()
    try:
        info = rig.pipeline.diagnostics()
        assert info["mic"]["ok"] is False
        assert info["mic"]["error"] and "no input device" in info["mic"]["error"]
    finally:
        await rig.pipeline.stop()

# --------------------------------------------------------------------------- diagnostics: audio pipeline
class ProbeMic:
    """Microphone used by record_and_recognize (no FrameQueue needed)."""

    def __init__(self, frames, *, samplerate: int = 16000, device_name: str = "Микрофон (USB)",
                 start_error: Exception | None = None) -> None:
        self.frames = list(frames)
        self.samplerate = samplerate
        self.device_name = device_name
        self.start_error = start_error
        self.pos = 0
        self.started = 0
        self.closed = 0

    def start(self) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    def read(self, timeout: float | None = 0.1):
        if self.pos < len(self.frames):
            frame = self.frames[self.pos]
            self.pos += 1
            return frame
        time.sleep(0.005)
        return None

    def stop(self) -> None:
        return None

    def close(self) -> None:
        self.closed += 1


def speech_block(seconds: float = 0.3, samplerate: int = 16000, level: int = 6000) -> list[np.ndarray]:
    frame = 512 if samplerate == 16000 else 1536
    total = int(seconds * samplerate)
    return [np.full(frame, level, dtype=np.int16) for _ in range(max(1, total // frame))]


def test_tail_pad_appends_exactly_500_ms(config, events):
    rig = Rig(config, events, block=False)
    padded = rig.pipeline._with_tail_pad(np.zeros(1600, dtype=np.int16))
    assert padded.size == 1600 + int(0.5 * 16000)
    assert int(np.abs(padded[1600:]).max()) == 0


def test_tail_pad_is_configurable(config, events):
    rig = Rig(config, events, block=False)
    config.set("vad.tail_pad_ms", 200)
    rig.pipeline.apply_config()
    padded = rig.pipeline._with_tail_pad(np.ones(100, dtype=np.int16))
    assert padded.size == 100 + int(0.2 * 16000)


async def test_stt_receives_the_padded_segment(config, events):
    quiet_config(config)
    rig = Rig(config, events, block=False)
    await rig.pipeline.start()
    try:
        await rig.pipeline.listen_now()
        await say_command(rig, wake=False)
        assert await until(lambda: rig.stt.calls >= 1)
        received = rig.stt.received[0]
        assert received.size > int(0.5 * 16000)  # segment + 500 ms tail
        assert int(np.abs(received[-int(0.5 * 16000):]).max()) == 0
    finally:
        await rig.pipeline.stop()


async def test_debug_save_audio_writes_both_wavs(config, events, data_dir):
    quiet_config(config)
    config.set("debug.save_audio", True)
    rig = Rig(config, events, block=False)
    await rig.pipeline.start()
    try:
        await rig.pipeline.listen_now()
        await say_command(rig, wake=False)
        assert await until(lambda: rig.stt.calls == 1)
        logs = data_dir / "logs"
        segment_wav = logs / "debug_mic.wav"
        raw_wav = logs / "debug_mic_raw.wav"
        assert await until(lambda: segment_wav.is_file() and raw_wav.is_file())

        samples, rate = voice_audio.read_wav_bytes(segment_wav.read_bytes())
        assert rate == 16000
        assert samples.size == rig.stt.received[0].size  # exactly what went to STT
        raw_samples, raw_rate = voice_audio.read_wav_bytes(raw_wav.read_bytes())
        assert raw_rate == 16000
        assert raw_samples.size > 0
        info = rig.pipeline.diagnostics()
        assert info["debug"]["save_audio"] is True
        assert info["debug"]["last_wav"].endswith("debug_mic.wav")
        assert info["debug"]["raw_wav"].endswith("debug_mic_raw.wav")
    finally:
        await rig.pipeline.stop()


async def test_debug_off_writes_nothing(config, events, data_dir):
    quiet_config(config)
    rig = Rig(config, events, block=False)
    await rig.pipeline.start()
    try:
        await rig.pipeline.listen_now()
        await say_command(rig, wake=False)
        assert await until(lambda: rig.stt.calls == 1)
        await asyncio.sleep(0.05)
        assert not (data_dir / "logs" / "debug_mic.wav").exists()
    finally:
        await rig.pipeline.stop()


async def test_record_and_recognize_returns_text_and_vad(config, events, data_dir):
    rig = Rig(config, events, block=False,
              stt_details={"confidence": 0.83, "peak_dbfs": -24.5, "normalized": True})
    probe = ProbeMic(speech_block(0.3))
    rig.pipeline.use_components(input_probe=lambda device: probe)

    result = await rig.pipeline.record_and_recognize(seconds=0.3)

    assert result["ok"] is True
    assert result["text"] == "открой блокнот"
    assert result["confidence"] == pytest.approx(0.83)
    assert result["normalized"] is True
    assert result["peak_dbfs"] == pytest.approx(-24.5)
    assert result["mic"]["samplerate"] == 16000
    assert result["vad"]["speech_ms"] >= 0
    assert result["vad"]["end_s"] > 0
    assert result["wav_path"].endswith("debug_mic.wav")
    assert (data_dir / "logs" / "debug_mic.wav").is_file()
    assert probe.closed == 1
    samples, rate = voice_audio.read_wav_bytes((data_dir / "logs" / "debug_mic.wav").read_bytes())
    assert rate == 16000
    assert samples.size == rig.stt.received[0].size


async def test_record_and_recognize_resamples_48k_to_16k(config, events, data_dir):
    rig = Rig(config, events, block=False)
    probe = ProbeMic([np.full(1536, 6000, dtype=np.int16) for _ in range(9)], samplerate=48000)
    rig.pipeline.use_components(input_probe=lambda device: probe)

    result = await rig.pipeline.record_and_recognize(seconds=0.3)

    assert result["ok"] is True
    assert result["mic"]["samplerate"] == 48000
    # debug_mic_raw.wav is the recording itself, saved after the resample:
    # 9 blocks * 1536 samples at 48 kHz must become 9 * 512 at 16 kHz.
    raw, rate = voice_audio.read_wav_bytes((data_dir / "logs" / "debug_mic_raw.wav").read_bytes())
    assert rate == 16000
    assert raw.size == pytest.approx(9 * 1536 / 3, abs=8)
    # and STT got 16 kHz too (segment + VAD silence + 500 ms tail pad)
    assert rig.stt.received[0].size >= raw.size


async def test_record_and_recognize_without_microphone(config, events):
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(input_probe=lambda device: ProbeMic([], start_error=AudioUnavailable("no device")))
    result = await rig.pipeline.record_and_recognize(seconds=0.2)
    assert result["ok"] is False
    assert "микрофон недоступен" in result["message"]
    assert result["wav_path"] == ""


async def test_record_and_recognize_without_data(config, events):
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(input_probe=lambda device: ProbeMic([]))
    result = await rig.pipeline.record_and_recognize(seconds=0.2)
    assert result["ok"] is False
    assert "не прислал данных" in result["message"]


async def test_record_and_recognize_reports_stt_failure(config, events):
    rig = Rig(config, events, block=False)
    rig.pipeline.use_components(stt=FakeStt(error=RuntimeError("cublas missing")))
    rig.pipeline.use_components(input_probe=lambda device: ProbeMic(speech_block(0.2)))
    result = await rig.pipeline.record_and_recognize(seconds=0.2)
    assert result["ok"] is False
    assert "STT упал" in result["message"] and "cublas missing" in result["message"]


async def test_diagnostics_reports_new_fields(config, events):
    rig = Rig(config, events, block=False)
    info = rig.pipeline.diagnostics()
    assert info["debug"]["save_audio"] is False
    assert info["debug"]["tail_pad_ms"] == pytest.approx(500.0)
    assert "requested" in info["mic"] and "resolved" in info["mic"]
    assert "samplerate" in info["mic"] and "native_samplerate" in info["mic"]
    assert info["wake"]["last_heard"] == ""
    assert "threshold" in info["wake"]
    assert info["stt"]["last_confidence"] is None
    assert info["stt"]["last_processing_s"] == 0.0
    assert "model_path" in info["stt"] and "model_size_mb" in info["stt"]


def test_silent_capture_stream_is_reopened(config, events, monkeypatch):
    rig = Rig(config, events, wake_mode="vosk")
    monkeypatch.setattr(pipeline_module, "MIC_SILENCE_RESTART_SEC", 0.0)
    pipeline = rig.pipeline
    pipeline._mic_reopens = 0
    pipeline._silent_since = 0.0
    pipeline._mic_dirty = False

    pipeline._watch_mic_silence(np.full(512, 3000, dtype=np.int16))
    assert pipeline._mic_dirty is False

    pipeline._watch_mic_silence(np.zeros(512, dtype=np.int16))
    pipeline._watch_mic_silence(np.zeros(512, dtype=np.int16))
    assert pipeline._mic_dirty is True
    assert pipeline._mic_reopens == 1


def test_loud_audio_resets_the_silence_watchdog(config, events):
    rig = Rig(config, events, wake_mode="vosk")
    pipeline = rig.pipeline
    pipeline._silent_since = 0.0
    pipeline._mic_reopens = 2

    pipeline._watch_mic_silence(np.full(512, 1200, dtype=np.int16))

    assert pipeline._mic_reopens == 0
    assert pipeline._silent_since == 0.0


# ------------------------------------------------- frame-starvation watchdog
def test_starved_stream_is_reopened(config, events, monkeypatch):
    rig = Rig(config, events, wake_mode="vosk")
    monkeypatch.setattr(pipeline_module, "MIC_STARVE_SEC", 10.0)
    monkeypatch.setattr(pipeline_module, "MIC_STARVE_REOPEN_COOLDOWN_SEC", 60.0)
    pipeline = rig.pipeline
    pipeline._mic_dirty = False
    pipeline._mic_starve_reopens = 0
    pipeline._last_starve_reopen_at = 0.0
    pipeline._last_frame_at = time.monotonic() - 30.0

    pipeline._check_mic_starvation()

    assert pipeline._mic_dirty is True
    assert pipeline._mic_starve_reopens == 1


def test_starvation_watchdog_respects_the_cooldown(config, events, monkeypatch):
    rig = Rig(config, events, wake_mode="vosk")
    monkeypatch.setattr(pipeline_module, "MIC_STARVE_SEC", 10.0)
    pipeline = rig.pipeline
    pipeline._mic_dirty = False
    pipeline._mic_starve_reopens = 0
    pipeline._last_starve_reopen_at = time.monotonic()
    pipeline._last_frame_at = time.monotonic() - 30.0

    pipeline._check_mic_starvation()

    assert pipeline._mic_dirty is False
    assert pipeline._mic_starve_reopens == 0


def test_fresh_frames_do_not_trigger_the_watchdog(config, events):
    rig = Rig(config, events, wake_mode="vosk")
    pipeline = rig.pipeline
    pipeline._mic_dirty = False
    pipeline._last_frame_at = time.monotonic()
    pipeline._last_starve_reopen_at = 0.0

    pipeline._check_mic_starvation()

    assert pipeline._mic_dirty is False

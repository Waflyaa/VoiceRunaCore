"""The diagnostic recording must reuse the assistant's live microphone.

Opening a second InputStream on the same device makes many Windows drivers
deliver digital silence to BOTH consumers. That is exactly what happened on the
owner's machine: the "тест распознавания" wrote a -90 dBFS WAV while the
settings level test showed a live signal.
"""

from __future__ import annotations

import numpy as np
import pytest

from voiceruna.voice.pipeline import VoicePipeline

from test_voice_pipeline import Rig


def _run(rig: Rig) -> None:
    """Pretend the voice loop is running with a live microphone."""
    pipeline = rig.pipeline
    pipeline._running = True
    pipeline._mic_ok = True
    pipeline._paused = False


@pytest.mark.asyncio
async def test_record_test_uses_the_live_ring_not_a_second_stream(config, events):
    rig = Rig(config, events)
    rig.pipeline.use_components(mic=rig.mic)
    rig.mic.start()
    _run(rig)

    speech = np.full(16000, 4000, dtype=np.int16)
    for offset in range(0, speech.size, 512):
        rig.pipeline._ring.push(speech[offset:offset + 512])

    result = await rig.pipeline.record_and_recognize(0.3)

    assert result["ok"] is True
    assert result["mic"]["silent"] is False
    assert result["text"] == "открой блокнот"
    assert result["wav_path"].endswith("debug_mic.wav")


@pytest.mark.asyncio
async def test_record_test_reports_digital_silence(config, events):
    rig = Rig(config, events)
    rig.pipeline.use_components(mic=rig.mic)
    rig.mic.start()
    _run(rig)
    for _ in range(32):
        rig.pipeline._ring.push(np.zeros(512, dtype=np.int16))

    result = await rig.pipeline.record_and_recognize(0.3)

    assert result["mic"]["silent"] is True
    assert "тишину" in result["message"]
    assert result["text"] == ""


@pytest.mark.asyncio
async def test_input_test_does_not_open_a_probe_when_live(config, events):
    probes = []

    def probe_factory(device):  # pragma: no cover - must not be used
        probes.append(device)
        raise AssertionError("второй InputStream открывать нельзя")

    rig = Rig(config, events)
    rig.pipeline.use_components(mic=rig.mic, input_probe=probe_factory)
    rig.mic.start()
    _run(rig)
    for _ in range(40):
        rig.pipeline._ring.push(np.full(512, 5000, dtype=np.int16))

    ok, message, level = await rig.pipeline.test_input(seconds=0.3)

    assert ok is True
    assert level > 0.1
    assert "уровень" in message
    assert probes == []


@pytest.mark.asyncio
async def test_input_test_reports_silence_instead_of_zero_level(config, events):
    rig = Rig(config, events)
    rig.pipeline.use_components(mic=rig.mic)
    rig.mic.start()
    _run(rig)
    for _ in range(40):
        rig.pipeline._ring.push(np.zeros(512, dtype=np.int16))

    ok, message, level = await rig.pipeline.test_input(seconds=0.3)

    assert ok is False
    assert level == 0.0
    assert "тишину" in message


def test_digital_silence_detector():
    digital = np.zeros(3200, dtype=np.int16)
    quiet_but_alive = (np.random.RandomState(0).randn(3200) * 40).astype(np.int16)
    loud = np.full(3200, 6000, dtype=np.int16)

    assert VoicePipeline._is_digital_silence(digital) is True
    assert VoicePipeline._is_digital_silence(np.array([], dtype=np.int16)) is True
    assert VoicePipeline._is_digital_silence(loud) is False
    assert VoicePipeline._is_digital_silence(quiet_but_alive) is False


def test_energy_gate_detects_speech_over_quiet_room():
    from voiceruna.voice.pipeline import VoicePipeline, ENERGY_GATE_MIN_LEVEL

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._noise_floor = 0.0

    # room noise ~ -70 dBFS establishes the floor
    for _ in range(30):
        assert pipeline._energy_speech(0.0003) is False
    floor = pipeline._noise_floor
    assert floor < 0.001

    # speech at ~ -32 dBFS must pass
    assert pipeline._energy_speech(0.025) is True
    # and a strictly silent frame must not
    assert pipeline._energy_speech(0.0) is False


def test_energy_gate_ignores_a_loud_constant_noise_after_adapting():
    from voiceruna.voice.pipeline import VoicePipeline

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._noise_floor = 0.5  # a loud room, already learnt
    # a steady 0.6 is not 3.5x the floor -> no false phrase
    assert pipeline._energy_speech(0.6) is False


def test_energy_gate_never_breaks_on_garbage():
    from voiceruna.voice.pipeline import VoicePipeline

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._noise_floor = 0.0
    assert pipeline._energy_speech(-1.0) is False
    assert pipeline._energy_speech(float("nan")) is False

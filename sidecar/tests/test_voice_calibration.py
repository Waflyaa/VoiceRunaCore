"""Adaptive microphone calibration: thresholds follow the measured noise floor."""

from __future__ import annotations

import numpy as np
import pytest

from voiceruna.voice.pipeline import VoicePipeline, ENERGY_GATE_MARGIN_DB, TOO_QUIET_MARGIN_DB


def test_noise_floor_uses_the_quiet_twenty_percent():
    frames = [np.zeros(1600, dtype=np.int16) for _ in range(8)]
    frames.append(np.full(1600, 6000, dtype=np.int16))

    stats = VoicePipeline._noise_floor_from_frames(frames)

    assert stats is not None
    noise, peak = stats
    assert noise < -100  # digital silence dominates the quiet part
    assert peak > -35


def test_thresholds_are_relative_to_the_measured_noise():
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._apply_noise_floor(-60.0)

    assert pipeline._noise_floor_db == -60.0
    assert pipeline._too_quiet_dbfs == pytest.approx(-60.0 + TOO_QUIET_MARGIN_DB)
    expected_gate = 10 ** ((-60.0 + ENERGY_GATE_MARGIN_DB) / 20.0)
    assert pipeline._energy_min == pytest.approx(expected_gate)


def test_loud_microphone_raises_the_thresholds():
    quiet = VoicePipeline.__new__(VoicePipeline)
    quiet._apply_noise_floor(-70.0)
    loud = VoicePipeline.__new__(VoicePipeline)
    loud._apply_noise_floor(-45.0)

    assert loud._too_quiet_dbfs > quiet._too_quiet_dbfs
    assert loud._energy_min > quiet._energy_min


def test_gate_is_clamped_to_a_sane_range():
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._apply_noise_floor(10.0)
    assert pipeline._energy_min <= 0.05

    pipeline._apply_noise_floor(-120.0)
    assert pipeline._energy_min >= 0.00005


@pytest.mark.asyncio
async def test_calibration_stores_per_device_thresholds(config, events):
    from test_voice_pipeline import Rig

    rig = Rig(config, events, wake_mode="vad_stt")
    rig.pipeline._running = True
    rig.pipeline._mic_ok = True
    rig.pipeline._paused = False
    rig.mic.start()
    for _ in range(40):
        rig.pipeline._ring.push(np.full(512, 30, dtype=np.int16))

    result = await rig.pipeline.calibrate(1.0)

    assert result["ok"] is True
    assert result["noise_floor_dbfs"] < -40
    assert "threshold_dbfs" in result
    stored = config.get("audio.calibration") or {}
    assert any(entry.get("noise_dbfs") == result["noise_floor_dbfs"] for entry in stored.values())


@pytest.mark.asyncio
async def test_silent_stream_calibration_is_rejected(config, events):
    """A dead camera stream must not overwrite the thresholds with silence."""
    from test_voice_pipeline import Rig

    rig = Rig(config, events, wake_mode="vad_stt")
    rig.pipeline._running = True
    rig.pipeline._mic_ok = True
    rig.pipeline._paused = False
    rig.mic.start()
    for _ in range(40):
        rig.pipeline._ring.push(np.zeros(512, dtype=np.int16))

    result = await rig.pipeline.calibrate(1.0)

    assert result["ok"] is False
    assert result.get("dead") is True
    assert "молчит" in result["message"]
    assert not (config.get("audio.calibration", {}) or {})


@pytest.mark.asyncio
async def test_existing_calibration_is_kept_when_the_new_one_is_dead(config, events):
    from test_voice_pipeline import Rig

    config.set("audio.calibration", {"index:1": {"noise_dbfs": -55.0, "peak_dbfs": -40.0}})
    rig = Rig(config, events, wake_mode="vad_stt")
    rig.pipeline._running = True
    rig.pipeline._mic_ok = True
    rig.pipeline._paused = False
    rig.mic.start()
    for _ in range(40):
        rig.pipeline._ring.push(np.zeros(512, dtype=np.int16))

    await rig.pipeline.calibrate(1.0)

    stored = config.get("audio.calibration") or {}
    assert stored.get("index:1", {}).get("noise_dbfs") == -55.0

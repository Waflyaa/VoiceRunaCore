"""The voice package must stay importable without the optional heavy wheels."""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest

HEAVY = (
    "sounddevice",
    "soundfile",
    "edge_tts",
    "faster_whisper",
    "onnxruntime",
    "vosk",
    "miniaudio",
    "pvporcupine",
    "openwakeword",
)


@pytest.fixture()
def blocked_imports(monkeypatch):
    for name in list(sys.modules):
        root = name.split(".")[0]
        if root in HEAVY or root == "voiceruna":
            monkeypatch.delitem(sys.modules, name, raising=False)
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        root = str(name).split(".")[0]
        if root in HEAVY:
            raise ImportError(f"{root} is blocked in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    return guard


def test_pipeline_imports_without_heavy_modules(blocked_imports):
    module = importlib.import_module("voiceruna.voice.pipeline")
    assert hasattr(module, "VoicePipeline")
    assert "VoicePipeline" in module.__all__


def test_voice_package_reexports_the_pipeline(blocked_imports):
    voice = importlib.import_module("voiceruna.voice")
    assert "VoicePipeline" in voice.__all__
    assert voice.VoicePipeline is importlib.import_module("voiceruna.voice.pipeline").VoicePipeline


def test_engines_degrade_without_models_or_wheels(blocked_imports, config):
    vad_module = importlib.import_module("voiceruna.voice.vad")
    wake_module = importlib.import_module("voiceruna.voice.wake")
    assert vad_module.create_vad(config) is not None
    detector = wake_module.create_wake_detector(config)
    assert detector.engine in {"vosk", "none"}


def test_pipeline_constructor_matches_the_contract(blocked_imports, config, events):
    from voiceruna.state import StateManager
    from voiceruna.voice.pipeline import VoicePipeline

    async def on_utterance(text: str) -> None:
        return None

    pipeline = VoicePipeline(
        config=config,
        state=StateManager(),
        emit=events,
        on_utterance=on_utterance,
    )
    for name in (
        "start",
        "stop",
        "pause",
        "resume",
        "listen_now",
        "speak",
        "stop_speaking",
        "preview",
        "list_voices",
        "apply_config",
        "list_devices",
        "test_output",
        "test_input",
        "test_stt",
        "diagnostics",
        "playback_status",
    ):
        assert callable(getattr(pipeline, name)), name
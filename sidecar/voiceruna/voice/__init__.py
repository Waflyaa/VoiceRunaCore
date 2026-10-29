"""Voice package (TZ 5): audio capture, VAD, wake word, STT and TTS.

The contract with the rest of the sidecar is VoicePipeline (see
docs/CONTRACTS.md); every engine below it degrades instead of raising when a
model, a wheel or the hardware is missing.
"""

from __future__ import annotations

from .audio import (
    AudioOutput,
    AudioUnavailable,
    FrameQueue,
    MicSource,
    SoundDeviceMic,
    create_mic,
    rms,
    to_int16_mono,
)
from .download import ModelDownloader
from .pipeline import VoicePipeline
from .stt import WhisperTranscriber, strip_wake_prefix
from .tts import Mp3Player, Speaker, split_sentences, truncate_text
from .vad import EnergyVad, SegmentEvent, SileroVad, UtteranceSegmenter, create_vad
from .wake import WakeDetector, create_wake_detector, matches_wake_word, strip_wake_word

__all__ = [
    "AudioOutput",
    "AudioUnavailable",
    "EnergyVad",
    "FrameQueue",
    "MicSource",
    "ModelDownloader",
    "Mp3Player",
    "SegmentEvent",
    "SileroVad",
    "SoundDeviceMic",
    "Speaker",
    "UtteranceSegmenter",
    "VoicePipeline",
    "WakeDetector",
    "WhisperTranscriber",
    "create_mic",
    "create_vad",
    "create_wake_detector",
    "matches_wake_word",
    "rms",
    "split_sentences",
    "strip_wake_prefix",
    "strip_wake_word",
    "to_int16_mono",
    "truncate_text",
]

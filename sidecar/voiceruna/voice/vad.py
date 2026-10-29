"""Voice activity detection and utterance segmentation (TZ 5.2).

Two independent layers:

* UtteranceSegmenter - deterministic frame bookkeeping: pre-buffer, speech
  start, end after vad.silence_ms of silence, forced end at
  vad.max_utterance_sec, minimum speech 200 ms. Pure Python + numpy, no model,
  fully unit tested.
* Speech probability engines - SileroVad on onnxruntime (lazy import) and an
  adaptive-energy EnergyVad used when the model or the runtime is unavailable.
  create_vad never raises: a broken Silero install silently degrades to energy.

Frame contract: 512 samples at 16 kHz (32 ms), int16, mono.
"""

from __future__ import annotations

import importlib.util
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .. import paths
from .audio import SAMPLE_RATE, rms

SILERO_FRAME = 512
DEFAULT_FRAME_MS = 32.0
DEFAULT_THRESHOLD = 0.5

#: Segmenter event kinds returned by UtteranceSegmenter.push().
IDLE = "idle"
START = "start"
CONTINUE = "continue"
END = "end"
DISCARD = "discard"

log = logging.getLogger(__name__)


@dataclass
class SegmentEvent:
    """Result of feeding one frame to the segmenter."""

    kind: str = IDLE
    audio: np.ndarray | None = None
    duration_ms: float = 0.0
    speech_ms: float = 0.0


# --------------------------------------------------------------------------- segmenter
class UtteranceSegmenter:
    """Turns a frame stream into utterances (start / continue / end / discard).

    The segmenter keeps a ring of the last pre_buffer_ms of audio so that the
    first fraction of a word is not cut off when the wake word fires (TZ 5.2).
    It does not know about models or hardware, only about "is this frame
    speech?", which keeps all timing rules trivially testable.
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        silence_ms: float = 800.0,
        max_utterance_sec: float = 12.0,
        min_speech_ms: float = 200.0,
        pre_buffer_ms: float = 700.0,
        nominal_frame_ms: float = DEFAULT_FRAME_MS,
    ) -> None:
        self._sample_rate = int(sample_rate) or SAMPLE_RATE
        self._silence_ms = float(silence_ms)
        self._max_utterance_ms = max(0.0, float(max_utterance_sec)) * 1000.0
        self._min_speech_ms = float(min_speech_ms)
        self._pre_buffer_ms = float(pre_buffer_ms)
        self._nominal_frame_ms = float(nominal_frame_ms) or DEFAULT_FRAME_MS

        self._armed = False
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self._duration_ms = 0.0
        self._speech_ms = 0.0
        self._silence_run_ms = 0.0
        self._pre: deque[np.ndarray] = deque(maxlen=self._pre_maxlen())

    # -- configuration -----------------------------------------------------
    def _pre_maxlen(self) -> int:
        return max(0, int(round(self._pre_buffer_ms / self._nominal_frame_ms)))

    def configure(
        self,
        *,
        silence_ms: float | None = None,
        max_utterance_sec: float | None = None,
        min_speech_ms: float | None = None,
        pre_buffer_ms: float | None = None,
    ) -> None:
        """Apply config changes without losing the in-memory pre-buffer."""
        if silence_ms is not None:
            self._silence_ms = float(silence_ms)
        if max_utterance_sec is not None:
            self._max_utterance_ms = max(0.0, float(max_utterance_sec)) * 1000.0
        if min_speech_ms is not None:
            self._min_speech_ms = float(min_speech_ms)
        if pre_buffer_ms is not None and float(pre_buffer_ms) != self._pre_buffer_ms:
            self._pre_buffer_ms = float(pre_buffer_ms)
            self._pre = deque(self._pre, maxlen=self._pre_maxlen())

    # -- state -------------------------------------------------------------
    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def pre_buffer_frames(self) -> int:
        return self._pre_maxlen()

    def arm(self) -> None:
        """Start looking for the beginning of an utterance."""
        self._armed = True

    def disarm(self) -> None:
        self._armed = False
        self._reset_recording()

    def reset(self) -> None:
        """Full reset, including the pre-buffer."""
        self._armed = False
        self._reset_recording()
        self._pre.clear()

    def _reset_recording(self) -> None:
        self._recording = False
        self._chunks = []
        self._duration_ms = 0.0
        self._speech_ms = 0.0
        self._silence_run_ms = 0.0

    # -- frame intake ------------------------------------------------------
    def push(self, frame: Any, level: float = 0.0, is_speech: bool = False) -> SegmentEvent:
        """Feed one frame; returns what the pipeline should do next."""
        samples = np.asarray(frame)
        if samples.size == 0:
            return SegmentEvent(self._finish_kind_or_idle())
        duration_ms = samples.size * 1000.0 / self._sample_rate
        speech = bool(is_speech)

        if not self._recording:
            self._pre.append(samples)
            if not self._armed or not speech:
                return SegmentEvent(IDLE, None, 0.0, 0.0)
            # First speech frame: prepend everything we kept before it. With a
            # zero-length pre-buffer the ring dropped this frame, so add it here.
            self._recording = True
            chunks = list(self._pre)
            if not chunks or chunks[-1] is not samples:
                chunks.append(samples)
            self._chunks = chunks
            self._pre.clear()
            self._duration_ms = sum(chunk.size for chunk in self._chunks) * 1000.0 / self._sample_rate
            self._speech_ms = duration_ms
            self._silence_run_ms = 0.0
            return SegmentEvent(START, None, self._duration_ms, self._speech_ms)

        self._chunks.append(samples)
        self._duration_ms += duration_ms
        if speech:
            self._speech_ms += duration_ms
            self._silence_run_ms = 0.0
        else:
            self._silence_run_ms += duration_ms

        silence_limit = self._silence_ms > 0 and self._silence_run_ms >= self._silence_ms
        length_limit = self._max_utterance_ms > 0 and self._duration_ms >= self._max_utterance_ms
        if not silence_limit and not length_limit:
            return SegmentEvent(CONTINUE, None, self._duration_ms, self._speech_ms)

        audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.int16)
        duration, speech_ms = self._duration_ms, self._speech_ms
        self._reset_recording()
        if speech_ms + 1e-9 < self._min_speech_ms or audio.size == 0:
            return SegmentEvent(DISCARD, audio, duration, speech_ms)
        return SegmentEvent(END, audio, duration, speech_ms)

    def _finish_kind_or_idle(self) -> str:
        if self._recording:
            self._reset_recording()
            return DISCARD
        return IDLE


# --------------------------------------------------------------------------- engines
@runtime_checkable
class VadEngine(Protocol):
    """Per-frame speech probability."""

    name: str

    def is_speech(self, frame: Any) -> tuple[bool, float]:
        """Return (is_speech, confidence)."""

    def reset(self) -> None:
        """Forget the recurrent state."""

    def close(self) -> None:
        """Release the model."""


class EnergyVad:
    """Adaptive RMS threshold VAD, the fallback when Silero is unusable.

    The noise floor follows the signal with a slow exponential average, so a
    fridge hum or a fan does not keep the microphone "speech" forever while a
    quiet room still triggers on a voice.
    """

    name = "energy"

    def __init__(
        self,
        *,
        threshold: float = 0.02,
        ratio: float = 2.5,
        adaptation: float = 0.05,
        floor: float = 1e-4,
    ) -> None:
        self._threshold = max(1e-5, float(threshold))
        self._ratio = max(1.0, float(ratio))
        self._adaptation = min(1.0, max(0.0, float(adaptation)))
        self._floor = max(1e-6, float(floor))
        self._noise = 0.002

    def is_speech(self, frame: Any) -> tuple[bool, float]:
        level = rms(frame)
        limit = max(self._threshold, self._noise * self._ratio)
        speech = level > limit
        if speech:
            return True, min(1.0, level / limit) if limit > 0 else 1.0
        self._noise = (1.0 - self._adaptation) * self._noise + self._adaptation * max(level, self._floor)
        return False, 0.0

    def reset(self) -> None:
        self._noise = 0.002

    def close(self) -> None:
        return None


class SileroVad:
    """Silero VAD v4/v5 through onnxruntime (lazy import, single-threaded)."""

    name = "silero"

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._model_path = Path(model_path) if model_path else default_vad_model_path()
        self._threshold = float(threshold)
        self._sample_rate = int(sample_rate)
        self._session: Any = None
        self._inputs: dict[str, Any] = {}
        self._state: dict[str, np.ndarray] = {}

    @staticmethod
    def is_available(model_path: str | Path | None = None) -> bool:
        path = Path(model_path) if model_path else default_vad_model_path()
        if not path.is_file():
            return False
        return importlib.util.find_spec("onnxruntime") is not None

    @property
    def model_path(self) -> Path:
        return self._model_path

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(self._model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self._inputs = {item.name: item for item in self._session.get_inputs()}
        self._state = {}
        for name, item in self._inputs.items():
            if name in ("input", "sr"):
                continue
            self._state[name] = np.zeros(_static_shape(item.shape), dtype=np.float32)

    def is_speech(self, frame: Any) -> tuple[bool, float]:
        self._ensure_session()
        samples = np.asarray(frame, dtype=np.float32) / 32768.0
        if samples.size != SILERO_FRAME:
            if samples.size > SILERO_FRAME:
                samples = samples[-SILERO_FRAME:]
            else:
                samples = np.pad(samples, (0, SILERO_FRAME - samples.size))
        feeds: dict[str, Any] = {"input": samples.reshape(1, -1)}
        if "sr" in self._inputs:
            feeds["sr"] = np.array([self._sample_rate], dtype=np.int64)
        feeds.update(self._state)
        outputs = self._session.run(None, feeds)
        prob = float(np.asarray(outputs[0]).reshape(-1)[0])
        state_names = [name for name in self._inputs if name not in ("input", "sr")]
        for index, name in enumerate(state_names, start=1):
            if index < len(outputs):
                self._state[name] = outputs[index]
        return prob >= self._threshold, prob

    def reset(self) -> None:
        if self._session is None:
            return
        for name, value in self._state.items():
            self._state[name] = np.zeros_like(value)

    def close(self) -> None:
        self._session = None
        self._state = {}


def _static_shape(shape: Any) -> tuple[int, ...]:
    """Replace symbolic onnx dimensions (batch, None, ...) with 1."""
    result: list[int] = []
    for dim in shape or ():
        try:
            result.append(int(dim))
        except (TypeError, ValueError):
            result.append(1)
    return tuple(result) or (1,)


def default_vad_model_path() -> Path:
    """Location of silero_vad.onnx inside %APPDATA%/VoiceRunaCore/models."""
    return paths.models_dir() / "silero-vad" / "silero_vad.onnx"


def create_vad(config: Any, *, model_path: str | Path | None = None) -> VadEngine:
    """Silero when the model and onnxruntime are usable, energy otherwise."""
    threshold = float(config.get("vad.threshold", DEFAULT_THRESHOLD))
    path = Path(model_path) if model_path else default_vad_model_path()
    if SileroVad.is_available(path):
        try:
            engine = SileroVad(path, threshold=threshold)
            engine.is_speech(np.zeros(SILERO_FRAME, dtype=np.int16))  # warm up once
            return engine
        except Exception as exc:  # pragma: no cover - depends on the machine
            log.warning("silero vad unusable (%s), falling back to energy vad", exc)
    return EnergyVad(threshold=float(config.get("vad.energy_threshold", 0.02)))


__all__ = [
    "CONTINUE",
    "DEFAULT_FRAME_MS",
    "DEFAULT_THRESHOLD",
    "DISCARD",
    "END",
    "EnergyVad",
    "IDLE",
    "SILERO_FRAME",
    "START",
    "SegmentEvent",
    "SileroVad",
    "UtteranceSegmenter",
    "VadEngine",
    "create_vad",
    "default_vad_model_path",
]

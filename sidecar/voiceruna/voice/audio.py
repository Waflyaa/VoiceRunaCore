"""Microphone capture and speaker output (TZ 5.2, 5.4).

All hardware access lives here and every heavy import happens lazily inside a
function, so this module imports cleanly on a machine without a sound card,
without PortAudio and without optional wheels.

Capture path
    sounddevice.InputStream (16 kHz, mono, 512-sample blocks) is opened from a
    dedicated PortAudio thread; its callback only copies the frame into a
    bounded FrameQueue. When the consumer is slower than the device the oldest
    frames are dropped, so latency never grows and the buffer never becomes a
    recording of the past.

Tests never touch hardware: they implement MicSource with synthetic int16
frames (see tests/test_voice_pipeline.py).
"""

from __future__ import annotations

import io
import logging
import queue
import sys
from collections import deque
import threading
import time
import wave
from typing import Any, Protocol, runtime_checkable

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SIZE = 512  # 32 ms at 16 kHz
DTYPE = "int16"
FRAME_MS = BLOCK_SIZE * 1000 // SAMPLE_RATE

# Roughly 1.5 s of audio; beyond that the consumer is clearly stuck.
DEFAULT_QUEUE_FRAMES = 48

#: A freshly opened stream may be a "phantom": the handle is valid but every
#: sample is zero (USB camera microphones do this after a fast restart, and the
#: device may also need a moment to wake from USB selective suspend). Each
#: candidate format is probed for this long, twice, before the next one is tried.
CAPTURE_PROBE_SEC = 1.0
CAPTURE_PROBE_ATTEMPTS = 1
CAPTURE_NO_DATA_SEC = 0.3

log = logging.getLogger(__name__)


class AudioUnavailable(RuntimeError):
    """Raised when no usable audio device or driver is available."""


# --------------------------------------------------------------------------- utils
def to_int16_mono(samples: Any) -> np.ndarray:
    """Return a contiguous 1-D int16 array for arbitrary frame input.

    Accepts int16 (fast path), float buffers in -1..1, or a 2-D (frames,
    channels) block as delivered by sounddevice.
    """
    array = np.asarray(samples)
    if array.ndim > 1:
        array = array[:, 0] if array.shape[1] else array.reshape(-1)
    if array.dtype != np.int16:
        if np.issubdtype(array.dtype, np.floating):
            array = np.clip(array, -1.0, 1.0) * 32767.0
        array = array.astype(np.int16, copy=False)
    return np.ascontiguousarray(array).ravel()


def rms(frame: Any) -> float:
    """Normalized (0..1) root-mean-square level of an int16 frame."""
    array = np.asarray(frame, dtype=np.float32)
    if array.size == 0:
        return 0.0
    value = float(np.sqrt(np.mean(np.square(array / 32768.0))))
    return min(1.0, max(0.0, value))


# --------------------------------------------------------------------------- mic
@runtime_checkable
class MicSource(Protocol):
    """Minimal capture interface the pipeline depends on."""

    def start(self) -> None:
        """Open the device. Raises AudioUnavailable when impossible."""

    def stop(self) -> None:
        """Close the device but keep the object reusable."""

    def read(self, timeout: float | None = None) -> np.ndarray | None:
        """Return the next int16 frame or None when the timeout expires."""

    @property
    def active(self) -> bool:
        """True while the device is open."""

    def close(self) -> None:
        """Release everything; the object is not reusable afterwards."""


class FrameQueue:
    """Bounded frame queue that drops the oldest frame when full.

    Shared by the real device (callback producer) and by test microphones
    (explicit producer), which keeps the pipeline tests deterministic.
    """

    def __init__(self, maxsize: int = DEFAULT_QUEUE_FRAMES) -> None:
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=max(1, int(maxsize)))
        self._closed = threading.Event()

    def push(self, frame: Any) -> None:
        if self._closed.is_set():
            return
        item = to_int16_mono(frame)
        try:
            self._queue.put_nowait(item)
            return
        except queue.Full:
            pass
        try:  # drop the oldest frame, keep the freshest audio
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(item)
        except queue.Full:  # pragma: no cover - race with another producer
            pass

    def read(self, timeout: float | None = 0.1) -> np.ndarray | None:
        if self._closed.is_set():
            return None
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._closed.set()
        self.clear()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def __len__(self) -> int:
        return self._queue.qsize()


class SoundDeviceMic(FrameQueue):
    """Capture from a sounddevice input stream, always at 16 kHz out.

    Not every microphone can be opened at 16 kHz. start() asks PortAudio first
    (check_input_settings); when the device refuses, the stream opens at its
    native rate (usually 48 kHz) and every block is resampled to 16 kHz before
    it reaches VAD/STT, so the rest of the pipeline always sees 16 kHz.
    """

    def __init__(
        self,
        device: int | None = None,
        sample_rate: int = SAMPLE_RATE,
        block_size: int = BLOCK_SIZE,
        maxsize: int = DEFAULT_QUEUE_FRAMES,
    ) -> None:
        super().__init__(maxsize)
        self._device = device
        self._sample_rate = int(sample_rate)
        self._block_size = int(block_size)
        self._stream: Any = None
        self._lock = threading.Lock()
        self._device_name = ""
        self._capture_samplerate = 0
        self._native_samplerate = 0
        self._channels = CHANNELS
        self.selection_note = ""

    @property
    def active(self) -> bool:
        return self._stream is not None

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def samplerate(self) -> int:
        """Rate of the frames this source returns (always 16 kHz)."""
        return self._sample_rate

    @property
    def device_index(self) -> int | None:
        """sounddevice index this source uses (None = system default)."""
        return self._device

    @property
    def capture_samplerate(self) -> int:
        """Rate the device actually runs at (16 kHz or its native rate)."""
        return self._capture_samplerate or self._sample_rate

    @property
    def native_samplerate(self) -> int:
        """Default rate reported by the device (0 when unknown)."""
        return self._native_samplerate

    @property
    def resampled(self) -> bool:
        return self.capture_samplerate != self._sample_rate

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            try:
                import sounddevice as sd
            except Exception as exc:  # pragma: no cover - depends on the machine
                raise AudioUnavailable(f"sounddevice is not available: {exc}") from exc
            try:
                info = sd.query_devices(self._device, kind="input")
            except Exception as exc:  # pragma: no cover
                raise AudioUnavailable(f"no input device: {exc}") from exc
            self._device_name = str(info.get("name", "") or "")
            try:
                self._native_samplerate = int(float(info.get("default_samplerate") or 0) or 0)
            except (TypeError, ValueError):  # pragma: no cover
                self._native_samplerate = 0
            try:
                max_channels = max(1, int(info.get("max_input_channels") or 1))
            except (TypeError, ValueError):  # pragma: no cover
                max_channels = 1

            candidates = self._capture_candidates(sd, max_channels)
            choice = self._pick_format(sd, candidates)
            last_error: Exception | None = None
            if choice is not None:
                rate, channels = choice
                try:
                    stream = self._open_stream(sd, rate, channels)
                except Exception as exc:  # pragma: no cover - driver specific
                    last_error = exc
                    stream = None
                if stream is not None:
                    self._stream = stream
                    self._capture_samplerate = int(rate)
                    self._channels = int(channels)
            if self._stream is None:
                if last_error is None:
                    raise AudioUnavailable(
                        "cannot open microphone: every format returns digital silence"
                    )
                raise AudioUnavailable(f"cannot open microphone: {last_error}")
            log.info(
                "microphone opened: %s at %d Hz, %d ch%s",
                self._device_name,
                self._capture_samplerate,
                self._channels,
                "" if self._capture_samplerate == self._sample_rate
                else f" (resampled to {self._sample_rate})",
            )

    def _pick_format(self, sd: Any, candidates: list[tuple[int, int]]) -> tuple[int, int] | None:
        """Probe every format and keep the one with the strongest signal.

        A USB camera microphone behaves very differently per format: 44.1 kHz
        stereo can be 30 dB louder than 16 kHz mono, and some formats return pure
        zeros after the device wakes up. Choosing by measured level is the only
        reliable rule across drivers.
        """
        best: tuple[float, int, int] | None = None
        for rate, channels in candidates:
            for attempt in range(1, CAPTURE_PROBE_ATTEMPTS + 1):
                try:
                    stream = self._open_stream(sd, rate, channels)
                except Exception as exc:  # pragma: no cover - driver specific
                    log.info(
                        "microphone %s cannot open at %d Hz with %d ch: %s",
                        self._device_name or self._device,
                        rate,
                        channels,
                        exc,
                    )
                    break
                self._stream = stream
                self._capture_samplerate = int(rate)
                self._channels = int(channels)
                stats = self._probe_stats()
                level = float(stats or 0.0)
                self._close_stream_locked(stream)
                self._stream = None
                if stats is None:
                    # No data at all: cannot judge, keep the first format as a
                    # safe fallback so a slow driver still gets a stream.
                    if best is None:
                        best = (0.0, rate, channels)
                    break
                if level > (best[0] if best is not None else 0.0):
                    best = (level, rate, channels)
                break
        if best is None:
            return None
        return best[1], best[2]

    def _probe_stats(self) -> float | None:
        """Probe the open stream: return its RMS level, None when no data came."""
        started = time.monotonic()
        deadline = started + CAPTURE_PROBE_SEC
        frames: list[np.ndarray] = []
        while time.monotonic() < deadline:
            frame = self.read(0.05)
            if frame is None:
                time.sleep(0.01)
                if not frames and time.monotonic() - started > CAPTURE_NO_DATA_SEC:
                    return None
                continue
            array = np.asarray(frame, dtype=np.int16)
            if array.size:
                frames.append(array)
        if not frames:
            return None
        audio = np.concatenate(frames)
        float_audio = audio.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(float_audio**2))) if audio.size else 0.0
        zeros = float(np.mean(audio == 0))
        if rms < 1e-4 and zeros > 0.5:
            return 0.0  # digital silence: never a winner
        return rms

    def _capture_candidates(self, sd: Any, max_channels: int) -> list[tuple[int, int]]:
        """Formats to try, best first.

        Some drivers accept 16 kHz mono but deliver zeros in that format while
        their native rate works (or they need stereo); the negotiated rate is
        tried first so a healthy device keeps the cheap configuration.
        """
        negotiated = self._negotiate_rate(sd, self._device)
        native = self._native_samplerate or negotiated
        candidates: list[tuple[int, int]] = []
        for entry in (
            (negotiated, 1),
            (native, 1),
            (native, min(2, max_channels)),
            (negotiated, min(2, max_channels)),
        ):
            rate, channels = int(entry[0]), int(entry[1])
            if channels < 1 or entry in candidates:
                continue
            candidates.append(entry)
        return candidates

    def _open_stream(self, sd: Any, rate: int, channels: int) -> Any:
        native_block = max(1, int(round(self._block_size * rate / float(self._sample_rate))))
        stream = sd.InputStream(
            samplerate=rate,
            channels=channels,
            dtype=DTYPE,
            blocksize=native_block,
            device=self._device,
            callback=self._callback,
        )
        stream.start()
        return stream

    def _probe_stream(self) -> bool:
        """False when the opened stream produces nothing but digital silence."""
        started = time.monotonic()
        deadline = started + CAPTURE_PROBE_SEC
        frames: list[np.ndarray] = []
        while time.monotonic() < deadline:
            frame = self.read(0.05)
            if frame is None:
                time.sleep(0.01)
                if not frames and time.monotonic() - started > CAPTURE_NO_DATA_SEC:
                    # Nothing at all: the driver may still be warming up and we
                    # cannot judge the format, so keep it (pipeline watchdog
                    # will reopen it later if it stays silent).
                    return True
                continue
            array = np.asarray(frame, dtype=np.int16)
            if array.size:
                frames.append(array)
        if not frames:
            return True
        audio = np.concatenate(frames)
        float_audio = audio.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(float_audio**2))) if audio.size else 0.0
        zeros = float(np.mean(audio == 0))
        return not (rms < 1e-4 and zeros > 0.5)

    def _negotiate_rate(self, sd: Any, device: int | None) -> int:
        """16 kHz when the device allows it, otherwise its native rate."""
        target = self._sample_rate
        checker = getattr(sd, "check_input_settings", None)
        if not callable(checker):
            return target
        try:
            checker(device=device, samplerate=target, channels=CHANNELS, dtype=DTYPE)
            return target
        except Exception as exc:
            native = self._native_samplerate or 48000
            if native == target:  # pragma: no cover - contradictory driver
                return target
            log.info(
                "device %s does not support %d Hz input (%s), using %d Hz and resampling",
                self._device_name or device,
                target,
                exc,
                native,
            )
            return native

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        self._close_stream_locked(stream)

    @staticmethod
    def _close_stream_locked(stream: Any) -> None:
        """Close a PortAudio stream without touching self._lock."""
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:  # pragma: no cover
            pass
        try:
            stream.close()
        except Exception:  # pragma: no cover
            pass

    def close(self) -> None:
        self.stop()
        super().close()

    # -- producer ----------------------------------------------------------
    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            log.debug("audio status: %s", status)
        try:
            block = indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata
            rate = self.capture_samplerate
            if rate != self._sample_rate:
                block = resample_int16(block, rate, self._sample_rate)
            self.push(block)
        except Exception:  # pragma: no cover - never kill the audio thread
            log.debug("microphone callback failed", exc_info=True)


def list_input_devices() -> list[dict[str, Any]]:
    """Enumerate input devices for the settings UI (empty without sounddevice)."""
    try:
        import sounddevice as sd
    except Exception:  # pragma: no cover
        return []
    result: list[dict[str, Any]] = []
    try:
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels", 0) or 0) <= 0:
                continue
            result.append(
                {
                    "index": index,
                    "name": str(device.get("name", "")),
                    "channels": int(device.get("max_input_channels", 0) or 0),
                }
            )
    except Exception:  # pragma: no cover
        return []
    return result


class AudioRingBuffer:
    """Keeps the last N seconds of 16 kHz audio for diagnostics (debug_mic_raw.wav).

    Frames are stored by reference; push() copies only through to_int16_mono when
    the input is not already int16, so an idle pipeline costs nothing.
    """

    def __init__(self, seconds: float = 10.0, sample_rate: int = SAMPLE_RATE) -> None:
        self._limit = max(1, int(float(seconds) * int(sample_rate or SAMPLE_RATE)))
        self._frames: deque[np.ndarray] = deque()
        self._size = 0

    @property
    def limit_samples(self) -> int:
        return self._limit

    @property
    def seconds(self) -> float:
        return self._limit / float(SAMPLE_RATE)

    def push(self, frame: Any) -> None:
        data = to_int16_mono(frame)
        if data.size == 0:
            return
        self._frames.append(data)
        self._size += int(data.size)
        # Keep at most limit samples: drop from the front, always by whole frames.
        while self._frames and self._size - int(self._frames[0].size) >= self._limit:
            self._size -= int(self._frames.popleft().size)

    def samples(self) -> np.ndarray:
        if not self._frames:
            return np.zeros(0, dtype=np.int16)
        data = np.concatenate(self._frames)
        if data.size > self._limit:
            data = data[-self._limit :]
        return np.ascontiguousarray(data, dtype=np.int16)

    def clear(self) -> None:
        self._frames.clear()
        self._size = 0


def create_mic(config: Any) -> MicSource:
    """Build the capture source, validating the configured input device."""
    device, note = resolve_input_device_info(config)
    mic = SoundDeviceMic(device=device)
    mic.selection_note = note
    return mic


def create_mic_for_device(device: int | None) -> MicSource:
    """Build a capture source for one explicit device (audio test in settings)."""
    return SoundDeviceMic(device=device)


# --------------------------------------------------------------------------- devices
def list_devices() -> dict[str, list[dict[str, Any]]]:
    """Enumerate input and output devices for the settings panel (TZ 11).

    Returns {"input": [...], "output": [...]} where every entry is
    {"index", "name", "channels", "hostapi", "default"}. A missing sounddevice
    (or a PortAudio failure) gives two empty lists instead of an exception: the
    UI then honestly shows that there is no device.
    """
    result: dict[str, list[dict[str, Any]]] = {"input": [], "output": []}
    try:
        import sounddevice as sd
    except Exception:  # pragma: no cover - depends on the machine
        return result
    try:
        devices = list(sd.query_devices())
        hostapis = list(sd.query_hostapis())
    except Exception:  # pragma: no cover
        return result
    try:
        defaults = list(sd.default.device)
    except Exception:  # pragma: no cover
        defaults = [-1, -1]
    default_input = int(defaults[0]) if len(defaults) > 0 and defaults[0] is not None else -1
    default_output = int(defaults[1]) if len(defaults) > 1 and defaults[1] is not None else -1
    for index, device in enumerate(devices):
        try:
            api_index = int(device.get("hostapi", -1))
        except (TypeError, ValueError):  # pragma: no cover
            api_index = -1
        hostapi = ""
        if 0 <= api_index < len(hostapis):
            hostapi = str(hostapis[api_index].get("name", ""))
        name = str(device.get("name", ""))
        inputs = int(device.get("max_input_channels", 0) or 0)
        outputs = int(device.get("max_output_channels", 0) or 0)
        if inputs > 0:
            result["input"].append(
                {"index": index, "name": name, "channels": inputs, "hostapi": hostapi, "default": index == default_input}
            )
        if outputs > 0:
            result["output"].append(
                {"index": index, "name": name, "channels": outputs, "hostapi": hostapi, "default": index == default_output}
            )
    return result


def resolve_device(value: Any, kind: str = "input") -> int | None:
    """Turn an audio.*_device config value into a sounddevice index.

    "default" / "по умолчанию" / an empty value -> None (the system default),
    "index:N" (or a bare number) -> N, anything else is looked up as a
    case-insensitive substring of the device name.
    """
    text = str("" if value is None else value).strip()
    lowered = text.lower()
    if lowered in {"", "default", "system", "по умолчанию"}:
        return None
    if lowered.startswith("index:"):
        try:
            return int(lowered.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        devices = list_devices().get(kind, [])
    except Exception:  # pragma: no cover
        return None
    for device in devices:
        if lowered in str(device.get("name", "")).lower():
            return int(device["index"])
    log.info("configured %s device %r not found, using the system default", kind, text)
    return None


def device_channels(index: Any, kind: str = "input") -> int:
    """How many channels of the requested kind the device reports (0 when unknown)."""
    try:
        import sounddevice as sd

        info = sd.query_devices(int(index))
    except Exception:  # pragma: no cover - depends on the machine
        return 0
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        return int(info.get(key, 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover
        return 0


def resolve_input_device_info(config: Any) -> tuple[int | None, str]:
    """Validate audio.input_device; an output-only choice falls back to the system.

    A device picked from the output list (or a look-alike name) has
    max_input_channels == 0 and PortAudio refuses it with a confusing
    "Invalid number of channels". The assistant says what is wrong and keeps
    listening on the default microphone instead.
    """
    try:
        value = config.get("audio.input_device", "default")
    except Exception:  # pragma: no cover - defensive against odd config mocks
        return None, ""
    index = resolve_device(value, "input")
    if index is None:
        return None, ""
    if device_channels(index, "input") <= 0:
        message = "выбранное устройство не поддерживает запись (это выход), выберите микрофон"
        log.warning("%s: input device %r resolved to %s", message, value, index)
        return None, message
    return index, ""


def resolve_input_device(config: Any) -> int | None:
    """Map audio.input_device from the config to a validated sounddevice index."""
    return resolve_input_device_info(config)[0]


def resolve_output_device(config: Any) -> int | None:
    """Map audio.output_device from the config to a sounddevice index."""
    try:
        value = config.get("audio.output_device", "default")
    except Exception:  # pragma: no cover
        return None
    return resolve_device(value, "output")


def winsound_available() -> bool:
    """True when the stdlib winsound module can be imported (Windows only)."""
    try:
        import winsound  # noqa: F401
    except Exception:
        return False
    return True


def wave_out_devices() -> int:
    """Number of waveOut devices Windows sees (0 means: no speakers at all)."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.winmm.waveOutGetNumDevs())
    except Exception:  # pragma: no cover - no winmm on this system
        return 0


def output_backend() -> tuple[str, str]:
    """Which playback path works right now: (backend, russian explanation).

    "portaudio" - PortAudio has a usable default output device;
    "winsound"  - PortAudio has none, but Windows waveOut has a device;
    "none"      - nothing can play sound on this machine.
    """
    try:
        import sounddevice as sd

        defaults = list(sd.default.device)
        default_output = int(defaults[1]) if len(defaults) > 1 and defaults[1] is not None else -1
        if default_output >= 0:
            info = sd.query_devices(default_output)
            if int(info.get("max_output_channels", 0) or 0) > 0:
                return "portaudio", f"звук через PortAudio: {info.get('name', '')}"
    except Exception:  # pragma: no cover - depends on the machine
        pass
    try:
        count = wave_out_devices()
    except Exception:  # pragma: no cover
        count = 0
    if count > 0:
        return "winsound", "звук через системное устройство Windows (winsound)"
    return "none", "нет устройства вывода: колонки или наушники не подключены"


def to_wav_bytes(samples: Any, sample_rate: int) -> bytes:
    """Pack int16 mono PCM into a RIFF/WAV container (stdlib wave + BytesIO)."""
    data = to_int16_mono(samples)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate) or SAMPLE_RATE)
        handle.writeframes(data.tobytes())
    return buffer.getvalue()


def read_wav_bytes(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Parse a WAV payload into (int16 mono samples, sample rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        rate = int(handle.getframerate() or SAMPLE_RATE)
        channels = int(handle.getnchannels() or 1)
        width = int(handle.getsampwidth() or 2)
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"unsupported WAV sample width: {width} bytes")
    data = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        data = data.reshape(-1, channels)[:, 0]
    return np.ascontiguousarray(data), rate


def resample_int16(samples: Any, from_rate: int, to_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Linear resampling to the model rate; used by the STT self-test."""
    data = to_int16_mono(samples)
    source = int(from_rate) or SAMPLE_RATE
    target = int(to_rate) or SAMPLE_RATE
    if source == target or data.size == 0:
        return data
    count = max(1, int(round(data.size * target / float(source))))
    positions = np.linspace(0.0, float(data.size - 1), num=count)
    resampled = np.interp(positions, np.arange(data.size, dtype=np.float64), data.astype(np.float64))
    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def wav_duration(wav_bytes: bytes) -> float:
    """Length of a WAV payload in seconds (2 s fallback for odd containers)."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
            rate = handle.getframerate() or 1
            return float(handle.getnframes()) / float(rate)
    except Exception:  # pragma: no cover - malformed payload
        return 2.0


class WavePlayer:
    """WAV playback through winsound (stdlib) on the system default device.

    This is the fallback when PortAudio cannot open an output stream - for
    example on a machine where only WDM-KS endpoints exist ("Blocking API not
    supported yet"). winsound goes through waveOut, which Windows routes to the
    default device, so it works in every normal configuration.

    Sound starts with SND_MEMORY | SND_ASYNC; the buffer is kept alive for the
    whole playback (Windows does not copy it) and stop() purges it.
    """

    def __init__(self, poll: float = 0.01) -> None:
        self._buffer: bytes | None = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._poll = max(0.001, float(poll))

    def play_wav(self, wav_bytes: bytes, *, wait: bool = True) -> None:
        if not wav_bytes:
            return
        try:
            import winsound
        except Exception as exc:  # pragma: no cover - non Windows
            raise AudioUnavailable(f"winsound недоступен: {exc}") from exc
        with self._lock:
            self._buffer = bytes(wav_bytes)
            self._stopped.clear()
            buffer = self._buffer
        try:
            winsound.PlaySound(buffer, winsound.SND_MEMORY | winsound.SND_ASYNC)
        except Exception as exc:
            with self._lock:
                self._buffer = None
            raise AudioUnavailable(f"winsound не воспроизводит WAV: {exc}") from exc
        if not wait:
            return
        try:
            deadline = time.monotonic() + wav_duration(buffer)
            while time.monotonic() < deadline:
                if self._stopped.is_set():
                    break
                time.sleep(self._poll)
        finally:
            with self._lock:
                self._buffer = None

    def stop(self) -> None:
        """Abort the current sound (safe from any thread)."""
        self._stopped.set()
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:  # pragma: no cover - no device or not Windows
            pass

    def close(self) -> None:
        self.stop()


# --------------------------------------------------------------------------- output
class AudioOutput:
    """Blocking int16 PCM player, interruptible from another thread.

    Used for TTS playback: writing in small chunks lets stop() abort within a
    few milliseconds, which is what barge-in needs (TZ 2.6).
    """

    CHUNK = 1024

    def __init__(self, device: int | None = None) -> None:
        self._device = device
        self._stream: Any = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def play(self, samples: Any, sample_rate: int = SAMPLE_RATE) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:  # pragma: no cover
            raise AudioUnavailable(f"sounddevice is not available: {exc}") from exc
        data = to_int16_mono(samples)
        if data.size == 0:
            return
        self._stop.clear()
        try:
            stream = sd.OutputStream(
                samplerate=int(sample_rate),
                channels=1,
                dtype=DTYPE,
                device=self._device,
            )
        except Exception as exc:  # pragma: no cover
            raise AudioUnavailable(f"no output device: {exc}") from exc
        with self._lock:
            self._stream = stream
        try:
            stream.start()
            for offset in range(0, data.size, self.CHUNK):
                if self._stop.is_set():
                    break
                stream.write(data[offset : offset + self.CHUNK].reshape(-1, 1))
        except Exception as exc:  # pragma: no cover
            if not self._stop.is_set():
                raise AudioUnavailable(f"playback failed: {exc}") from exc
        finally:
            with self._lock:
                self._stream = None
            for action in (stream.stop, stream.close):
                try:
                    action()
                except Exception:  # pragma: no cover
                    pass

    def stop(self) -> None:
        """Abort the current playback (safe from any thread)."""
        self._stop.set()
        with self._lock:
            stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:  # pragma: no cover
                pass

    def close(self) -> None:
        self.stop()


__all__ = [
    "AudioOutput",
    "AudioRingBuffer",
    "AudioUnavailable",
    "BLOCK_SIZE",
    "CHANNELS",
    "DTYPE",
    "DEFAULT_QUEUE_FRAMES",
    "FRAME_MS",
    "FrameQueue",
    "MicSource",
    "SAMPLE_RATE",
    "SoundDeviceMic",
    "create_mic",
    "create_mic_for_device",
    "device_channels",
    "list_devices",
    "list_input_devices",
    "output_backend",
    "read_wav_bytes",
    "resample_int16",
    "resolve_device",
    "resolve_output_device",
    "resolve_input_device",
    "rms",
    "to_int16_mono",
    "to_wav_bytes",
    "wave_out_devices",
    "wav_duration",
    "WavePlayer",
    "winsound_available",
]
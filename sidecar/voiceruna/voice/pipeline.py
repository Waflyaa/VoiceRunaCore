"""Voice pipeline: the single owner of audio and of the voice states (TZ 3.2, 5).

Responsibility split agreed with the runtime owner:
    pipeline sets   listening | speaking | paused | error | downloading
    runtime  sets   thinking | idle
The pipeline only writes idle when it has to complete a transition the runtime
cannot observe: the 6 s "nobody spoke after the wake word" timeout (TZ 5.2),
a discarded too-short phrase, resume(), shutdown, and the error phrases it
plays itself. It never writes thinking.

One background asyncio task pulls 32 ms frames, runs VAD, the wake detector and
the utterance segmenter, then hands the finished utterance to a separate task
so wake detection and barge-in keep working while STT and TTS run. Heavy work
(STT, playback, device open) is pushed to worker threads.

Hardware is optional: without a microphone start() emits an error event with
errors.mic_phrase, switches to state error and keeps text input, speech and the
rest of the assistant alive (TZ 15). Nothing here raises into asyncio tasks.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from ..config import ConfigManager
from ..events import EventSink, error_event, state_event, transcript_event
from ..state import (
    DOWNLOADING,
    ERROR,
    IDLE,
    LISTENING,
    PAUSED,
    SPEAKING,
    StateManager,
)
from .. import paths
from .audio import (
    AudioRingBuffer,
    AudioUnavailable,
    MicSource,
    SoundDeviceMic,
    create_mic,
    list_devices,
    output_backend,
    read_wav_bytes,
    resample_int16,
    resolve_device,
    resolve_output_device,
    rms,
    to_int16_mono,
    to_wav_bytes,
)
from .download import ModelDownloader
from .stt import WhisperTranscriber
from .tts import PREVIEW_TEXT, Mp3Player, SapiSpeaker, Speaker
from .vad import CONTINUE, DISCARD, END, START, UtteranceSegmenter, create_vad
from .wake import WakeDetector, create_wake_detector

log = logging.getLogger(__name__)

#: How long a single blocking read may wait for a frame.
READ_TIMEOUT = 0.1
#: TZ 2.6: the microphone stays deaf for 300 ms after the answer finished.
MIC_MUTE_AFTER_SPEECH_SEC = 0.8
#: TZ 5.2: minimum speech inside a phrase, everything shorter is noise.
MIN_SPEECH_MS = 200.0
#: The wake capture is deliberately short: "ассистент, открой блокнот" is ~2 s.
WAKE_MAX_SEC = 3.5
#: Reopen a capture stream that keeps returning digital silence (0/-90 dBFS).
#: Deliberately conservative: a quiet microphone with a noise gate must not be
#: reopened in the middle of a phrase, so this needs 10 s of *near-digital*
#: silence, at most two attempts, one minute apart.
MIC_SILENCE_RESTART_SEC = 10.0
MIC_SILENCE_ZERO_RATIO = 0.9
MIC_REOPEN_ATTEMPTS = 2
MIC_REOPEN_COOLDOWN_SEC = 60.0
#: Retry interval for a microphone that was not available at start().
MIC_RETRY_SEC = 5.0
#: A capture stream can stay "active" while the device has silently stopped
#: delivering frames (USB headset re-plugged, WDM-KS hiccup): PortAudio reports
#: no error and every read just times out. Without this watchdog the assistant
#: idles in silence until the whole app is restarted - /health still answers 200
#: and the state stays "idle", which is exactly the owner's "it stopped hearing
#: me" (owner report 2026-10-29). Reopen the stream after this long without a
#: single frame.
MIC_STARVE_SEC = 10.0
#: Never reopen a starved stream more often than this, so a device that is
#: permanently gone is retried once a minute instead of spinning.
MIC_STARVE_REOPEN_COOLDOWN_SEC = 60.0
#: The sidecar's event hub forwards StateManager notifications, so the pipeline
#: does not emit a second copy of every state transition.
EMIT_STATE_EVENTS = False
#: Phrase for the "проверить звук" button; deliberately one edge-tts likes.
TEST_OUTPUT_PHRASE = "Проверка звука"
#: Peak level below this counts as silence in the microphone check.
SILENCE_LEVEL = 0.01
#: Phrase the STT self-test speaks and expects back from the model.
TEST_STT_PHRASE = "Открой блокнот"
#: Rate whisper is trained on; SAPI renders at 22 kHz, so we resample.
STT_SAMPLE_RATE = 16000


def _normalize_word(word: Any) -> str:
    """Lowercase word without punctuation (ё -> е), for wake-word matching."""
    return re.sub(r"[^0-9a-zа-я]+", "", str(word).lower().replace("ё", "е"))


#: Energy gate that runs in parallel with the VAD. A quiet far-field mic (USB
#: camera at -65 dBFS ambient) often stays under the neural VAD threshold even
#: while the owner speaks; the gate compares the frame level with a slowly
#: adapting noise floor so those phrases still start a recording.
ENERGY_GATE_RATIO = 3.5
ENERGY_GATE_MIN_LEVEL = 0.0015  # fallback when the microphone was never calibrated
#: The "too quiet" floor is relative to the measured room noise (auto-calibration).
TOO_QUIET_MARGIN_DB = 10.0
#: Gate level sits this far above the measured noise floor (Leon uses the same
#: +10 dB rule for its RMS threshold, just with a fixed value instead).
ENERGY_GATE_MARGIN_DB = 10.0
#: A calibration whose recording is silence must not be stored: a dead camera
#: stream once produced noise_floor -94 dBFS and a gate of -86 dBFS, which
#: turned every room sound into "speech" (owner report 2026-10-29).
DEAD_CALIBRATION_PEAK_DB = -70.0
DEAD_CALIBRATION_NOISE_DB = -75.0
DEAD_CALIBRATION_ZERO_RATIO = 0.5


def _folder_size_mb(path: Any) -> float:
    """Total size of a model directory in MB (0 when it is not there)."""
    try:
        root = Path(str(path))
        if not root.is_dir():
            return 0.0
        total = sum(item.stat().st_size for item in root.rglob("*") if item.is_file())
        return round(total / 1_000_000.0, 1)
    except Exception:  # pragma: no cover - permissions
        return 0.0


class VoicePipeline:
    """Microphone -> VAD -> wake word -> STT -> callback, plus TTS playback."""

    def __init__(
        self,
        config: ConfigManager,
        state: StateManager,
        emit: EventSink,
        on_utterance: Callable[[str], Awaitable[None]],
    ) -> None:
        self._config = config
        self._state = state
        self._emit = emit
        self._on_utterance = on_utterance

        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        # Reads must never wait behind whisper/piper work in the default
        # executor: a starved reader drops frames ("input overflow"), which is
        # exactly how a phrase turns into garbage before STT even sees it.
        self._mic_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vox-mic-read")
        self._turn_task: asyncio.Task | None = None
        self._running = False
        self._closed = False
        self._paused = False
        self._mic_dirty = False
        self._mic_ok = False
        self._mic_error_emitted = False
        self._mic_retry_at = 0.0
        # Frame-starvation watchdog: when the last frame arrived, how many
        # reopens a starved stream needed, and when the last one happened.
        self._last_frame_at = 0.0
        self._mic_starve_reopens = 0
        self._last_starve_reopen_at = 0.0

        self._mic: MicSource | None = None
        self._mic_factory: Callable[[], MicSource] = lambda: create_mic(self._config)
        self._vad: Any = None
        self._vad_factory: Callable[[], Any] = lambda: create_vad(self._config)
        self._wake: WakeDetector | None = None
        self._wake_factory: Callable[[], WakeDetector] = lambda: create_wake_detector(self._config)

        self._segmenter = UtteranceSegmenter()
        # Wake detection uses its own short segmenter: the main one can run for
        # 12 s in a noisy room and whisper then transcribes the background
        # instead of "ассистент, ..." (owner report 2026-10-28).
        self._wake_segmenter = UtteranceSegmenter(
            silence_ms=500,
            max_utterance_sec=WAKE_MAX_SEC,
            min_speech_ms=MIN_SPEECH_MS,
            pre_buffer_ms=500,
        )
        self._stt = WhisperTranscriber(config)
        self._speaker = Speaker(config, emit=self._emit_safe)
        self._downloader: ModelDownloader | None = None

        self._listen_deadline: float | None = None
        self._mic_mute_until = 0.0
        self._speaking = False
        self._barge_in = False
        self._turn = 0
        self._last_unload_check = 0.0

        self._listen_timeout = 6.0
        self._input_device = "default"
        self._output_device = "default"
        self._speaker_injected = False
        self._output_probe_factory: Callable[[int | None], Any] = self._make_output_probe
        self._input_probe_factory: Callable[[int | None], Any] = self._make_input_probe
        self._sapi_probe_factory: Callable[[], Any] = SapiSpeaker
        self._ring = AudioRingBuffer(10.0, STT_SAMPLE_RATE)
        self._debug_save = False
        self._debug_raw_seconds = 10.0
        self._tail_pad_ms = 500.0
        self._last_debug_wav = ""
        self._raw_debug_wav = ""
        self._mic_note_shown = False
        self._last_stt_confidence: float | None = None
        self._last_stt_processing = 0.0
        self._last_stt_peak_db: float | None = None
        self._last_stt_normalized = False
        self._wake_mode = "vad_stt"
        self._wake_phrase = "вокс"
        self._wake_fallback = True
        self._followup_window = 5.0
        self._wake_injected = False
        self._busy = False
        self._last_heard = ""
        self._current_speech = ""
        self._stt_error: str | None = None
        self._mic_error: str | None = None
        self._silent_since = 0.0
        self._mic_reopens = 0
        self._noise_floor_db: float | None = None
        self._too_quiet_dbfs = -45.0
        self._energy_min = ENERGY_GATE_MIN_LEVEL
        self._stt_ready = True
        self._stt_ready_error = ""
        self._stt_failed_phrase = "Не расслышал, повтори"
        self._mic_phrase = "микрофон недоступен"
        self._no_connection_phrase = "нет связи"
        self._reload_settings()

    # ------------------------------------------------------------------ config
    def _reload_settings(self) -> None:
        """Re-read everything that can change while the pipeline is running."""
        cfg = self._config
        try:
            self._segmenter.configure(
                silence_ms=float(cfg.get("vad.silence_ms", 800)),
                max_utterance_sec=float(cfg.get("vad.max_utterance_sec", 12)),
                min_speech_ms=float(cfg.get("vad.min_speech_ms", MIN_SPEECH_MS)),
                pre_buffer_ms=float(cfg.get("vad.pre_buffer_ms", 700)),
            )
            self._wake_segmenter.configure(
                silence_ms=float(cfg.get("vad.wake_silence_ms", 500)),
                max_utterance_sec=float(cfg.get("vad.wake_max_sec", WAKE_MAX_SEC)),
                min_speech_ms=float(cfg.get("vad.min_speech_ms", MIN_SPEECH_MS)),
                pre_buffer_ms=float(cfg.get("vad.wake_pre_buffer_ms", 500)),
            )
            self._listen_timeout = float(cfg.get("wakeword.listen_timeout_sec", 6) or 6)
            self._followup_window = float(cfg.get("wakeword.followup_window_sec", 5) or 0)
            self._tail_pad_ms = float(cfg.get("vad.tail_pad_ms", 500) or 0)
            self._debug_save = bool(cfg.get("debug.save_audio", False))
            raw_seconds = float(cfg.get("debug.raw_seconds", 10) or 0)
            if raw_seconds != self._debug_raw_seconds:
                self._debug_raw_seconds = raw_seconds
                self._ring = AudioRingBuffer(raw_seconds, STT_SAMPLE_RATE)
            self._input_device = str(cfg.get("audio.input_device", "default") or "default")
            self._output_device = str(cfg.get("audio.output_device", "default") or "default")
            self._wake_phrase = str(cfg.get("wakeword.phrase", self._wake_phrase) or self._wake_phrase)
            mode = str(cfg.get("wakeword.engine", self._wake_mode) or "").strip().lower()
            if mode in {"vad_stt", "vosk", "none"}:
                self._wake_mode = mode
            self._wake_fallback = bool(cfg.get("wakeword.fallback_to_stt", True))
            self._stt_failed_phrase = str(cfg.get("errors.stt_failed_phrase", self._stt_failed_phrase))
            self._mic_phrase = str(cfg.get("errors.mic_phrase", self._mic_phrase))
            self._no_connection_phrase = str(cfg.get("errors.no_connection_phrase", self._no_connection_phrase))
        except Exception:  # pragma: no cover - a broken config must not kill the loop
            log.warning("cannot reload voice settings", exc_info=True)
        for component in (self._wake, self._stt, self._speaker):
            configure = getattr(component, "configure", None)
            if not callable(configure):
                continue
            try:
                configure(cfg)
            except Exception:  # pragma: no cover
                log.debug("component reconfigure failed", exc_info=True)

    def apply_config(self) -> None:
        """Hot reload tts/audio/vad/wake/errors; safe before start() (TZ 11)."""
        if self._closed:
            return
        try:
            device = str(self._config.get("audio.input_device", "default") or "default")
            if device != self._input_device:
                self._input_device = device
                self._mic_dirty = True
            output = str(self._config.get("audio.output_device", "default") or "default")
            if output != self._output_device:
                self._output_device = output
                # The device is baked into the player, so the speaker is rebuilt
                # (TZ 11 allows a restart for what cannot be hot-swapped).
                self._rebuild_speaker()
            previous_phrase = self._wake_phrase
            previous_mode = self._wake_mode
            self._reload_settings()
            if (
                self._running
                and not self._closed
                and (self._wake_phrase != previous_phrase or self._wake_mode != previous_mode)
            ):
                # A new name or engine cannot be hot-swapped into an open
                # detector: rebuild it so the very next phrase is matched.
                log.info("wake settings changed, restarting the detector")
                self._wake = None
                self._segmenter.reset()
                self._wake_segmenter.reset()
                self._start_wake_engine()
        except Exception:  # pragma: no cover - config save must never explode
            log.warning("apply_config failed", exc_info=True)

    # ------------------------------------------------------------------ helpers
    def _set_state(self, value: str) -> None:
        try:
            self._state.set(value)
        except Exception:  # pragma: no cover
            log.debug("state set failed", exc_info=True)
        if EMIT_STATE_EVENTS:
            self._emit_safe(state_event(value))

    def _emit_safe(self, event: Any) -> None:
        try:
            self._emit(event)
        except Exception:  # pragma: no cover - the sink must never break the loop
            log.debug("event sink failed", exc_info=True)

    def _enter_idle(self, *, after_speech: bool = False) -> None:
        """Return to idle from a pipeline-owned state only (never from thinking).

        after_speech allows the SPEAKING state, so a phrase the pipeline plays
        itself (errors.stt_failed_phrase) does not leave the UI stuck on
        "speaking" when no runtime call follows.
        """
        self._segmenter.disarm()
        self._listen_deadline = None
        current = self._state.state
        if current in (LISTENING, ERROR) or (after_speech and current == SPEAKING):
            self._set_state(IDLE)

    def _begin_listening(self) -> None:
        if self._closed or self._paused:
            return
        self._segmenter.arm()
        self._listen_deadline = time.monotonic() + self._listen_timeout
        if self._state.state != LISTENING:
            self._set_state(LISTENING)

    def begin_followup(self) -> None:
        """Keep the microphone armed right after a spoken answer (v2 UX).

        A reply without the wake word is a continuation of the dialogue: the
        segmenter is armed exactly like after a wake word, and the ordinary
        listen timeout is replaced by wakeword.followup_window_sec. Silence
        for the whole window drops back to wake-word mode via
        _check_listen_timeout.
        """
        if self._closed or self._paused or not self._running or self._mic is None:
            return
        window = float(self._followup_window or 0)
        if window <= 0:
            return
        self._segmenter.arm()
        self._listen_deadline = time.monotonic() + window
        if self._state.state != LISTENING:
            self._set_state(LISTENING)
        log.debug("follow-up window open for %.1f s", window)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Open the microphone and start the frame loop; never raises (TZ 5.2)."""
        if self._running or self._closed:
            return
        self._loop = asyncio.get_running_loop()
        self._reload_settings()
        if self._vad is None:
            try:
                self._vad = self._vad_factory()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("vad unavailable: %s", exc)
                self._vad = None
        self._start_wake_engine()
        self._running = True
        self._report_stt_readiness()
        opened = await self._ensure_mic()
        if opened:
            self._load_calibration()
            asyncio.create_task(self._auto_calibrate(), name="vox-calibrate")
        self._last_frame_at = time.monotonic()
        self._task = asyncio.create_task(self._run(), name="vox-voice")
        if opened and self._state.state in (IDLE, ERROR):
            self._set_state(IDLE)

    def _start_wake_engine(self) -> None:
        """Choose the wake detection mode (TZ 5.1).

        vad_stt keeps the microphone in a passive VAD capture and lets Whisper
        decide whether the phrase started with "вокс". It needs no keyword
        model and works with far-field microphones; the Vosk KWS stays available
        for users who prefer the cheaper path. If Vosk is selected but its model
        is missing or broken, the STT fallback takes over automatically.
        """
        if self._wake_mode == "vad_stt":
            self._wake = None
            log.info("wake: vad_stt (VAD capture + Whisper wake-word check)")
            return
        if self._wake_mode == "none":
            self._wake = None
            log.info("wake: disabled (hotkey only)")
            return
        if self._wake is None:
            try:
                self._wake = self._wake_factory()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("wake word unavailable: %s", exc)
                self._wake = None
        engine = str(getattr(self._wake, "engine", "none") or "none") if self._wake is not None else "none"
        if engine in ("none", "") and self._wake_fallback and not self._wake_injected:
            self._wake_mode = "vad_stt"
            self._wake = None
            log.info("wake: vosk is not usable here, falling back to vad_stt")
            return
        log.info("wake: %s", engine or "none")

    async def stop(self) -> None:
        """Close everything; safe to call twice and after a failed start."""
        if self._closed:
            return
        self._closed = True
        self._running = False
        self._paused = False
        self._speaking = False
        for attribute in ("_task", "_turn_task"):
            task = getattr(self, attribute)
            setattr(self, attribute, None)
            if task is None or task.done():
                continue
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        with suppress(Exception):
            self._speaker.stop()
        with suppress(Exception):
            self._speaker.close()
        mic, self._mic = self._mic, None
        self._mic_ok = False
        if mic is not None:
            with suppress(Exception):
                await asyncio.to_thread(mic.close)
        for component in (self._wake, self._vad):
            closer = getattr(component, "close", None)
            if callable(closer):
                with suppress(Exception):
                    await asyncio.to_thread(closer)
        if self._downloader is not None:
            with suppress(Exception):
                await self._downloader.aclose()
        self._segmenter.reset()
        self._listen_deadline = None
        self._set_state(IDLE)
        with suppress(Exception):
            self._mic_executor.shutdown(wait=False, cancel_futures=True)

    async def pause(self) -> None:
        """Microphone off (tray toggle), state paused (TZ 10.2)."""
        if self._closed or self._paused:
            return
        self._paused = True
        self._segmenter.disarm()
        self._listen_deadline = None
        mic = self._mic
        if mic is not None:
            with suppress(Exception):
                await asyncio.to_thread(mic.stop)
        self._set_state(PAUSED)

    async def resume(self) -> None:
        """Microphone back on, state idle."""
        if self._closed or not self._paused:
            return
        self._paused = False
        if await self._ensure_mic():
            self._set_state(IDLE)

    async def listen_now(self) -> None:
        """Listen immediately without the wake word (hotkey, POST /listen)."""
        if self._closed or self._paused or not self._running:
            return
        if not self._mic_ok and not await self._ensure_mic():
            return
        if self._speaking:
            await self.stop_speaking()
        self._begin_listening()

    # ------------------------------------------------------------------ calibration
    @staticmethod
    def _noise_floor_from_frames(frames: list[Any]) -> tuple[float, float] | None:
        """Return (noise_floor_dbfs, peak_dbfs) of the quietest part of the buffer."""
        if not frames:
            return None
        levels: list[float] = []
        for frame in frames:
            array = np.asarray(frame, dtype=np.float32) / 32768.0
            if array.size == 0:
                continue
            rms = float(np.sqrt(np.mean(array**2)))
            levels.append(20 * np.log10(max(rms, 1e-9)))
        if not levels:
            return None
        noise = float(np.percentile(levels, 20))
        peak = float(max(levels))
        return round(noise, 1), round(peak, 1)

    @staticmethod
    def _zeros_ratio(frames: list[Any]) -> float:
        """Share of exact zero samples in the recording (dead stream detector)."""
        total = 0
        zeros = 0
        for frame in frames:
            array = np.asarray(frame)
            if array.size == 0:
                continue
            total += int(array.size)
            zeros += int(np.count_nonzero(array == 0))
        return zeros / total if total else 1.0

    def _apply_noise_floor(self, noise_db: float) -> None:
        """Derive the per-device thresholds from the measured room noise."""
        self._noise_floor_db = float(noise_db)
        self._too_quiet_dbfs = round(float(noise_db) + TOO_QUIET_MARGIN_DB, 1)
        level = 10 ** ((float(noise_db) + ENERGY_GATE_MARGIN_DB) / 20.0)
        self._energy_min = float(min(max(level, 0.00005), 0.05))
        log.info(
            "calibration: noise floor %.1f dBFS, speech gate %.1f dBFS, "
            "too-quiet %.1f dBFS (energy %.5f)",
            self._noise_floor_db,
            self._noise_floor_db + ENERGY_GATE_MARGIN_DB,
            self._too_quiet_dbfs,
            self._energy_min,
        )

    def _calibration_key(self) -> str:
        mic = self._mic
        index = getattr(mic, "device_index", None) if mic is not None else None
        return f"index:{index}" if index is not None else str(self._input_device or "default")

    def _load_calibration(self) -> bool:
        """Apply the stored calibration for the current microphone, if any."""
        try:
            table = self._config.get("audio.calibration", {}) or {}
        except Exception:  # pragma: no cover
            table = {}
        if not isinstance(table, dict):
            return False
        entry = table.get(self._calibration_key())
        if not isinstance(entry, dict):
            return False
        noise = entry.get("noise_dbfs")
        try:
            value = float(noise)
        except (TypeError, ValueError):
            return False
        self._apply_noise_floor(value)
        return True

    async def calibrate(self, seconds: float = 3.0) -> dict:
        """Measure the room noise on the current microphone and store thresholds.

        Called by the "Калибровка микрофона" button and automatically once after
        the microphone opens when the device has no calibration yet.
        """
        seconds = max(1.0, min(float(seconds or 3.0), 10.0))
        result: dict = {"ok": False, "seconds": seconds, "message": ""}
        probe = None
        try:
            if self._live_capture_ready():
                frames: list[np.ndarray] = []
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    frame = self._ring.samples()
                    window = int(0.4 * STT_SAMPLE_RATE)
                    if frame is not None and frame.size >= window:
                        frames.append(np.asarray(frame[-window:], dtype=np.int16))
                    await asyncio.sleep(0.25)
                if not frames:
                    frame = self._ring.samples()
                    if frame is not None and frame.size:
                        frames.append(np.asarray(frame, dtype=np.int16))
            else:
                device = self._resolve_test_device(None, "input")
                probe = self._input_probe_factory(device)
                await asyncio.to_thread(probe.start)
                frames = []
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    frame = await asyncio.to_thread(probe.read, 0.1)
                    if frame is not None:
                        frames.append(np.asarray(frame, dtype=np.int16))
            stats = self._noise_floor_from_frames(frames)
        except Exception as exc:
            result["message"] = f"калибровка не удалась: {type(exc).__name__}: {exc}"
            return result
        finally:
            if probe is not None:
                with suppress(Exception):
                    await asyncio.to_thread(probe.close)
        if stats is None:
            result["message"] = "калибровка не удалась: микрофон не прислал данных"
            return result
        noise_db, peak_db = stats
        zeros_ratio = self._zeros_ratio(frames)
        dead_peak = peak_db < DEAD_CALIBRATION_PEAK_DB
        dead_stream = (
            noise_db < DEAD_CALIBRATION_NOISE_DB
            and zeros_ratio > DEAD_CALIBRATION_ZERO_RATIO
        )
        if dead_peak or dead_stream:
            log.warning(
                "calibration rejected: peak %.1f dBFS, noise %.1f dBFS, zeros %.0f%%",
                peak_db,
                noise_db,
                zeros_ratio * 100,
            )
            result.update(
                {
                    "ok": False,
                    "dead": True,
                    "device": self._calibration_key(),
                    "noise_floor_dbfs": noise_db,
                    "peak_dbfs": peak_db,
                    "message": (
                        "микрофон молчит: калибровка не сохранена. "
                        "Проверьте устройство ввода и повторите в тишине"
                    ),
                }
            )
            return result
        self._apply_noise_floor(noise_db)
        entry = {
            "noise_dbfs": noise_db,
            "peak_dbfs": peak_db,
            "seconds": seconds,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            table = dict(self._config.get("audio.calibration", {}) or {})
            table[self._calibration_key()] = entry
            self._config.set("audio.calibration", table)
        except Exception as exc:  # pragma: no cover - config write problems
            log.warning("cannot store calibration: %s", exc)
        result.update(
            {
                "ok": True,
                "device": self._calibration_key(),
                "noise_floor_dbfs": noise_db,
                "peak_dbfs": peak_db,
                "threshold_dbfs": self._too_quiet_dbfs,
                "gate_dbfs": round(noise_db + ENERGY_GATE_MARGIN_DB, 1),
                "message": (
                    f"тишина {noise_db:.0f} dBFS, порог речи {noise_db + ENERGY_GATE_MARGIN_DB:.0f} dBFS"
                ),
            }
        )
        return result

    async def _auto_calibrate(self) -> None:
        """Measure the room once after the microphone opens (if not calibrated)."""
        try:
            await asyncio.sleep(1.2)
            if self._closed or not self._live_capture_ready():
                return
            if self._load_calibration():
                return
            log.info("microphone has no calibration yet, measuring the room noise")
            await self.calibrate(2.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - never break startup
            log.debug("auto calibration failed: %s", exc)

    # ------------------------------------------------------------------ microphone
    async def _ensure_mic(self) -> bool:
        mic = self._mic
        if mic is None:
            try:
                mic = self._mic_factory()
            except Exception as exc:
                return self._report_mic_failure(exc)
            self._mic = mic
        note = str(getattr(mic, "selection_note", "") or "")
        if note and not self._mic_note_shown:
            self._mic_note_shown = True
            self._emit_safe(error_event(note, "mic"))
        if getattr(mic, "active", False):
            self._mic_ok = True
            self._mic_error = None
            self._mic_error_emitted = False
            return True
        try:
            await asyncio.to_thread(mic.start)
        except AudioUnavailable as exc:
            return self._report_mic_failure(exc)
        except Exception as exc:  # pragma: no cover - driver specific
            return self._report_mic_failure(exc)
        self._mic_ok = True
        self._mic_error = None
        self._mic_error_emitted = False
        return True

    def _report_mic_failure(self, exc: BaseException) -> bool:
        self._mic_ok = False
        self._mic_error = f"{type(exc).__name__}: {exc}"
        self._mic_retry_at = time.monotonic() + MIC_RETRY_SEC
        log.warning("microphone unavailable: %s", exc)
        if not self._mic_error_emitted:
            self._mic_error_emitted = True
            self._emit_safe(error_event(self._mic_phrase, "mic"))
            self._set_state(ERROR)
        return False

    async def _restart_mic(self) -> None:
        self._mic_dirty = False
        self._mic_note_shown = False
        mic, self._mic = self._mic, None
        if mic is not None:
            with suppress(Exception):
                await asyncio.to_thread(mic.close)
        self._mic_ok = False
        if await self._ensure_mic():
            # The device changed: its noise floor is different, so reload the
            # stored calibration or measure it again after the stream settles.
            if not self._load_calibration():
                asyncio.create_task(self._auto_calibrate(), name="vox-calibrate")

    # ------------------------------------------------------------------ main loop
    async def _run(self) -> None:
        while self._running:
            try:
                if self._mic_dirty:
                    await self._restart_mic()
                if not self._mic_ok:
                    if self._paused or time.monotonic() < self._mic_retry_at:
                        await asyncio.sleep(0.05)
                        continue
                    if not await self._ensure_mic():
                        await asyncio.sleep(0.05)
                        continue
                    self._last_frame_at = time.monotonic()
                    if self._state.state == ERROR:  # microphone came back
                        self._set_state(IDLE)
                self._check_listen_timeout()
                await self._tick_unload()
                if self._paused or self._mic is None:
                    # Deliberately silent, not starving: keep the clock fresh so
                    # a pause longer than MIC_STARVE_SEC does not look like a
                    # dead stream when it resumes.
                    self._last_frame_at = time.monotonic()
                    await asyncio.sleep(0.05)
                    continue
                loop = asyncio.get_running_loop()
                frame = await loop.run_in_executor(
                    self._mic_executor, self._mic.read, READ_TIMEOUT
                )
                if not self._running:
                    break
                if frame is None:
                    self._check_mic_starvation()
                    await asyncio.sleep(0.002)
                    continue
                self._last_frame_at = time.monotonic()
                if self._mic_starve_reopens:
                    log.info("microphone is delivering frames again")
                    self._mic_starve_reopens = 0
                # Raw input for debug_mic_raw.wav: shows whether VAD cut the phrase.
                self._ring.push(frame)
                self._log_input_level(frame)
                await self._on_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - the loop must survive anything
                log.warning("voice loop error: %s", exc)
                await asyncio.sleep(0.05)

    def _watch_mic_silence(self, frame: Any) -> None:
        """Reopen a microphone that delivers nothing but digital silence.

        Some USB devices open a "phantom" capture stream after a fast restart:
        the handle is valid but every sample is 0 (peak -90 dBFS). The user sees
        "it does not hear me" while the correct device works in other apps, so
        the pipeline reopens the stream a few times before giving up.
        """
        try:
            array = np.asarray(frame)
            if array.size == 0:
                return
            float_audio = array.astype(np.float32) / 32768.0
            level = float(np.sqrt(np.mean(float_audio ** 2)))
            zeros = float(np.mean(array == 0))
        except Exception:  # pragma: no cover - never break the loop
            return
        now = time.monotonic()
        # A dead stream is not always exact zeros: some drivers add +-1 LSB
        # dither, so judge by RMS (< -80 dBFS) together with the zero ratio.
        if not (level < 3e-5 and zeros > MIC_SILENCE_ZERO_RATIO):
            if now - self._silent_since > MIC_REOPEN_COOLDOWN_SEC:
                self._mic_reopens = 0
            self._silent_since = 0.0
            return
        if self._silent_since <= 0.0:
            self._silent_since = now
            return
        if now - self._silent_since < MIC_SILENCE_RESTART_SEC:
            return
        self._silent_since = 0.0
        if self._mic_reopens >= MIC_REOPEN_ATTEMPTS:
            return
        if now - getattr(self, "_last_reopen_at", 0.0) < MIC_REOPEN_COOLDOWN_SEC:
            return
        self._last_reopen_at = now
        self._mic_reopens += 1
        log.warning(
            "microphone returns digital silence for %.0f s, reopening (attempt %d/%d)",
            MIC_SILENCE_RESTART_SEC,
            self._mic_reopens,
            MIC_REOPEN_ATTEMPTS,
        )
        self._mic_dirty = True

    def _check_mic_starvation(self) -> None:
        """Reopen a stream that is "active" but has stopped delivering frames.

        PortAudio keeps a broken capture stream open: no error, no callback, so
        every read simply times out. The old loop slept 2 ms and tried again
        forever - the assistant stayed "idle", /health answered 200 and the app
        looked healthy while it could not hear a word. Reopening the device is
        the only thing that brings such a stream back.
        """
        now = time.monotonic()
        if not self._last_frame_at:
            self._last_frame_at = now
            return
        silent_for = now - self._last_frame_at
        if silent_for < MIC_STARVE_SEC:
            return
        if now - self._last_starve_reopen_at < MIC_STARVE_REOPEN_COOLDOWN_SEC:
            return
        self._last_starve_reopen_at = now
        self._mic_starve_reopens += 1
        self._last_frame_at = now
        log.warning(
            "microphone delivered no frames for %.0f s, reopening (attempt %d)",
            silent_for,
            self._mic_starve_reopens,
        )
        self._mic_dirty = True

    def _frame_age_seconds(self) -> float | None:
        """Seconds since the last captured frame (diagnostics)."""
        if not self._last_frame_at:
            return None
        return round(max(0.0, time.monotonic() - self._last_frame_at), 2)

    def _log_input_level(self, frame: Any) -> None:
        """Log the live input level every couple of seconds (diagnostics).

        This is what tells "the assistant cannot hear me" apart from "it hears
        but cannot recognise": a healthy idle microphone sits around -60 dBFS,
        a dead or muted one at -90 dBFS with zeros.
        """
        now = time.monotonic()
        if now - getattr(self, "_last_level_log", 0.0) < 2.0:
            return
        self._last_level_log = now
        try:
            array = np.asarray(frame)
            if array.size == 0:
                return
            peak = float(np.abs(array.astype(np.int32)).max()) / 32768.0
            rms = float(np.sqrt(np.mean((array.astype(np.float32) / 32768.0) ** 2)))
            self._last_input_peak_db = round(20 * np.log10(max(peak, 1e-9)), 1)
            self._last_input_rms_db = round(20 * np.log10(max(rms, 1e-9)), 1)
            zeros = float(np.mean(array == 0))
            log.debug(
                "input level: peak %.1f dBFS, rms %.1f dBFS, zeros %.0f%%",
                self._last_input_peak_db,
                self._last_input_rms_db,
                zeros * 100,
            )
        except Exception:  # pragma: no cover - never break the loop on a log
            pass

    def _check_listen_timeout(self) -> None:
        deadline = self._listen_deadline
        if deadline is None:
            return
        if time.monotonic() < deadline:
            return
        self._listen_deadline = None
        if not self._segmenter.recording:
            log.debug("nobody spoke after the wake word, back to idle")
            self._enter_idle()

    async def _tick_unload(self) -> None:
        now = time.monotonic()
        if now - self._last_unload_check < 1.0:
            return
        self._last_unload_check = now
        unload = getattr(self._stt, "maybe_unload", None)
        if not callable(unload):
            return
        with suppress(Exception):
            await asyncio.to_thread(unload)

    # ------------------------------------------------------------------ frames
    async def _on_frame(self, frame: np.ndarray) -> None:
        if time.monotonic() < self._mic_mute_until:
            return
        if self._paused or self._closed:
            return
        self._watch_mic_silence(frame)
        current = self._state.state
        passive = self._passive_wake_enabled()
        if passive and current in (IDLE, SPEAKING):
            await self._wake_frame(frame, barge_in=current == SPEAKING)
            return
        if current == IDLE:
            if passive:
                # Keep a passive capture armed: the phrase is transcribed and
                # the wake word is checked in the text (no keyword model needed).
                if not self._busy and not self._segmenter.armed:
                    self._segmenter.arm()
            elif self._wake_hit(frame, barge_in=False):
                self._begin_listening()
        elif self._speaking:
            if passive:
                if not self._busy and not self._segmenter.armed:
                    self._segmenter.arm()
            elif self._wake_hit(frame, barge_in=True):
                await self.stop_speaking()

        listening = self._segmenter.armed and not self._paused
        speech = False
        level = 0.0
        if listening:
            level = rms(frame)
            speech = self._is_speech(frame) or self._energy_speech(level, frame)
        event = self._segmenter.push(frame, level, speech)
        if event.kind == START:
            self._listen_deadline = None
        elif event.kind == END:
            # Stop capturing until the next wake word / listen_now: the turn
            # task runs off the loop, so a still-armed segmenter would happily
            # start a second phrase while STT is busy.
            was_listening = self._state.state == LISTENING
            was_speaking = self._state.state == SPEAKING
            self._segmenter.disarm()
            self._listen_deadline = None
            self._drain_mic()
            audio = await self._prepare_utterance(event.audio)
            if passive and not was_listening:
                self._turn_task = asyncio.create_task(
                    self._run_wake_check(audio, barge_in=was_speaking), name="vox-wake"
                )
            else:
                self._turn_task = asyncio.create_task(self._run_turn(audio), name="vox-turn")
        elif event.kind == DISCARD:
            log.debug("phrase discarded: %.0f ms of speech", event.speech_ms)
            if self._state.state == LISTENING:
                self._enter_idle()
        elif event.kind == CONTINUE:
            pass

    @property
    def last_heard(self) -> str:
        """Last phrase the wake logic actually heard (diagnostics)."""
        return self._last_heard

    def _passive_wake_enabled(self) -> bool:
        return self._wake_mode == "vad_stt" and not self._closed and not self._paused

    async def _wake_frame(self, frame: Any, *, barge_in: bool) -> None:
        """Feed the short wake segmenter and check a finished phrase."""
        if self._busy:
            return
        if not self._wake_segmenter.armed:
            self._wake_segmenter.arm()
        level = rms(frame)
        speech = self._is_speech(frame) or self._energy_speech(level, frame)
        event = self._wake_segmenter.push(frame, level, speech)
        if event.kind == END:
            self._wake_segmenter.disarm()
            self._drain_mic()
            audio = await self._prepare_utterance(event.audio)
            self._turn_task = asyncio.create_task(
                self._run_wake_check(audio, barge_in=barge_in), name="vox-wake"
            )
        elif event.kind == DISCARD:
            self._wake_segmenter.disarm()

    def _wake_stems(self) -> set[str]:
        base = _normalize_word(self._wake_phrase) or "вокс"
        stems = {base, "vox", "вос", "вок", "вокс"}
        stems.update(base + suffix for suffix in ("а", "у", "ом", "е", "ы", "ой"))
        return stems

    def _strip_wake_word(self, text: str) -> str | None:
        """Return the command without the wake word, or None if it is absent.

        Accepts "ассистент, ...", "вокса", "эй вокс", "vox": the check is done on the
        normalised first word, because Whisper writes the wake word in different
        cases and with different punctuation.
        """
        words = [part for part in re.split(r"\s+", str(text or "").strip()) if part]
        if not words:
            return None
        stems = self._wake_stems()

        def is_wake(word: str) -> bool:
            normalized = _normalize_word(word)
            if not normalized:
                return False
            return normalized in stems or any(
                normalized.startswith(stem) for stem in stems if len(stem) >= 3
            )

        fillers = {"эй", "хей", "окей", "слушай", "алло", "так"}
        # "ассистент, открой блокнот", "эй вокс, ...", "открой, вокс, блокнот":
        # whisper sometimes moves the word, so the first three words are checked.
        for index, word in enumerate(words[:3]):
            if is_wake(word):
                rest = [
                    part
                    for position, part in enumerate(words)
                    if position != index
                    and not (position < index and _normalize_word(part) in fillers)
                ]
                return " ".join(rest)
        return None

    def _is_own_echo(self, text: str) -> bool:
        """True when the phrase is a piece of the answer being spoken now."""
        spoken = _normalize_word(self._current_speech)
        heard = _normalize_word(text)
        if not spoken or not heard:
            return False
        return len(heard) >= 4 and heard in spoken

    async def _dispatch_text(self, text: str) -> None:
        """Send a recognised command to the runtime (transcript + callback)."""
        self._emit_safe(transcript_event(text, "voice"))
        try:
            await self._on_utterance(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("on_utterance failed: %s", exc)
            self._emit_safe(error_event(self._no_connection_phrase, "runtime"))

    def _wake_audio_window(self, audio: Any) -> Any:
        """Cut the wake phrase out of the recent ring audio.

        The passive segmenter can run for many seconds when the room has
        continuous noise, and whisper then transcribes the background instead of
        the phrase. The "record 3 seconds" test works because it re-segments the
        audio offline, so the wake path does the same over the last seconds.
        """
        try:
            window = self._ring.samples()
            if window is None or window.size < STT_SAMPLE_RATE // 2:
                return audio
            window = window[-int(WAKE_WINDOW_SEC * STT_SAMPLE_RATE):]
            segment, info, _note = self._segment_offline(window)
            if segment.size and info.get("speech_ms", 0.0) >= MIN_SPEECH_MS:
                return self._with_tail_pad(segment)
            return audio
        except Exception:  # pragma: no cover - never break the wake path
            return audio

    async def _run_wake_check(self, audio: Any, *, barge_in: bool = False) -> None:
        """Transcribe a passively captured phrase and look for the wake word."""
        self._busy = True
        try:
            text = await self._transcribe(audio, strip_wake=False)
            if self._closed:
                return
            if not text:
                log.debug("wake: phrase without text")
                self._enter_idle(after_speech=barge_in)
                return
            self._last_heard = text
            log.info("wake: heard %r", text)
            if barge_in and self._is_own_echo(text):
                # The microphone heard the answer we are playing right now.
                log.debug("wake: ignoring our own speech echo")
                return
            command = self._strip_wake_word(text)
            if command is None:
                log.info("wake: ignored speech without the wake word")
                self._enter_idle(after_speech=barge_in)
                return
            if barge_in:
                await self.stop_speaking()
            command = command.strip(" ,.!?-—")
            if not command:
                # Just the wake word: give the owner the usual 6 s to speak.
                self._emit_safe(transcript_event(text, "voice"))
                self._begin_listening()
                return
            await self._dispatch_text(command)
        finally:
            self._busy = False

    def _wake_hit(self, frame: np.ndarray, *, barge_in: bool) -> bool:
        detector = self._wake
        if detector is None:
            return False
        try:
            return bool(detector.feed(frame, barge_in=barge_in))
        except TypeError:  # a backend fake without the keyword argument
            try:
                return bool(detector.feed(frame))
            except Exception:  # pragma: no cover
                return False
        except Exception:  # pragma: no cover
            log.debug("wake detector failed", exc_info=True)
            return False

    def _energy_speech(self, level: float, frame: Any = None) -> bool:
        """Loudness gate that adapts to the room noise floor.

        A far-field USB microphone is quiet, so level alone is not enough: a fan,
        keyboard or the device's own gate noise is also "loud". Speech is
        required to look like speech: most of its energy sits in the voice band
        and the spectrum is not flat like broadband noise.
        """
        floor = getattr(self, "_noise_floor", 0.0)
        try:
            if level <= 0.0:
                return False
            if floor <= 0.0:
                # Start from the calibration (or a low default): the very first
                # frame may already be speech and must not hide the phrase.
                start = getattr(self, "_energy_min", ENERGY_GATE_MIN_LEVEL) * 0.5
                self._noise_floor = min(level, start)
                return False
            if level >= max(floor * ENERGY_GATE_RATIO, getattr(self, "_energy_min", ENERGY_GATE_MIN_LEVEL)):
                if frame is not None and not self._looks_like_speech(frame):
                    return False
                self._noise_floor = floor * 0.98 + min(level, floor * 2.0) * 0.02
                return True
            # quiet frame: adapt the floor towards it (fast when it is lower)
            weight = 0.25 if level < floor else 0.05
            self._noise_floor = floor * (1.0 - weight) + level * weight
            return False
        except Exception:  # pragma: no cover - never break the loop
            return False

    @staticmethod
    def _looks_like_speech(frame: Any) -> bool:
        """Cheap spectral test that rejects fans, hiss and keyboard clicks.

        Speech keeps 60 %+ of its energy between 150 and 4000 Hz and has a
        spectral flatness clearly below 1 (broadband noise is close to 1). The
        check is skipped for buffers too short to analyse.
        """
        try:
            samples = np.asarray(frame, dtype=np.float32)
            if samples.size < 256:
                return True
            window = np.hanning(samples.size)
            spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
            total = float(spectrum.sum())
            if total <= 0.0:
                return False
            freqs = np.fft.rfftfreq(samples.size, d=1.0 / STT_SAMPLE_RATE)
            voice = spectrum[(freqs >= 150.0) & (freqs <= 4000.0)]
            voice_ratio = float(voice.sum()) / total
            power = spectrum[1:]
            if power.size == 0 or float(power.min()) <= 0.0:
                return voice_ratio >= 0.35
            flatness = float(np.exp(np.mean(np.log(power + 1e-12))) / (np.mean(power) + 1e-12))
            return voice_ratio >= 0.35 and flatness <= 0.65
        except Exception:  # pragma: no cover - never break the loop
            return True

    def _is_speech(self, frame: np.ndarray) -> bool:
        engine = self._vad
        if engine is None:
            return False
        try:
            result = engine.is_speech(frame)
        except Exception:  # pragma: no cover
            log.debug("vad failed", exc_info=True)
            return False
        if isinstance(result, tuple):
            return bool(result[0]) if result else False
        return bool(result)

    def _drain_mic(self) -> None:
        clear = getattr(self._mic, "clear", None)
        if callable(clear):
            with suppress(Exception):
                clear()

    # ------------------------------------------------------------------ turn
    async def _run_turn(self, audio: Any) -> None:
        """STT + transcript + runtime callback, off the frame loop."""
        self._turn += 1
        turn = self._turn
        text = await self._transcribe(audio)
        if self._closed or turn != self._turn:
            return
        if not text:
            log.info("empty transcript, asking to repeat")
            if not self._stt_error:
                # No technical cause was shown yet: report the phrase itself.
                self._emit_safe(error_event(self._stt_failed_phrase, "stt"))
            await self.speak(self._stt_failed_phrase)
            if turn == self._turn and not self._barge_in:
                self._enter_idle(after_speech=True)
            return
        await self._dispatch_text(text)

    def _stt_availability(self) -> tuple[bool, str]:
        """Ask the transcriber whether it can work, without loading the model."""
        checker = getattr(self._stt, "availability", None)
        if not callable(checker):
            return True, ""
        try:
            result = checker()
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"распознавание речи: {type(exc).__name__}: {exc}"
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1] or "")
        return bool(result), ""

    def _report_stt_readiness(self) -> None:
        """Say once, at startup, that speech recognition cannot work (TZ 15)."""
        self._stt_ready, self._stt_ready_error = self._stt_availability()
        if self._stt_ready:
            return
        log.warning("stt is not ready: %s", self._stt_ready_error)
        self._emit_safe(error_event(self._stt_ready_error or "распознавание речи недоступно", "stt"))

    async def _transcribe(
        self,
        audio: Any,
        sample_rate: int = STT_SAMPLE_RATE,
        strip_wake: bool = True,
    ) -> str | None:
        """Run STT; a failure is reported to the UI, never silently dropped."""
        details = await self._transcribe_details(audio, sample_rate, strip_wake=strip_wake)
        error = details.get("error")
        if error:
            message = f"ошибка распознавания: {error}"
            self._stt_error = message
            log.warning("stt failed: %s", error)
            self._emit_safe(error_event(message, "stt"))
            return None
        self._stt_error = None
        return str(details.get("text") or "") or None

    async def _transcribe_details(
        self,
        audio: Any,
        sample_rate: int = STT_SAMPLE_RATE,
        strip_wake: bool = True,
    ) -> dict:
        """Transcribe and keep confidence/timing/peak for diagnostics."""
        result: dict = {
            "text": "",
            "confidence": None,
            "segments": 0,
            "processing_s": 0.0,
            "language": "",
            "peak_dbfs": None,
            "normalized": False,
            "error": None,
        }
        method = getattr(self._stt, "transcribe_details", None)
        try:
            if callable(method):
                try:
                    value = await asyncio.to_thread(
                        method, audio, sample_rate, strip_wake, self._too_quiet_dbfs
                    )
                except TypeError:  # a fake transcriber with the old signature
                    try:
                        value = await asyncio.to_thread(
                            method, audio, sample_rate, strip_wake
                        )
                    except TypeError:
                        value = await asyncio.to_thread(method, audio, sample_rate)
                if isinstance(value, dict):
                    result.update(value)
            else:
                transcribe = getattr(self._stt, "transcribe", None)
                if not callable(transcribe):
                    result["error"] = "распознавание речи недоступно"
                    return result
                started = time.monotonic()
                text = await asyncio.to_thread(transcribe, audio)
                result["text"] = str(text or "")
                result["segments"] = 1 if text else 0
                result["processing_s"] = time.monotonic() - started
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        self._last_stt_confidence = result.get("confidence")
        self._last_stt_processing = float(result.get("processing_s") or 0.0)
        self._last_stt_peak_db = result.get("peak_dbfs")
        self._last_stt_normalized = bool(result.get("normalized", False))
        return result

    # ------------------------------------------------------------------ utterances
    def _with_tail_pad(self, audio: Any) -> np.ndarray:
        """Append vad.tail_pad_ms of silence before STT (TZ 5.2, tail clipping)."""
        data = to_int16_mono(audio)
        count = int(round(max(0.0, float(self._tail_pad_ms)) / 1000.0 * STT_SAMPLE_RATE))
        if count <= 0 or data.size == 0:
            return data
        return np.concatenate([data, np.zeros(count, dtype=np.int16)])

    async def _prepare_utterance(self, audio: Any) -> np.ndarray:
        """Pad the segment, log its levels and store the debug WAVs."""
        padded = self._with_tail_pad(audio)
        duration = padded.size / float(STT_SAMPLE_RATE)
        peak = float(np.abs(padded).max()) / 32768.0 if padded.size else 0.0
        log.info(
            "utterance: %.2f s (%.0f ms), rms=%.4f, peak=%.3f, rate=%d Hz",
            duration,
            duration * 1000.0,
            rms(padded) if padded.size else 0.0,
            peak,
            STT_SAMPLE_RATE,
        )
        if not self._debug_save:
            return padded
        try:
            path = await asyncio.to_thread(self._write_wav, padded, "debug_mic.wav")
            raw = self._ring.samples()
            raw_path = await asyncio.to_thread(self._write_wav, raw, "debug_mic_raw.wav")
            self._last_debug_wav = str(path)
            self._raw_debug_wav = str(raw_path)
            log.info(
                "debug audio: %s (%.2f s), raw %s (%.2f s)",
                path,
                duration,
                raw_path,
                raw.size / float(STT_SAMPLE_RATE),
            )
        except Exception as exc:  # pragma: no cover - disk problems
            log.warning("cannot save debug audio: %s", exc)
        return padded

    def _write_wav(self, data: Any, name: str) -> Any:
        """Write 16 kHz mono int16 PCM into logs/<name> and return the path."""
        path = paths.logs_dir() / name
        path.write_bytes(to_wav_bytes(data, STT_SAMPLE_RATE))
        return path

    def _segment_offline(self, audio: np.ndarray) -> tuple[np.ndarray, dict, str]:
        """Run the live VAD rules over a finished recording (debug screen)."""
        info = {"start_s": 0.0, "end_s": 0.0, "speech_ms": 0.0}
        note = ""
        engine = None
        try:
            engine = self._vad_factory()
        except Exception as exc:  # pragma: no cover - defensive
            engine = None
            note = f"VAD недоступен: {type(exc).__name__}"
        captured = None
        start_index = None
        try:
            segmenter = UtteranceSegmenter(
                silence_ms=float(self._config.get("vad.silence_ms", 800)),
                max_utterance_sec=float(self._config.get("vad.max_utterance_sec", 12)),
                min_speech_ms=float(self._config.get("vad.min_speech_ms", MIN_SPEECH_MS)),
                pre_buffer_ms=float(self._config.get("vad.pre_buffer_ms", 700)),
            )
            segmenter.arm()
            frame_size = 512
            cursor = 0
            for offset in range(0, max(0, audio.size - frame_size + 1), frame_size):
                frame = audio[offset : offset + frame_size]
                speech = False
                if engine is not None:
                    try:
                        verdict = engine.is_speech(frame)
                        speech = bool(verdict[0]) if isinstance(verdict, tuple) else bool(verdict)
                    except Exception:  # pragma: no cover
                        speech = False
                cursor = offset + frame_size
                event = segmenter.push(frame, rms(frame), speech)
                if event.kind == START and start_index is None:
                    start_index = offset
                elif event.kind == END:
                    captured = event.audio
                    info["start_s"] = round((start_index or 0) / float(STT_SAMPLE_RATE), 2)
                    info["end_s"] = round(cursor / float(STT_SAMPLE_RATE), 2)
                    info["speech_ms"] = round(event.speech_ms, 0)
                    break
            if captured is None and segmenter.recording:
                silence = np.zeros(frame_size, dtype=np.int16)
                limit = int(max(float(self._config.get("vad.silence_ms", 800)), 300) / 32.0) + 2
                for _ in range(limit):
                    event = segmenter.push(silence, 0.0, False)
                    cursor += frame_size
                    if event.kind == END:
                        captured = event.audio
                        info["start_s"] = round((start_index or 0) / float(STT_SAMPLE_RATE), 2)
                        info["end_s"] = round(min(audio.size, cursor) / float(STT_SAMPLE_RATE), 2)
                        info["speech_ms"] = round(event.speech_ms, 0)
                        break
                    if event.kind == DISCARD:
                        break
        except Exception as exc:  # pragma: no cover - defensive
            note = f"VAD не смог разобрать запись: {type(exc).__name__}"
        finally:
            closer = getattr(engine, "close", None)
            if callable(closer):
                with suppress(Exception):
                    closer()
        if captured is None or captured.size == 0:
            return audio, info, note or "VAD не нашёл речь, распознавал всю запись"
        return captured, info, note

    def _live_capture_ready(self) -> bool:
        """True when the running loop already owns the microphone."""
        return bool(
            self._running
            and not self._closed
            and not self._paused
            and self._mic is not None
            and self._mic_ok
        )

    async def _ring_window(self, seconds: float) -> np.ndarray:
        """Wait for fresh audio and return the last N seconds from the ring.

        A second InputStream on the same device makes many Windows drivers
        deliver digital silence to both consumers, so the diagnostics must reuse
        the stream the assistant is already listening on (owner report
        2026-10-28: the test recorded -90 dBFS silence while the mic was busy).
        """
        deadline = time.monotonic() + max(0.3, float(seconds))
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        samples = np.asarray(self._ring.samples(), dtype=np.int16)
        want = int(STT_SAMPLE_RATE * max(0.3, float(seconds)))
        if samples.size > want:
            samples = samples[-want:]
        return np.ascontiguousarray(samples)

    @staticmethod
    def _is_digital_silence(audio: np.ndarray) -> bool:
        if audio.size == 0:
            return True
        peak = float(np.abs(audio.astype(np.int32)).max()) / 32768.0
        zeros = float(np.mean(audio == 0))
        return peak < 1e-4 or zeros > 0.6

    @staticmethod
    def _silence_message() -> str:
        return (
            "микрофон вернул тишину: проверьте, что выбрано правильное устройство "
            "и оно не занято другой программой"
        )

    async def record_and_recognize(self, seconds: float = 3.0) -> dict:
        """Record from the live microphone and run the full recognition path.

        Used by the "тест распознавания" button: it records, resamples to 16 kHz,
        segments with the same VAD rules as the loop, applies the tail pad and
        transcribes with the same model, always leaving debug_mic.wav behind.
        """
        result: dict = {
            "ok": False,
            "text": "",
            "confidence": None,
            "processing_s": 0.0,
            "wav_path": "",
            "message": "",
            "vad": {"start_s": 0.0, "end_s": 0.0, "speech_ms": 0.0},
            "mic": {"device": "", "samplerate": STT_SAMPLE_RATE},
            "peak_dbfs": None,
            "normalized": False,
        }
        source_rate = STT_SAMPLE_RATE
        if self._live_capture_ready():
            # Reuse the assistant's own microphone: no second stream.
            audio = await self._ring_window(seconds)
            result["mic"] = {
                "device": str(getattr(self._mic, "device_name", "") or self._input_device or "default"),
                "samplerate": int(getattr(self._mic, "capture_samplerate", STT_SAMPLE_RATE) or STT_SAMPLE_RATE),
            }
            if audio.size == 0:
                result["message"] = "микрофон не прислал данных"
                return result
        else:
            try:
                device = self._resolve_test_device(None, "input")
                probe = self._input_probe_factory(device)
            except Exception as exc:
                result["message"] = f"микрофон недоступен: {type(exc).__name__}: {exc}"
                return result
            try:
                await asyncio.to_thread(probe.start)
            except Exception as exc:
                result["message"] = f"микрофон недоступен: {type(exc).__name__}: {exc}"
                return result
            frames: list[np.ndarray] = []
            try:
                deadline = time.monotonic() + max(0.3, float(seconds))
                while time.monotonic() < deadline:
                    frame = await asyncio.to_thread(probe.read, 0.1)
                    if frame is None:
                        continue
                    data = np.asarray(frame, dtype=np.int16)
                    if data.size:
                        frames.append(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result["message"] = f"ошибка чтения с микрофона: {type(exc).__name__}: {exc}"
                return result
            finally:
                with suppress(Exception):
                    await asyncio.to_thread(probe.close)
            if not frames:
                result["message"] = "микрофон не прислал данных"
                return result
            audio = np.concatenate(frames)
            source_rate = int(getattr(probe, "samplerate", STT_SAMPLE_RATE) or STT_SAMPLE_RATE)
            if source_rate != STT_SAMPLE_RATE:
                audio = resample_int16(audio, source_rate, STT_SAMPLE_RATE)
            result["mic"] = {
                "device": str(getattr(probe, "device_name", "") or self._input_device or "default"),
                "samplerate": source_rate,
            }
        result["mic"]["silent"] = self._is_digital_silence(audio)
        segment, vad_info, note = self._segment_offline(audio)
        result["vad"] = vad_info
        padded = self._with_tail_pad(segment)
        try:
            path = await asyncio.to_thread(self._write_wav, padded, "debug_mic.wav")
            result["wav_path"] = str(path)
            self._last_debug_wav = str(path)
            raw_path = await asyncio.to_thread(self._write_wav, audio, "debug_mic_raw.wav")
            self._raw_debug_wav = str(raw_path)
        except Exception as exc:  # pragma: no cover - disk problems
            result["message"] = f"не удалось сохранить запись: {exc}"
            return result

        if result["mic"].get("silent"):
            # Nothing to recognise: say why instead of burning 2-3 s of CPU on
            # a silent buffer (and instead of pretending it was "not understood").
            result["ok"] = True
            result["message"] = self._silence_message()
            return result

        details = await self._transcribe_details(padded, STT_SAMPLE_RATE)
        result["text"] = str(details.get("text") or "")
        result["confidence"] = details.get("confidence")
        result["processing_s"] = float(details.get("processing_s") or 0.0)
        result["peak_dbfs"] = details.get("peak_dbfs")
        result["normalized"] = bool(details.get("normalized", False))
        if details.get("error"):
            result["message"] = f"STT упал: {details['error']}"
            return result
        result["ok"] = True
        if result["mic"].get("silent"):
            result["message"] = self._silence_message()
            return result
        if not result["text"]:
            result["message"] = "запись сделана, но речь не распознана" + (f" ({note})" if note else "")
        else:
            confidence = result["confidence"]
            suffix = f", уверенность {confidence:.2f}" if isinstance(confidence, float) else ""
            result["message"] = f"распознано за {result['processing_s']:.1f} с{suffix}"
        return result

    # ------------------------------------------------------------------ speech
    async def speak(self, text: str) -> None:
        """Synthesize and play an answer; returns when done or interrupted."""
        if self._closed or self._paused:
            return
        value = str(text or "").strip()
        if not value:
            return
        self._barge_in = False
        self._speaking = True
        self._current_speech = value
        self._set_state(SPEAKING)
        ok = False
        try:
            ok = await self._speaker.speak(value)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("tts failed: %s", exc)
            ok = False
        finally:
            self._speaking = False
            self._current_speech = ""
        if not ok:
            self._emit_safe(error_event(self._tts_failure_message(), "tts"))
        if not self._barge_in:
            self._mic_mute_until = time.monotonic() + MIC_MUTE_AFTER_SPEECH_SEC

    async def stop_speaking(self) -> None:
        """Barge-in (TZ 2.6): stop playback now and switch to listening."""
        self._barge_in = True
        with suppress(Exception):
            self._speaker.stop()
        if not self._closed and not self._paused:
            self._begin_listening()

    # ------------------------------------------------------------------ tts extras
    async def preview(self, voice: str, text: str = "") -> tuple[bool, str]:
        """Play a short sample and report what really happened (POST /tts/preview)."""
        if self._closed:
            return False, "озвучка недоступна"
        try:
            result = await self._speaker.preview(voice, text or PREVIEW_TEXT)
        except Exception as exc:  # pragma: no cover - never break the HTTP route
            log.warning("tts preview failed: %s", exc)
            return False, f"не удалось воспроизвести: {exc}"
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1] or "")
        return bool(result), ""

    def _tts_failure_message(self) -> str:
        """Real reason for the UI: no device, edge error, SAPI error."""
        result = getattr(self._speaker, "last_result", None)
        if callable(result):
            try:
                message = str(result().get("message") or "").strip()
            except Exception:  # pragma: no cover
                message = ""
            if message:
                return message
        return self._no_connection_phrase

    async def list_voices(self) -> list[dict]:
        """Russian edge-tts voices for the settings UI (GET /voices)."""
        if self._closed:
            return []
        try:
            return await self._speaker.list_voices()
        except Exception:  # pragma: no cover - never break the HTTP route
            log.info("cannot list voices", exc_info=True)
            return []

    # ------------------------------------------------------------------ models
    async def ensure_models(self, stage: str = "all") -> bool:
        """Download the models a stage needs, with download_progress events."""
        if self._closed:
            return False
        if self._downloader is None:
            self._downloader = ModelDownloader(self._config, self._emit)
        previous = self._state.state
        self._set_state(DOWNLOADING)
        try:
            ok = await self._downloader.ensure_models(stage)
        except Exception as exc:  # pragma: no cover - downloader reports its own errors
            log.warning("model download failed: %s", exc)
            ok = False
        if self._closed:
            return ok
        if previous == PAUSED:
            self._set_state(PAUSED)
        else:
            self._set_state(IDLE)
        return ok

    async def ensure_local_voice(self) -> bool:
        """Download the local neural voice (~63 MB) with progress events."""
        if self._closed:
            return False
        ensure = getattr(self._speaker, "ensure_local_voice", None)
        if not callable(ensure):
            return False
        return bool(await ensure())

    # ------------------------------------------------------------------ audio devices
    async def list_devices(self) -> dict:
        """Input/output devices for the settings panel (GET /audio/devices)."""
        try:
            return await asyncio.to_thread(list_devices)
        except Exception:  # pragma: no cover - never break the HTTP route
            log.info("cannot enumerate audio devices", exc_info=True)
            return {"input": [], "output": []}

    def playback_status(self) -> dict:
        """Which playback path works right now, for the settings panel."""
        backend, message = "none", ""
        try:
            backend, message = output_backend()
        except Exception as exc:  # pragma: no cover - defensive
            message = f"не удалось проверить вывод: {exc}"
        return {"backend": backend, "ok": backend != "none", "message": message}

    async def test_output(self, device: str | int | None = None) -> tuple[bool, str]:
        """Play a short phrase on the given device and report the real result."""
        if self._closed:
            return False, "озвучка недоступна"
        resolved = self._resolve_test_device(device, "output")
        place = self._device_label(resolved, "output")
        probe = None
        try:
            probe = self._output_probe_factory(resolved)
            result = await probe.preview(self._preview_voice(), TEST_OUTPUT_PHRASE)
        except Exception as exc:
            log.warning("output test failed: %s", exc)
            return False, f"проверка звука не удалась: {exc}"
        finally:
            if probe is not None:
                close = getattr(probe, "close", None)
                if callable(close):
                    with suppress(Exception):
                        close()
        if isinstance(result, tuple) and len(result) == 2:
            ok, message = bool(result[0]), str(result[1] or "")
        else:  # a probe that only reports success
            ok, message = bool(result), ""
        if ok:
            return True, f"{message}; устройство: {place}" if message else f"звук есть; устройство: {place}"
        return False, message or "нет звука"

    async def test_input(
        self,
        device: str | int | None = None,
        seconds: float = 2.0,
    ) -> tuple[bool, str, float]:
        """Record a short sample and report the peak level (POST /audio/test)."""
        if self._closed:
            return False, "микрофон недоступен", 0.0
        if device is None and self._live_capture_ready():
            # No second InputStream: many drivers silence both consumers.
            audio = await self._ring_window(seconds)
            if audio.size == 0:
                return False, "микрофон не прислал данных", 0.0
            if self._is_digital_silence(audio):
                return False, self._silence_message(), 0.0
            peak = float(np.abs(audio.astype(np.int32)).max()) / 32768.0
            level = round(min(1.0, max(0.0, peak)), 3)
            if level < SILENCE_LEVEL:
                return True, "тишина", level
            return True, f"микрофон работает, уровень {level * 100:.0f}%", level
        resolved = self._resolve_test_device(device, "input")
        probe = None
        try:
            probe = self._input_probe_factory(resolved)
            await asyncio.to_thread(probe.start)
        except Exception as exc:
            log.warning("input test cannot open the microphone: %s", exc)
            return False, "микрофон недоступен", 0.0
        peak = 0.0
        frames = 0
        try:
            deadline = time.monotonic() + max(0.2, float(seconds))
            while time.monotonic() < deadline:
                frame = await asyncio.to_thread(probe.read, 0.1)
                if frame is None:
                    continue
                frames += 1
                data = np.asarray(frame)
                if data.size:
                    peak = max(peak, float(np.abs(data).max()) / 32768.0)
        except Exception as exc:
            log.warning("input test read failed: %s", exc)
            return False, "ошибка чтения с микрофона", 0.0
        finally:
            with suppress(Exception):
                await asyncio.to_thread(probe.close)
        level = round(min(1.0, max(0.0, peak)), 3)
        if frames == 0:
            return False, "микрофон не прислал данных", 0.0
        if level < SILENCE_LEVEL:
            return True, "тишина", level
        return True, f"микрофон работает, уровень {level * 100:.0f}%", level

    def _resolve_test_device(self, device: str | int | None, kind: str) -> int | None:
        """None in the request means "whatever the config currently uses"."""
        if device is not None:
            return resolve_device(device, kind)
        try:
            value = self._config.get(f"audio.{kind}_device", "default")
        except Exception:  # pragma: no cover
            value = "default"
        return resolve_device(value, kind)

    def _device_label(self, resolved: int | None, kind: str) -> str:
        try:
            devices = list_devices().get(kind, [])
        except Exception:  # pragma: no cover
            devices = []
        if resolved is None:
            for device in devices:
                if device.get("default"):
                    return f"системное по умолчанию ({device.get('name', '')})"
            return "системное по умолчанию"
        for device in devices:
            try:
                if int(device.get("index", -1)) == int(resolved):
                    return str(device.get("name") or f"устройство {resolved}")
            except (TypeError, ValueError):  # pragma: no cover
                continue
        return f"устройство {resolved}"

    def _resolved_input_label(self) -> str:
        """Human label for the input device the config actually resolves to."""
        index = getattr(self._mic, "device_index", None)
        if index is None:
            try:
                from .audio import resolve_input_device_info

                index, _note = resolve_input_device_info(self._config)
            except Exception:  # pragma: no cover - defensive
                index = None
        return f"index:{index}" if index is not None else "системное по умолчанию"

    def _preview_voice(self) -> str:
        voice = getattr(self._speaker, "voice", "")
        return str(voice or "")

    def _make_output_probe(self, device: int | None) -> Any:
        return Speaker(self._config, player=Mp3Player(device=device))

    def _make_input_probe(self, device: int | None) -> Any:
        return SoundDeviceMic(device=device)

    def _rebuild_speaker(self) -> None:
        """Recreate the player after audio.output_device changed (TZ 11)."""
        if self._speaker_injected or self._closed:
            return
        previous = self._speaker
        try:
            self._speaker = Speaker(self._config, emit=self._emit_safe)
        except Exception:  # pragma: no cover - keep the old one working
            log.warning("cannot rebuild the speaker", exc_info=True)
            self._speaker = previous
            return
        close = getattr(previous, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

    async def test_stt(self) -> tuple[bool, str, str]:
        """Self-test of the real recognition chain: SAPI speaks, whisper hears.

        The phrase is rendered offline (no network), resampled to 16 kHz mono and
        pushed through the very same transcriber the listening loop uses, so a
        failure here is the same failure the owner hears as "ничего не понял".
        """
        sapi = self._sapi_probe()
        try:
            available = bool(await asyncio.to_thread(sapi.available))
        except Exception as exc:
            return False, f"нет офлайн-синтеза для самотеста: {type(exc).__name__}: {exc}", ""
        if not available:
            return False, "нет офлайн-синтеза для самотеста: SAPI недоступен", ""
        voice = ""
        try:
            voice = str(await asyncio.to_thread(sapi.preferred_voice) or "")
        except Exception:  # pragma: no cover - voice selection is optional
            voice = ""
        try:
            wav = await asyncio.to_thread(sapi.synthesize, TEST_STT_PHRASE, voice)
        except Exception as exc:
            return False, f"нет офлайн-синтеза для самотеста: {type(exc).__name__}: {exc}", ""
        try:
            samples, rate = read_wav_bytes(wav)
            pcm = resample_int16(samples, rate, STT_SAMPLE_RATE)
        except Exception as exc:
            return False, f"не удалось подготовить запись для самотеста: {type(exc).__name__}: {exc}", ""
        details = await self._transcribe_details(pcm, STT_SAMPLE_RATE)
        if details.get("error"):
            message = str(details["error"])
            self._stt_error = message
            log.warning("stt self-test failed: %s", message)
            return False, f"STT упал: {message}", ""
        text = str(details.get("text") or "")
        if not text:
            self._stt_error = "STT вернул пустую строку"
            return False, "STT вернул пустую строку (модель не слышит синтезированную запись)", ""
        self._stt_error = None
        took = float(details.get("processing_s") or 0.0)
        model = str(getattr(self._stt, "model_name", "") or "")
        label = f"{model}, {took:.1f} с" if model else f"{took:.1f} с"
        return True, f"распознано ({label}): «{text}»", str(text)

    def _sapi_probe(self) -> Any:
        """SAPI instance for the self-test: the speaker's own, or a fresh one."""
        if not self._speaker_injected:
            existing = getattr(self._speaker, "sapi", None)
            if existing is not None:
                return existing
        return self._sapi_probe_factory()

    def diagnostics(self) -> dict:
        """Read-only health snapshot for the UI: no model load, never raises."""
        result: dict[str, Any] = {}

        try:
            result["state"] = str(self._state.state)
        except Exception:  # pragma: no cover - defensive
            result["state"] = ""

        try:
            mic = self._mic
            device = str(getattr(mic, "device_name", "") or "")
            result["mic"] = {
                "ok": bool(self._mic_ok),
                "device": device or str(self._input_device or "default"),
                "error": self._mic_error,
                "requested": str(self._input_device or "default"),
                "resolved": device or self._resolved_input_label(),
                "samplerate": int(getattr(mic, "capture_samplerate", STT_SAMPLE_RATE) or STT_SAMPLE_RATE),
                "native_samplerate": int(getattr(mic, "native_samplerate", 0) or 0),
                "resampled": bool(getattr(mic, "resampled", False)),
                "peak_dbfs": getattr(self, "_last_input_peak_db", None),
                "rms_dbfs": getattr(self, "_last_input_rms_db", None),
                # Freshness of the audio, not just its level: a stream can be
                # "ok" with peak -90 dBFS either because the room is silent or
                # because it stopped delivering frames at all.
                "last_frame_age_s": self._frame_age_seconds(),
                "starved_reopens": getattr(self, "_mic_starve_reopens", 0),
            }
        except Exception as exc:  # pragma: no cover
            result["mic"] = {"ok": False, "device": "", "error": str(exc)}

        try:
            vad = self._vad
            result["vad"] = {
                "type": str(getattr(vad, "name", "") or type(vad).__name__) if vad is not None else "none",
                "ready": vad is not None,
            }
        except Exception as exc:  # pragma: no cover
            result["vad"] = {"type": "", "ready": False, "error": str(exc)}
        try:
            wake = self._wake
            detector_engine = str(getattr(wake, "engine", "none")) if wake is not None else "none"
            engine = "vad_stt" if self._wake_mode == "vad_stt" else (detector_engine or "none")
            heard = self._last_heard or str(getattr(wake, "last_heard", "") or "")
            result["wake"] = {
                "engine": engine,
                "ready": engine in {"vad_stt", "vosk"} and (engine == "vad_stt" or detector_engine not in ("", "none")),
                "threshold": float(getattr(wake, "threshold", 0.0) or 0.0),
                "last_heard": heard,
            }
        except Exception as exc:  # pragma: no cover
            result["wake"] = {"engine": "none", "ready": False, "error": str(exc)}

        try:
            ready, reason = self._stt_availability()
            model_name = str(getattr(self._stt, "model_name", "") or "")
            model_path = getattr(self._stt, "model_path", None)
            result["stt"] = {
                "ready": bool(ready),
                "model": model_name,
                "loaded": bool(getattr(self._stt, "loaded", False)),
                "error": self._stt_error or (None if ready else reason),
                "model_path": str(model_path) if model_path else "",
                "model_size_mb": _folder_size_mb(model_path),
                "last_confidence": self._last_stt_confidence,
                "last_processing_s": round(self._last_stt_processing, 3),
                "last_peak_db": self._last_stt_peak_db,
                "normalized": self._last_stt_normalized,
            }
        except Exception as exc:  # pragma: no cover
            result["stt"] = {"ready": False, "model": "", "loaded": False, "error": str(exc)}

        try:
            backend = "none"
            try:
                backend = str(output_backend()[0])
            except Exception:  # pragma: no cover
                backend = "none"
            last: dict = {}
            reader = getattr(self._speaker, "last_result", None)
            if callable(reader):
                try:
                    last = dict(reader() or {})
                except Exception:  # pragma: no cover
                    last = {}
            local: dict = {}
            status = getattr(self._speaker, "local_voice_status", None)
            if callable(status):
                try:
                    local = dict(status() or {})
                except Exception:  # pragma: no cover
                    local = {}
            result["tts"] = {
                "engine": str(getattr(self._speaker, "engine_mode", "") or ""),
                "backend": backend,
                "last": last,
                "local": local,
            }
        except Exception as exc:  # pragma: no cover
            result["tts"] = {"engine": "", "backend": "none", "last": {"error": str(exc)}, "local": {}}

        try:
            counter = getattr(self._speaker, "cached_voices", None)
            voices = counter() if callable(counter) else []
            result["voices"] = {"count": len(voices or [])}
        except Exception:  # pragma: no cover
            result["voices"] = {"count": 0}

        try:
            result["debug"] = {
                "save_audio": bool(self._debug_save),
                "last_wav": str(self._last_debug_wav or ""),
                "raw_wav": str(self._raw_debug_wav or ""),
                "tail_pad_ms": float(self._tail_pad_ms),
                "raw_seconds": float(self._debug_raw_seconds),
            }
        except Exception:  # pragma: no cover
            result["debug"] = {"save_audio": False, "last_wav": "", "raw_wav": ""}

        try:
            result["models_dir"] = str(paths.models_dir())
        except Exception:  # pragma: no cover
            result["models_dir"] = ""

        try:
            downloader = self._downloader
            if downloader is not None:
                result["download"] = {
                    "whisper": bool(downloader.whisper_ready()),
                    "vosk": bool(downloader.vosk_ready()),
                    "vad": bool(downloader.vad_ready()),
                }
        except Exception as exc:  # pragma: no cover
            result["download"] = {"error": str(exc)}

        return result

    # ------------------------------------------------------------------ testing hooks
    def use_components(
        self,
        *,
        mic: Any = None,
        vad: Any = None,
        wake: Any = None,
        stt: Any = None,
        tts: Any = None,
        downloader: Any = None,
        output_probe: Any = None,
        input_probe: Any = None,
        sapi_probe: Any = None,
    ) -> None:
        """Swap hardware/engines (tests, alternate backends). Call before start()."""
        if mic is not None:
            self._mic = mic
            self._mic_ok = False
        if vad is not None:
            self._vad = vad
        if wake is not None:
            self._wake = wake
            self._wake_injected = True
        if stt is not None:
            self._stt = stt
        if tts is not None:
            self._speaker = tts
            self._speaker_injected = True
        if downloader is not None:
            self._downloader = downloader
        if output_probe is not None:
            self._output_probe_factory = output_probe
        if input_probe is not None:
            self._input_probe_factory = input_probe
        if sapi_probe is not None:
            self._sapi_probe_factory = sapi_probe


__all__ = [
    "EMIT_STATE_EVENTS",
    "MIC_MUTE_AFTER_SPEECH_SEC",
    "MIC_STARVE_SEC",
    "MIN_SPEECH_MS",
    "READ_TIMEOUT",
    "STT_SAMPLE_RATE",
    "TEST_STT_PHRASE",
    "VoicePipeline",
]

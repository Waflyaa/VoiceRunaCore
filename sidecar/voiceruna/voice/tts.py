"""Text to speech with edge-tts (TZ 5.4, 5.5).

Playback path, chosen once and documented here (TZ allows one decision):

    edge-tts -> MP3 bytes (never written to disk)
             -> miniaudio.decode (in-memory MP3 -> int16 PCM, ~1.5 MB wheel)
             -> sounddevice.OutputStream in 1024-sample chunks.

    When PortAudio cannot open an output (no default device, or WDM-KS only:
    "Blocking API not supported yet") the same PCM is wrapped into RIFF/WAV and
    played with winsound on the system default device. If winsound has no
    device either, the MP3 is written to a temp file and played through the
    Windows MCI API (winmm mciSendStringW). Every import stays lazy.

    Synthesis has its own fallback: when edge-tts fails (offline, or the
    NoAudioReceived glitch it sometimes returns for short phrases) the text is
    spoken by Windows SAPI (System.Speech through PowerShell, Russian
    "Microsoft Irina Desktop"), so the assistant still has a voice offline.
    tts.engine: "auto" (edge, then SAPI) | "edge" (edge only) | "sapi".

Streaming: the answer is split into sentences, synthesis of sentence N+1 runs
while sentence N is on the speakers, so the first sound arrives in well under
the 1.5 s budget of TZ 5.5. Stopping is immediate: stop() aborts the output
stream from any thread and speak() returns early.

Failure policy (TZ 15): a network or engine error never raises; speak() returns
False and the pipeline keeps the answer on screen only.

Tests inject synthesize= (async callable -> MP3 bytes) and player= (an object
with play(mp3)/stop()/close()).
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from .. import paths
from .audio import (
    AudioOutput,
    AudioUnavailable,
    WavePlayer,
    output_backend,
    resolve_output_device,
    to_wav_bytes,
)
from .piper_tts import PiperTts, local_voices

DEFAULT_VOICE = "ru-RU-DmitryNeural"
DEFAULT_RATE = "+10%"
#: Long enough for natural prosody, short enough to start playing quickly.
MAX_SENTENCE_CHARS = 220
PREVIEW_TEXT = "Привет, я ассистент"
#: How long edge-tts is skipped after a failed attempt (offline / blocked host).
EDGE_COOLDOWN_SEC = 90.0
OUTPUT_SAMPLE_RATE = 24000

log = logging.getLogger(__name__)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:—])\s+")

_RU_NAMES = {
    "Dmitry": "Дмитрий",
    "Svetlana": "Светлана",
    "Dariya": "Дария",
    "DariyaNeural": "Дария",
    "Ekaterina": "Екатерина",
    "Irina": "Ирина",
    "Pavel": "Павел",
    "SvetlanaNeural": "Светлана",
}
_GENDER_LABELS = {"female": "женский", "male": "мужской"}


# --------------------------------------------------------------------------- text
def truncate_text(text: str, max_chars: int = 0) -> str:
    """Apply tts.max_chars; 0 (the default) means "no truncation" (TZ 5.4)."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not max_chars or int(max_chars) <= 0 or len(cleaned) <= int(max_chars):
        return cleaned
    limit = int(max_chars)
    cut = cleaned.rfind(" ", 0, limit + 1)
    if cut <= 0:
        cut = limit
    return cleaned[:cut].rstrip(" ,;:—-") + "…"


def split_sentences(text: str, *, max_len: int = MAX_SENTENCE_CHARS) -> list[str]:
    """Split an answer into speakable pieces, keeping punctuation."""
    result: list[str] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for sentence in _SENTENCE_SPLIT.split(line):
            sentence = sentence.strip()
            if sentence:
                result.extend(_split_long(sentence, max_len))
    return result


def _split_long(chunk: str, max_len: int) -> list[str]:
    if len(chunk) <= max_len:
        return [chunk]
    pieces: list[str] = []
    current = ""
    for part in _CLAUSE_SPLIT.split(chunk):
        part = part.strip()
        if not part:
            continue
        if current and len(current) + 1 + len(part) > max_len:
            pieces.append(current)
            current = part
        else:
            current = part if not current else current + " " + part
    if current:
        pieces.append(current)
    final: list[str] = []
    for piece in pieces:
        while len(piece) > max_len:
            cut = piece.rfind(" ", 0, max_len)
            if cut <= 0:
                cut = max_len
            final.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            final.append(piece)
    return final


def voice_label(name: str, gender: str | None = None) -> str:
    """Human label for a voice short name (settings UI, TZ 11)."""
    short = str(name or "").strip()
    if not short:
        return ""
    tail = short.split("-")[-1]
    for suffix in ("Neural", "Multilingual"):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
    base = _RU_NAMES.get(tail, _RU_NAMES.get(tail + "Neural", tail))
    label = _GENDER_LABELS.get(str(gender or "").lower())
    return f"{base} ({label})" if label else base


# --------------------------------------------------------------------------- audio plumbing
#: Shared across Speaker instances: "edge-tts is down until" (monotonic seconds).
_EDGE_STATE = {"down_until": 0.0}
_EDGE_LOCK = threading.Lock()


def edge_available() -> bool:
    with _EDGE_LOCK:
        return time.monotonic() >= _EDGE_STATE["down_until"]


def note_edge(ok: bool) -> None:
    """Remember whether the last edge-tts attempt worked (machine-wide)."""
    with _EDGE_LOCK:
        if ok:
            _EDGE_STATE["down_until"] = 0.0
        else:
            _EDGE_STATE["down_until"] = time.monotonic() + EDGE_COOLDOWN_SEC
    if not ok:
        log.info("edge-tts unavailable, using offline SAPI for %.0f s", EDGE_COOLDOWN_SEC)


def reset_edge_cooldown() -> None:
    """For tests and for a manual re-check from the settings window."""
    with _EDGE_LOCK:
        _EDGE_STATE["down_until"] = 0.0


def _decode_wav(wav: bytes) -> tuple[np.ndarray, int]:
    """Decode a RIFF/WAV payload to int16 mono PCM (stdlib only)."""
    with wave.open(io.BytesIO(wav), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise AudioUnavailable(f"неподдерживаемая разрядность WAV: {width * 8} бит")
    samples = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return samples.copy(), int(rate)


class _DecodeUnavailable(RuntimeError):
    """No MP3 decoder available in this environment."""


def decode_mp3(mp3: bytes) -> tuple[np.ndarray, int]:
    """Decode MP3 bytes to int16 mono PCM with miniaudio."""
    try:
        import miniaudio
    except Exception as exc:  # pragma: no cover - optional wheel
        raise _DecodeUnavailable(f"miniaudio is not installed: {exc}") from exc
    decoded = miniaudio.decode(
        mp3,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=OUTPUT_SAMPLE_RATE,
    )
    samples = np.frombuffer(decoded.samples, dtype=np.int16).copy()
    return samples, int(decoded.sample_rate)


class Mp3Player:
    """Plays MP3 payloads through the first output path that works.

    Order (TZ 5.4, TZ 15):
        1. miniaudio.decode -> AudioOutput(device)  (PortAudio, chunked, stoppable)
        2. the same PCM packed as WAV -> winsound   (system default device)
        3. the raw MP3 -> Windows MCI               (decodes MP3 natively)
    When none of them can open a device the player raises AudioUnavailable with
    every reason it collected, so the UI can say what actually happened instead
    of silently doing nothing.
    """

    def __init__(
        self,
        *,
        output: AudioOutput | None = None,
        device: int | None = None,
        allow_mci: bool = True,
        wave_player: Any | None = None,
    ) -> None:
        self._device = device
        self._output = output if output is not None else AudioOutput(device)
        self._allow_mci = allow_mci
        self._wave_player = wave_player
        self._mci: _MciPlayer | None = None
        self._lock = threading.Lock()

    @property
    def device(self) -> int | None:
        return self._device

    def _wave(self) -> Any:
        if self._wave_player is None:
            self._wave_player = WavePlayer()
        return self._wave_player

    def play(self, mp3: bytes) -> None:
        if not mp3:
            return
        reasons: list[str] = []
        samples: np.ndarray | None = None
        rate = OUTPUT_SAMPLE_RATE
        try:
            samples, rate = decode_mp3(mp3)
        except _DecodeUnavailable as exc:
            reasons.append(str(exc))
        except Exception as exc:  # pragma: no cover - broken wheel
            reasons.append(f"mp3 \u0434\u0435\u043a\u043e\u0434\u0435\u0440: {type(exc).__name__}")

        if samples is not None and samples.size:
            try:
                self._output.play(samples, rate)
                return
            except AudioUnavailable as exc:
                reasons.append(f"PortAudio: {exc}")
            except Exception as exc:  # pragma: no cover - driver specific
                reasons.append(f"PortAudio: {type(exc).__name__}: {exc}")
            try:
                self._wave().play_wav(to_wav_bytes(samples, rate))
                return
            except AudioUnavailable as exc:
                reasons.append(f"winsound: {exc}")
            except Exception as exc:  # pragma: no cover
                reasons.append(f"winsound: {type(exc).__name__}: {exc}")

        if self._allow_mci:
            try:
                self._play_mci(mp3)
                return
            except AudioUnavailable as exc:
                reasons.append(f"MCI: {exc}")
            except Exception as exc:  # pragma: no cover - Windows only
                reasons.append(f"MCI: {type(exc).__name__}")

        detail = "; ".join(reason for reason in reasons if reason)
        raise AudioUnavailable("\u043d\u0435\u0442 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430 \u0432\u044b\u0432\u043e\u0434\u0430" + (f" ({detail})" if detail else ""))

    def _play_mci(self, mp3: bytes) -> None:
        if not self._allow_mci:
            raise AudioUnavailable("\u0434\u0435\u043a\u043e\u0434\u0435\u0440 MP3 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d")
        with self._lock:
            if self._mci is None:
                self._mci = _MciPlayer()
            player = self._mci
        player.play(mp3)

    def stop(self) -> None:
        self._output.stop()
        wave = self._wave_player
        if wave is not None:
            stop = getattr(wave, "stop", None)
            if callable(stop):
                with suppress(Exception):
                    stop()
        with self._lock:
            player = self._mci
        if player is not None:
            player.stop()

    def close(self) -> None:
        self.stop()
        self._output.close()
        wave, self._wave_player = self._wave_player, None
        if wave is not None:
            close = getattr(wave, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        with self._lock:
            player, self._mci = self._mci, None
        if player is not None:
            player.close()


class _MciPlayer:
    """MP3 playback through the Windows MCI API (no extra wheels needed)."""

    _counter = itertools.count(1)

    def __init__(self) -> None:
        self._alias: str | None = None
        self._path: str | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _send(command: str, *, check: bool = True) -> int:
        """Run one MCI command; a non-zero error code is a failure, not silence."""
        import ctypes

        code = int(ctypes.windll.winmm.mciSendStringW(command, None, 0, None))  # pragma: no cover
        if check and code != 0:
            raise AudioUnavailable(f"MCI ошибка {code}")
        return code

    def play(self, mp3: bytes) -> None:  # pragma: no cover - Windows only
        handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        try:
            handle.write(mp3)
            handle.close()
        except Exception:
            handle.close()
            raise
        alias = f"voxmci{next(self._counter)}"
        try:
            self._send(f'open "{handle.name}" type mpegvideo alias {alias}')
        except Exception as exc:
            with suppress(OSError):
                Path(handle.name).unlink()
            raise AudioUnavailable(f"MCI не открывает MP3: {exc}") from exc
        with self._lock:
            self._alias, self._path = alias, handle.name
        try:
            self._send(f"play {alias} wait")
        finally:
            with self._lock:
                self._alias, self._path = None, None
            with suppress(Exception):
                self._send(f"close {alias}", check=False)
            with suppress(OSError):
                Path(handle.name).unlink()

    def stop(self) -> None:  # pragma: no cover - Windows only
        with self._lock:
            alias = self._alias
        if alias:
            with suppress(Exception):
                self._send(f"stop {alias}", check=False)

    def close(self) -> None:  # pragma: no cover - Windows only
        self.stop()


# --------------------------------------------------------------------------- SAPI
SAPI_SCRIPT = """param([string]$TextFile, [string]$OutFile, [string]$VoiceName)
$ErrorActionPreference = 'Stop'
$text = [IO.File]::ReadAllText($TextFile, [Text.Encoding]::UTF8)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($VoiceName) { try { $s.SelectVoice($VoiceName) } catch {} }
$s.SetOutputToWaveFile($OutFile)
$s.Speak($text)
$s.Dispose()
"""


def _no_window_flag() -> int:
    """CREATE_NO_WINDOW so a console window never flashes over the assistant."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


class SapiSpeaker:
    """Offline synthesis with Windows SAPI through PowerShell.

    Used when edge-tts is unavailable (no network) or returns no audio. The text
    goes through a UTF-8 file and the script through a temporary .ps1 written as
    UTF-8 with BOM - never through command-line quoting, which would mangle the
    Russian text. Synthesis of a short phrase takes a few hundred milliseconds.
    """

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = float(timeout)
        self._voices: list[dict[str, str]] | None = None

    @staticmethod
    def powershell() -> str | None:
        for name in ("powershell", "pwsh"):
            path = shutil.which(name)
            if path:
                return path
        return None

    def voices(self) -> list[dict[str, str]]:
        """Installed SAPI voices as [{"name", "culture"}] (cached)."""
        if self._voices is not None:
            return self._voices
        executable = self.powershell()
        if executable is None:
            self._voices = []
            return self._voices
        command = (
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture }; "
            "$s.Dispose()"
        )
        try:
            completed = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True,
                timeout=self._timeout,
                creationflags=_no_window_flag(),
                check=False,
            )
        except Exception as exc:  # pragma: no cover - depends on the machine
            log.info("SAPI voices unavailable: %s", exc)
            self._voices = []
            return self._voices
        voices: list[dict[str, str]] = []
        for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
            name, _, culture = line.strip().partition("|")
            name = name.strip()
            if name:
                voices.append({"name": name, "culture": culture.strip()})
        self._voices = voices
        return voices

    def available(self) -> bool:
        """True when PowerShell and at least one SAPI voice are installed."""
        return bool(self.voices())

    def preferred_voice(self) -> str:
        """Russian voice for the assistant (Irina first), or an empty string."""
        voices = self.voices()
        for voice in voices:
            if "irina" in voice["name"].lower():
                return voice["name"]
        for voice in voices:
            if voice["culture"].lower().startswith("ru"):
                return voice["name"]
        return ""

    def synthesize(self, text: str, voice: str = "") -> bytes:
        """Render text to WAV bytes with SAPI; raises RuntimeError on failure."""
        value = str(text or "").strip()
        if not value:
            raise ValueError("\u043f\u0443\u0441\u0442\u043e\u0439 \u0442\u0435\u043a\u0441\u0442 \u0434\u043b\u044f SAPI")
        executable = self.powershell()
        if executable is None:
            raise RuntimeError("PowerShell \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d")
        with tempfile.TemporaryDirectory(prefix="voxsapi") as folder:
            root = Path(folder)
            text_file = root / "text.txt"
            text_file.write_text(value, encoding="utf-8")
            wav_file = root / "out.wav"
            script = root / "synth.ps1"
            script.write_text(SAPI_SCRIPT, encoding="utf-8-sig")
            command = [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-TextFile",
                str(text_file),
                "-OutFile",
                str(wav_file),
                "-VoiceName",
                voice or "",
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=self._timeout,
                    creationflags=_no_window_flag(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"SAPI \u043d\u0435 \u043e\u0442\u0432\u0435\u0442\u0438\u043b \u0437\u0430 {self._timeout:.0f} \u0441") from exc
            except OSError as exc:
                raise RuntimeError(f"SAPI \u043d\u0435 \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u043b\u0441\u044f: {exc}") from exc
            if completed.returncode != 0 or not wav_file.is_file() or wav_file.stat().st_size <= 44:
                raw = completed.stderr or completed.stdout
                detail = raw.decode("utf-8", errors="replace").strip()
                short = detail.splitlines()[-1][:200] if detail else "\u043d\u0435\u0442 WAV \u043d\u0430 \u0432\u044b\u0445\u043e\u0434\u0435"
                raise RuntimeError(f"SAPI: {short}")
            return wav_file.read_bytes()


# --------------------------------------------------------------------------- engine
class Speaker:
    """Voice output: edge-tts first, SAPI as the offline fallback."""

    def __init__(
        self,
        config: Any,
        *,
        synthesize: Callable[[str, str, str], Awaitable[bytes]] | None = None,
        player: Any | None = None,
        voices_path: str | Path | None = None,
        sapi: Any | None = None,
        wave_player: Any | None = None,
        piper: Any | None = None,
        emit: Callable[[Any], None] | None = None,
    ) -> None:
        self._config = config
        self._synth_injected = synthesize is not None
        self._synthesize = synthesize or self._edge_synthesize
        self._player_injected = player is not None
        self._device = resolve_output_device(config)
        self._player = player if player is not None else Mp3Player(device=self._device)
        self._sapi = sapi
        self._wave_player = wave_player
        self._piper = piper
        self._piper_injected = piper is not None
        self._emit = emit
        self._voices_path = Path(voices_path) if voices_path else paths.voices_path()
        self._voice = DEFAULT_VOICE
        self._rate = DEFAULT_RATE
        self._max_chars = 0
        self._engine_mode = "auto"
        self._gen = 0
        self._lock = threading.Lock()
        self._last_result: dict[str, Any] = {"ok": True, "engine": "", "message": "", "detail": ""}
        self.configure(config)

    # -- configuration -----------------------------------------------------
    def configure(self, config: Any | None = None) -> None:
        if config is not None:
            self._config = config
        cfg = self._config
        self._voice = str(cfg.get("tts.voice", DEFAULT_VOICE) or DEFAULT_VOICE)
        self._rate = str(cfg.get("tts.rate", DEFAULT_RATE) or DEFAULT_RATE)
        mode = str(cfg.get("tts.engine", "auto") or "auto").strip().lower()
        if mode in {"silero", "chatterbox"}:
            # not implemented yet: keep the app speaking instead of dying silently
            log.info("tts engine %r is not implemented, using local piper", mode)
            mode = "piper"
        self._engine_mode = mode if mode in ("auto", "edge", "sapi", "piper") else "auto"
        # A different local voice needs a new ORT session.
        if (
            self._piper is not None
            and not self._piper_injected
            and self._piper_voice_name() != str(getattr(self._piper, "voice_name", "") or "")
        ):
            with suppress(Exception):
                self._piper.close()
            self._piper = None
        try:
            self._max_chars = int(cfg.get("tts.max_chars", 0) or 0)
        except (TypeError, ValueError):
            self._max_chars = 0
        device = resolve_output_device(cfg)
        if device != self._device and not self._player_injected:
            # audio.output_device changed: the device is baked into the player
            previous, self._player = self._player, Mp3Player(device=device)
            self._device = device
            close = getattr(previous, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def rate(self) -> str:
        return self._rate

    @property
    def max_chars(self) -> int:
        return self._max_chars

    @property
    def engine_mode(self) -> str:
        return self._engine_mode

    def local_voice_status(self) -> dict[str, Any]:
        """Local voice state for /diagnostics; never loads the model."""
        try:
            available, reason = self.piper.availability()
            return {
                "available": bool(available),
                "message": str(reason or ""),
                "voice": str(getattr(self.piper, "voice_name", "") or "ru_RU-irina-medium"),
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"available": False, "message": f"{type(exc).__name__}: {exc}", "voice": ""}

    @property
    def sapi(self) -> SapiSpeaker:
        if self._sapi is None:
            self._sapi = SapiSpeaker()
        return self._sapi

    def _piper_voice_name(self) -> str:
        """Which Piper .onnx to use: the selected local voice, else Irina."""
        voice = str(self._voice or "").strip()
        if not voice:
            return "ru_RU-irina-medium"
        if (paths.voices_dir() / f"{voice}.onnx").is_file():
            return voice
        if voice.startswith("ru_RU-"):
            return voice
        return "ru_RU-irina-medium"

    @property
    def piper(self) -> PiperTts:
        """Local neural voice (offline). Import is lazy, so this is cheap."""
        if self._piper is None:
            self._piper = PiperTts(self._config, voice_name=self._piper_voice_name())
        return self._piper

    def local_voice_ready(self) -> bool:
        """True when the local neural voice is downloaded and usable."""
        try:
            available, _reason = self.piper.availability()
            return bool(available)
        except Exception:  # pragma: no cover - never break answering on a probe
            return False

    async def ensure_local_voice(self) -> bool:
        """Download the Piper voice (~63 MB) in the background; False on failure."""
        try:
            return bool(await self.piper.ensure_voice(self._emit))
        except Exception as exc:  # pragma: no cover - network/storage problems
            log.warning("piper voice download failed: %s", exc)
            return False

    def _wave(self) -> Any:
        if self._wave_player is None:
            self._wave_player = WavePlayer()
        return self._wave_player

    def _record(self, ok: bool, engine: str, message: str, detail: str = "") -> None:
        with self._lock:
            self._last_result = {"ok": bool(ok), "engine": engine, "message": message, "detail": detail}

    def _output_blocked(self) -> bool:
        """True when this machine has no output device at all (fast failure)."""
        if self._synth_injected:
            return False
        try:
            backend, _message = output_backend()
        except Exception:  # pragma: no cover - defensive
            return False
        return backend == "none"

    def last_result(self) -> dict[str, Any]:
        """What the previous speak()/preview() actually did (for the UI)."""
        with self._lock:
            return dict(self._last_result)

    # -- edge-tts availability ---------------------------------------------
    def _edge_available(self) -> bool:
        """False while edge-tts is known to be down (offline / blocked / 403).

        Without this, every phrase first waits out the failing websocket
        handshake before falling back to SAPI, which makes the answer audibly
        late for a whole session. The state is shared by all Speaker instances:
        a test probe and the main speaker talk to the same Microsoft endpoint.
        """
        if self._synth_injected:
            return True
        return edge_available()

    def _note_edge(self, ok: bool) -> None:
        if self._synth_injected:
            return
        note_edge(ok)

    # -- synthesis ---------------------------------------------------------
    async def _edge_synthesize(self, text: str, voice: str, rate: str) -> bytes:
        import edge_tts  # lazy: optional wheel

        communicate = edge_tts.Communicate(text, voice, rate=rate)
        buffer = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buffer.extend(chunk["data"])
        if not buffer:
            raise RuntimeError("edge-tts returned no audio")
        return bytes(buffer)

    def sentences(self, text: str) -> list[str]:
        return split_sentences(truncate_text(text, self._max_chars))

    # -- playback ----------------------------------------------------------
    async def speak(self, text: str, *, voice: str | None = None) -> bool:
        """Speak text; True when the phrase was actually heard.

        Never raises: TZ 15 requires the answer to stay available as text when
        neither the network nor the synthesis engine can produce sound.
        """
        pieces = self.sentences(text)
        chosen = voice or self._voice
        if not pieces:
            self._record(True, "", "")
            return True
        if self._device is None and self._output_blocked():
            # Nothing can play sound: say so at once instead of synthesizing
            # the whole answer twice and only then failing to hear it. An
            # explicitly selected output device is always tried: it may be
            # usable even when PortAudio reports no system default.
            self._record(False, "", "нет устройства вывода", "output_backend() = none")
            return False

        if self._engine_mode == "sapi":
            ok, message, detail = await self._speak_sapi(pieces)
            self._record(ok, "sapi", message, detail)
            return ok

        message = ""
        detail = ""
        piper_note = ""
        # The local neural voice is preferred in "auto" as soon as its model is
        # downloaded: it is offline, ~0.2 s per phrase and no longer a robot.
        local_first = self._engine_mode == "piper" or (
            self._engine_mode == "auto" and self.local_voice_ready()
        )
        if local_first:
            ok, message, detail = await self._speak_piper(pieces)
            if ok:
                self._record(True, "piper", message, detail)
                return True
            piper_note = message
            if self._engine_mode == "piper":
                self._record(False, "piper", message, detail)
                return False
            log.info("local voice failed (%s), falling back to edge-tts", message)
            message = ""
            detail = ""
        if self._edge_available():
            ok, message, detail = await self._speak_edge(pieces, chosen)
            if not ok:
                # One repeat: the edge NoAudioReceived glitch is not deterministic.
                ok, message, detail = await self._speak_edge(pieces, chosen)
            if ok:
                self._note_edge(True)
                self._record(True, "edge", message, detail)
                return True
            self._note_edge(False)
            if self._engine_mode == "edge":
                self._record(False, "edge", message, detail)
                return False

        sapi_ok, sapi_message, sapi_detail = await self._speak_sapi(pieces)
        if sapi_ok:
            self._record(True, "sapi", sapi_message, f"{detail} | {sapi_detail}".strip(" |"))
            return True
        # A missing output device is not something SAPI can fix, and the user
        # needs the short answer, not the whole chain.
        if not message:  # edge was skipped because it recently failed
            combined = sapi_message
        elif message.startswith("нет устройства вывода"):
            combined = message
        else:
            combined = f"{message}; SAPI: {sapi_message}"
        if piper_note and not combined.startswith(piper_note):
            combined = f"{piper_note}; {combined}"
        self._record(False, "edge", combined, f"{detail} | {sapi_detail}".strip(" |"))
        return False

    async def _speak_edge(self, pieces: list[str], voice: str) -> tuple[bool, str, str]:
        """One edge-tts pass: synthesize while the previous sentence plays."""
        with self._lock:
            self._gen += 1
            generation = self._gen
        # Unbounded on purpose: a bounded queue can deadlock between "producer
        # waited for room" and "consumer waited for the end marker", and a
        # spoken answer is a handful of short MP3 buffers at most.
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        producer = asyncio.create_task(
            self._produce(pieces, voice, generation, queue),
            name="vox-tts-producer",
        )
        played = 0
        synth_error: BaseException | None = None
        play_error: BaseException | None = None
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "end":
                    break
                if generation != self._gen:
                    break
                if kind == "error":
                    synth_error = payload
                    continue
                try:
                    await asyncio.to_thread(self._player.play, payload)
                    played += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("tts playback failed: %s", exc)
                    play_error = exc
                if generation != self._gen:
                    break
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await producer

        if generation != self._gen:
            return True, "\u0432\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0434\u0435\u043d\u0438\u0435 \u043f\u0440\u0435\u0440\u0432\u0430\u043d\u043e", ""
        if played > 0 and play_error is None:
            return True, f"\u0412\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0434\u0435\u043d\u043e \u0447\u0435\u0440\u0435\u0437 edge-tts ({voice})", ""
        if isinstance(play_error, AudioUnavailable):
            return False, "\u043d\u0435\u0442 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430 \u0432\u044b\u0432\u043e\u0434\u0430", str(play_error)
        if play_error is not None:
            return False, f"\u043e\u0448\u0438\u0431\u043a\u0430 \u0432\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0434\u0435\u043d\u0438\u044f: {type(play_error).__name__}", str(play_error)
        if synth_error is not None:
            return False, f"edge-tts: {type(synth_error).__name__}", str(synth_error)
        return False, "edge-tts: \u043d\u0435\u0442 \u0437\u0432\u0443\u043a\u0430", ""

    async def _speak_sapi(self, pieces: list[str]) -> tuple[bool, str, str]:
        """Offline pass: SAPI renders each sentence to WAV, winsound plays it."""
        with self._lock:
            self._gen += 1
            generation = self._gen
        sapi_speaker = self.sapi
        try:
            available = await asyncio.to_thread(sapi_speaker.available)
        except Exception as exc:  # pragma: no cover - defensive
            return False, "SAPI \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d", str(exc)
        if not available:
            return False, "SAPI \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d", "\u043d\u0435\u0442 \u0433\u043e\u043b\u043e\u0441\u043e\u0432 SAPI"
        voice_name = sapi_speaker.preferred_voice()
        for piece in pieces:
            if generation != self._gen:  # barge-in while SAPI was rendering
                return True, "воспроизведение прервано", ""
            try:
                wav = await asyncio.to_thread(sapi_speaker.synthesize, piece, voice_name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("SAPI synthesis failed: %s", exc)
                return False, f"SAPI: {type(exc).__name__}", str(exc)
            try:
                await asyncio.to_thread(self._play_wav, wav)
            except asyncio.CancelledError:
                raise
            except AudioUnavailable as exc:
                return False, "\u043d\u0435\u0442 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430 \u0432\u044b\u0432\u043e\u0434\u0430", str(exc)
            except Exception as exc:  # pragma: no cover
                return False, f"\u043e\u0448\u0438\u0431\u043a\u0430 \u0432\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0434\u0435\u043d\u0438\u044f: {type(exc).__name__}", str(exc)
        label = voice_name or "\u0441\u0438\u0441\u0442\u0435\u043c\u043d\u044b\u0439 \u0433\u043e\u043b\u043e\u0441"
        return True, f"\u0412\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0434\u0435\u043d\u043e \u0447\u0435\u0440\u0435\u0437 SAPI ({label})", ""

    def _play_pcm(self, samples: Any, sample_rate: int) -> None:
        """Play int16 PCM through every available path.

        PortAudio comes first: on machines where waveOut is empty (Bluetooth
        headsets that only expose WDM-KS/MME) winsound cannot play at all,
        while sounddevice can still open the default output device.
        """
        errors: list[str] = []
        try:
            AudioOutput(self._device).play(samples, sample_rate)
            return
        except Exception as exc:  # PortAudio path failed, try the cheaper ones
            errors.append(f"PortAudio: {exc}")
            log.debug("PCM playback via PortAudio failed: %s", exc)
        try:
            self._wave().play_wav(to_wav_bytes(samples, sample_rate))
            return
        except Exception as exc:
            errors.append(f"winsound: {exc}")
            log.debug("PCM playback via winsound failed: %s", exc)
        raise AudioUnavailable("; ".join(errors) or "нет устройства вывода")

    def _play_wav(self, wav: bytes) -> None:
        """Play a 16-bit WAV payload (SAPI output) through the same chain.

        Decoding and playback are attempted independently: an unparsable WAV
        can still be handed to winsound, which does its own decoding.
        """
        errors: list[str] = []
        samples: Any = None
        rate = 0
        try:
            samples, rate = _decode_wav(wav)
        except Exception as exc:
            errors.append(f"WAV: {exc}")
            log.debug("cannot decode WAV locally: %s", exc)
        if samples is not None:
            try:
                AudioOutput(self._device).play(samples, rate)
                return
            except Exception as exc:
                errors.append(f"PortAudio: {exc}")
                log.debug("WAV playback via PortAudio failed: %s", exc)
        try:
            self._wave().play_wav(wav)
            return
        except Exception as exc:
            errors.append(f"winsound: {exc}")
            log.debug("WAV playback via winsound failed: %s", exc)
        raise AudioUnavailable("; ".join(errors) or "нет устройства вывода")

    async def _speak_piper(self, pieces: list[str]) -> tuple[bool, str, str]:
        """Local neural voice (Piper, offline). Returns (ok, message, detail)."""
        with self._lock:
            self._gen += 1
            generation = self._gen
        try:
            available, reason = await asyncio.to_thread(self.piper.availability)
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"Piper недоступен: {type(exc).__name__}", str(exc)
        if not available:
            return False, str(reason or "локальный голос не готов"), str(reason or "")
        for piece in pieces:
            if generation != self._gen:  # barge-in while synthesizing
                return True, "воспроизведение прервано", ""
            try:
                samples, rate = await asyncio.to_thread(self.piper.synthesize, piece)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("piper synthesis failed: %s", exc)
                return False, f"Piper: {type(exc).__name__}", str(exc)
            try:
                await asyncio.to_thread(self._play_pcm, samples, rate)
            except asyncio.CancelledError:
                raise
            except AudioUnavailable as exc:
                return False, "нет устройства вывода", str(exc)
            except Exception as exc:  # pragma: no cover - driver specific
                return False, f"ошибка воспроизведения: {type(exc).__name__}", str(exc)
        return True, "Воспроизведено локальным голосом Piper (ru_RU-irina)", ""

    async def _produce(
        self,
        pieces: list[str],
        voice: str,
        generation: int,
        queue: asyncio.Queue[tuple[str, Any]],
    ) -> None:
        try:
            for piece in pieces:
                if generation != self._gen:
                    return
                try:
                    mp3 = await self._synthesize(piece, voice, self._rate)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("tts synthesis failed: %s", exc)
                    await queue.put(("error", exc))
                    continue
                if generation != self._gen:
                    return
                await queue.put(("pcm", mp3))
        finally:
            queue.put_nowait(("end", None))

    def stop(self) -> None:
        """Abort the current utterance (barge-in, TZ 2.6)."""
        with self._lock:
            self._gen += 1
        for player in (self._player, self._wave_player):
            if player is None:
                continue
            stop = getattr(player, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:  # pragma: no cover
                    pass

    async def preview(self, voice: str, text: str = PREVIEW_TEXT) -> tuple[bool, str]:
        """Play a short sample; returns (ok, message for the UI)."""
        self.stop()
        await self.speak(text or PREVIEW_TEXT, voice=voice or self._voice)
        result = self.last_result()
        return bool(result.get("ok")), str(result.get("message") or "")

    # -- voices ------------------------------------------------------------
    def _read_cache(self) -> list[dict[str, str]]:
        path = self._voices_path
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("voices") or []
        result: list[dict[str, str]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            result.append(
                {
                    "engine": str(item.get("engine") or "edge"),
                    "name": name,
                    "label": str(item.get("label") or name),
                }
            )
        return result

    def _write_cache(self, voices: list[dict[str, str]]) -> None:
        try:
            self._voices_path.parent.mkdir(parents=True, exist_ok=True)
            self._voices_path.write_text(
                json.dumps(voices, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover
            log.warning("cannot cache voices: %s", exc)

    def _config_voices(self) -> list[dict[str, str]]:
        raw = []
        try:
            raw = self._config.get("tts.voices", []) or []
        except Exception:  # pragma: no cover
            raw = []
        result: list[dict[str, str]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            result.append(
                {
                    "engine": str(item.get("engine") or "edge"),
                    "name": name,
                    "label": str(item.get("label") or voice_label(name)),
                }
            )
        return result

    def cached_voices(self) -> list[dict[str, str]]:
        """Voices known without a network call: the cache file, then the config."""
        try:
            cached = self._read_cache()
        except Exception:  # pragma: no cover - defensive
            cached = []
        return cached or self._config_voices()

    async def list_voices(self) -> list[dict[str, str]]:
        """Local Piper voices plus Russian edge-tts voices (TZ 11, 13)."""
        local = local_voices()
        cached = self._read_cache()
        if cached:
            return local + cached
        try:
            import edge_tts  # lazy

            raw = await edge_tts.list_voices()
            voices: list[dict[str, str]] = []
            for item in raw if isinstance(raw, list) else []:
                if not isinstance(item, dict):
                    continue
                short = str(item.get("ShortName") or "").strip()
                locale = str(item.get("Locale") or "")
                if not short or not locale.lower().startswith("ru"):
                    continue
                voices.append(
                    {
                        "engine": "edge",
                        "name": short,
                        "label": voice_label(short, str(item.get("Gender") or "")),
                    }
                )
            if voices:
                self._write_cache(voices)
                return voices
        except Exception as exc:
            log.info("cannot fetch edge-tts voices (%s), using cached config", exc)
        return local + self._config_voices()

    def close(self) -> None:
        self.stop()
        close = getattr(self._player, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover
                pass
        if self._piper is not None:
            try:
                self._piper.close()
            except Exception:  # pragma: no cover
                pass


__all__ = [
    "DEFAULT_RATE",
    "DEFAULT_VOICE",
    "MAX_SENTENCE_CHARS",
    "Mp3Player",
    "OUTPUT_SAMPLE_RATE",
    "SAPI_SCRIPT",
    "SapiSpeaker",
    "PREVIEW_TEXT",
    "Speaker",
    "decode_mp3",
    "edge_available",
    "note_edge",
    "reset_edge_cooldown",
    "split_sentences",
    "truncate_text",
    "voice_label",
]
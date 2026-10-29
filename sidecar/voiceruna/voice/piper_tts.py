r"""Local neural voice with Piper (offline replacement for the SAPI robot).

Engine choice (TZ 5.4, sprint 4): piper-tts 1.8.0 from PyPI, the Python binding
of Piper.  Why this one and not sherpa-onnx or a hand-written VITS runner:

* the wheel is self-contained: it ships piper/espeakbridge.pyd (compiled
  espeak-ng) together with the full piper/espeak-ng-data tree, so Russian
  phonemization works without a system espeak-ng install and without torch;
  the only runtime dependencies are onnxruntime and pathvalidate, and
  onnxruntime 1.20.1 is already in the bundle;
* PiperVoice.synthesize() is the reference implementation of the exact
  int64 [1,T] / scales[3] graph contract researched for ru_RU-irina-medium;
* a hand-written VITS runner plus piper_phonemize.dll would need the same
  espeak data anyway, and sherpa-onnx would add a second ONNX runtime.

Licence: piper-tts is GPL-3.0-or-later.  The ru_RU-irina-medium MODEL_CARD
says the training data licence is "Unknown", so the voice itself needs a
separate redistribution clearance.  Both facts belong in the distribution
notes.

Model storage (TZ 13): "%APPDATA%\VoiceRunaCore\models\piper" via
paths.models_dir().  Both files are downloaded on demand by ensure_voice();
nothing else in this module touches the network.

Freezing (PyInstaller onedir): the phonemizer is a compiled .pyd and the espeak
data is package data, so the build must include them:

    --collect-all piper

(collect_data_files("piper") + collect_dynamic_libs("piper") +
hiddenimports=["piper", "piper.espeakbridge"] in a .spec).  Without the data
directory PiperVoice.load raises and the module reports a Russian error instead
of crashing the sidecar.  _espeak_data_dir also checks sys._MEIPASS and
paths.resource_dir() as fallbacks.

Threading: synthesize is synchronous and blocking (ORT inference 0.1-0.2 s on a
warm session, 5-8 s for the first call because of the session load).  The
caller runs it in a worker thread; the session is created once under a lock so
a background warm_up and the first request cannot load it twice.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .. import paths
from ..events import download_progress_event, error_event

VOICE_NAME = "ru_RU-irina-medium"
VOICE_DIR_NAME = "piper"
VOICE_DOWNLOAD_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium"
)
MODEL_FILENAME = f"{VOICE_NAME}.onnx"
CONFIG_FILENAME = f"{MODEL_FILENAME}.json"
MODEL_URL = f"{VOICE_DOWNLOAD_URL}/{MODEL_FILENAME}"
CONFIG_URL = f"{VOICE_DOWNLOAD_URL}/{CONFIG_FILENAME}"

#: Exact sizes measured from the HuggingFace build (2026-10).
MODEL_EXPECTED_BYTES = 63_201_294
CONFIG_EXPECTED_BYTES = 4_765
#: Sanity window for the medium ru voice.  A truncated transfer or an HTML
#: error page is far outside it; a re-exported upstream model still fits.
MODEL_MIN_BYTES = 50_000_000
MODEL_MAX_BYTES = 100_000_000
CONFIG_MAX_BYTES = 64_000

SAMPLE_RATE = 22_050
#: TZ guard: one request never feeds more than this to the engine.
MAX_TEXT_CHARS = 500
STAGE = "piper"

#: Same transfer policy as the STT model downloader: long read timeout for the
#: 63 MB file, resume from .part, a few retries on transport errors.
DOWNLOAD_TIMEOUT = {"connect": 20.0, "read": 180.0, "write": 60.0, "pool": 20.0}
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAYS = (1.0, 3.0)

_RATE_PATTERN = re.compile(r"([+-]?\d+(?:[.,]\d+)?)\s*%")
_HTML_PREFIXES = (b"<", b"{", b"\x1f", b"PK")

log = logging.getLogger(__name__)


class PiperError(RuntimeError):
    """Base error with a message the UI can show to the user."""


class PiperUnavailable(PiperError):
    """The wheel, the voice files or the espeak-ng data are not usable."""


def _trim_silence(samples: np.ndarray, rate: int, keep_ms: float = 60.0) -> np.ndarray:
    """Cut the silent tail/head Piper adds, keeping a short natural pause.

    Without this the assistant keeps "speaking" for a second after the phrase
    and the next command feels delayed (owner report: «паузы после точки»).
    """
    if samples.size == 0:
        return samples
    try:
        threshold = max(1.0, float(np.max(np.abs(samples.astype(np.int32)))) * 0.02)
        loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > threshold)
        if loud.size == 0:
            return samples
        keep = int(rate * keep_ms / 1000.0)
        start = max(0, int(loud[0]) - keep)
        end = min(samples.size, int(loud[-1]) + 1 + keep)
        return samples[start:end].copy()
    except Exception:  # pragma: no cover - never break synthesis
        return samples


def local_voices(directory: Path | None = None) -> list[dict[str, str]]:
    """Piper voices found in the drop-in folder: *.onnx with an *.onnx.json."""
    folder = Path(directory) if directory is not None else paths.voices_dir()
    result: list[dict[str, str]] = []
    try:
        for path in sorted(folder.glob("*.onnx")):
            if not path.with_suffix(path.suffix + ".json").is_file():
                continue
            result.append(
                {
                    "engine": "piper",
                    "name": path.stem,
                    "label": f"Piper: {path.stem}",
                }
            )
    except OSError:  # pragma: no cover - unreadable folder
        return []
    return result


def _piper_installed() -> bool:
    """True when the piper package can be imported, without importing it."""
    if "piper" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("piper") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken environment
        return False


def _espeak_data_dir(piper_module: Any) -> Path:
    """Locate the espeak-ng data tree in dev and in a frozen onedir bundle."""
    candidates: list[Path] = []
    try:
        from piper.phonemize_espeak import ESPEAK_DATA_DIR

        candidates.append(Path(ESPEAK_DATA_DIR))
    except Exception:  # pragma: no cover - old/new piper layout
        pass
    package_file = getattr(piper_module, "__file__", None)
    if package_file:
        candidates.append(Path(package_file).resolve().parent / "espeak-ng-data")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "piper" / "espeak-ng-data")
    candidates.append(paths.resource_dir() / "piper" / "espeak-ng-data")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise PiperUnavailable(
        "в пакете piper-tts не найдены данные espeak-ng; "
        "пересоберите бандл с ключом --collect-all piper"
    )


class PiperTts:
    """One cached Piper voice: availability, download, synthesis and warm-up."""

    def __init__(
        self,
        config: Any,
        *,
        voice_name: str = VOICE_NAME,
        voice_dir: str | Path | None = None,
        client_factory: Callable[[], Any] | None = None,
        voice_factory: Callable[[], Any] | None = None,
        attempts: int = DOWNLOAD_ATTEMPTS,
        retry_delays: tuple[float, ...] = DOWNLOAD_RETRY_DELAYS,
        timeout: dict[str, float] | None = None,
    ) -> None:
        self._config = config
        self._voice_name = str(voice_name or VOICE_NAME)
        self._dir = Path(voice_dir) if voice_dir else paths.models_dir() / VOICE_DIR_NAME
        self._client_factory = client_factory
        self._voice_factory = voice_factory
        self._attempts = max(1, int(attempts))
        self._retry_delays = tuple(float(value) for value in retry_delays)
        self._timeout = dict(timeout or DOWNLOAD_TIMEOUT)
        self._voice: Any = None
        self._syn_config: Any = None
        self._lock = threading.RLock()
        self._filename = (
            MODEL_FILENAME if self._voice_name == VOICE_NAME else f"{self._voice_name}.onnx"
        )

    # ---------------------------------------------------------------- paths
    @property
    def voice_dir(self) -> Path:
        return self._dir

    def voice_path(self) -> str:
        r"""Path of the .onnx model.

        A voice dropped into %APPDATA%\VoiceRunaCore\voices wins over the bundled
        download, so the owner can add a third-party Piper voice without
        rebuilding the app (TZ §11).
        """
        local = paths.voices_dir() / self._filename
        if local.is_file():
            return str(local)
        return str(self._dir / self._filename)

    def config_path(self) -> str:
        """Path of the .onnx.json voice config next to the model."""
        return f"{self.voice_path()}.json"

    # ---------------------------------------------------------- diagnostics
    def availability(self) -> tuple[bool, str]:
        """Cheap readiness check: no model loading, no network."""
        if not _piper_installed():
            return False, "локальный голос Piper: пакет piper-tts не установлен"
        model = Path(self.voice_path())
        if not model.is_file():
            return False, f"локальный голос Piper: голос {self._voice_name} не скачан ({model})"
        if not self._model_looks_valid(model):
            return False, f"локальный голос Piper: файл модели повреждён ({model})"
        config = Path(self.config_path())
        if not config.is_file():
            return False, f"локальный голос Piper: нет файла настроек {config.name}"
        if not self._config_looks_valid(config):
            return False, f"локальный голос Piper: файл настроек повреждён ({config})"
        return True, f"локальный голос Piper {self._voice_name} готов"

    # ------------------------------------------------------------- download
    async def ensure_voice(self, emit: Any = None) -> bool:
        """Download the .onnx and .onnx.json into models/piper; False on failure."""
        if self._voice_ready():
            self._progress(emit, 100.0, "голос Piper уже скачан")
            return True
        client: Any = None
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            client = self._make_client()
            total = MODEL_EXPECTED_BYTES + CONFIG_EXPECTED_BYTES
            self._progress(emit, 0.0, "скачиваю голос Piper")
            await self._download(
                client,
                MODEL_URL,
                Path(self.voice_path()),
                base_done=0,
                total=total,
                emit=emit,
            )
            await self._download(
                client,
                CONFIG_URL,
                Path(self.config_path()),
                base_done=MODEL_EXPECTED_BYTES,
                total=total,
                emit=emit,
            )
            if not self._voice_ready():
                raise PiperUnavailable("скачанные файлы голоса Piper не прошли проверку")
            self._progress(emit, 100.0, "голос Piper готов")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("piper voice download failed: %s: %s", type(exc).__name__, exc)
            self._emit(
                emit,
                error_event(
                    f"не удалось скачать голос Piper ({type(exc).__name__})",
                    "download",
                ),
            )
            return False
        finally:
            if client is not None:
                close = getattr(client, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:  # pragma: no cover - defensive
                        log.debug("cannot close piper download client", exc_info=True)

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import httpx  # lazy: the voice package imports without httpx

        return httpx.AsyncClient(
            timeout=httpx.Timeout(**self._timeout),
            follow_redirects=True,
        )

    async def _download(
        self,
        client: Any,
        url: str,
        target: Path,
        *,
        base_done: int,
        total: int,
        emit: Any,
    ) -> int:
        """Stream one file to target with resume, retries and byte progress."""
        import httpx

        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        last_error: BaseException | None = None
        for attempt in range(1, self._attempts + 1):
            existing = partial.stat().st_size if partial.is_file() else 0
            headers = {"Accept-Encoding": "identity"}
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
            if existing > 0 and total > 0:
                self._progress(
                    emit,
                    100.0 * min(1.0, (base_done + existing) / total),
                    target.name,
                )
            received = 0
            advertised = 0
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    if existing > 0 and response.status_code == 416:
                        # The partial file cannot be continued: restart it clean.
                        self._discard(partial)
                        last_error = RuntimeError("HTTP 416: partial file is not usable")
                        existing = 0
                        continue
                    if existing > 0 and response.status_code == 200:
                        # The server ignored Range: start the file over.
                        existing = 0
                    response.raise_for_status()
                    advertised = int(response.headers.get("content-length") or 0)
                    mode = "ab" if existing > 0 else "wb"
                    with partial.open(mode) as handle:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            handle.write(chunk)
                            received += len(chunk)
                            if total > 0:
                                self._progress(
                                    emit,
                                    100.0
                                    * min(
                                        1.0,
                                        (base_done + existing + received) / total,
                                    ),
                                    target.name,
                                )
                size = partial.stat().st_size if partial.is_file() else 0
                if advertised > 0 and size < existing + advertised:
                    raise IOError(
                        f"{target.name}: получено {size} из {existing + advertised} байт"
                    )
                if size <= 0:
                    raise IOError(f"{target.name}: пустой ответ сервера")
                os.replace(partial, target)
                return size
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if 400 <= status < 500 and status != 416:
                    self._discard(partial)
                    raise
                log.warning(
                    "%s: HTTP %s on attempt %d/%d",
                    target.name,
                    status,
                    attempt,
                    self._attempts,
                )
            except (httpx.TransportError, OSError) as exc:
                last_error = exc
                log.warning(
                    "%s: %s on attempt %d/%d, %d bytes saved",
                    target.name,
                    type(exc).__name__,
                    attempt,
                    self._attempts,
                    partial.stat().st_size if partial.is_file() else 0,
                )
            await self._retry_delay(attempt)
        self._discard(partial)
        raise last_error if last_error is not None else RuntimeError(f"cannot download {url}")

    async def _retry_delay(self, attempt: int) -> None:
        if attempt >= self._attempts or not self._retry_delays:
            return
        delay = self._retry_delays[min(attempt - 1, len(self._retry_delays) - 1)]
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - Windows lock
            log.debug("cannot remove %s", path)

    # ------------------------------------------------------------ validation
    @staticmethod
    def _model_looks_valid(path: Path) -> bool:
        """A real ONNX model of the expected size, not an error page."""
        try:
            size = path.stat().st_size
            if not (MODEL_MIN_BYTES <= size <= MODEL_MAX_BYTES):
                return False
            with path.open("rb") as handle:
                header = handle.read(8)
        except OSError:
            return False
        if len(header) < 4:
            return False
        return not header.startswith(_HTML_PREFIXES)

    @staticmethod
    def _config_looks_valid(path: Path) -> bool:
        """The voice JSON must carry audio.sample_rate and a phoneme map."""
        try:
            size = path.stat().st_size
            if size <= 0 or size > CONFIG_MAX_BYTES:
                return False
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        audio = data.get("audio")
        phonemes = data.get("phoneme_id_map")
        espeak = data.get("espeak")
        if not isinstance(audio, dict) or not isinstance(espeak, dict):
            return False
        if not isinstance(phonemes, dict) or not phonemes:
            return False
        try:
            return int(audio.get("sample_rate") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _voice_ready(self) -> bool:
        model = Path(self.voice_path())
        config = Path(self.config_path())
        return (
            model.is_file()
            and self._model_looks_valid(model)
            and config.is_file()
            and self._config_looks_valid(config)
        )

    # ------------------------------------------------------------- synthesis
    def _length_scale(self) -> float:
        """Map the edge-tts rate setting (+10%) to Piper's length scale."""
        try:
            rate = str(self._config.get("tts.rate", "") or "")
        except Exception:  # pragma: no cover - custom config stub
            return 1.0
        match = _RATE_PATTERN.search(rate)
        if not match:
            return 1.0
        try:
            percent = float(match.group(1).replace(",", "."))
        except ValueError:  # pragma: no cover - regex guarantees a number
            return 1.0
        if percent <= -95.0:
            percent = -95.0
        scale = 100.0 / (100.0 + percent)
        return max(0.5, min(2.0, scale))

    def _load_voice(self) -> Any:
        """Create (once) and return the PiperVoice with its ORT session."""
        voice = self._voice
        if voice is not None:
            return voice
        with self._lock:
            voice = self._voice
            if voice is not None:
                return voice
            if self._voice_factory is not None:
                voice = self._voice_factory()
                self._voice = voice
                return voice
            try:
                import piper
                from piper.config import SynthesisConfig
            except Exception as exc:
                raise PiperUnavailable(
                    "локальный голос Piper недоступен: пакет piper-tts не установлен"
                ) from exc
            model = Path(self.voice_path())
            config = Path(self.config_path())
            if not model.is_file() or not config.is_file():
                raise PiperUnavailable(
                    f"локальный голос Piper не скачан: {model} "
                    "(вызовите ensure_voice())"
                )
            espeak_dir = _espeak_data_dir(piper)
            started = time.perf_counter()
            try:
                voice = piper.PiperVoice.load(
                    str(model),
                    str(config),
                    espeak_data_dir=str(espeak_dir),
                    download_dir=str(self._dir),
                )
            except Exception as exc:
                raise PiperUnavailable(
                    f"не удалось загрузить локальный голос Piper: {exc}"
                ) from exc
            self._syn_config = SynthesisConfig(
                length_scale=self._length_scale(),
                normalize_audio=True,
            )
            self._voice = voice
            log.info(
                "piper voice %s loaded in %.2f s (espeak data: %s)",
                self._voice_name,
                time.perf_counter() - started,
                espeak_dir,
            )
            return voice

    def warm_up(self) -> None:
        """Create the ORT session ahead of the first answer (background thread)."""
        try:
            self._load_voice()
        except Exception as exc:  # a warm-up failure must not kill its thread
            log.warning("piper warm-up failed: %s", exc)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Synthesize int16 mono PCM at 22 050 Hz; synchronous and blocking."""
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        if not cleaned:
            return np.zeros(0, dtype=np.int16), SAMPLE_RATE
        if len(cleaned) > MAX_TEXT_CHARS:
            raise ValueError(
                f"текст для локального синтеза длиннее {MAX_TEXT_CHARS} символов"
            )
        voice = self._load_voice()
        kwargs = {"syn_config": self._syn_config} if self._syn_config is not None else {}
        try:
            chunks = list(voice.synthesize(cleaned, **kwargs))
        except Exception as exc:
            raise PiperError(
                f"не удалось синтезировать речь локальным голосом Piper: {exc}"
            ) from exc
        arrays: list[np.ndarray] = []
        rate = SAMPLE_RATE
        for chunk in chunks:
            try:
                rate = int(getattr(chunk, "sample_rate", rate) or rate)
                samples = np.asarray(chunk.audio_int16_array, dtype=np.int16).reshape(-1)
            except Exception as exc:  # pragma: no cover - broken piper chunk
                raise PiperError(
                    f"локальный голос Piper вернул неожиданный формат аудио: {exc}"
                ) from exc
            if samples.size:
                arrays.append(samples)
        if not arrays:
            return np.zeros(0, dtype=np.int16), rate
        audio = np.concatenate(arrays).astype(np.int16, copy=False)
        return _trim_silence(audio, rate), rate

    def close(self) -> None:
        """Drop the ORT session and its memory (the next call reloads it)."""
        with self._lock:
            if self._voice is None:
                return
            self._voice = None
            self._syn_config = None
        gc.collect()
        log.debug("piper voice %s closed", self._voice_name)

    # ----------------------------------------------------------------- emit
    @staticmethod
    def _sink(emit: Any) -> Callable[[Any], None] | None:
        if emit is None:
            return None
        if callable(emit):
            return emit
        candidate = getattr(emit, "emit", None)
        return candidate if callable(candidate) else None

    def _progress(self, emit: Any, percent: float, message: str) -> None:
        sink = self._sink(emit)
        if sink is None:
            return
        try:
            sink(download_progress_event(STAGE, max(0.0, min(100.0, percent)), message))
        except Exception:  # pragma: no cover - the UI must not break a download
            log.debug("piper progress sink failed", exc_info=True)

    def _emit(self, emit: Any, event: Any) -> None:
        sink = self._sink(emit)
        if sink is None:
            return
        try:
            sink(event)
        except Exception:  # pragma: no cover
            log.debug("piper event sink failed", exc_info=True)


__all__ = [
    "CONFIG_EXPECTED_BYTES",
    "CONFIG_FILENAME",
    "CONFIG_URL",
    "MAX_TEXT_CHARS",
    "MODEL_EXPECTED_BYTES",
    "MODEL_FILENAME",
    "MODEL_URL",
    "PiperError",
    "PiperTts",
    "PiperUnavailable",
    "SAMPLE_RATE",
    "STAGE",
    "VOICE_DIR_NAME",
    "VOICE_NAME",
]

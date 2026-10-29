"""Sprint 0 wake word spike (TZ 17): standalone, outside the sidecar.

It measures, per engine, the three numbers that decide wakeword.engine:

    latency        frames from the first audio frame to the hit, in ms
    false alarms   hits on silence / room noise / ordinary speech
    missed wakes   wake samples that never triggered

These are exactly the inputs the TZ asks for ("есть движок, порог, задержка,
уровень ложных срабатываний; проверены Porcupine / openWakeWord / Vosk KWS").

Modes
    --mode wav     read *.wav files (recommended: real recordings)
    --mode mic     record from the microphone (default)
    --mode synth   no audio at all: synthetic silence and tones. Use it only to
                   smoke-test the plumbing, it proves nothing about accuracy.

File naming for --mode wav:
    wake-*.wav     contains the wake word once, at the beginning
    silence-*.wav  room noise or silence, must never trigger
    speech-*.wav   ordinary speech without the wake word, must never trigger

Usage
    python spike/wake_spike.py --mode wav --wav spike/samples --engine all
    python spike/wake_spike.py --mode mic --engine vosk --seconds 10
    python spike/wake_spike.py --mode synth

Nothing is written to disk and the assistant is not started.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import statistics
import sys
import time
from math import gcd
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))

FRAME = 512
FRAME_MS = FRAME * 1000.0 / 16000
SAMPLE_RATE = 16000
ENGINES = ("vosk", "porcupine", "openwakeword")
VOSK_DIR = "vosk-model-small-ru-0.22"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wake word spike (TZ 17)")
    parser.add_argument("--mode", choices=("wav", "mic", "synth"), default="mic")
    parser.add_argument("--engine", default="all", help="vosk | porcupine | openwakeword | all")
    parser.add_argument("--wav", type=Path, default=ROOT / "spike" / "samples")
    parser.add_argument("--models", type=Path, default=None, help="models dir (default: %%APPDATA%%/VoiceRunaCore/models)")
    parser.add_argument("--phrase", default="вокс")
    parser.add_argument("--seconds", type=float, default=6.0, help="recording length for --mode mic")
    parser.add_argument("--repeat", type=int, default=3, help="wake takes for --mode mic")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- audio
def to_frames(pcm, sample_rate: int = SAMPLE_RATE) -> list[np.ndarray]:
    """Split int16 PCM into 512-sample frames, resampling to 16 kHz if needed."""
    samples = np.asarray(pcm).ravel()
    if samples.dtype != np.int16:
        samples = np.clip(samples, -32768, 32767).astype(np.int16)
    if sample_rate != SAMPLE_RATE and samples.size:
        step = gcd(int(sample_rate), SAMPLE_RATE)
        up, down = SAMPLE_RATE // step, int(sample_rate) // step
        samples = np.interp(
            np.arange(0, samples.size * up, down) / up,
            np.arange(samples.size),
            samples,
        ).astype(np.int16)
    return [samples[offset : offset + FRAME] for offset in range(0, samples.size - FRAME + 1, FRAME)]


def record(seconds: float) -> np.ndarray:
    """Record mono 16 kHz int16 from the default microphone."""
    import sounddevice as sd

    device = sd.query_devices(kind="input")
    print(f"  recording {seconds:.1f} s from: {device.get('name')}")
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    return np.asarray(audio).reshape(-1)


def load_wav_files(directory: Path) -> dict[str, list[np.ndarray]]:
    import soundfile as sf

    if not directory.is_dir():
        raise SystemExit(f"no such directory: {directory}")
    result: dict[str, list[np.ndarray]] = {}
    for path in sorted(directory.glob("*.wav")):
        audio, rate = sf.read(str(path), dtype="int16", always_2d=False)
        if getattr(audio, "ndim", 1) > 1:
            audio = audio[:, 0]
        result[path.stem] = to_frames(audio, int(rate))
    if not result:
        raise SystemExit(f"no *.wav files in {directory}")
    return result


# --------------------------------------------------------------------------- engines
def build_backend(engine: str, args: argparse.Namespace):
    """Create one backend, or return (None, reason) when it cannot run here."""
    from voiceruna import paths
    from voiceruna.voice import wake as wake_module

    models = Path(args.models) if args.models else paths.models_dir()
    try:
        if engine == "vosk":
            model = models / VOSK_DIR
            if not wake_module.vosk_available():
                return None, "vosk is not installed"
            if not model.is_dir():
                return None, f"model missing: {model}"
            return wake_module.VoskBackend(model, phrase=args.phrase), ""
        if engine == "porcupine":
            if importlib.util.find_spec("pvporcupine") is None:
                return None, "pvporcupine is not installed"
            key = os.environ.get("PORCUPINE_ACCESS_KEY", "")
            if not key:
                return None, "PORCUPINE_ACCESS_KEY is not set"
            return wake_module.PorcupineBackend(key), ""
        if engine == "openwakeword":
            model = models / "openwakeword" / "vox.onnx"
            if importlib.util.find_spec("openwakeword") is None:
                return None, "openwakeword is not installed"
            if not model.is_file():
                return None, f"model missing: {model}"
            return wake_module.OpenWakeWordBackend(model), ""
    except Exception as exc:  # pragma: no cover - machine specific
        return None, f"{type(exc).__name__}: {exc}"
    return None, f"unknown engine {engine!r}"


def run_case(backend, label: str, frames: list[np.ndarray]) -> dict:
    started = time.perf_counter()
    hit = None
    for index, frame in enumerate(frames):
        if backend.feed(frame) is not None:
            hit = index
            break
    wall_ms = (time.perf_counter() - started) * 1000.0
    reset = getattr(backend, "reset", None)
    if callable(reset):
        try:
            reset()
        except Exception:
            pass
    latency = (hit + 1) * FRAME_MS if hit is not None else None
    print(f"    {label:<24}{'HIT' if hit is not None else 'no':<5}"
          f"{'' if latency is None else f'{latency:.0f} ms'}")
    return {"label": label, "hit": hit is not None, "latency_ms": latency, "wall_ms": wall_ms}


# --------------------------------------------------------------------------- report
def print_table(rows: list[dict]) -> None:
    header = f"{'engine':<14}{'cases':<10}{'wake hit':>10}{'false hits':>12}{'median latency':>18}{'cpu ms':>10}"
    print()
    print(header)
    print("-" * len(header))
    for row in rows:
        latency = "-" if row["latency_ms"] is None else f"{row['latency_ms']:.0f} ms"
        detail = row["note"] or row["case"]
        print(
            f"{row['engine']:<14}{row['cases']:<10}{row['wake_hit']:>10}"
            f"{row['false_hit']:>12}{latency:>18}{row['cpu_ms']:>10.0f}"
        )
        if row["note"]:
            print(f"    {detail}")
    print()


def summarise(engine: str, results: list[dict], note: str = "") -> dict:
    wake = [item for item in results if item["label"].startswith("wake")]
    other = [item for item in results if not item["label"].startswith("wake")]
    latencies = [item["latency_ms"] for item in wake if item["latency_ms"] is not None]
    return {
        "engine": engine,
        "case": "",
        "note": note,
        "cases": len(results),
        "wake_hit": f"{sum(1 for i in wake if i['hit'])}/{len(wake)}",
        "false_hit": f"{sum(1 for i in other if i['hit'])}/{len(other)}",
        "latency_ms": statistics.median(latencies) if latencies else None,
        "cpu_ms": sum(item["wall_ms"] for item in results),
    }


def collect_cases(args: argparse.Namespace) -> dict[str, list[np.ndarray]]:
    if args.mode == "wav":
        cases = load_wav_files(args.wav)
        print(f"loaded {len(cases)} wav files from {args.wav}")
        return cases
    if args.mode == "mic":
        cases = {}
        print("\nSilence pass: stay quiet for the whole recording.")
        cases["silence-1"] = to_frames(record(args.seconds))
        for take in range(max(1, args.repeat)):
            print(f"\nWake pass {take + 1}/{args.repeat}: say the wake word right away.")
            cases[f"wake-{take + 1}"] = to_frames(record(args.seconds))
        return cases
    print("\nSynth mode: synthetic audio only, it does not prove detection quality.")
    time_axis = np.arange(int(3.0 * SAMPLE_RATE)) / float(SAMPLE_RATE)
    tone = (0.3 * np.sin(2 * np.pi * 220.0 * time_axis) * 32767).astype(np.int16)
    return {
        "silence-1": to_frames(np.zeros(int(3.0 * SAMPLE_RATE), dtype=np.int16)),
        "speech-tone": to_frames(tone),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print("VoiceRunaCore wake word spike (TZ 17)")
    print(f"mode: {args.mode}   engines: {args.engine}   phrase: {args.phrase!r}")

    engines = ENGINES if args.engine == "all" else (args.engine,)
    cases = collect_cases(args)
    rows: list[dict] = []

    for engine in engines:
        backend, reason = build_backend(engine, args)
        if backend is None:
            print(f"\n[{engine}] unavailable: {reason}")
            rows.append(
                {
                    "engine": engine,
                    "case": "",
                    "note": f"unavailable: {reason}",
                    "cases": 0,
                    "wake_hit": "-",
                    "false_hit": "-",
                    "latency_ms": None,
                    "cpu_ms": 0.0,
                }
            )
            continue
        print(f"\n[{engine}] measuring...")
        results = [run_case(backend, label, frames) for label, frames in cases.items()]
        rows.append(summarise(engine, results))
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    print_table(rows)
    print("Target for the default engine: wake latency well under 300 ms, zero false")
    print("alarms on silence and on ordinary speech, every wake sample detected.")
    print("Without a microphone and real recordings this script only proves the")
    print("plumbing; see docs/spike-wakeword.md for the honest status.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

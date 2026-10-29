"""Audio devices, backend detection and the winsound WAV player (TZ 5.2, 11).

No real hardware and no real winsound: sounddevice and winsound are replaced by
small fakes in sys.modules.
"""

from __future__ import annotations

import sys
import time
import types

import numpy as np
import pytest

from voiceruna.voice import audio


# --------------------------------------------------------------------------- fakes
DEV_IN = {"name": "\u041c\u0438\u043a\u0440\u043e\u0444\u043e\u043d (USB)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0}
DEV_OUT = {"name": "\u0414\u0438\u043d\u0430\u043c\u0438\u043a\u0438 (Realtek)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}
DEV_MIX = {"name": "\u0421\u0442\u0435\u0440\u0435\u043e \u043c\u0438\u043a\u0448\u0435\u0440", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 2}
HOSTAPIS = [{"name": "MME"}]


class FakeSoundDevice:
    def __init__(self, devices, default=(-1, -1)) -> None:
        self._devices = list(devices)
        self._hostapis = list(HOSTAPIS)
        self.default = types.SimpleNamespace(device=list(default))

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return self._devices
        if isinstance(device, int):
            return self._devices[device]
        index = self.default.device[0 if kind == "input" else 1]
        return self._devices[max(0, index)]

    def query_hostapis(self):
        return self._hostapis


class FakeWinsound:
    SND_MEMORY = 0x0004
    SND_ASYNC = 0x0001
    SND_PURGE = 0x0040

    def __init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    def PlaySound(self, data, flags):
        self.calls.append((data, flags))


def use_sounddevice(monkeypatch, devices, default=(-1, -1)) -> FakeSoundDevice:
    fake = FakeSoundDevice(devices, default=default)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return fake


# --------------------------------------------------------------------------- devices
def test_list_devices_splits_inputs_and_outputs(monkeypatch):
    use_sounddevice(monkeypatch, [DEV_IN, DEV_OUT, DEV_MIX], default=(0, 1))
    devices = audio.list_devices()
    assert [item["name"] for item in devices["input"]] == [DEV_IN["name"], DEV_MIX["name"]]
    assert [item["name"] for item in devices["output"]] == [DEV_OUT["name"], DEV_MIX["name"]]
    assert devices["input"][0]["default"] is True
    assert devices["output"][0]["default"] is True
    assert devices["output"][0]["hostapi"] == "MME"
    assert devices["output"][0]["channels"] == 2
    assert devices["output"][0]["index"] == 1


def test_list_devices_without_sounddevice_is_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert audio.list_devices() == {"input": [], "output": []}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("default", None),
        ("", None),
        (None, None),
        ("\u043f\u043e \u0443\u043c\u043e\u043b\u0447\u0430\u043d\u0438\u044e", None),
        ("index:2", 2),
        ("2", 2),
        (3, 3),
        ("\u043d\u0435\u0442 \u0442\u0430\u043a\u043e\u0433\u043e", None),
    ],
)
def test_resolve_device_values(value, expected):
    assert audio.resolve_device(value, "output") == expected


def test_resolve_device_by_name(monkeypatch):
    use_sounddevice(monkeypatch, [DEV_IN, DEV_OUT, DEV_MIX], default=(0, 1))
    assert audio.resolve_device("realtek", "output") == 1
    assert audio.resolve_device("\u0414\u0418\u041d\u0410\u041c\u0418\u041a\u0418", "output") == 1
    assert audio.resolve_device("usb", "input") == 0


def test_resolve_output_device_reads_the_config(config):
    assert audio.resolve_output_device(config) is None
    config.set("audio.output_device", "index:5")
    assert audio.resolve_output_device(config) == 5


# --------------------------------------------------------------------------- backend
def test_output_backend_reports_portaudio(monkeypatch):
    use_sounddevice(monkeypatch, [DEV_IN, DEV_OUT], default=(0, 1))
    backend, message = audio.output_backend()
    assert backend == "portaudio"
    assert "PortAudio" in message


def test_output_backend_falls_back_to_winsound(monkeypatch):
    use_sounddevice(monkeypatch, [DEV_IN], default=(0, -1))
    monkeypatch.setattr(audio, "wave_out_devices", lambda: 2)
    backend, _message = audio.output_backend()
    assert backend == "winsound"


def test_output_backend_none_when_no_device_at_all(monkeypatch):
    use_sounddevice(monkeypatch, [DEV_IN], default=(0, -1))
    monkeypatch.setattr(audio, "wave_out_devices", lambda: 0)
    backend, message = audio.output_backend()
    assert backend == "none"
    assert "\u043d\u0435\u0442 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430 \u0432\u044b\u0432\u043e\u0434\u0430" in message


# --------------------------------------------------------------------------- wav
def test_to_wav_bytes_is_a_valid_riff():
    wav = audio.to_wav_bytes(np.zeros(1600, dtype=np.int16), 16000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert audio.wav_duration(wav) == pytest.approx(0.1, abs=0.01)


def test_wave_player_sends_memory_flags(monkeypatch):
    fake = FakeWinsound()
    monkeypatch.setitem(sys.modules, "winsound", fake)
    wav = audio.to_wav_bytes(np.zeros(800, dtype=np.int16), 16000)
    player = audio.WavePlayer()
    player.play_wav(wav, wait=False)
    assert fake.calls == [(wav, FakeWinsound.SND_MEMORY | FakeWinsound.SND_ASYNC)]


def test_wave_player_waits_for_the_sound_and_releases_the_buffer(monkeypatch):
    fake = FakeWinsound()
    monkeypatch.setitem(sys.modules, "winsound", fake)
    wav = audio.to_wav_bytes(np.zeros(1600, dtype=np.int16), 16000)  # 100 ms
    player = audio.WavePlayer(poll=0.005)
    started = time.monotonic()
    player.play_wav(wav)
    assert time.monotonic() - started >= 0.08
    assert player._buffer is None


def test_wave_player_stop_purges(monkeypatch):
    fake = FakeWinsound()
    monkeypatch.setitem(sys.modules, "winsound", fake)
    player = audio.WavePlayer()
    player.stop()
    assert fake.calls == [(None, FakeWinsound.SND_PURGE)]


def test_wave_player_without_winsound(monkeypatch):
    monkeypatch.setitem(sys.modules, "winsound", None)
    player = audio.WavePlayer()
    with pytest.raises(audio.AudioUnavailable):
        player.play_wav(b"RIFF____WAVE")


def test_wave_player_reports_a_playback_error(monkeypatch):
    class Broken(FakeWinsound):
        def PlaySound(self, data, flags):
            raise RuntimeError("no device")

    monkeypatch.setitem(sys.modules, "winsound", Broken())
    player = audio.WavePlayer()
    with pytest.raises(audio.AudioUnavailable):
        player.play_wav(b"RIFF____WAVE", wait=False)

# --------------------------------------------------------------------------- input validation
DEV_OUTPUT_ONLY = {"name": "G435 Наушники", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}


def test_output_only_device_is_rejected_and_falls_back(monkeypatch, config):
    use_sounddevice(monkeypatch, [DEV_IN, DEV_OUTPUT_ONLY], default=(0, -1))
    config.set("audio.input_device", "index:1")
    index, note = audio.resolve_input_device_info(config)
    assert index is None  # falls back to the system default input
    assert "не поддерживает запись" in note
    assert audio.resolve_input_device(config) is None


def test_valid_input_device_passes_without_a_note(monkeypatch, config):
    use_sounddevice(monkeypatch, [DEV_IN, DEV_OUTPUT_ONLY], default=(0, -1))
    config.set("audio.input_device", "index:0")
    index, note = audio.resolve_input_device_info(config)
    assert index == 0
    assert note == ""


def test_create_mic_carries_the_selection_note(monkeypatch, config):
    use_sounddevice(monkeypatch, [DEV_IN, DEV_OUTPUT_ONLY], default=(0, -1))
    config.set("audio.input_device", "index:1")
    mic = audio.create_mic(config)
    assert mic.selection_note and "не поддерживает запись" in mic.selection_note
    assert mic.device_index is None


# --------------------------------------------------------------------------- samplerate negotiation
class FakeInputStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.aborted = False

    def start(self) -> None:
        self.started = True

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        return None


class NegotiatingSoundDevice(FakeSoundDevice):
    """Refuses 16 kHz input, reports a 48 kHz native rate."""

    def __init__(self, devices, default=(-1, -1), supported=(16000,)) -> None:
        super().__init__(devices, default=default)
        self.supported = set(supported)
        self.streams: list[FakeInputStream] = []

    def check_input_settings(self, device=None, samplerate=16000, channels=1, dtype="int16"):
        if int(samplerate) not in self.supported:
            raise ValueError("Invalid sample rate")
        return None

    def InputStream(self, **kwargs):
        self.streams.append(FakeInputStream(**kwargs))
        return self.streams[-1]


def test_mic_opens_at_16k_when_supported(monkeypatch):
    fake = NegotiatingSoundDevice([dict(DEV_IN, default_samplerate=48000)], default=(0, -1))
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    mic = audio.SoundDeviceMic(device=0)
    mic.start()
    assert fake.streams[-1].kwargs["samplerate"] == 16000
    assert mic.capture_samplerate == 16000
    assert mic.native_samplerate == 48000
    assert mic.resampled is False


def test_mic_falls_back_to_native_rate_and_resamples(monkeypatch):
    fake = NegotiatingSoundDevice([dict(DEV_IN, default_samplerate=48000)], default=(0, -1), supported=(48000,))
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    mic = audio.SoundDeviceMic(device=0)
    mic.start()
    stream = fake.streams[-1]
    assert stream.kwargs["samplerate"] == 48000
    assert stream.kwargs["blocksize"] == 1536  # 512 frames after a 3:1 downsample
    assert mic.capture_samplerate == 48000
    assert mic.resampled is True

    # a 48 kHz callback block becomes a 16 kHz frame in the queue
    block = np.zeros((1536, 1), dtype=np.int16)
    mic._callback(block, 1536, None, None)
    frame = mic.read(0.1)
    assert frame is not None and frame.size == 512


def test_silent_format_is_skipped_in_favour_of_a_live_one(monkeypatch):
    """Camera microphones can be silent at 16 kHz mono but live at 44.1 kHz stereo."""
    fake = NegotiatingSoundDevice([dict(DEV_IN, default_samplerate=44100, max_input_channels=2)], default=(0, -1))
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(audio, "CAPTURE_PROBE_SEC", 0.05)
    monkeypatch.setattr(audio, "CAPTURE_NO_DATA_SEC", 0.02)

    mic = audio.SoundDeviceMic(device=0)
    opened: list[tuple[int, int]] = []

    class DummyStream:
        def start(self) -> None:
            return None

        def abort(self) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_open(sd, rate, channels):
        opened.append((rate, channels))
        mic._capture_samplerate = rate
        mic._channels = channels
        quiet = rate == 16000
        level = 5000 if channels == 2 else 3000
        for _ in range(4):
            mic.push(np.zeros(512, dtype=np.int16) if quiet else np.full(512, level, dtype=np.int16))
        return DummyStream()

    monkeypatch.setattr(audio.SoundDeviceMic, "_open_stream", staticmethod(fake_open))
    mic.start()

    assert mic.capture_samplerate == 44100
    assert mic._channels == 2
    assert (16000, 1) in opened, "тихий формат тоже должен быть проверен"

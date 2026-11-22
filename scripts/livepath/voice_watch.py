"""Read main's TTS timing rows and report silence on the shared Windows cable."""

from __future__ import annotations

import argparse
from collections import deque
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
import json
import math
import os
from pathlib import Path
import queue
import re
import statistics
import sys
import time


RATE = 48_000
BLOCK = 0.10
MAX_PLAY = 120.0
NOISE_LIMIT = 10 ** (-75 / 20)
MIN_FLOOR = 10 ** (-100 / 20)
WASAPI = "Windows WASAPI"
_TOKEN = re.compile(rb'"(?:[^"\\\r\n]|\\.)*"|[{}\[\],:]|[^\s{}\[\],:"]+')


@dataclass(frozen=True)
class Play:
    logged_at: float
    seconds: float

    @property
    def end(self):
        return self.logged_at - 0.30  # Main's post-playback mic tail.

    @property
    def start(self):
        return self.end - self.seconds

    @property
    def measurement_start(self):
        return self.start - 0.15

    @property
    def measurement_end(self):
        return self.end + 1.15  # The local row timestamp truncates fractional seconds.


def _token_bytes(match):
    return match.group()


def _skip_value(first, tokens):
    # Strings are skipped as tokens, without decoding their contents.
    depth = int(first[0] in (ord("{"), ord("[")))
    while depth:
        char = next(tokens)[0]
        depth += int(char in (ord("{"), ord("["))) - int(char in (ord("}"), ord("]")))


def _object(tokens, keys, *, extra=False):
    fields = {}
    key = next(tokens)
    while key != b"}":
        if key[:1] != b'"' or next(tokens) != b":":
            raise ValueError("invalid telemetry object")
        value = next(tokens)
        if key in keys and value[:1] not in (b"{", b"["):
            fields[key] = value
        elif extra and key == b'"extra"' and value == b"{":
            fields.update(_object(tokens, {b'"play_ms"'}))
        else:
            _skip_value(value, tokens)
        delimiter = next(tokens)
        if delimiter == b"}":
            break
        if delimiter != b",":
            raise ValueError("invalid telemetry delimiter")
        key = next(tokens)
    return fields


def parse_play(line: bytes) -> Play | None:
    """Extract only main's subsystem, success bit, local timestamp and play duration."""
    tokens = map(_token_bytes, _TOKEN.finditer(line))
    try:
        if next(tokens) != b"{":
            return None
        row = _object(tokens, {b'"subsystem"', b'"ts"', b'"ok"'}, extra=True)
        if row.get(b'"subsystem"') != b'"tts"' or row.get(b'"ok"') != b"true":
            return None
        seconds = float(row[b'"play_ms"']) / 1000
        stamp = datetime.strptime(row[b'"ts"'].decode("ascii"), '"%Y-%m-%dT%H:%M:%S"').timestamp()
        return Play(stamp, seconds) if 0 < seconds <= MAX_PLAY else None
    except (StopIteration, KeyError, ValueError, OverflowError, OSError):
        return None


class JournalTail:
    """Follow one explicitly supplied API telemetry file; skip existing history."""

    def __init__(self, path):
        self.path = path
        self.offset = None
        self.identity = None

    def read(self):
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            if self.offset is None:
                self.offset = 0
            return []
        with stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if self.offset is None:
                self.offset = stat.st_size
            elif (self.identity is not None and identity != self.identity) or stat.st_size < self.offset:
                self.offset = 0
            self.identity = identity
            stream.seek(self.offset)
            plays = []
            for _ in range(256):
                line = stream.readline()
                if not line.endswith(b"\n"):
                    break
                self.offset += len(line)
                play = parse_play(line)
                if play is not None:
                    plays.append(play)
            return plays


class CaptureUnavailable(ValueError):
    pass


def device_facts(sd, cable_name):
    """Require one WASAPI pair that agrees with main's first output name."""
    devices = list(sd.query_devices())
    hosts = list(sd.query_hostapis())
    needle = cable_name.lower()
    if not needle.strip():
        raise CaptureUnavailable()

    def describe(index):
        device = devices[index]
        return {"index": index, "name": device["name"], "host": hosts[device["hostapi"]]["name"]}

    outputs = [describe(i) for i, device in enumerate(devices)
               if needle in device["name"].lower() and device["max_output_channels"] > 0]
    renders = [device for device in outputs if device["host"] == WASAPI]
    if len(renders) != 1:
        raise CaptureUnavailable()
    main, render = outputs[0], renders[0]
    main_name, render_name = main["name"].casefold(), render["name"].casefold()
    truncated_mme = main["host"] == "MME" and len(main_name) == 31 and render_name.startswith(main_name)
    if main_name != render_name and not truncated_mme:
        raise CaptureUnavailable()
    capture_name, changes = re.subn(r"\binput(?=\s*(?:\(|$))", "output", render_name, count=1)
    captures = [describe(i) for i, device in enumerate(devices)
                if device["name"].casefold() == capture_name and device["max_input_channels"] > 0
                and hosts[device["hostapi"]]["name"] == WASAPI]
    if not changes or len(captures) != 1:
        raise CaptureUnavailable()
    default_index = sd.default.device[1]
    default = describe(default_index) if default_index is not None and 0 <= default_index < len(devices) else None
    return {"main_match": main, "cable_playback": render, "cable_capture": captures[0],
            "portaudio_default_playback": default}


def preferred_playback():
    """Read the current WinMM preferred output without opening a device."""
    class WaveOutCaps(ctypes.Structure):
        _fields_ = [("manufacturer", ctypes.c_uint16), ("product", ctypes.c_uint16),
                    ("version", ctypes.c_uint32), ("name", ctypes.c_wchar * 32),
                    ("formats", ctypes.c_uint32), ("channels", ctypes.c_uint16),
                    ("reserved", ctypes.c_uint16), ("support", ctypes.c_uint32)]

    winmm = ctypes.WinDLL("winmm")
    message = winmm.waveOutMessage
    message.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    message.restype = ctypes.c_uint
    caps = winmm.waveOutGetDevCapsW
    caps.argtypes = [ctypes.c_size_t, ctypes.POINTER(WaveOutCaps), ctypes.c_uint]
    caps.restype = ctypes.c_uint
    index, flags = ctypes.c_uint32(), ctypes.c_uint32()
    # WAVE_MAPPER and DRVM_MAPPER_PREFERRED_GET from mmddk.h.
    if message(0xFFFFFFFF, 0x2000 + 21, ctypes.byref(index), ctypes.byref(flags)) or index.value == 0xFFFFFFFF:
        return None
    device = WaveOutCaps()
    if caps(index.value, ctypes.byref(device), ctypes.sizeof(device)):
        return None
    return {"index": index.value, "name": device.name, "index_space": "WinMM"}


@dataclass(frozen=True)
class Level:
    start: float
    end: float
    peak: float | None


class VoiceWatch:
    def __init__(self, out, *, seconds=6.0, window=45.0, log=print, live_default=lambda: None):
        self.out = out
        self.seconds = seconds
        self.window = window
        self.log = log
        self.live_default = live_default
        self.levels = deque(maxlen=math.ceil((window + MAX_PLAY + 10) / BLOCK))
        self.pending = deque()
        self.recent = deque()
        self.silent = deque()
        self.faulted = False
        self.unmeasurable = False

    def unavailable(self, reason):
        self.silent.clear()
        if not self.unmeasurable:
            self.log(f"voice_watch UNMEASURABLE reason={reason}")
            self.unmeasurable = True

    def capture_closed(self):
        self.levels.clear()
        self.pending.clear()
        self.recent.clear()
        self.unavailable("capture")

    def _measure(self, play):
        start, end = play.measurement_start, play.measurement_end
        inside, outside = [], []
        covered = start
        gap = 0.025
        for level in self.levels:
            if level.end <= start:
                if (level.start >= start - self.window and level.peak is not None
                        and not any(other.measurement_start < level.end and other.measurement_end > level.start
                                    for other in self.recent)):
                    outside.append(level.peak)
            elif level.start < end:
                if level.peak is None or level.start > covered + gap:
                    return None
                inside.append(level.peak)
                covered = max(covered, level.end)
            else:
                break
        if len(outside) < 5 or not inside or covered < end - gap:
            return None
        return max(inside), statistics.median(outside)

    def _publish(self, now, facts):
        spans = list(self.silent)
        seconds = sum(play.seconds for play, _, _ in spans)
        if self.faulted or len(spans) < 2 or seconds < self.seconds:
            return
        try:
            default = self.live_default()
        except OSError:
            default = None
        event = {
            "event": "voice_output_lost",
            "time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "devices": {**facts, "default_playback": default},
            "plays": len(spans),
            "play_seconds": round(seconds, 3),
            "window_seconds": self.window,
            "max_play_peak": max(peak for _, peak, _ in spans),
            "max_outside_floor": max(floor for _, _, floor in spans),
            "noise_peak_limit": NOISE_LIMIT,
        }
        path = self.out / "voice_output_lost.json"
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(event) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError:
            self.log("voice_watch report_unavailable")
            return
        self.log(f"voice_output_lost at={event['time']} plays={len(spans)} seconds={seconds:.3f}")
        self.faulted = True

    def poll(self, now, plays, levels, facts):
        self.levels.extend(levels)
        for play in plays:
            if now - self.window <= play.logged_at <= now + 1:
                self.pending.append(play)
                self.recent.append(play)
        while self.recent and self.recent[0].measurement_end < now - self.window - MAX_PLAY - 5:
            self.recent.popleft()
        while self.pending and self.pending[0].logged_at < now - self.window:
            self.pending.popleft()
        if not any(level.peak is not None and level.end >= now - 1
                   for level in islice(reversed(self.levels), 15)):
            self.unavailable("no_capture_samples")
            return
        while self.pending and now >= self.pending[0].measurement_end + 0.2:
            play = self.pending.popleft()
            measured = self._measure(play)
            if measured is None:
                self.unavailable("play_coverage_or_baseline")
                continue
            self.unmeasurable = False
            peak, floor = measured
            relative_limit = 2 * max(floor, MIN_FLOOR)
            if peak > relative_limit:
                self.silent.clear()
                if self.faulted:
                    self.log("voice_output_back")
                    self.faulted = False
            elif peak <= NOISE_LIMIT:
                while self.silent and self.silent[0][0].start < play.end - self.window:
                    self.silent.popleft()
                if not self.silent or play.start >= self.silent[-1][0].end:
                    self.silent.append((play, peak, floor))
                    self._publish(now, facts)
            else:
                self.silent.clear()


def run(sd, journal, watch, cable_name, *, clock=time.time, sleep=time.sleep):
    journal.read()  # Establish the tail before capture. Existing rows lack readback.
    while True:
        captured = queue.Queue(maxsize=200)

        def read_level(data, frames, timing, status, output=captured):
            if not frames:
                return
            now = clock()
            start = now + timing.inputBufferAdcTime - timing.currentTime
            end = start + frames / RATE
            if not math.isfinite(end) or abs(end - now) > 2:
                start, end = now - frames / RATE, now
                peak = None
            else:
                peak = None if status else max(float(data.max()), -float(data.min()))
                if peak is not None and not math.isfinite(peak):
                    peak = None
            try:
                output.put_nowait(Level(start, end, peak))
            except queue.Full:
                pass  # Missing time ranges will fail the coverage check.

        # The stream is owned here, not by a `with`: constructing it opens the native endpoint, and a start that
        # raises after that (the device unplugged during startup) would skip a context exit and leave the handle
        # open on the audio path OBS shares, once per retry. It is closed in every case (review M7c).
        stream = None
        try:
            facts = device_facts(sd, cable_name)
            stream = sd.InputStream(device=facts["cable_capture"]["index"], samplerate=RATE,
                                    channels=2, blocksize=int(RATE * BLOCK), dtype="float32",
                                    extra_settings=sd.WasapiSettings(exclusive=False, auto_convert=True),
                                    callback=read_level)
            stream.start()
            opened = clock()
            while stream.active:
                levels = []
                while not captured.empty():
                    levels.append(captured.get_nowait())
                now = clock()
                if levels or now - opened >= 1.0:
                    watch.poll(now, journal.read(), levels, facts)
                sleep(BLOCK)
        except (OSError, ValueError, sd.PortAudioError):
            pass
        finally:
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError, sd.PortAudioError):
                    pass
        watch.capture_closed()
        sleep(1.0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path, help="Main's per-session API telemetry JSONL")
    parser.add_argument("--out", required=True, type=Path, help="Directory for voice_output_lost.json")
    parser.add_argument("--virtual-cable-name", default="CABLE Input",
                        help="Match main's deployed virtual_cable_name")
    parser.add_argument("--seconds", type=float, default=6.0, help="Required silent play time")
    parser.add_argument("--window", type=float, default=45.0, help="Maximum episode evidence window in seconds")
    args = parser.parse_args(argv)
    if not (0 < args.seconds <= args.window <= 600):
        parser.error("require 0 < --seconds <= --window <= 600")
    args.out.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        parser.error("Windows shared WASAPI is required")
    import sounddevice as sd

    try:
        run(sd, JournalTail(args.journal),
            VoiceWatch(args.out, seconds=args.seconds, window=args.window, live_default=preferred_playback,
                       log=lambda line: print(line, flush=True)),
            args.virtual_cable_name)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Handwritten main-format telemetry and scalar cable levels. No native audio."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from scripts.livepath import voice_watch as vw


START = 1_801_000_000
FLOOR = 0.00001
FACTS = {
    "cable_playback": {"name": "CABLE Input", "index": 3, "host": "Windows WASAPI"},
    "cable_capture": {"name": "CABLE Output", "index": 4, "host": "Windows WASAPI"},
    "portaudio_default_playback": {"name": "Speakers", "index": 0, "host": "MME"},
}


def main_row(end, seconds, **extra_fields):
    return {
        "ts": datetime.fromtimestamp(START + end).strftime("%Y-%m-%dT%H:%M:%S"),
        "subsystem": "tts",
        "ok": True,
        "extra": {"play_ms": seconds * 1000, "synth_ms": 200},
        **extra_fields,
    }


class Fixture:
    def __init__(self, tmp_path):
        self.out = tmp_path / "out"
        self.out.mkdir()
        self.path = tmp_path / "synthetic.jsonl"
        self.path.touch()
        self.journal = vw.JournalTail(self.path)
        assert self.journal.read() == []
        self.watch = vw.VoiceWatch(self.out, live_default=lambda: {
            "name": "Headset (ExampleBuds)", "index": 2, "index_space": "WinMM",
        })
        self.step = 0

    @property
    def fault(self):
        return self.out / "voice_output_lost.json"

    def advance(self, until, *, voice=None, missing=None):
        levels = []
        for i in range(self.step, round(until * 10)):
            at = i / 10
            if missing and missing[0] <= at < missing[1]:
                continue
            peak = voice[2] if voice and voice[0] <= at < voice[1] else FLOOR
            levels.append(vw.Level(START + at, START + (i + 1) / 10, peak))
        self.step = round(until * 10)
        self.watch.poll(START + until, self.journal.read(), levels, FACTS)

    def play(self, end, seconds, *, audible=FLOOR, missing=None, **extra_fields):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(main_row(end, seconds, **extra_fields)) + "\n")
        self.advance(end + 1.2, voice=(end - seconds - 0.3, end - 0.3, audible), missing=missing)


def test_fault_after_two_silent_plays_reports_actual_totals_and_device_facts(tmp_path, capsys):
    rig = Fixture(tmp_path)
    rig.play(10, 3)
    assert not rig.fault.exists()
    rig.play(20, 3)
    fault = json.loads(rig.fault.read_text())
    assert fault["event"] == "voice_output_lost"
    assert fault["plays"] == 2 and fault["play_seconds"] == 6 and fault["window_seconds"] == 45
    assert fault["max_play_peak"] == fault["max_outside_floor"] == FLOOR
    assert fault["devices"]["cable_capture"] == FACTS["cable_capture"]
    assert fault["devices"]["default_playback"]["name"] == "Headset (ExampleBuds)"
    assert capsys.readouterr().out.count("voice_output_lost") == 1


def test_pauses_and_one_intentionally_silent_clip_do_not_count_as_an_episode(tmp_path, capsys):
    rig = Fixture(tmp_path)
    rig.advance(20)
    rig.play(31, 7)
    rig.advance(42)
    assert not rig.fault.exists()
    assert not capsys.readouterr().out
    rig.play(80, 3)
    assert not rig.fault.exists()  # The previous play has fallen out of the window.


def test_quiet_but_present_speech_breaks_silent_evidence(tmp_path):
    rig = Fixture(tmp_path)
    rig.play(10, 3)
    rig.play(20, 3, audible=0.00006)  # Below the absolute noise limit, above this cable's floor.
    rig.play(30, 3)
    assert not rig.fault.exists()


def test_one_fault_per_episode_and_single_back_line(tmp_path, capsys):
    rig = Fixture(tmp_path)
    rig.play(10, 3)
    rig.play(20, 3)
    original = rig.fault.read_bytes()
    rig.play(30, 4)
    assert rig.fault.read_bytes() == original
    rig.play(40, 4, audible=0.004)
    rig.play(50, 4, audible=0.004)
    rig.play(60, 3)
    rig.play(70, 3)
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == [
        "voice_output_lost", "voice_output_back", "voice_output_lost",
    ]
    assert rig.fault.read_bytes() != original


def test_capture_gap_is_unmeasurable_and_clears_evidence(tmp_path, capsys):
    rig = Fixture(tmp_path)
    rig.play(10, 3)
    rig.play(20, 3, missing=(17.8, 18.1))
    rig.play(30, 3, missing=(27.8, 28.1))
    rig.play(40, 3)
    assert not rig.fault.exists()
    assert capsys.readouterr().out.count("UNMEASURABLE") == 1


def test_journal_words_do_not_reach_stdout_or_any_output_file(tmp_path, capsys):
    rig = Fixture(tmp_path)
    marker = "NEVER FORWARD THESE WORDS"
    words = marker + ' \"subsystem\":\"tts\" ' + '{"extra":{"play_ms":9999}}'
    rig.play(10, 3, text=words, unknown={"nested": [words, {"text": marker}]})
    rig.play(20, 3, text=marker)
    streams = capsys.readouterr()
    assert rig.fault.exists()
    assert marker not in streams.out and marker not in streams.err
    assert all(marker.encode() not in path.read_bytes() for path in rig.out.iterdir())
    assert marker.encode() in rig.path.read_bytes()


def test_tail_needs_a_complete_successful_main_timing_row(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    path.write_text(json.dumps(main_row(10, 3)) + "\n")
    tail = vw.JournalTail(path)
    assert tail.read() == []
    row = json.dumps(main_row(20, 3))
    with path.open("a") as stream:
        stream.write(json.dumps(main_row(18, 3, ok=False)) + "\n")
        stream.write(row[:15])
    assert tail.read() == []
    with path.open("a") as stream:
        stream.write(row[15:] + "\n")
    assert tail.read() == [vw.Play(START + 20, 3)]
    assert tail.read() == []


class FakeSoundDevice:
    class PortAudioError(Exception):
        pass

    def __init__(self):
        render = "CABLE Input (VB-Audio Virtual Cable)"
        self.devices = [self.device(render[:31], 0, playback=True),
                        self.device(render, 1, playback=True),
                        self.device("CABLE Output (VB-Audio Virtual Cable)", 1, playback=False)]
        self.default = SimpleNamespace(device=(None, 0))
        self.capture_attempts = []

    @staticmethod
    def device(name, host, *, playback):
        return {"name": name, "hostapi": host, "max_output_channels": 2 if playback else 0,
                "max_input_channels": 0 if playback else 2}

    def query_devices(self):
        return self.devices

    def query_hostapis(self):
        return [{"name": "MME"}, {"name": "Windows WASAPI"}]

    def WasapiSettings(self, **settings):
        assert settings == {"exclusive": False, "auto_convert": True}
        return settings

    def InputStream(self, **request):
        self.capture_attempts.append(request["device"])
        raise self.PortAudioError("synthetic capture failure")


def test_unopenable_capture_logs_once_and_never_reports_a_fault(tmp_path, capsys):
    rig = Fixture(tmp_path)
    sd = FakeSoundDevice()

    def next_iteration(_):
        if len(sd.capture_attempts) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        vw.run(sd, rig.journal, rig.watch, "CABLE Input", sleep=next_iteration)
    assert sd.capture_attempts == [2, 2]
    assert not rig.fault.exists()
    assert capsys.readouterr().out.splitlines() == ["voice_watch UNMEASURABLE reason=capture"]


def test_shared_capture_callback_and_journal_loop_report_without_playback(tmp_path, monkeypatch):
    rig = Fixture(tmp_path)
    sd = FakeSoundDevice()
    steps, closed = 0, []
    request = {}

    def input_stream(**kwargs):
        request.update(kwargs)
        return SimpleNamespace(active=True, start=lambda: None, close=lambda: closed.append(True))

    monkeypatch.setattr(sd, "InputStream", input_stream)

    def sleep(seconds):
        nonlocal steps
        assert seconds == vw.BLOCK
        steps += 1
        request["callback"](SimpleNamespace(max=lambda: FLOOR, min=lambda: -FLOOR),
                            4800, SimpleNamespace(inputBufferAdcTime=0, currentTime=vw.BLOCK), False)
        if steps in (101, 201):
            with rig.path.open("a") as stream:
                stream.write(json.dumps(main_row(steps // 10, 3)) + "\n")
        if steps >= 225:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        vw.run(sd, rig.journal, rig.watch, "CABLE Input", clock=lambda: START + steps / 10, sleep=sleep)
    assert request["device"] == 2 and request["extra_settings"]["exclusive"] is False
    assert closed == [True]
    assert json.loads(rig.fault.read_text())["play_seconds"] == 6


def test_a_start_that_fails_after_the_open_still_closes_the_stream_on_every_retry(tmp_path, monkeypatch, capsys):
    """Review M7c: the endpoint is opened by the constructor; a start that raises must not leak it per retry."""
    rig = Fixture(tmp_path)
    sd = FakeSoundDevice()
    opened, closed = [], []

    def failing_start():
        raise sd.PortAudioError("synthetic: the device went away during start")

    def input_stream(**kwargs):
        opened.append(kwargs["device"])
        return SimpleNamespace(active=True, start=failing_start, close=lambda: closed.append(True))

    monkeypatch.setattr(sd, "InputStream", input_stream)

    def next_iteration(_):
        if len(opened) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        vw.run(sd, rig.journal, rig.watch, "CABLE Input", sleep=next_iteration)
    assert opened == [2, 2, 2] and closed == [True, True, True]
    assert not rig.fault.exists()
    assert capsys.readouterr().out.splitlines() == ["voice_watch UNMEASURABLE reason=capture"]


@pytest.mark.parametrize("second_render", [False, True])
def test_wrong_or_ambiguous_pair_cannot_be_measured(second_render):
    sd = FakeSoundDevice()
    sd.devices[0] = sd.device("CABLE Input (Different Cable)", 0, playback=True)
    if second_render:
        sd.devices.append(sd.device("CABLE Input (Different Cable)", 1, playback=True))
    with pytest.raises(vw.CaptureUnavailable):
        vw.device_facts(sd, "CABLE Input")
    assert sd.capture_attempts == []

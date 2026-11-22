"""Offline tests for scripts/m1_operator/audio_witness.py (A3): analysis + verdicts on synthetic WAVs, the wall-clock
WAV timeline, the recorder with a fake capture source / meter / clock, preconditions and the COM struct layouts.

No audio device, no COM activation, no game, no window, no port. Run:
  ./venv/Scripts/python.exe -m pytest tests/test_m1_operator_audio_witness.py -q
"""
import ctypes, json, os, sys, time, wave
from argparse import Namespace
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("audio_witness.py builds WASAPI COM GUID structs at import (Windows ctypes layout)",
                allow_module_level=True)
np = pytest.importorskip("numpy")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts", "m1_operator"))
import audio_witness as A  # noqa: E402

RATE = 8000          # small files; the analysis reads the rate from the WAV
T0E = time.mktime((2026, 10, 7, 12, 0, 0, 0, 0, -1))


def tone(seconds, dbfs, rate=RATE, freq=440.0):
    n = int(seconds * rate)
    amp = 10 ** (dbfs / 20) * 32767 * np.sqrt(2)          # RMS = dbfs
    x = amp * np.sin(2 * np.pi * freq * np.arange(n) / rate)
    return np.stack([x, x], axis=1)


def write_case(d, segments, swings_rel=(), fg_switch=30.0, wall=None, meter_peak=0.2, meter_ok=True, activation_ok=True,
               fg_violation_at=None, pad_spans=(), bursts=True):
    """segments: [(seconds, dbfs or None=digital silence)]; swings_rel: transient times (s) burst +20 dB."""
    parts = [tone(s, db) if db is not None else np.zeros((int(s * RATE), 2)) for s, db in segments]
    x = np.concatenate(parts)
    for t in (swings_rel if bursts else ()):
        i = int((t + 0.2) * RATE)
        x[i:i + int(0.15 * RATE)] = tone(0.15, -6)[: len(x[i:i + int(0.15 * RATE)])]
    x = np.clip(x, -32768, 32767).astype("<i2")
    with wave.open(str(d / "audio.wav"), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(x.tobytes())
    wall = wall if wall is not None else len(x) / RATE
    rows = [dict(t=round(float(t), 2), fg=1, fh_fg=bool(t < fg_switch), fh_iconic=False, fh_exists=True)
            for t in np.arange(0, wall, 0.1)]
    if fg_violation_at is not None:
        for r in rows:
            if abs(r["t"] - fg_violation_at) < 0.3:
                r["fh_fg"] = True
    (d / "foreground.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    m = [dict(t=round(float(t), 2), fh_peak=meter_peak, host_peak=0.0) for t in np.arange(0, wall, 0.05)]
    (d / "meter.jsonl").write_text("\n".join(json.dumps(r) for r in m) + "\n", encoding="utf-8")
    lines = []
    for t in swings_rel:
        lt = time.localtime(T0E + t)
        lines.append(f"[{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d} INFO  Arti Stardew Bridge] background HOLD COMPLETION "
                     f"run=1 kind=Tool outcome=InputCompleted press_count=14 cleanup=True reason=null swings=1 opened=False")
    (d / "smapi-slice.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cap = dict(t0_epoch=T0E, utc_start="x", wall_seconds=wall, wav_seconds=len(x) / RATE, padded_seconds=0.0,
               pad_spans=list(pad_spans), activation=dict(ok=activation_ok, error=None if activation_ok else "E_FAIL"),
               meter_ok=meter_ok, meter_error=None if meter_ok else "no COM", capture_error=None)
    (d / "capture.json").write_text(json.dumps(cap), encoding="utf-8")
    return d


# ---------------------------------------------------------------- verdicts
def test_pass_by_reference_and_swings(tmp_path):
    swings = [40 + 2.0 * i for i in range(20)]               # integral seconds: SMAPI stamps are floor(second)
    write_case(tmp_path, [(30, -30), (130, -35)], swings_rel=swings)
    r = A.analyze(tmp_path)
    assert r["verdict"] == "PASS", r["causes"]
    assert r["criteria"]["crit_i"] and r["criteria"]["crit_ii"]
    assert r["criteria"]["swings_B"] == 20 and r["criteria"]["swings_B_matched"] >= 16
    assert r["A"]["seconds"] == pytest.approx(30, abs=0.2) and r["B"]["seconds"] >= 120
    for name in ("result.json", "WITNESS.md", "SHA256SUMS.txt", "reference.wav"):
        assert (tmp_path / name).exists()
    sums = (tmp_path / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "audio.wav" in sums and "result.json" in sums and "SHA256SUMS.txt" not in sums


def test_pass_by_swings_alone_without_reference(tmp_path):
    swings = [5 + 3.0 * i for i in range(10)]
    write_case(tmp_path, [(140, -70)], swings_rel=swings, fg_switch=0.0)        # quiet music, distinct chops
    r = A.analyze(tmp_path)
    assert r["segments"]["A"] is None
    assert r["criteria"]["crit_ii"] and not r["criteria"]["crit_i"]
    assert r["verdict"] == "PASS", r["causes"]


def test_product_failure_when_inactive_goes_silent(tmp_path):
    write_case(tmp_path, [(30, -30), (130, None)], swings_rel=[50, 60, 70], meter_peak=0.0, bursts=False)
    r = A.analyze(tmp_path)
    assert r["verdict"] == "PRODUCT_FAILURE", r["causes"]


def test_silent_b_with_live_meter_is_not_a_product_failure(tmp_path):
    write_case(tmp_path, [(30, -30), (130, None)], swings_rel=[50, 60, 70], meter_peak=0.3, bursts=False)
    assert A.analyze(tmp_path)["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("kw,cause", [
    (dict(segments=[(30, -30), (60, -35)]), "segment_B_too_short"),
    (dict(segments=[(30, -30), (130, -35)], fg_violation_at=100.0), "farmhand_foreground_or_minimized_during_B"),
    (dict(segments=[(30, None), (130, None)], meter_peak=0.0), "reference_A_silent"),
    (dict(segments=[(30, -30), (130, -35)], meter_peak=0.0), "meter_disagrees"),
    (dict(segments=[(30, -30), (130, -35)], activation_ok=False), "activation_failed"),
    (dict(segments=[(30, -30), (130, -35)], meter_ok=False), "meter_unavailable"),
    (dict(segments=[(30, -30), (130, -35)], wall=200.0), "wav_duration_mismatch"),
])
def test_inconclusive_names_the_cause(tmp_path, kw, cause):
    segs = kw.pop("segments")
    write_case(tmp_path, segs, **kw)
    r = A.analyze(tmp_path)
    assert r["verdict"] == "INCONCLUSIVE"
    assert any(c.startswith(cause) for c in r["causes"]), r["causes"]


def test_padding_is_counted_separately(tmp_path):
    write_case(tmp_path, [(30, -30), (130, -35)], pad_spans=[(int(50 * RATE), int(10 * RATE))])
    r = A.analyze(tmp_path)
    assert r["B"]["padded_fraction"] == pytest.approx(10 / r["B"]["seconds"], abs=0.01)


# ---------------------------------------------------------------- pieces
def test_parse_swings_dates_window_and_midnight():
    t0 = time.mktime((2026, 10, 7, 23, 59, 50, 0, 0, -1))
    text = "\n".join([
        "[23:59:55 INFO  Arti Stardew Bridge] background HOLD COMPLETION run=1 kind=Tool outcome=InputCompleted swings=1",
        "[00:00:05 INFO  Arti Stardew Bridge] background HOLD COMPLETION run=2 kind=Tool outcome=InputCompleted x=1 swings=1",
        "[00:00:06 INFO  Arti Stardew Bridge] background HOLD COMPLETION run=3 kind=Tool outcome=InputCompleted swings=0",
        "[00:00:07 INFO  Arti Stardew Bridge] background HOLD COMPLETION run=4 kind=Action outcome=InputCompleted swings=1",
        "[12:00:00 INFO  Arti Stardew Bridge] background HOLD COMPLETION run=5 kind=Tool outcome=InputCompleted swings=1",
    ])
    got = A.parse_swings(text, t0, t0 + 60)
    assert [round(g - t0) for g in got] == [5, 15]


def test_segments_reference_switch_and_violation():
    rows = [dict(t=t / 10, fh_fg=t < 200, fh_iconic=False, fh_exists=True) for t in range(0, 1600)]
    s = A.segments(rows, 160.0)
    assert s["A"] == (0.0, 20.0) and s["B"][0] == pytest.approx(20.5) and s["b_violations"] == 0
    rows[1000]["fh_iconic"] = True
    assert A.segments(rows, 160.0)["b_violations"] == 1
    assert A.segments([dict(t=0, fh_fg=True)], 10)["B"] is None


def test_wav_timeline_pads_gaps_and_tail(tmp_path):
    w = A.WavTimeline(tmp_path / "a.wav", rate=RATE)
    w.start(100.0)
    pkt = bytes(4 * 80)                                   # 10 ms
    for i in range(100):                                  # 1 s of packets, QPC-placed
        w.packet(pkt, 80, False, 100.0 + i * 0.01, 100.0 + i * 0.01)
    w.idle(101.0)                                         # not idle yet (< 0.25 s)
    w.idle(103.0)                                         # 2 s without packets: pad to now - idle/2
    w.packet(pkt, 80, True, 104.0, 104.0)                 # silent-flag packet after a 1 s gap: gap padded first
    st = w.close(106.0)
    with wave.open(str(tmp_path / "a.wav")) as r:
        dur = r.getnframes() / r.getframerate()
    assert dur == pytest.approx(6.0, abs=0.02)
    assert st["padded_frames"] / RATE == pytest.approx(5.0 - 0.01, abs=0.05)
    assert st["silent_flag_frames"] == 80 and st["packets"] == 101
    assert len(st["pad_spans"]) == 3


class FakeSource:
    def __init__(self, clock, fail=False):
        self.clock, self.fail, self.closed = clock, fail, False

    def open(self):
        if self.fail:
            raise OSError("process-loopback activation failed hr=0x80070057")
        return dict(ok=True, buffer_frames=480)

    def read(self, _timeout):
        t = self.clock()
        if 3.0 <= t - 1000.0 <= 4.0:                     # the process renders nothing for 1 s
            return []
        return [(bytes(4 * 400), 400, False, t)]          # 50 ms at 8 kHz per tick

    def close(self):
        self.closed = True


class FakeMeter:
    sessions = [dict(role="fh", pid=11, device="d", state=1, volume=1.0, mute=0)]

    def open(self):
        pass

    def refresh(self):
        return self.sessions

    def sample(self):
        return dict(fh_peak=0.25, host_peak=0.1)

    def close(self):
        pass


def _clock():
    st = dict(t=1000.0)
    return (lambda: st["t"]), (lambda s: st.__setitem__("t", st["t"] + s))


def test_record_with_fake_source_matches_wall_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "RATE", RATE)
    clock, sleep = _clock()
    src = FakeSource(clock)
    smapi = type("S", (), dict(start=lambda self: None, read_new=lambda self: "line\n"))()
    orig = A.WavTimeline.__init__
    monkeypatch.setattr(A.WavTimeline, "__init__", lambda self, path, rate=RATE, **kw: orig(self, path, rate=RATE, **kw))
    cap = A.record(tmp_path, 6.0, src, FakeMeter(), lambda: dict(fg=1, fh_fg=False, fh_iconic=False, fh_exists=True),
                   clock, lambda: T0E, sleep, log=lambda m: None, threaded=False, smapi=smapi)
    assert cap["activation"]["ok"] and src.closed and cap["meter_ok"]
    assert cap["wav_seconds"] == pytest.approx(cap["wall_seconds"], rel=0.02)
    assert cap["padded_seconds"] > 0.5
    rows = A.load_jsonl(tmp_path / "foreground.jsonl")
    assert len(rows) >= 55                                # >= 10 Hz over 6 s
    assert len(A.load_jsonl(tmp_path / "meter.jsonl")) >= 110   # 20 Hz
    assert (tmp_path / "smapi-slice.txt").read_text(encoding="utf-8") == "line\n"


def test_record_activation_failure_still_writes_a_timeline(tmp_path):
    clock, sleep = _clock()
    cap = A.record(tmp_path, 1.0, FakeSource(clock, fail=True), None, lambda: dict(fg=1, fh_fg=False), clock,
                   lambda: T0E, sleep, log=lambda m: None, threaded=False)
    assert cap["activation"]["ok"] is False and "activation failed" in cap["activation"]["error"]
    assert cap["padded_seconds"] == pytest.approx(cap["wall_seconds"], abs=0.01)


class FakeFacts:
    def __init__(self, **kw):
        self.w = {7: dict(exists=True, pid=11, title="Stardew Valley - running SMAPI [farmhand]"),
                  9: dict(exists=True, pid=22, title="Stardew Valley")}
        self.ex = kw.get("exe", "StardewModdingAPI.exe")
        self.sess = kw.get("session", dict(world_ready=True, session_id="s1"))
        self.is_alive = kw.get("alive", True)

    def window(self, h):
        return self.w.get(h, dict(exists=False))

    def exe(self, pid):
        return self.ex

    def alive(self, pid):
        return self.is_alive

    def session(self):
        return self.sess


@pytest.mark.parametrize("facts,args,needle", [
    (dict(exe="Stardew Valley.exe"), (11, 7, 9), "expected StardewModdingAPI.exe"),
    (dict(), (12, 7, 9), "belongs to pid 11"),
    (dict(), (11, 9, 7), "belongs to pid 22"),
    (dict(session=dict(world_ready=False)), (11, 7, 9), "world_ready"),
    (dict(alive=False), (11, 7, 9), "not alive"),
    (dict(), (11, 7, 7), "host hwnd == farmhand hwnd"),
])
def test_preconditions_refuse(facts, args, needle):
    ok, rec = A.check_preconditions(*args, FakeFacts(**facts))
    assert not ok and any(needle in r for r in rec["refusals"]), rec["refusals"]


def test_preconditions_accept_and_title_check():
    f = FakeFacts()
    assert A.check_preconditions(11, 7, 9, f)[0]
    f.w[7]["title"] = "Stardew Valley - running SMAPI"
    assert not A.check_preconditions(11, 7, 9, f)[0]


def test_cmd_record_refuses_and_dry_run_writes_nothing(tmp_path, capsys):
    base = dict(pid=11, fh_hwnd=7, host_hwnd=9, seconds=150.0, reference=0.0, out=str(tmp_path / "o"),
                smapi_log=None)
    assert A.cmd_record(Namespace(dry_run=False, **base), facts=FakeFacts(exe="x.exe")) == 2
    assert A.cmd_record(Namespace(dry_run=True, **base), facts=FakeFacts(), factories=dict(meter=FakeMeter)) == 0
    assert not (tmp_path / "o").exists()
    assert "would_write" in capsys.readouterr().out


def test_com_struct_layouts():
    assert ctypes.sizeof(A.WAVEFORMATEX) == 18
    assert ctypes.sizeof(A.ACTIVATION_PARAMS) == 12
    assert ctypes.sizeof(A.PROPVARIANT_BLOB) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    g = A.GUID.of("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    assert (g.Data1, g.Data2, g.Data3) == (0x1CB9AD4C, 0xDBFA, 0x4C32) and bytes(g.Data4) == bytes.fromhex("B178C2F568A703B2")


def test_quiet_transient_below_active_level_is_not_a_match():
    db = np.full(200, -100.0)
    db[50] = -80.0                                         # +20 dB over the median but below -60 dBFS
    m = A.match_transients(db, 0.0, [4.9], (0, 20))
    assert m and not m[0]["matched"]
    db[50] = -40.0
    assert A.match_transients(db, 0.0, [4.9], (0, 20))[0]["matched"]


def test_reference_shorter_than_10s_is_not_an_active_reference(tmp_path):
    write_case(tmp_path, [(6, -30), (130, -35)], fg_switch=6.0)
    r = A.analyze(tmp_path)
    assert not r["criteria"]["A_active"] and not r["criteria"]["crit_i"]
    assert any(c.startswith("reference_A_too_short") for c in r["causes"])


def test_analyze_rerun_never_overwrites(tmp_path):
    write_case(tmp_path, [(30, -30), (130, -35)])
    A.analyze(tmp_path)
    before = {n: (tmp_path / n).read_bytes() for n in ("result.json", "WITNESS.md", "SHA256SUMS.txt", "reference.wav")}
    r = A.analyze(tmp_path)
    for n, b in before.items():
        assert (tmp_path / n).read_bytes() == b
    assert r["files"]["result"].startswith("result-reanalyzed-")
    for k in ("result", "witness", "reference", "sums"):
        assert (tmp_path / r["files"][k]).exists()


def test_simple_audio_volume_iid_is_pinned():
    g = A.IID_ISimpleAudioVolume
    assert (g.Data1, g.Data2, g.Data3) == (0x87CE5498, 0x68D6, 0x44E5)
    assert bytes(g.Data4) == bytes.fromhex("92156DA47EF883D8")       # live: the old ...6F24A1C9D2EE read -1 everywhere


def test_session_warnings_separate_muted_zero_and_not_read():
    ok = [dict(role="fh", pid=11, device="d", volume=1.0, mute=False), dict(role="host", pid=22, volume=0.0, mute=True)]
    assert A.session_warnings(ok) == []                               # the HOST muted (owner rule) is fine
    assert any("MUTED" in w for w in A.session_warnings([dict(role="fh", volume=1.0, mute=True)]))
    assert any("volume 0.0" in w for w in A.session_warnings([dict(role="fh", volume=0.0, mute=False)]))
    assert any("not read" in w for w in A.session_warnings([dict(role="fh", volume=None, mute=None, volume_hr="0x80004002")]))
    assert any("no render session" in w for w in A.session_warnings([dict(role="host", volume=1.0, mute=False)]))


class MutedMeter(FakeMeter):
    sessions = [dict(role="fh", pid=11, device="d", state=1, volume=1.0, mute=True),
                dict(role="host", pid=22, device="d", state=1, volume=1.0, mute=True)]


def test_dry_run_lists_sessions_and_warns_when_the_farmhand_is_muted(tmp_path, capsys):
    a = Namespace(pid=11, fh_hwnd=7, host_hwnd=9, seconds=150.0, reference=0.0, out=str(tmp_path / "o"),
                  smapi_log=None, dry_run=True)
    assert A.cmd_record(a, facts=FakeFacts(), factories=dict(meter=MutedMeter)) == 0
    cap = capsys.readouterr()
    assert '"role": "host"' in cap.out and '"mute": true' in cap.out
    assert "WARNING: farmhand session MUTED" in cap.err and not (tmp_path / "o").exists()


def test_d24_dense_1hz_swings_are_matched_against_non_swing_audio():
    db = np.full(600, -35.0)                               # 60 s of ambience at -35 dB (100 ms blocks)
    swings = [20.0 + i for i in range(15)]                 # 15 swings at 1 Hz (stamps at whole seconds)
    for ts in swings:
        db[int((ts + 0.3) * 10)] = -1.0                    # each chop peaks ~-1 dBFS inside its own second
    m = A.match_transients(db, 0.0, swings, (0, 60))
    assert sum(x["matched"] for x in m) == 15
    assert all(x["median_db"] == -35.0 for x in m)         # the baseline never includes swing seconds
    weak = np.full(600, -35.0)
    for ts in swings:
        weak[int((ts + 0.3) * 10)] = -31.0                 # +4 dB only: below the +6 dB rise
    assert not any(x["matched"] for x in A.match_transients(weak, 0.0, swings, (0, 60)))

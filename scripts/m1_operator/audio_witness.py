"""audio_witness.py: A3 witness, the farmhand's game audio captured while its window is inactive (no OBS, no UAC).

  record   [--pid P] [--fh-hwnd H] [--host-hwnd H] [--seconds 150] [--reference 20] [--out DIR] [--smapi-log F] [--dry-run]
  analyze  DIR [--smapi-log F]       re-run the analysis + verdict on a recorded folder (offline; numpy)
  selftest [--seconds 2] [--tone-dbfs -90]   activate process loopback on THIS python process only (never the game)

Capture: WASAPI process loopback (`ActivateAudioInterfaceAsync("VAD\\Process_Loopback")`, PROCESS_LOOPBACK of the SMAPI
pid incl. its process tree) through plain ctypes COM: only that process's render audio, so the HOST, ARTI's TTS and
VB-CABLE routing are excluded. PCM 16-bit / 2 ch / 48 kHz (AUTOCONVERTPCM; GetMixFormat is not supported here).
Packets keep arriving while the process renders nothing (dither around -96 dBFS; 203 packets in 2 s measured live on
build 26200), so silence shows up as low-level audio, not as a gap. The WAV is still padded with zeros by the QPC wall
clock for real gaps (activation delay, device stalls) so file time = wall time; `padded_seconds` is reported
separately (padding is evidence of neither sound nor silence).
Secondary channel: the per-process session peak meter (IAudioSessionManager2 -> IAudioMeterInformation, 20 Hz, read
only; ISimpleAudioVolume volume/mute are read, never written) for the SMAPI pid and the HOST pid.

Procedure (the CT runs it; this tool sends NO input, binds no port, writes no inbox, needs no window in front):
  optional segment A (active reference): `ui_op.py fg <fh>`, start with --reference 20, then `ui_op.py fg <host>`;
  segment B (inactive, >= 120 s) is everything after the farmhand stops being foreground (+0.5 s guard). Best run
  during a normal background trial so ARTI swings the axe (transients with known SMAPI log times).
The tool samples GetForegroundWindow/IsIconic every 50 ms (>= 10 Hz) (foreground.jsonl) and decides the segments from that.

Output (new folder, nothing deleted): audio.wav, reference.wav (if A), foreground.jsonl, meter.jsonl, capture.json,
smapi-slice.txt, result.json, WITNESS.md, SHA256SUMS.txt.
Verdicts (WS5 brief 1.4): PASS (A3 VERIFIED LIVE candidate) | PRODUCT_FAILURE | INCONCLUSIVE (setup result, cause named).
Exit codes: 0 PASS | 2 usage / precondition refused | 3 INCONCLUSIVE | 4 PRODUCT_FAILURE.
Analysis needs numpy (run with the project venv); --help and --dry-run do not.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402
import win_facts as wf  # noqa: E402

RATE, CHANNELS, SAMPWIDTH = 48000, 2, 2
BLOCK_ALIGN = CHANNELS * SAMPWIDTH
BLOCK_S = 0.1
ACTIVE_DBFS = -60.0
FLOOR_DB = -120.0
B_MIN_SECONDS = 120.0
A_MIN_SECONDS = 10.0            # a shorter reference does not count as an active reference
WAV_TOLERANCE = 0.02
SEGMENT_GUARD_S = 0.5
TRANSIENT_RISE_DB = 6.0        # over the median of NON-swing audio (D24); was +12 dB over a +-3 s median
SWING_WINDOW = (-0.3, 1.3)      # SMAPI stamps are floor(second): true time in [s, s+1), +-300 ms around that
MEDIAN_SPAN_S = 10.0            # baseline window +-10 s around each swing ...
SWING_EXCLUDE = (-0.5, 1.5)     # ... minus [stamp-0.5, stamp+1.5] around EVERY stamp (the swing's own second +-0.5 s)
MEDIAN_SPAN_FALLBACK_S = 30.0   # when +-10 s holds no non-swing audio (a long run of 1 Hz swings)
METER_NONZERO = 1e-4
METER_AGREE_SAMPLES = 10        # >= 0.5 s of non-zero peaks at 20 Hz during B
METER_ZERO_MAX = 1e-3
FARMHAND_EXE = "StardewModdingAPI.exe"
SWING_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d) \w+\s+Arti Stardew Bridge\] background HOLD COMPLETION .*?"
                      r"kind=Tool outcome=InputCompleted.*?swings=(\d+)")


def _np():
    import numpy  # dev-checkout venv
    return numpy


# ============================================================ analysis (pure, offline)
def read_wav(path):
    np = _np()
    with wave.open(str(path), "rb") as w:
        rate, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"{path}: sample width {sw} != 2")
    return np.frombuffer(raw, dtype="<i2").reshape(-1, ch), rate


def block_db(samples, rate, block_s=BLOCK_S):
    """(dBFS per block, digital-silence mask). RMS over both channels; all-zero block -> FLOOR_DB."""
    np = _np()
    bs = int(round(rate * block_s))
    nb = len(samples) // bs
    if nb == 0:
        return np.zeros(0), np.zeros(0, dtype=bool)
    x = samples[: nb * bs].astype(np.float64).reshape(nb, -1) / 32768.0
    rms = np.sqrt(np.mean(x * x, axis=1))
    silent = ~np.any(samples[: nb * bs].reshape(nb, -1) != 0, axis=1)
    with np.errstate(divide="ignore"):
        db = np.where(rms > 0, 20 * np.log10(np.maximum(rms, 1e-12)), FLOOR_DB)
    return np.maximum(db, FLOOR_DB), silent


def padded_mask(n_blocks, pad_spans, rate, block_s=BLOCK_S):
    """Blocks that are mostly (>50 %) wall-clock padding."""
    np = _np()
    bs = int(round(rate * block_s))
    cover = np.zeros(n_blocks)
    for start, n in pad_spans:
        a, b = start, start + n
        for i in range(max(0, a // bs), min(n_blocks, (b + bs - 1) // bs)):
            lo, hi = max(a, i * bs), min(b, (i + 1) * bs)
            if hi > lo:
                cover[i] += hi - lo
    return cover > bs / 2


def longest_true(mask) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def segments(rows, wall, guard=SEGMENT_GUARD_S):
    """Split the timeline by the foreground samples. A = from 0 while the farmhand is foreground; B = from the first
    sample where it is not (+guard after A) to the end. B violations = samples in B with the farmhand foreground,
    minimized or gone."""
    rows = sorted(rows, key=lambda r: r["t"])
    if not rows:
        return dict(A=None, B=None, t_switch=None, b_violations=0, note="no foreground samples")
    t_switch = next((r["t"] for r in rows if not r.get("fh_fg")), None)
    if t_switch is None:
        return dict(A=(0.0, wall), B=None, t_switch=None, b_violations=0, note="farmhand foreground the whole time")
    a = (0.0, t_switch) if rows[0].get("fh_fg") else None
    b0 = t_switch + guard if a else t_switch
    b = (b0, wall) if wall > b0 else None
    viol = [r for r in rows if b and b[0] <= r["t"] <= b[1]
            and (r.get("fh_fg") or r.get("fh_iconic") or r.get("fh_exists") is False)]
    return dict(A=a, B=b, t_switch=t_switch, b_violations=len(viol),
                b_violation_first=(viol[0]["t"] if viol else None), note=None)


def seg_blocks(seg, n_blocks, block_s=BLOCK_S):
    if not seg:
        return range(0)
    return range(max(0, math.ceil(seg[0] / block_s - 1e-9)), min(n_blocks, int(seg[1] / block_s + 1e-9)))


def seg_stats(db, silent, padded, idx, block_s=BLOCK_S):
    np = _np()
    idx = list(idx)
    if not idx:
        return dict(blocks=0, seconds=0.0, active_fraction=None, median_db=None, peak_db=None,
                    longest_digital_silence_s=None, padded_fraction=None)
    d = db[idx]
    return dict(blocks=len(idx), seconds=round(len(idx) * block_s, 2),
                active_fraction=round(float(np.mean(d > ACTIVE_DBFS)), 4),
                median_db=round(float(np.median(d)), 2), peak_db=round(float(np.max(d)), 2),
                longest_digital_silence_s=round(longest_true(silent[idx]) * block_s, 2),
                padded_fraction=round(float(np.mean(padded[idx])), 4))


def parse_swings(text, start_epoch, end_epoch=None):
    """Epoch times of `background HOLD COMPLETION ... kind=Tool outcome=InputCompleted ... swings=K` (K >= 1) lines.
    SMAPI stamps are local HH:MM:SS (1 s resolution); the date comes from start_epoch (a stamp more than 12 h before
    the start rolls to the next day). With end_epoch, only lines within [start-2 s, end+2 s] are kept."""
    lt = time.localtime(start_epoch)
    out = []
    for line in (text or "").splitlines():
        m = SWING_RE.match(line.strip())
        if not m or int(m.group(4)) < 1:
            continue
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, s, 0, 0, -1))
        if t < start_epoch - 12 * 3600:
            t += 86400
        if end_epoch is None or start_epoch - 2 <= t <= end_epoch + 2:
            out.append(float(t))
    return out


def match_transients(db, t0_epoch, swings, seg=None, block_s=BLOCK_S, window=SWING_WINDOW,
                     span=MEDIAN_SPAN_S, rise=TRANSIENT_RISE_DB, exclude=SWING_EXCLUDE):
    """For each swing (epoch) inside seg (relative seconds), a transient = some block in [t+w0, t+w1] whose dB is
    >= rise over the median of NON-swing blocks within +-span s (every stamp's [s+e0, s+e1] is excluded, so 1 Hz swings
    cannot raise their own baseline: live B4W5b matched 4/15 with the old +-3 s median although every swing second
    peaked -0.1..-1.7 dBFS), and >= ACTIVE_DBFS. Returns [{t_rel, matched, peak_db, median_db, baseline_blocks}]."""
    np = _np()
    out = []
    n = len(db)
    swing_mask = np.zeros(n, dtype=bool)
    for ts in swings:
        rel = ts - t0_epoch
        a, b = int(max(0, (rel + exclude[0]) / block_s)), int(min(n, math.ceil((rel + exclude[1]) / block_s)))
        if b > a:
            swing_mask[a:b] = True
    for ts in swings:
        rel = ts - t0_epoch
        if seg and not (seg[0] <= rel <= seg[1]):
            continue
        lo, hi = int(max(0, (rel + window[0]) / block_s)), int(min(n, math.ceil((rel + window[1]) / block_s)))
        if hi <= lo:
            out.append(dict(t_rel=round(rel, 2), matched=False, peak_db=None, median_db=None, note="outside audio"))
            continue
        base = None
        for sp in (span, MEDIAN_SPAN_FALLBACK_S):
            mlo, mhi = int(max(0, (rel - sp) / block_s)), int(min(n, math.ceil((rel + sp) / block_s)))
            sel = db[mlo:mhi][~swing_mask[mlo:mhi]]
            if len(sel):
                base = sel
                break
        pk = float(np.max(db[lo:hi]))
        if base is None:
            out.append(dict(t_rel=round(rel, 2), matched=False, peak_db=round(pk, 2), median_db=None,
                            baseline_blocks=0, note="no non-swing audio within +-30 s"))
            continue
        med = float(np.median(base))
        out.append(dict(t_rel=round(rel, 2), matched=bool(pk >= med + rise and pk >= ACTIVE_DBFS),
                        peak_db=round(pk, 2), median_db=round(med, 2), baseline_blocks=int(len(base))))
    return out


def meter_summary(rows, seg):
    """Session-meter facts inside seg (relative seconds)."""
    inb = [r for r in rows if seg and seg[0] <= r["t"] <= seg[1]]
    fh = [r.get("fh_peak") for r in inb]
    vals = [v for v in fh if isinstance(v, (int, float))]
    host = [r.get("host_peak") for r in inb if isinstance(r.get("host_peak"), (int, float))]
    return dict(samples=len(inb), fh_session_seen=bool(vals), fh_nonzero=sum(v > METER_NONZERO for v in vals),
                fh_max=round(max(vals), 6) if vals else None, host_max=round(max(host), 6) if host else None,
                agree=sum(v > METER_NONZERO for v in vals) >= METER_AGREE_SAMPLES,
                near_zero=all(v < METER_ZERO_MAX for v in vals))


def decide(cap, seg, a, b, swings_b, meter_b):
    """Verdict exactly per WS5 brief 1.4. Returns (verdict, causes, criteria)."""
    causes = []
    act = cap.get("activation") or {}
    if not act.get("ok"):
        causes.append(f"activation_failed: {act.get('error')}")
    wall, wav_s = cap.get("wall_seconds") or 0, cap.get("wav_seconds") or 0
    if not wall or abs(wav_s - wall) > WAV_TOLERANCE * wall:
        causes.append(f"wav_duration_mismatch: wav {wav_s:.2f}s vs wall {wall:.2f}s (> {WAV_TOLERANCE:.0%})")
    if cap.get("capture_error"):
        causes.append(f"capture_error: {cap['capture_error']}")
    if not seg.get("B"):
        causes.append(f"no_segment_B: {seg.get('note')}")
    elif b["seconds"] < B_MIN_SECONDS:
        causes.append(f"segment_B_too_short: {b['seconds']}s < {B_MIN_SECONDS:.0f}s")
    if seg.get("b_violations"):
        causes.append(f"farmhand_foreground_or_minimized_during_B: {seg['b_violations']} samples (first t={seg.get('b_violation_first')})")
    if not cap.get("meter_ok"):
        causes.append(f"meter_unavailable: {cap.get('meter_error')}")
    valid = not causes
    a_active = (bool(a.get("blocks")) and (a.get("seconds") or 0) >= A_MIN_SECONDS
                and (a.get("active_fraction") or 0) >= 0.5)
    n_sw = len(swings_b)
    n_match = sum(1 for s in swings_b if s["matched"])
    crit_i = (a_active and (b.get("active_fraction") or 0) >= 0.5
              and b.get("median_db") is not None and b["median_db"] >= a["median_db"] - 20)
    crit_ii = n_sw >= 5 and n_match >= 0.8 * n_sw
    criteria = dict(A_present=bool(a.get("blocks")), A_active=a_active, crit_i=bool(crit_i), crit_ii=bool(crit_ii),
                    swings_B=n_sw, swings_B_matched=n_match, meter_agree=bool(meter_b.get("agree")))
    if valid and (crit_i or crit_ii) and meter_b.get("agree"):
        return "PASS", [], criteria
    if (valid and a_active and b.get("active_fraction") is not None and b["active_fraction"] < 0.05
            and n_sw >= 1 and n_match == 0 and meter_b.get("near_zero")):
        return "PRODUCT_FAILURE", ["audio stops while inactive: A active, B silent while ARTI swung, meter ~0"], criteria
    if valid:
        if not a.get("blocks"):
            causes.append("no_active_reference_A (criterion i unavailable)")
        elif (a.get("seconds") or 0) < A_MIN_SECONDS:
            causes.append(f"reference_A_too_short ({a.get('seconds')}s < {A_MIN_SECONDS:.0f}s)")
        elif not a_active:
            causes.append(f"reference_A_silent (activeFraction {a.get('active_fraction')}): wrong pid, muted session or game volume 0?")
        if not crit_i and a_active:
            causes.append(f"criterion_i_not_met (B activeFraction {b.get('active_fraction')}, median B {b.get('median_db')} vs A {a.get('median_db')})")
        if not crit_ii:
            causes.append(f"criterion_ii_not_met ({n_match}/{n_sw} swings in B matched; need >=5 and >=80 %)")
        if (crit_i or crit_ii) and not meter_b.get("agree"):
            causes.append(f"meter_disagrees (non-zero peaks during B: {meter_b.get('fh_nonzero')} < {METER_AGREE_SAMPLES})")
    return "INCONCLUSIVE", causes, criteria


def load_jsonl(path):
    rows = []
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def analyze(out_dir, smapi_text=None):
    """Analyse a recorded folder; writes result.json, reference.wav (if A), WITNESS.md, SHA256SUMS.txt."""
    np = _np()
    out = Path(out_dir)
    cap = json.loads((out / "capture.json").read_text(encoding="utf-8"))
    fg_rows, meter_rows = load_jsonl(out / "foreground.jsonl"), load_jsonl(out / "meter.jsonl")
    wav_path = out / "audio.wav"
    if wav_path.exists():
        samples, rate = read_wav(wav_path)
        cap["wav_seconds"] = round(len(samples) / rate, 3)
    else:
        samples, rate = np.zeros((0, CHANNELS), dtype=np.int16), RATE
        cap["wav_seconds"] = 0.0
    db, silent = block_db(samples, rate)
    pad = padded_mask(len(db), cap.get("pad_spans") or [], rate)
    wall = cap.get("wall_seconds") or 0.0
    seg = segments(fg_rows, wall)
    a = seg_stats(db, silent, pad, seg_blocks(seg["A"], len(db)))
    b = seg_stats(db, silent, pad, seg_blocks(seg["B"], len(db)))
    whole = seg_stats(db, silent, pad, range(len(db)))
    if smapi_text is None and (out / "smapi-slice.txt").exists():
        smapi_text = (out / "smapi-slice.txt").read_text(encoding="utf-8", errors="replace")
    t0e = cap.get("t0_epoch") or 0.0
    swings = parse_swings(smapi_text or "", t0e, t0e + wall)
    sw_b = match_transients(db, t0e, swings, seg["B"]) if len(db) else []
    sw_a = match_transients(db, t0e, swings, seg["A"]) if len(db) else []
    meter_b = meter_summary(meter_rows, seg["B"])
    meter_a = meter_summary(meter_rows, seg["A"])
    verdict, causes, criteria = decide(cap, seg, a, b, sw_b, meter_b)
    sfx = f"-reanalyzed-{wf.stamp()}" if (out / "result.json").exists() else ""     # a re-run never overwrites
    names = dict(result=f"result{sfx}.json", witness=f"WITNESS{sfx}.md", reference=f"reference{sfx}.wav",
                 sums=f"SHA256SUMS{sfx}.txt")
    if seg["A"] and len(samples):
        n = int(min(len(samples), seg["A"][1] * rate))
        with wave.open(str(out / names["reference"]), "wb") as w:
            w.setnchannels(CHANNELS); w.setsampwidth(SAMPWIDTH); w.setframerate(rate)
            w.writeframes(samples[:n].tobytes())
    result = dict(schema="arti_audio_witness.v1", verdict=verdict, causes=causes, criteria=criteria,
                  label="HYPOTHESIS until the CT reviews a live run" if verdict != "PASS" else "PASS (CT confirms before VERIFIED LIVE)",
                  wall_seconds=wall, wav_seconds=cap["wav_seconds"], padded_seconds=cap.get("padded_seconds"),
                  segments=seg, A=a, B=b, whole=whole, swings=dict(total=len(swings), A=sw_a, B=sw_b,
                  window_s=list(SWING_WINDOW), rise_db=TRANSIENT_RISE_DB, note="SMAPI stamps have 1 s resolution"),
                  meter=dict(A=meter_a, B=meter_b, ok=cap.get("meter_ok"), error=cap.get("meter_error"),
                             sessions=cap.get("meter_sessions")),
                  activation=cap.get("activation"), preconditions=cap.get("preconditions"),
                  thresholds=dict(active_dbfs=ACTIVE_DBFS, b_min_s=B_MIN_SECONDS, wav_tolerance=WAV_TOLERANCE,
                                  meter_nonzero=METER_NONZERO, meter_agree_samples=METER_AGREE_SAMPLES))
    result["files"] = names
    (out / names["result"]).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    (out / names["witness"]).write_text(witness_md(result, cap), encoding="utf-8")
    write_sums(out, names["sums"])
    return result


def witness_md(r, cap):
    a, b, c = r["A"], r["B"], r["criteria"]
    lines = [f"# A3 audio witness: {r['verdict']}", "",
             f"- utc start: {cap.get('utc_start')}  pid: {cap.get('pid')}  fh hwnd: {cap.get('fh_hwnd')}  host hwnd: {cap.get('host_hwnd')}",
             f"- wall {r['wall_seconds']:.2f} s, wav {r['wav_seconds']:.2f} s, padded {r.get('padded_seconds')} s",
             f"- activation: {json.dumps(r.get('activation'), default=str)[:300]}",
             f"- segment A (farmhand foreground): {r['segments'].get('A')}  B (inactive): {r['segments'].get('B')}  "
             f"B violations: {r['segments'].get('b_violations')}",
             "", "| segment | seconds | activeFraction | median dB | peak dB | longest digital silence s | padded fraction |",
             "|---|---|---|---|---|---|---|"]
    for name, s in (("A", a), ("B", b)):
        lines.append(f"| {name} | {s['seconds']} | {s['active_fraction']} | {s['median_db']} | {s['peak_db']} | "
                     f"{s['longest_digital_silence_s']} | {s['padded_fraction']} |")
    lines += ["", f"- swings in B: {c['swings_B']}, matched to a transient: {c['swings_B_matched']} "
              f"(window {SWING_WINDOW} s around the 1 s SMAPI stamp, rise {TRANSIENT_RISE_DB} dB over the median of non-swing audio within +-{MEDIAN_SPAN_S:.0f} s)",
              f"- meter B: {json.dumps(r['meter']['B'])}",
              f"- criteria: {json.dumps(c)}", "", "## Causes", *(f"- {x}" for x in (r['causes'] or ['none'])), "",
              "Evidence label: the verdict is the tool's; the CT reviews the WAV/timeline before any VERIFIED LIVE claim."]
    return "\n".join(lines) + "\n"


def write_sums(out, name="SHA256SUMS.txt"):
    out = Path(out)
    if (out / name).exists():                      # never overwrite an earlier checksum list
        name = f"{Path(name).stem}-{wf.stamp()}.txt"
    rows = []
    for p in sorted(out.iterdir()):
        if p.is_file() and not p.name.startswith("SHA256SUMS"):
            rows.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}")
    (out / name).write_text("\n".join(rows) + "\n", encoding="utf-8")


# ============================================================ WAV timeline (wall-clock padding)
class WavTimeline:
    """Incremental 16-bit stereo WAV whose length follows the wall clock. A packet with a plausible QPC time is
    placed at that time (a gap > gap_s before it is zero-padded); with no packet for idle_s the file is padded up to
    now - idle_s/2; close() pads the tail to the end time. Every padded span is recorded."""

    def __init__(self, path, rate=RATE, idle_s=0.25, gap_s=0.010):
        self.path, self.rate, self.idle_s, self.gap = Path(path), rate, idle_s, int(rate * gap_s)
        self.written = self.padded = self.packets = self.silent_frames = 0
        self.pad_spans = []
        self._f = self._w = None
        self.t0 = self.last_packet = None
        self.closed = False

    def start(self, t0):
        self._f = open(self.path, "wb")
        self._w = wave.open(self._f, "wb")
        self._w.setnchannels(CHANNELS); self._w.setsampwidth(SAMPWIDTH); self._w.setframerate(self.rate)
        self.t0 = self.last_packet = t0

    def _frame_at(self, t):
        return int(round((t - self.t0) * self.rate))

    def _pad(self, n):
        if n <= 0:
            return
        self.pad_spans.append((self.written, n))
        chunk = bytes(BLOCK_ALIGN * min(n, self.rate))
        left = n
        while left > 0:
            k = min(left, self.rate)
            self._w.writeframesraw(chunk[: k * BLOCK_ALIGN])
            left -= k
        self.written += n
        self.padded += n

    def packet(self, data, frames, silent, t_pkt, now):
        if self.closed:
            return
        if t_pkt is not None and abs(t_pkt - now) < 5.0:
            gap = self._frame_at(t_pkt) - self.written
            if gap > self.gap:
                self._pad(gap)
        self._w.writeframesraw(bytes(data[: frames * BLOCK_ALIGN]) if not silent else bytes(frames * BLOCK_ALIGN))
        self.written += frames
        self.packets += 1
        if silent:
            self.silent_frames += frames
        self.last_packet = now

    def idle(self, now):
        if self.closed:
            return
        if now - self.last_packet > self.idle_s:
            self._pad(self._frame_at(now - self.idle_s / 2) - self.written)

    def close(self, t_end):
        self.closed = False
        self._pad(self._frame_at(t_end) - self.written)
        self.closed = True
        self._w.close()          # patches the header; does not close a caller-supplied file object
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()
        return dict(frames=self.written, padded_frames=self.padded, packets=self.packets,
                    silent_flag_frames=self.silent_frames, pad_spans=self.pad_spans)


# ============================================================ WASAPI process loopback + session meter (ctypes COM)
_KEEPALIVE = []        # vtables/callbacks must outlive late COM Release calls


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s):
        import uuid
        return cls.from_buffer_copy(uuid.UUID(s).bytes_le)


IID_IUnknown = GUID.of("{00000000-0000-0000-C000-000000000046}")
IID_IAgileObject = GUID.of("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
IID_IActivateCompletion = GUID.of("{41D949AB-9862-444A-80F6-C261334DA5EB}")
IID_IAudioClient = GUID.of("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID.of("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
CLSID_MMDeviceEnumerator = GUID.of("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID.of("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioSessionManager2 = GUID.of("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
IID_IAudioSessionControl2 = GUID.of("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
IID_IAudioMeterInformation = GUID.of("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
IID_ISimpleAudioVolume = GUID.of("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
E_NOINTERFACE = -2147467262
VT_BLOB = 65
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_S_BUFFER_EMPTY = 0x08890001
CLSCTX_ALL = 23


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort), ("nSamplesPerSec", ctypes.c_ulong),
                ("nAvgBytesPerSec", ctypes.c_ulong), ("nBlockAlign", ctypes.c_ushort),
                ("wBitsPerSample", ctypes.c_ushort), ("cbSize", ctypes.c_ushort)]


class ACTIVATION_PARAMS(ctypes.Structure):     # AUDIOCLIENT_ACTIVATION_PARAMS with the process-loopback union arm
    _fields_ = [("ActivationType", ctypes.c_int), ("TargetProcessId", ctypes.c_ulong),
                ("ProcessLoopbackMode", ctypes.c_int)]


class PROPVARIANT_BLOB(ctypes.Structure):      # 24 bytes on x64: vt + 3 reserved WORDs + BLOB{cbSize, pBlobData}
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]


def _call(ptr, index, restype, argtypes, *args):
    vtbl = ctypes.cast(ctypes.c_void_p(ptr), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])(ptr, *args)


def _ok(hr, what):
    if hr is None or hr < 0:
        raise OSError(f"{what} failed hr=0x{(hr or 0) & 0xFFFFFFFF:08X}")
    return hr


def _release(ptr):
    if ptr:
        try:
            _call(ptr, 2, ctypes.c_ulong, [])
        except OSError:
            pass


def _qi(ptr, iid):
    out = ctypes.c_void_p()
    hr = _call(ptr, 0, ctypes.c_long, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
               ctypes.byref(iid), ctypes.byref(out))
    return out.value if hr >= 0 else None


def _hr_hex(hr):
    """None = not read (no ISimpleAudioVolume); else the HRESULT as 0xXXXXXXXX (kept so 'not read' != 'muted')."""
    return None if hr is None else f"0x{hr & 0xFFFFFFFF:08X}"


def session_warnings(sessions):
    """Setup warnings from the session list: the farmhand has no session, is muted, or is at volume 0."""
    fh = [x for x in sessions or [] if x.get("role") == "fh"]
    if not fh:
        return ["no render session for the farmhand pid (it has not played audio yet, or plays on no active endpoint)"]
    out = []
    for x in fh:
        if x.get("mute") is True:
            out.append(f"farmhand session MUTED in the mixer (device {x.get('device')}): a witness will be silent")
        if x.get("volume") is not None and x["volume"] <= 0.001:
            out.append(f"farmhand session volume {x['volume']} (device {x.get('device')}): a witness will be silent")
        if x.get("volume") is None or x.get("mute") is None:
            out.append(f"farmhand session volume/mute not read (hr {x.get('volume_hr')}/{x.get('mute_hr')})")
    return out


def _ole32():
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]; ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(GUID),
                                     ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    return ole


def _completion_handler(done, result):
    """Hand-built IActivateAudioInterfaceCompletionHandler (+ IAgileObject) with a ctypes vtable."""
    QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    DONE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

    class Vtbl(ctypes.Structure):
        _fields_ = [("QueryInterface", QI), ("AddRef", REF), ("Release", REF), ("ActivateCompleted", DONE)]

    class Obj(ctypes.Structure):
        _fields_ = [("lpVtbl", ctypes.POINTER(Vtbl))]

    allowed = {bytes(IID_IUnknown), bytes(IID_IActivateCompletion), bytes(IID_IAgileObject)}
    refs = [1]

    def qi(this, riid, ppv):
        try:
            if riid and bytes(riid.contents) in allowed:
                ppv[0] = this
                refs[0] += 1
                return 0
            ppv[0] = None
        except Exception:  # never raise into COM
            pass
        return E_NOINTERFACE

    def addref(_this):
        refs[0] += 1
        return refs[0]

    def release(_this):
        refs[0] = max(0, refs[0] - 1)
        return refs[0]

    def completed(_this, op):
        try:
            hr_act, unk = ctypes.c_long(0), ctypes.c_void_p()
            hr = _call(op, 3, ctypes.c_long, [ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_void_p)],
                       ctypes.byref(hr_act), ctypes.byref(unk))
            result.update(hr=hr, hr_activate=hr_act.value, unk=unk.value)
        except Exception as e:
            result.update(error=repr(e))
        finally:
            done.set()
        return 0

    fns = (QI(qi), REF(addref), REF(release), DONE(completed))
    vt = Vtbl(*fns)
    obj = Obj(ctypes.pointer(vt))
    _KEEPALIVE.append((fns, vt, obj))
    return obj


class ProcessLoopback:
    """WASAPI process-loopback capture of one pid (+ its process tree). Call open()/read()/close() on ONE thread."""

    def __init__(self, pid, rate=RATE, include_tree=True, activate_timeout=10.0):
        self.pid, self.rate, self.tree, self.timeout = int(pid), rate, include_tree, activate_timeout
        self.ac = self.cc = self.event = None
        self.info = {}
        self._k32 = None

    def open(self):
        from ctypes import wintypes as W
        ole = _ole32()
        hr = ole.CoInitializeEx(None, 0)          # COINIT_MULTITHREADED on the capture thread
        self.info["coinit_hr"] = hr
        self._coinit_ok = hr >= 0                  # CoUninitialize only when this thread's init succeeded
        mmdev = ctypes.WinDLL("Mmdevapi")
        act = mmdev.ActivateAudioInterfaceAsync
        act.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p)]
        act.restype = ctypes.c_long
        params = ACTIVATION_PARAMS(1, self.pid, 0 if self.tree else 1)   # PROCESS_LOOPBACK, INCLUDE_TARGET_PROCESS_TREE
        pv = PROPVARIANT_BLOB(vt=VT_BLOB, cbSize=ctypes.sizeof(params), pBlobData=ctypes.addressof(params))
        done, result = threading.Event(), {}
        handler = _completion_handler(done, result)
        op = ctypes.c_void_p()
        _KEEPALIVE.append((params, pv))
        _ok(act("VAD\\Process_Loopback", ctypes.byref(IID_IAudioClient), ctypes.byref(pv),
                ctypes.c_void_p(ctypes.addressof(handler)), ctypes.byref(op)), "ActivateAudioInterfaceAsync")
        if not done.wait(self.timeout):
            raise OSError(f"activation did not complete within {self.timeout}s")
        _release(op.value)
        if result.get("error"):
            raise OSError("ActivateCompleted: " + result["error"])
        _ok(result.get("hr"), "GetActivateResult")
        _ok(result.get("hr_activate"), "process-loopback activation")
        unk = result.get("unk")
        self.ac = _qi(unk, IID_IAudioClient) if unk else None
        _release(unk)                              # always: QI holds its own reference
        if not self.ac:
            raise OSError("activated interface does not expose IAudioClient")
        wfx = WAVEFORMATEX(1, CHANNELS, self.rate, self.rate * BLOCK_ALIGN, BLOCK_ALIGN, 16, 0)
        flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                 | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
        _ok(_call(self.ac, 3, ctypes.c_long, [ctypes.c_int, ctypes.c_ulong, ctypes.c_longlong, ctypes.c_longlong,
                                              ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p],
                  0, flags, 2_000_000, 0, ctypes.byref(wfx), None), "IAudioClient.Initialize")
        k32 = ctypes.WinDLL("kernel32")
        k32.CreateEventW.argtypes = [ctypes.c_void_p, W.BOOL, W.BOOL, ctypes.c_wchar_p]; k32.CreateEventW.restype = W.HANDLE
        k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; k32.WaitForSingleObject.restype = W.DWORD
        k32.CloseHandle.argtypes = [W.HANDLE]
        k32.QueryPerformanceCounter.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
        self._k32 = k32
        self.event = k32.CreateEventW(None, False, False, None)
        _ok(_call(self.ac, 13, ctypes.c_long, [ctypes.c_void_p], self.event), "IAudioClient.SetEventHandle")
        cc = ctypes.c_void_p()
        _ok(_call(self.ac, 14, ctypes.c_long, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                  ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(cc)), "IAudioClient.GetService(capture)")
        self.cc = cc.value
        buf = ctypes.c_uint32()
        _call(self.ac, 4, ctypes.c_long, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(buf))
        _ok(_call(self.ac, 10, ctypes.c_long, []), "IAudioClient.Start")
        self.info.update(ok=True, buffer_frames=buf.value, flags=hex(flags), format="PCM16/2ch/%d" % self.rate)
        return self.info

    def read(self, timeout_s=0.1):
        """[(data bytes, frames, silent, t_qpc_seconds | None)] drained after one event wait."""
        self._k32.WaitForSingleObject(self.event, int(timeout_s * 1000))
        out = []
        while True:
            n = ctypes.c_uint32()
            _ok(_call(self.cc, 5, ctypes.c_long, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(n)), "GetNextPacketSize")
            if n.value == 0:
                break
            data, frames, flags = ctypes.POINTER(ctypes.c_ubyte)(), ctypes.c_uint32(), ctypes.c_ulong()
            dev, qpc = ctypes.c_uint64(), ctypes.c_uint64()
            hr = _call(self.cc, 3, ctypes.c_long, [ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                                                   ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_ulong),
                                                   ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)],
                       ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), ctypes.byref(dev), ctypes.byref(qpc))
            if hr == AUDCLNT_S_BUFFER_EMPTY:
                break
            _ok(hr, "IAudioCaptureClient.GetBuffer")
            silent = bool(flags.value & AUDCLNT_BUFFERFLAGS_SILENT)
            raw = b"" if silent or not frames.value else ctypes.string_at(data, frames.value * BLOCK_ALIGN)
            _ok(_call(self.cc, 4, ctypes.c_long, [ctypes.c_uint32], frames.value), "ReleaseBuffer")
            out.append((raw, frames.value, silent, (qpc.value / 1e7) if qpc.value else None))
        return out

    def close(self):
        if self.ac:
            try:
                _call(self.ac, 11, ctypes.c_long, [])      # Stop
            except OSError:
                pass
        _release(self.cc); _release(self.ac)
        self.cc = self.ac = None
        if self.event and self._k32:
            self._k32.CloseHandle(self.event)
            self.event = None
        if getattr(self, "_coinit_ok", False):
            self._coinit_ok = False
            try:
                ctypes.WinDLL("ole32").CoUninitialize()
            except OSError:
                pass


def qpc_seconds():
    """QueryPerformanceCounter / frequency (same base as the capture packets' u64QPCPosition / 1e7)."""
    k32 = ctypes.WinDLL("kernel32")
    c, f = ctypes.c_longlong(), ctypes.c_longlong()
    k32.QueryPerformanceCounter(ctypes.byref(c)); k32.QueryPerformanceFrequency(ctypes.byref(f))
    return c.value / f.value


class SessionMeter:
    """Read-only per-process session peak meter over every active render endpoint. Same-thread COM (MTA)."""

    def __init__(self, fh_pid, host_pid=None):
        self.roles = {int(fh_pid): "fh"}
        if host_pid:
            self.roles[int(host_pid)] = "host"
        self.enum = None
        self.kept = []          # (role, pid, device_id, meter_ptr, volume_ptr, ctl_ptr)
        self.sessions = []

    def open(self):
        ole = _ole32()
        ole.CoInitializeEx(None, 0)
        p = ctypes.c_void_p()
        _ok(ole.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                 ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(p)), "CoCreateInstance(MMDeviceEnumerator)")
        self.enum = p.value
        self._ole = ole
        self.refresh()

    def _drop(self):
        for _, _, _, m, v, c in self.kept:
            _release(m); _release(v); _release(c)
        self.kept = []

    def refresh(self):
        self._drop()
        sessions = []
        col = ctypes.c_void_p()
        _ok(_call(self.enum, 3, ctypes.c_long, [ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)],
                  0, 1, ctypes.byref(col)), "EnumAudioEndpoints")
        try:
            n = ctypes.c_uint()
            _call(col.value, 3, ctypes.c_long, [ctypes.POINTER(ctypes.c_uint)], ctypes.byref(n))
            for i in range(n.value):
                dev = ctypes.c_void_p()
                if _call(col.value, 4, ctypes.c_long, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)], i, ctypes.byref(dev)) < 0:
                    continue
                try:
                    sessions += self._device_sessions(dev.value)
                finally:
                    _release(dev.value)
        finally:
            _release(col.value)
        self.sessions = sessions
        return sessions

    def _device_sessions(self, dev):
        out = []
        sid = ctypes.c_wchar_p()
        dev_id = None
        if _call(dev, 5, ctypes.c_long, [ctypes.POINTER(ctypes.c_wchar_p)], ctypes.byref(sid)) >= 0 and sid.value:
            dev_id = sid.value
            self._ole.CoTaskMemFree(ctypes.cast(sid, ctypes.c_void_p))
        mgr = ctypes.c_void_p()
        if _call(dev, 3, ctypes.c_long, [ctypes.POINTER(GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                 ctypes.byref(IID_IAudioSessionManager2), CLSCTX_ALL, None, ctypes.byref(mgr)) < 0:
            return out
        se = ctypes.c_void_p()
        try:
            if _call(mgr.value, 5, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(se)) < 0:
                return out
            cnt = ctypes.c_int()
            _call(se.value, 3, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(cnt))
            for i in range(cnt.value):
                ctl = ctypes.c_void_p()
                if _call(se.value, 4, ctypes.c_long, [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], i, ctypes.byref(ctl)) < 0:
                    continue
                ctl2 = _qi(ctl.value, IID_IAudioSessionControl2)
                pid = ctypes.c_ulong()
                if ctl2:
                    _call(ctl2, 14, ctypes.c_long, [ctypes.POINTER(ctypes.c_ulong)], ctypes.byref(pid))
                    _release(ctl2)
                role = self.roles.get(pid.value)
                if not role:
                    _release(ctl.value)
                    continue
                st = ctypes.c_int(-1)
                _call(ctl.value, 3, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(st))
                meter = _qi(ctl.value, IID_IAudioMeterInformation)
                vol = _qi(ctl.value, IID_ISimpleAudioVolume)
                level, mute = ctypes.c_float(0), ctypes.c_int(0)
                hr_v = hr_m = None
                if vol:
                    hr_v = _call(vol, 4, ctypes.c_long, [ctypes.POINTER(ctypes.c_float)], ctypes.byref(level))
                    hr_m = _call(vol, 6, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(mute))
                self.kept.append((role, pid.value, dev_id, meter, vol, ctl.value))
                out.append(dict(role=role, pid=pid.value, device=dev_id, state=st.value,
                                volume=round(level.value, 4) if hr_v is not None and hr_v >= 0 else None,
                                mute=bool(mute.value) if hr_m is not None and hr_m >= 0 else None,
                                volume_hr=_hr_hex(hr_v), mute_hr=_hr_hex(hr_m)))
        finally:
            _release(se.value); _release(mgr.value)
        return out

    def sample(self):
        peaks = {}
        for role, _, _, meter, _, _ in self.kept:
            if not meter:
                continue
            v = ctypes.c_float()
            if _call(meter, 3, ctypes.c_long, [ctypes.POINTER(ctypes.c_float)], ctypes.byref(v)) >= 0:
                peaks[role] = max(peaks.get(role, 0.0), float(v.value))
        return dict(fh_peak=peaks.get("fh"), host_peak=peaks.get("host"))

    def close(self):
        self._drop()
        _release(self.enum)
        self.enum = None


# ============================================================ recorder
def record(out, seconds, source, meter, probe, clock, epoch, sleep, log=print, threaded=True, reference=0.0,
           meter_refresh_s=2.0, tick_s=0.05, smapi=None):
    """Capture `seconds` of wall time. source: open()/read(t)/close(); meter: open()/refresh()/sample()/close() or
    None; probe(): {fg, fh_fg, fh_iconic, fh_exists}; smapi: (start(), read_new()) or None. Writes audio.wav,
    foreground.jsonl, meter.jsonl, smapi-slice.txt, capture.json. Returns the capture dict."""
    out = Path(out)
    wav = WavTimeline(out / "audio.wav")
    cap = dict(activation=None, meter_ok=False, meter_error=None, capture_error=None)
    stop, activated = threading.Event(), threading.Event()

    def _activate():
        try:
            cap["activation"] = dict(source.open(), ok=True)
        except Exception as e:
            cap["activation"] = dict(ok=False, error=repr(e))
        finally:
            activated.set()

    def _step():
        for data, frames, silent, t_pkt in source.read(0.1 if threaded else 0.0):
            wav.packet(data, frames, silent, t_pkt, clock())
        wav.idle(clock())

    def _loop():
        _activate()
        try:
            while cap["activation"]["ok"] and not stop.is_set():
                _step()
        except Exception as e:
            cap["capture_error"] = repr(e)
        finally:
            try:
                source.close()
            except Exception:
                pass

    if smapi:
        smapi.start()
    t0, t0e = clock(), epoch()
    wav.start(t0)
    cap.update(t0_epoch=t0e, utc_start=wf.utc(t0e))
    th = None
    if threaded:
        th = threading.Thread(target=_loop, name="audio-capture", daemon=True)
        th.start()
        activated.wait(15)
    else:
        _activate()
    if not (cap.get("activation") or {}).get("ok"):
        log(f"ACTIVATION FAILED: {(cap.get('activation') or {}).get('error')}")
    if meter:
        try:
            meter.open()
            cap.update(meter_ok=True, meter_sessions=list(getattr(meter, "sessions", [])))
            cap["session_warnings"] = session_warnings(cap["meter_sessions"])
            for w in cap["session_warnings"]:
                log("WARNING: " + w)
        except Exception as e:
            cap.update(meter_ok=False, meter_error=repr(e))
    fgf = (out / "foreground.jsonl").open("w", encoding="utf-8")
    mf = (out / "meter.jsonl").open("w", encoding="utf-8")
    last_refresh = -1e9
    last_progress = 0.0
    hinted = False
    try:
        while True:
            now = clock()
            t = now - t0
            if t >= seconds:
                break
            if not threaded and (cap.get("activation") or {}).get("ok") and not cap.get("capture_error"):
                try:
                    _step()
                except Exception as e:
                    cap["capture_error"] = repr(e)
            fgf.write(json.dumps(dict(t=round(t, 3), **probe())) + "\n")     # every tick (20 Hz nominal, >= 10 Hz live)
            if meter and cap["meter_ok"]:
                try:
                    if t - last_refresh >= meter_refresh_s:
                        sess = meter.refresh()
                        last_refresh = t
                        seen = {(s["role"], s["pid"], s.get("device")) for s in cap.get("meter_sessions") or []}
                        cap["meter_sessions"] = (cap.get("meter_sessions") or []) + [
                            dict(s, first_t=round(t, 2)) for s in sess if (s["role"], s["pid"], s.get("device")) not in seen]
                    mf.write(json.dumps(dict(t=round(t, 3), **meter.sample())) + "\n")
                except Exception as e:
                    cap.update(meter_ok=False, meter_error=f"t={t:.1f}: {e!r}")
            if reference and not hinted and t >= reference:
                log(f"t={t:.0f}s reference window over: the CT now runs `ui_op.py fg <host>` (this tool sends no input)")
                hinted = True
            if t - last_progress >= 10:
                log(f"t={t:.0f}s packets={wav.packets} padded={wav.padded / RATE:.1f}s")
                last_progress = t
            sleep(tick_s)
    except KeyboardInterrupt:
        cap["interrupted"] = True
        log("interrupted: closing the WAV and analysing what was recorded")
    finally:
        stop.set()
        if th:
            th.join(10)
            if th.is_alive():                      # never close the WAV under a running writer silently
                cap["capture_error"] = "capture thread did not stop within 10 s (WAV closed while it ran)"
                wav.closed = True
        elif (cap.get("activation") or {}).get("ok"):
            try:
                source.close()
            except Exception:
                pass
        t_end = clock()
        stats = wav.close(t_end)
        fgf.close(); mf.close()
        if meter:
            try:
                meter.close()
            except Exception:
                pass
        if smapi:
            (out / "smapi-slice.txt").write_text(smapi.read_new(), encoding="utf-8")
    with wave.open(str(out / "audio.wav"), "rb") as w:
        wav_seconds = w.getnframes() / w.getframerate()
    cap.update(wall_seconds=round(t_end - t0, 3), wav_seconds=round(wav_seconds, 3),
               padded_seconds=round(stats["padded_frames"] / RATE, 3), packets=stats["packets"],
               silent_flag_seconds=round(stats["silent_flag_frames"] / RATE, 3), pad_spans=stats["pad_spans"])
    return cap


SmapiTail = wf.FileTail     # bytes appended after start() only


# ============================================================ preconditions + CLI
def _read_text(p):
    try:
        return Path(p).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None


def _session(retries=5, sleep=time.sleep):
    for _ in range(retries):
        try:
            return paths.read_session()
        except (OSError, ValueError):
            sleep(0.2)
    return None


class LiveFacts:
    def window(self, h):
        return wf.window(h)

    def exe(self, pid):
        return wf.exe_name(pid)

    def alive(self, pid):
        return wf.pid_alive(pid)

    def session(self):
        return _session()


def check_preconditions(pid, fh, host, facts):
    """(ok, facts dict, refusals). Read-only."""
    w, hw = facts.window(fh), facts.window(host) if host else None
    sess = facts.session() or {}
    rec = dict(pid=pid, pid_alive=facts.alive(pid), exe=facts.exe(pid), fh_window=w, host_window=hw,
               world_ready=sess.get("world_ready"), session_id=sess.get("session_id"))
    bad = []
    if not rec["pid_alive"]:
        bad.append(f"pid {pid} not alive")
    if (rec["exe"] or "").lower() != FARMHAND_EXE.lower():
        bad.append(f"pid {pid} exe is {rec['exe']!r}, expected {FARMHAND_EXE}")
    if not w.get("exists"):
        bad.append(f"farmhand hwnd {fh} does not exist")
    elif w.get("pid") != pid:
        bad.append(f"farmhand hwnd {fh} belongs to pid {w.get('pid')}, not {pid}")
    elif "[farmhand]" not in (w.get("title") or ""):
        bad.append(f"farmhand title has no [farmhand]: {w.get('title')!r}")
    if hw is not None and hw.get("exists") and hw.get("pid") == pid:
        bad.append("host hwnd belongs to the farmhand pid (host/farmhand swapped?)")
    if host and str(host) == str(fh):
        bad.append("host hwnd == farmhand hwnd")
    if rec["world_ready"] is not True:
        bad.append(f"session.json world_ready is {rec['world_ready']!r}")
    rec["refusals"] = bad
    return not bad, rec


def _default_pid():
    try:
        return int(json.loads(paths.FARMHAND_LAUNCH.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _new_dir(base: Path) -> Path:
    p, i = base, 1
    while p.exists():
        i += 1
        p = base.with_name(f"{base.name}-{i}")
    p.mkdir(parents=True)
    return p


def cmd_record(a, facts=None, factories=None):
    facts = facts or LiveFacts()
    pid = a.pid if a.pid is not None else _default_pid()
    fh = a.fh_hwnd if a.fh_hwnd is not None else _read_text(paths.FH_HWND_FILE)
    host = a.host_hwnd if a.host_hwnd is not None else _read_text(paths.HOST_HWND_FILE)
    if pid is None or fh is None:
        print(f"REFUSED: need --pid / --fh-hwnd (or {paths.FARMHAND_LAUNCH.name} / {paths.FH_HWND_FILE.name})", file=sys.stderr)
        return 2
    fh = int(fh); host = int(host) if host else None
    ok, pre = check_preconditions(int(pid), fh, host, facts)
    print(json.dumps(dict(preconditions=pre), default=str))
    if not ok:
        print("REFUSED: " + "; ".join(pre["refusals"]), file=sys.stderr)
        return 2
    out = Path(a.out) if a.out else paths.RUN / f"audio-witness-{wf.stamp()}"
    f = factories or {}
    meter = f.get("meter", lambda: SessionMeter(int(pid), (facts.window(host) or {}).get("pid") if host else None))()
    if a.dry_run:
        sessions, err = [], None                   # read-only session listing (volume/mute read, never written)
        try:
            meter.open()
            sessions = list(getattr(meter, "sessions", []))
        except Exception as e:
            err = repr(e)
        finally:
            try:
                meter.close()
            except Exception:
                pass
        warns = session_warnings(sessions) if err is None else [f"session listing failed: {err}"]
        print(json.dumps(dict(dry_run=True, would_write=str(out), seconds=a.seconds, reference=a.reference,
                              smapi_log=str(a.smapi_log), sessions=sessions, warnings=warns,
                              segments="A = farmhand foreground from t=0 (optional); "
                              "B = after the CT's `ui_op.py fg <host>`, >= 120 s needed"), indent=1))
        for w in warns:
            print("WARNING: " + w, file=sys.stderr)
        return 0
    out = _new_dir(out)
    source = f.get("source", lambda: ProcessLoopback(int(pid)))()

    def probe():
        fg = wf.foreground()
        w = wf.window(fh)
        return dict(fg=fg, fh_fg=fg == fh, host_fg=bool(host) and fg == host, fh_iconic=w["iconic"], fh_exists=w["exists"])

    print(f"recording {a.seconds:.0f}s of pid {pid} into {out} (reference {a.reference:.0f}s); no input is sent", flush=True)
    cap = record(out, a.seconds, source, meter, f.get("probe", probe), f.get("clock", qpc_seconds), time.time,
                 time.sleep, log=lambda m: print(m, flush=True), reference=a.reference,
                 smapi=SmapiTail(a.smapi_log) if a.smapi_log else None)
    cap.update(pid=int(pid), fh_hwnd=fh, host_hwnd=host, preconditions=pre, args=dict(seconds=a.seconds, reference=a.reference))
    (out / "capture.json").write_text(json.dumps(cap, indent=1, default=str), encoding="utf-8")
    return _finish(out)


def _finish(out, smapi_text=None):
    try:
        res = analyze(out, smapi_text)
    except ImportError:
        write_sums(out)
        print(f"numpy missing: recorded into {out}; run `analyze {out}` with the project venv", file=sys.stderr)
        return 3
    print(json.dumps(dict(verdict=res["verdict"], causes=res["causes"], criteria=res["criteria"],
                          A=res["A"], B=res["B"], out=str(out)), indent=1, default=str))
    print(f"VERDICT {res['verdict']}")
    return {"PASS": 0, "PRODUCT_FAILURE": 4}.get(res["verdict"], 3)


def cmd_analyze(a):
    text = Path(a.smapi_log).read_text(encoding="utf-8", errors="replace") if a.smapi_log else None
    return _finish(Path(a.dir), text)


def cmd_selftest(a):
    """Process loopback of THIS python process (never the game). Optional quiet tone through sounddevice."""
    import tempfile
    tone = None
    if a.tone_dbfs is not None:
        np = _np()
        import sounddevice as sd
        n = int(RATE * (a.seconds + 0.5))
        amp = 10 ** (a.tone_dbfs / 20)
        tone = (amp * np.sin(2 * np.pi * 1000 * np.arange(n) / RATE)).astype("float32")
    with tempfile.TemporaryDirectory() as td:
        rows = []
        th = None
        if tone is not None:
            th = threading.Thread(target=lambda: (sd.play(tone, RATE), sd.wait()), daemon=True)
            th.start()
        cap = record(td, a.seconds, ProcessLoopback(os.getpid()), SessionMeter(os.getpid()),
                     lambda: dict(fg=0, fh_fg=False, fh_iconic=False, fh_exists=True), qpc_seconds, time.time,
                     time.sleep, log=lambda m: rows.append(m))
        if th:
            th.join(5)
        res = dict(activation=cap["activation"], wall=cap["wall_seconds"], wav=cap["wav_seconds"],
                   padded=cap["padded_seconds"], packets=cap["packets"], silent_flag_s=cap["silent_flag_seconds"],
                   capture_error=cap["capture_error"], meter_ok=cap["meter_ok"], meter_error=cap["meter_error"],
                   own_sessions=len(cap.get("meter_sessions") or []))
        try:
            samples, rate = read_wav(Path(td) / "audio.wav")
            db, _ = block_db(samples, rate)
            res["max_block_dbfs"] = round(float(db.max()), 2) if len(db) else None
            res["peak_sample"] = int(abs(samples.astype("int32")).max()) if len(samples) else 0
        except ImportError:
            pass
    print(json.dumps(res, indent=1, default=str))
    return 0 if (res["activation"] or {}).get("ok") else 3


def build_parser():
    ap = argparse.ArgumentParser(prog="audio_witness.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    r = sub.add_parser("record", help="record + analyse (default paths from m1_paths)")
    r.add_argument("--pid", type=int, help=f"SMAPI pid (default: {paths.FARMHAND_LAUNCH.name})")
    r.add_argument("--fh-hwnd", type=int, help=f"farmhand hwnd (default: {paths.FH_HWND_FILE.name})")
    r.add_argument("--host-hwnd", type=int, help=f"host hwnd (default: {paths.HOST_HWND_FILE.name})")
    r.add_argument("--seconds", type=float, default=150.0, help="total wall seconds to record (default 150)")
    r.add_argument("--reference", type=float, default=0.0, help="seconds of active reference A expected at the start (0 = none)")
    r.add_argument("--out", help="output folder (default RUN/audio-witness-<utc>; a suffix is added on clash)")
    r.add_argument("--smapi-log", default=str(paths.SMAPI_LOG), help="SMAPI log for swing times (only bytes appended during the run are read)")
    r.add_argument("--dry-run", action="store_true", help="check preconditions and print the plan; no capture")
    r.set_defaults(f=cmd_record)
    z = sub.add_parser("analyze", help="re-run analysis on a recorded folder")
    z.add_argument("dir"); z.add_argument("--smapi-log", help="parse this log (default: the folder's smapi-slice.txt)")
    z.set_defaults(f=cmd_analyze)
    s = sub.add_parser("selftest", help="process loopback of this python process only (never the game)")
    s.add_argument("--seconds", type=float, default=2.0)
    s.add_argument("--tone-dbfs", type=float, help="play a 1 kHz tone at this level from this process (e.g. -90)")
    s.set_defaults(f=cmd_selftest)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "record" and a.seconds <= 0:
        print("--seconds must be > 0", file=sys.stderr)
        return 2
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())

"""Long-session audit fixes (L01 L02 L03 L06 L08 L09) + Stardew two-driver guard.

Fakes only: no providers, devices, game or network. Synthetic text only.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import arti_curator as curator
import arti_executors
import arti_health
import arti_observer_catchup as catchup
import arti_observer_pipeline as pipeline
import arti_observer_rag as obs_rag
import arti_observer_shutdown as obs_shutdown
import arti_stardew_driver_guard as driver_guard
import session_transcript as st
from arti_buffered_writer import BatchedWriter


class FailOnceSink(io.StringIO):
    """write() raises once (disk full), then works."""

    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures
        self.attempts = threading.Event()
        self.final = ""

    def write(self, data):
        self.attempts.set()
        if self.failures > 0:
            self.failures -= 1
            raise OSError(28, "No space left on device")
        return super().write(data)

    def close(self):
        self.final = self.getvalue()
        super().close()


def _wait(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --- L01: a write error is not sticky ----------------------------------------

def test_writer_error_is_cleared_after_next_successful_batch():
    sink = FailOnceSink()
    writer = BatchedWriter(sink, interval=0.01, lines=1)
    try:
        writer.write("lost\n")
        assert _wait(lambda: writer.write_health()[1] == 1)
        error, failed, dropped = writer.write_health()
        assert isinstance(error, OSError) and failed == 1 and dropped == 1
        writer.write("kept\n")  # must not re-raise the stored error
        assert _wait(lambda: writer.write_health()[0] is None)
        writer.flush()  # last batch ok -> no raise
    finally:
        writer.close()
    assert sink.final == "kept\n"
    assert writer.write_health()[1:] == (1, 1)


def _transcript_with_failing_writer(monkeypatch, tmp_path, failures=1):
    st.close_transcript_file()
    monkeypatch.setattr(st, "_ROOT", tmp_path)
    config = {"stream_session_id": "unit-l01", "session_log_keep_n": 0,
              "transcript_write_error_log_interval_sec": 300}
    st.init_session_artifacts(config)
    st.close_transcript_file()  # swap the real file writer for the failing fake
    sink = FailOnceSink(failures)
    monkeypatch.setattr(st, "_file", BatchedWriter(sink, interval=0.01, lines=1))
    return config, sink


def test_a_transcript_folder_outside_the_checkout_is_written(monkeypatch, tmp_path):
    """A live run keeps its transcript in the run folder; init must not raise before the file is opened."""
    root, run = tmp_path / "checkout", tmp_path / "run" / "transcripts"
    root.mkdir()
    monkeypatch.setattr(st, "_ROOT", root)
    config = {"transcript_dir": str(run), "session_log_keep": 0}
    st.init_session_artifacts(config)
    try:
        st.append_transcript({"ts": "20:00:00", "kind": "streamer", "text": "halo"}, config)
    finally:
        st.close_transcript_file()
    written = list(run.glob("*.jsonl"))
    assert len(written) == 1 and "halo" in written[0].read_text(encoding="utf-8")
    manifest = json.loads((run / "session_manifest.json").read_text(encoding="utf-8"))
    assert Path(manifest["transcript_path"]) == written[0]
    assert not (root / "data").exists()


def test_log_trigger_survives_writer_failure_and_transcript_resumes(monkeypatch, tmp_path, capsys):
    config, sink = _transcript_with_failing_writer(monkeypatch, tmp_path)
    try:
        first = st.log_trigger("mic", None, "trigger satu", config)
        assert _wait(lambda: st._file.write_health()[1] == 1)
        second = st.log_trigger("mic", None, "trigger dua", config)
        third = st.log_trigger("mic", None, "trigger tiga", config)
        assert (first, second, third) == ("t-0001", "t-0002", "t-0003")
        assert _wait(lambda: "trigger tiga" in sink.getvalue())
    finally:
        st.close_transcript_file()
    out = capsys.readouterr().out
    assert out.count("[Transcript] Gagal menulis") == 1, "rate-limited: one line, not per line"
    rows = [json.loads(line) for line in sink.final.splitlines()]
    assert [r["turn_id"] for r in rows if r.get("kind") == "trigger"] == ["t-0002", "t-0003"]


def test_failure_log_is_rate_limited_per_interval(monkeypatch, tmp_path, capsys):
    config, _sink = _transcript_with_failing_writer(monkeypatch, tmp_path, failures=50)
    try:
        for i in range(6):
            st.append_transcript({"kind": "system", "text": f"baris {i}"}, config)
            assert _wait(lambda i=i: st._file.write_health()[1] >= i + 1)
    finally:
        st.close_transcript_file()
    assert capsys.readouterr().out.count("[Transcript] Gagal menulis") == 1


def test_log_trigger_returns_turn_id_even_if_writer_raises(monkeypatch, tmp_path):
    st.close_transcript_file()
    monkeypatch.setattr(st, "_ROOT", tmp_path)

    class Broken:
        def write(self, _data):
            raise ValueError("write to closed batched writer")

    config = {"stream_session_id": "unit-l01b", "session_log_keep_n": 0}
    st.init_session_artifacts(config)
    st.close_transcript_file()
    monkeypatch.setattr(st, "_file", Broken())
    assert st.log_trigger("ptt", None, "teks", config).startswith("t-")
    monkeypatch.setattr(st, "_file", None)


def test_queue_voice_trigger_still_queues_when_transcript_write_fails(monkeypatch, tmp_path):
    import arti_bridge as bridge

    config, sink = _transcript_with_failing_writer(monkeypatch, tmp_path)
    q = queue.Queue()
    monkeypatch.setitem(bridge.CONFIG, "voice_queue_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", False)
    monkeypatch.setattr(bridge, "voice_trigger_queue", q)
    monkeypatch.setattr(bridge, "_stardew_runtime", None)
    monkeypatch.setattr(bridge, "_stardew_bridge_trial", None)
    monkeypatch.setattr(bridge, "_brain_busy", False)
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    monkeypatch.setattr(bridge, "_host_mode", False)
    monkeypatch.setattr(bridge, "_note_screen_privacy", lambda *_a: None)
    monkeypatch.setattr(bridge, "_note_streamer_text_for_afk", lambda *_a: None)
    texts = []
    try:
        for text in ("pertama", "kedua", "ketiga"):
            assert bridge.queue_voice_trigger(text, trigger_type="mic") is True
            if text == "pertama":
                assert _wait(lambda: st._file.write_health()[1] == 1)
            # Consume each admitted turn before the next owner utterance
            # supersedes it; a transcript write failure must never lose it.
            texts.append(q.get_nowait().text)
            assert q.empty()
    finally:
        st.close_transcript_file()
    assert texts == ["pertama", "kedua", "ketiga"]


# --- L02: incremental shutdown observer ---------------------------------------

def _observer_sandbox(monkeypatch, tmp_path, sid):
    for module in (pipeline, curator, catchup):
        monkeypatch.setattr(module, "_ROOT", tmp_path)
    tx = tmp_path / "transcripts" / f"{sid}.jsonl"
    tx.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(st, "_session_id", sid)
    monkeypatch.setattr(st, "_transcript_path", tx)
    monkeypatch.setattr(obs_rag, "reindex_beats_session", lambda *_a, **_kw: None)
    monkeypatch.setattr(obs_rag, "promote_approved_to_vault", lambda *_a, **_kw: None)
    return tx, {"observer_segment_minutes": 10, "transcript_dir": str(tx.parent)}


def _append_rows(tx: Path, rows):
    with tx.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _fake_observer(monkeypatch, calls, fail_on=None):
    import arti_observer_client as client

    def fake(block, config):
        calls.append(block)
        if fail_on is not None and len(calls) == fail_on:
            raise RuntimeError("terminal ditutup")
        return {"summary": f"ringkasan {len(calls)}", "topics": [], "facts": [],
                "worth_embed": True, "worth_learning": False,
                "noise_level": "low", "provider": "fake"}

    monkeypatch.setattr(client, "summarize_segment", fake)


def _noop_progress(*_a):
    return None


def test_second_same_date_shutdown_only_summarizes_its_own_segments(monkeypatch, tmp_path):
    sid = "2026-10-20-default"
    tx, cfg = _observer_sandbox(monkeypatch, tmp_path, sid)
    calls: list = []
    _fake_observer(monkeypatch, calls)
    _append_rows(tx, [{"ts": "10:01:00", "kind": "streamer", "text": "sesi pagi awal"},
                      {"ts": "10:12:00", "kind": "streamer", "text": "sesi pagi lanjut"}])
    beats1 = obs_shutdown.run_observer_shutdown(cfg, on_progress=_noop_progress)
    assert len(calls) == 2 and len(beats1) == 2

    calls.clear()
    _append_rows(tx, [{"ts": "20:03:00", "kind": "streamer", "text": "sesi malam awal"},
                      {"ts": "20:15:00", "kind": "streamer", "text": "sesi malam lanjut"}])
    beats2 = obs_shutdown.run_observer_shutdown(cfg, on_progress=_noop_progress)
    assert len(calls) == 2, "earlier session's segments must not be re-summarized"
    assert all("malam" in block and "pagi" not in block for block in calls)
    jsonl, md = pipeline.beats_paths(sid, cfg)
    saved = catchup._load_existing_beats(jsonl)
    assert [b.segment_index for b in saved] == [60, 61, 120, 121] == [b.segment_index for b in beats2]
    assert [b.summary for b in saved[:2]] == ["ringkasan 1", "ringkasan 2"]
    assert md.is_file() and "## 20:00:00" in md.read_text(encoding="utf-8")
    assert not catchup._partial_marker(jsonl).exists()


def test_same_bucket_restart_resummarizes_the_grown_segment(monkeypatch, tmp_path):
    sid = "2026-10-20-default"
    tx, cfg = _observer_sandbox(monkeypatch, tmp_path, sid)
    calls: list = []
    _fake_observer(monkeypatch, calls)
    _append_rows(tx, [{"ts": "14:02:00", "kind": "streamer", "text": "sebelum restart"}])
    obs_shutdown.run_observer_shutdown(cfg, on_progress=_noop_progress)
    calls.clear()
    _append_rows(tx, [{"ts": "14:08:00", "kind": "streamer", "text": "sesudah restart"}])
    beats = obs_shutdown.run_observer_shutdown(cfg, on_progress=_noop_progress)
    assert len(calls) == 1 and "sesudah restart" in calls[0]
    assert [(b.segment_index, b.event_count) for b in beats] == [(84, 2)]


def test_interrupted_shutdown_keeps_finished_segments_and_resumes(monkeypatch, tmp_path):
    sid = "2026-10-20-default"
    tx, cfg = _observer_sandbox(monkeypatch, tmp_path, sid)
    calls: list = []
    _fake_observer(monkeypatch, calls, fail_on=2)
    _append_rows(tx, [{"ts": "10:01:00", "kind": "streamer", "text": "satu"},
                      {"ts": "10:12:00", "kind": "streamer", "text": "dua"},
                      {"ts": "10:25:00", "kind": "streamer", "text": "tiga"}])
    with pytest.raises(RuntimeError, match="terminal ditutup"):
        obs_shutdown.run_observer_shutdown(cfg, on_progress=_noop_progress)
    jsonl, _ = pipeline.beats_paths(sid, cfg)
    assert [b.segment_index for b in catchup._load_existing_beats(jsonl)] == [60]
    assert catchup._partial_marker(jsonl).exists()
    # Beats are now newer than the transcript; the marker still flags it pending.
    later = time.time() + 600
    os.utime(jsonl, (later, later))
    assert catchup.find_pending(cfg, current_session_id="other") == [sid]

    calls.clear()
    _fake_observer(monkeypatch, calls)
    obs_shutdown.run_observer_shutdown(cfg, on_progress=_noop_progress)
    assert len(calls) == 2 and "satu" not in "".join(calls)
    assert [b.segment_index for b in catchup._load_existing_beats(jsonl)] == [60, 61, 62]
    assert not catchup._partial_marker(jsonl).exists()


def test_progress_stages_unchanged(monkeypatch, tmp_path):
    sid = "2026-10-20-default"
    tx, cfg = _observer_sandbox(monkeypatch, tmp_path, sid)
    _fake_observer(monkeypatch, [])
    _append_rows(tx, [{"ts": "10:01:00", "kind": "streamer", "text": "a"},
                      {"ts": "10:12:00", "kind": "streamer", "text": "b"}])
    seen = []
    obs_shutdown.run_observer_shutdown(cfg, on_progress=lambda *a: seen.append(a[:3]))
    assert seen == [("summarize", 0, 2), ("summarize", 1, 2), ("summarize", 2, 2),
                    ("curate", 0, 1), ("curate", 1, 1),
                    ("embed_observer", 0, 1), ("embed_observer", 1, 1)]


# --- L03: midnight crossing keeps the start-date id ---------------------------

def _frozen_clock(monkeypatch, start: datetime):
    clock = [start.timestamp()]
    real = time.strftime

    def fake_strftime(fmt, t=None):
        return real(fmt, time.localtime(clock[0]) if t is None else t)

    monkeypatch.setattr(time, "strftime", fake_strftime)
    return clock


def test_session_started_before_midnight_keeps_start_date_id(monkeypatch, tmp_path):
    clock = _frozen_clock(monkeypatch, datetime(2026, 10, 20, 23, 50))
    st.close_transcript_file()
    monkeypatch.setattr(st, "_ROOT", tmp_path)
    config = {"active_profile": "default", "session_log_keep_n": 0, "observer_segment_minutes": 10}
    try:
        st.init_session_artifacts(config)
        st.append_transcript({"ts": "23:55:00", "kind": "streamer", "text": "sebelum tengah malam"}, config)
        clock[0] = datetime(2026, 10, 21, 0, 30).timestamp()
        st.append_transcript({"ts": "00:20:00", "kind": "streamer", "text": "sesudah tengah malam"}, config)
        st.close_transcript_file()
        assert st.get_session_id(config) == "2026-10-21-default"
        assert st.current_session_id(config) == "2026-10-20-default"

        for module in (pipeline, curator, catchup):
            monkeypatch.setattr(module, "_ROOT", tmp_path)
        monkeypatch.setattr(obs_rag, "reindex_beats_session", lambda *_a, **_kw: None)
        monkeypatch.setattr(obs_rag, "promote_approved_to_vault", lambda *_a, **_kw: None)
        _fake_observer(monkeypatch, [])
        obs_shutdown.run_observer_shutdown(config, on_progress=_noop_progress)
        sessions = tmp_path / "vault" / "sessions"
        assert (sessions / "2026-10-20-default_beats.jsonl").is_file()
        assert not (sessions / "2026-10-21-default_beats.jsonl").exists()

        learnings = tmp_path / "vault" / "concepts" / "arti_live_learnings.md"
        learnings.parent.mkdir(parents=True, exist_ok=True)
        learnings.write_text("- [2026-10-19] lama\n- [2026-10-20] sebelum\n- [2026-10-21] sesudah\n",
                             encoding="utf-8")
        path = st.write_vault_session_md(config, "ringkas", {"counts": {}}, [])
        text = path.read_text(encoding="utf-8")
        assert path.name == "2026-10-20-default.md"
        assert "[2026-10-20] sebelum" in text and "[2026-10-21] sesudah" in text
        assert "[2026-10-19] lama" not in text
    finally:
        st.close_transcript_file()


def test_bridge_shutdown_and_catchup_use_stored_session_id(monkeypatch):
    import arti_api_telemetry as tel
    import arti_bridge as bridge

    monkeypatch.setattr(st, "_session_id", "2026-10-20-default")
    monkeypatch.setitem(bridge.CONFIG, "stream_session_id", "")
    monkeypatch.setitem(bridge.CONFIG, "observer_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "vault_rag_reindex_on_shutdown", False)
    bound = []
    monkeypatch.setattr(tel, "set_session_id", bound.append)
    monkeypatch.setattr(tel, "flush", lambda *_a, **_kw: None)
    monkeypatch.setattr(st, "finalize_session_artifacts", lambda *_a, **_kw: None)
    import arti_telemetry_dashboard as dash
    monkeypatch.setattr(dash, "generate_dashboard", lambda *_a, **_kw: "unit")
    bridge.save_stream_session_log()
    assert bound == ["2026-10-20-default"]
    src = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "current_session_id=session_transcript.current_session_id(CONFIG)" in src
    assert "session_transcript.get_session_id(" not in src


# --- L06: health line + telemetry row ----------------------------------------

def test_health_snapshot_line_and_telemetry_row(tmp_path):
    import arti_api_telemetry as tel

    tel.set_session_id("unit-health")
    lines = []

    async def exercise():
        return arti_health.emit_health({}, loop=asyncio.get_running_loop(), disk_path=tmp_path,
                                       started_monotonic=time.monotonic() - 3725, log=lines.append)

    stats = asyncio.run(exercise())
    assert set(stats) == {"rss_bytes", "threads", "asyncio_tasks", "pools", "disk_free_bytes", "uptime_sec"}
    assert stats["threads"] >= 1 and stats["asyncio_tasks"] >= 1
    assert stats["disk_free_bytes"] > 0 and stats["uptime_sec"] >= 3725
    assert set(stats["pools"]) >= {"voice", "llm", "rag", "vision", "background"}
    if os.name == "nt":
        assert stats["rss_bytes"] > 1024 * 1024
    assert len(lines) == 1 and lines[0].startswith("[Health] rss=") and "uptime=1h02m" in lines[0]
    rows = tel.load_events({}, "unit-health")
    assert [(r["subsystem"], r["provider"]) for r in rows] == [("health", "bridge")]
    assert rows[0]["extra"]["threads"] == stats["threads"]


def test_health_never_raises(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("probe broke")

    assert arti_health.emit_health({}, record=boom, log=boom) is not None
    monkeypatch.setattr(arti_health, "collect", boom)
    assert arti_health.emit_health({}, record=boom, log=boom) is None


def test_health_thread_ticks_and_stops():
    rows = []
    t = arti_health.start_health_monitor({"health_log_interval_sec": 0.02},
                                         record=lambda stats, _cfg: rows.append(stats),
                                         log=lambda _line: None)
    try:
        assert t is not None and t.daemon
        assert _wait(lambda: len(rows) >= 2)
    finally:
        arti_health.stop_health_monitor()
    assert not t.is_alive()
    assert arti_health.start_health_monitor({"health_log_interval_sec": 0}) is None


def test_workpool_stats_report_busy_and_queued():
    pool = arti_executors.WorkPool("unit-stats", 1)
    release = threading.Event()

    async def exercise():
        first = asyncio.create_task(pool.run(release.wait, 5))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(pool.run(lambda: "ok"))
        await asyncio.sleep(0.05)
        during = pool.stats()
        release.set()
        await first
        assert await second == "ok"
        return during

    try:
        during = asyncio.run(exercise())
        assert during == {"busy": 1, "queued": 1, "workers": 1}
        assert _wait(lambda: pool.stats()["busy"] == 0)
        assert pool.stats()["queued"] == 0
    finally:
        release.set()
        pool.shutdown()


def test_main_loop_starts_health_monitor_after_transcript_init():
    import arti_bridge as bridge

    src = Path(bridge.__file__).read_text(encoding="utf-8")
    start = src.index("async def main_loop")
    body = src[start:]
    assert body.index("init_session_artifacts(CONFIG)") < body.index("arti_health.start_health_monitor(")


# --- L09: Stardew Codex scratch dirs are removed ------------------------------

def test_codex_close_removes_lane_scratch_dirs(tmp_path):
    import arti_stardew_codex as codex

    ours = tmp_path / "arti-stardew-codex-unit"
    ours.mkdir()
    other = tmp_path / "keep-me"
    other.mkdir()
    closed = []
    provider = codex.CodexSubscriptionDecisionProvider.__new__(codex.CodexSubscriptionDecisionProvider)
    provider._lock = threading.Lock()
    provider._closed = False
    provider._background_spare = None
    provider._clients = {"foreground": SimpleNamespace(close=lambda: closed.append(True))}
    provider._scratch = {"foreground": str(ours), "background": str(other)}
    provider.close()
    assert _wait(lambda: not ours.exists())
    assert closed == [True]
    assert other.exists(), "only arti-stardew-codex-* dirs may be removed"


# --- Stardew: never two drivers ----------------------------------------------

def _cycle(run: Path, name="live7-rehearsal-3", cycle="c1") -> Path:
    folder = run / name / "live7" / cycle
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _state(folder: Path, status: str, age_sec: float = 0.0):
    path = folder / "cycle_state.json"
    path.write_text(json.dumps({"schema": "live7_cycle.v1", "status": status}), encoding="utf-8")
    if age_sec:
        old = time.time() - age_sec
        os.utime(path, (old, old))


def test_guard_lock_present_is_active(tmp_path):
    (_cycle(tmp_path) / "supervisor.lock").write_text("x", encoding="utf-8")
    run = str(tmp_path / "live7-rehearsal-3")
    assert driver_guard.active_live7_run_dir({"live7_help_dir": str(tmp_path)}) == run


def test_guard_fresh_running_state_is_active(tmp_path):
    _state(_cycle(tmp_path), "active")
    run = str(tmp_path / "live7-rehearsal-3")
    assert driver_guard.active_live7_run_dir({"stardew_live7_help_dir": str(tmp_path)}) == run


@pytest.mark.parametrize("status,age", [("ready", 13 * 3600), ("ready", 0), ("stopped", 0),
                                        ("ended", 0), ("safety_stop", 0), ("running", 13 * 3600)])
def test_guard_old_or_stopped_state_is_allowed(tmp_path, status, age):
    _state(_cycle(tmp_path), status, age)
    assert driver_guard.active_live7_run_dir({"live7_help_dir": str(tmp_path)}) is None


def test_guard_without_run_dir_or_with_garbage_is_allowed(tmp_path):
    assert driver_guard.active_live7_run_dir({}) is None
    assert driver_guard.active_live7_run_dir({"live7_help_dir": str(tmp_path / "missing")}) is None
    (_cycle(tmp_path) / "cycle_state.json").write_text("{not json", encoding="utf-8")
    assert driver_guard.active_live7_run_dir({"live7_help_dir": str(tmp_path)}) is None


def test_guard_falls_back_to_command_dir_parent(tmp_path):
    _state(_cycle(tmp_path), "host_wait")
    cfg = {"stardew_command_dir": str(tmp_path / "commands")}
    assert driver_guard.active_live7_run_dir(cfg) == str(tmp_path / "live7-rehearsal-3")


def test_bridge_refuses_runtime_but_help_relay_starts(monkeypatch, tmp_path, capsys):
    import arti_bridge as bridge
    import arti_help_relay as relay_module
    import arti_stardew_runtime
    from arti_help_relay import HelpRelay

    (_cycle(tmp_path) / "supervisor.lock").write_text("x", encoding="utf-8")

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            pass

        def join(self, **_kwargs):
            pass

    monkeypatch.setattr(bridge, "_live7_help_relay", None)
    monkeypatch.setattr(bridge, "_stardew_runtime", None)
    monkeypatch.setattr(bridge, "_stardew_bridge_trial", None)
    monkeypatch.setitem(bridge.CONFIG, "stardew_runtime_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_dir", str(tmp_path))
    monkeypatch.setitem(bridge.CONFIG, "stardew_live7_help_dir", "")
    monkeypatch.setitem(bridge.CONFIG, "live7_help_speak_enabled", False)
    monkeypatch.setattr(arti_stardew_runtime, "construct_if_enabled",
                        lambda _cfg: pytest.fail("second Stardew driver constructed"))
    monkeypatch.setattr(relay_module, "HelpRelay", lambda *args, **kwargs: HelpRelay(
        *args, **kwargs, clock=lambda: 10000, thread_factory=FakeThread))
    try:
        bridge._start_stardew_runtime_if_enabled()
        bridge._start_live7_help_if_enabled()
        relay = bridge._live7_help_relay
        assert bridge._stardew_runtime is None
        run = tmp_path / "live7-rehearsal-3"
        relay.tick()  # combined3: with status facts on, the channel is resolved on the first tick
        # The help file channel follows the active cycle's run folder.
        assert relay is not None and relay._channel.help_dir == run
        assert relay._runtime is None
        out = capsys.readouterr().out
        assert f"[Stardew] runtime NOT started: Live7 cycle aktif di {run}" in out
        assert f"[Help] file channel → {run} (cycle c1)" in out
    finally:
        bridge._stop_live7_help_relay()

"""Operational Live7 facts: worker I/O, honest state, and acknowledged delivery."""

import asyncio
import json
import os
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import arti_bridge as bridge
from arti_help_bus import HelpRequest
from arti_help_relay import HelpRelay, Live7HelpChannelResolver
from arti_live7_moments import MomentsRelay
from arti_live7_status_facts import StatusFactsRelay, StatusText, parse_event
from arti_stardew_driver_guard import ActiveCycle


def append(path, **row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(domain="stardew", cycle="c1", **row)) + "\n")


@pytest.mark.parametrize("row,group,resolved,words", [
    ({"ev": "SAFETY_STOP", "reason": "pad_watch_violation", "line": 14}, "stop", False, "pad_watch_violation"),
    ({"ev": "live7_stop_file_created", "source": "cycle_safety_stop"}, "stop", False, "cycle_safety_stop"),
    ({"ev": "cycle_safety_stop"}, "stop", False, "darurat"),
    ({"ev": "live7_stop_file", "action": "stop"}, "stop", False, "berhenti"),
    ({"ev": "cycle_stopped"}, "stop", True, "Bohan menghentikan"),
    ({"ev": "live7_monitor_violation"}, "stop", False, "darurat"),
    ({"ev": "part_live_row", "outcome_class": "SAFETY_STOP", "reason": "live7:identity_unavailable"}, "stop", False, "identity_unavailable"),
    ({"ev": "crash", "day_trial": "day-0001-attempt-000"}, "connection", False, "crash"),
    ({"ev": "farmhand_crash"}, "connection", False, "crash"),
    ({"ev": "trial_crash"}, "connection", False, "crash"),
    ({"ev": "session_dropped"}, "connection", False, "terputus"),
    ({"ev": "host_crash", "message": "Waiting for the owner to reload HOST"}, "connection", False, "HOST"),
    ({"ev": "rejoin_attempt", "attempt": 1, "slot": "name:Arti"}, "connection", False, "menyambung ulang"),
    ({"ev": "rejoin_start"}, "connection", False, "menyambung ulang"),
    ({"ev": "rejoin_started"}, "connection", False, "menyambung ulang"),
    ({"ev": "rejoin_failed", "attempt": 2}, "connection", False, "gagal"),
    ({"ev": "rejoin_complete", "attempt": 1, "day_replay": False}, "connection", True, "kembali"),
    ({"ev": "rejoin_end"}, "connection", True, "kembali"),
    ({"ev": "rejoin_ended"}, "connection", True, "kembali"),
    ({"ev": "host_wait", "goal_id": "goal-84d6868d9826", "line": 3676}, "host_wait", False, "tidur"),
    ({"ev": "bed_waiting_for_host", "menu_type": "ReadyCheckDialog", "waited_s": 0.2}, "host_wait", False, "tidur"),
    ({"ev": "host_wait_ended", "day_trial": "day-0003-attempt-000"}, "host_wait", True, "selesai"),
    ({"ev": "bed_wait_ended", "reason": "day_changed", "waited_s": 13.9}, "host_wait", True, "selesai"),
    ({"ev": "live7_stop_file_cleared"}, "stop", True, "belum dipastikan"),
    ({"ev": "stop_cleared"}, "stop", True, "belum dipastikan"),
])
def test_realistic_event_contracts(row, group, resolved, words):
    row = dict(at=1792696175.7633483, domain="stardew", cycle="c1", **row)
    event = parse_event(row)
    assert (event.group, event.resolved) == (group, resolved)
    assert words in event.fact
    assert event == parse_event(json.loads(json.dumps(row)))
    assert event.event_id != parse_event(row, "other-cycle").event_id


@pytest.mark.parametrize("row", [None, [], {}, {"ev": "rejoin_exit_recorded"},
                                      {"ev": "session_read_recovered"},
                                      {"ev": "crash", "domain": "minecraft"}])
def test_unrelated_and_nonresolution_events_are_ignored(row):
    assert parse_event(row) is None


@pytest.fixture
def rig(tmp_path):
    clock, voice = [10000.0], []
    gates = {"viewer": False, "queue": False, "ready": True}
    config = {"live7_status_facts_enabled": True, "live7_help_enabled": False,
              "live7_moments_enabled": False, "live7_moments_min_gap_sec": 0}
    cycle = tmp_path / "live7" / "c1"
    cycle.mkdir(parents=True)
    moments = MomentsRelay(config, started_at=clock[0],
                           viewer_waiting=lambda: gates["viewer"],
                           game_trigger_waiting=lambda: gates["queue"])
    facts = StatusFactsRelay(config, moments, clock=lambda: clock[0],
                             voice_ready=lambda: gates["ready"])

    def enqueue(text, **kw):
        voice.append(text)
        assert kw == {"trigger_type": "game"}
        return True

    def tick(**kw):
        return facts.tick(cycle, enqueue, now=clock[0], **kw)

    return SimpleNamespace(clock=clock, voice=voice, gates=gates, config=config,
        cycle=cycle, events=cycle / "cycle_events.jsonl", moments=moments,
        facts=facts, enqueue=enqueue, tick=tick)


def test_priority_retries_until_voiced_and_bounds_attempts(rig):
    r = rig
    for ev in ("host_wait", "crash", "SAFETY_STOP"):
        append(r.events, ev=ev, at=r.clock[0])
    r.tick()
    assert "darurat" in r.voice[-1]
    assert r.voice[-1].is_current()
    r.clock[0] += 9
    r.tick()
    assert len(r.voice) == 1  # acceptance did not acknowledge delivery
    r.clock[0] += 1
    r.tick()
    assert len(r.voice) == 2 and not r.voice[0].is_current()
    r.voice[-1].mark_voiced()
    r.tick()
    assert "crash" in r.voice[-1]
    r.clock[0] += 301
    r.tick()
    assert len(r.voice) == 3 and not r.voice[-1].is_current()
    assert "darurat" in r.facts.status_block()  # retry expiry is not recovery


@pytest.mark.parametrize("failure", [False, "raise"])
def test_enqueue_rejection_and_exception_retry(failure, rig):
    r = rig
    append(r.events, ev="session_dropped", at=r.clock[0])
    attempts = []

    def enqueue(text, **_):
        attempts.append(text)
        if failure == "raise":
            raise RuntimeError("queue unavailable")
        return False

    for advance in (0, 10, 299, 300):
        r.clock[0] = 10000 + advance
        r.facts.tick(r.cycle, enqueue, now=r.clock[0])
    assert len(attempts) == 3


@pytest.mark.parametrize("gate", ["viewer", "queue", "ready", "help", "gap"])
def test_shared_gates_defer_without_losing_fact(gate, rig):
    r = rig
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    if gate in ("viewer", "queue"):
        r.gates[gate] = True
    elif gate == "ready":
        r.gates[gate] = False
    elif gate == "gap":
        r.config["live7_moments_min_gap_sec"] = 90
        r.moments._last_voiced_at = r.clock[0]
    r.tick(help_open=gate == "help")
    assert not r.voice and "darurat" in r.facts.status_block()
    r.gates.update(viewer=False, queue=False, ready=True)
    r.clock[0] += 91
    r.tick()
    assert len(r.voice) == 1


def test_dedupe_native_and_derived_event_ids_even_after_truncation(rig):
    r = rig
    row = dict(ev="crash", at=r.clock[0], event_id="crash-1")
    append(r.events, **row)
    append(r.events, **row)
    r.tick()
    r.voice[-1].mark_voiced()
    row["at"] += 1  # native ID remains authoritative
    append(r.events, **row)
    r.clock[0] += 10
    r.tick()
    assert len(r.voice) == 1
    r.events.write_text("", encoding="utf-8")
    r.tick()
    append(r.events, **row)
    r.tick()
    assert len(r.voice) == 1
    assert parse_event({"ev": "crash"}).event_id == parse_event({"ev": "crash"}).event_id


def test_resolution_retires_obsolete_facts_and_clears_all_status(rig):
    r = rig
    for ev in ("SAFETY_STOP", "crash", "host_wait"):
        append(r.events, ev=ev, at=r.clock[0])
    r.tick()
    old = r.voice[-1]
    for ev in ("rejoin_complete", "host_wait_ended", "stop_cleared"):
        append(r.events, ev=ev, at=r.clock[0] + 1)
    r.clock[0] += 1
    r.tick()
    assert r.facts.status_block() == "" and not old.is_current()
    assert "belum dipastikan" in r.voice[-1]
    old.mark_voiced()  # late completion must not ack its replacement
    assert r.voice[-1].is_current()


def test_stop_sentinel_disappearance_is_resolution_without_claiming_game_recovered(rig):
    r = rig
    stop = r.cycle.parent.parent / "LIVE7_STOP"
    stop.touch()
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    r.tick()
    assert not r.voice and "darurat" in r.facts.status_block()
    stop.unlink()
    r.tick()
    assert r.facts.status_block() == ""
    assert "dilepas" in r.voice[-1] and "belum dipastikan" in r.voice[-1]


def test_partial_utf8_invalid_rows_rotation_and_incremental_reads(rig):
    r = rig
    payload = json.dumps(dict(ev="SAFETY_STOP", at=r.clock[0], reason="berhenti — cek"),
                         ensure_ascii=False).encode("utf-8")
    cut = payload.index("—".encode("utf-8")) + 1
    r.events.write_bytes(payload[:cut])
    r.tick()
    assert not r.voice
    with r.events.open("ab") as handle:
        handle.write(payload[cut:] + b"\ninvalid\n[]\n")
    r.tick()
    assert "berhenti — cek" in r.voice[-1]
    r.voice[-1].mark_voiced()
    r.tick()
    assert len(r.voice) == 1
    r.events.replace(r.events.with_suffix(".old"))
    append(r.events, ev="stop_cleared", at=r.clock[0])
    r.tick()
    assert "dilepas" in r.voice[-1]


def test_journal_enriches_reason_and_new_safety_rows_reach_model(rig):
    r = rig
    trial = "day-0003-attempt-000"
    journal = r.cycle / trial / "journal" / "g1_journal.jsonl"
    append(journal, ev="day_start_blocked", reason="ui_key_uncertain", menu_type="LevelUpMenu", at=r.clock[0])
    append(r.events, ev="day_trial_started", day_trial=trial, at=r.clock[0])
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    r.tick()
    assert "ui_key_uncertain" in r.voice[-1]
    append(journal, ev="part_live_row", outcome_class="SAFETY_STOP", reason="identity_changed")
    r.clock[0] += 10
    r.tick()
    assert "identity_changed" in r.voice[-1]


def test_ownership_facts_from_journal_are_facts_only_and_one_line(rig):
    """Smoke 10-24 rows 14/22: own gold and own plantings as facts; no incident wording without an incident."""
    r = rig
    trial = "day-0001-attempt-000"
    journal = r.cycle / trial / "journal" / "g1_journal.jsonl"
    append(r.events, ev="day_trial_started", day_trial=trial, at=r.clock[0])
    append(journal, ev="live7_baseline", skill="kirim", arg="Daffodil",
           before={"inventory": {"(O)18": 1}, "money": 278, "location": "Farm"})
    append(journal, ev="live7_crop_memory", crops={
        "tf:Farm:25,34:HoeDirt": {"location": "Farm", "tile": [25, 34], "planted_date": [1, "spring", 7]},
        "tf:Farm:26,34:HoeDirt": {"location": "Farm", "tile": [26, 34], "planted_date": [1, "spring", 7]}})
    r.tick()
    block = r.facts.status_block()
    assert "Uangmu sendiri: 278g" in block and "dompet karakter ARTI" in block
    assert "Tanaman yang kamu tanam sendiri (terverifikasi): 2; 2 di Farm (petak 25,34, 26,34)" in block
    assert "kenapa diam" not in block and not r.voice  # facts, never a voiced incident
    assert block.strip().count("\n") == 0 and block.startswith("\n[STARDEW STATUS] ")
    append(journal, ev="live7_baseline", skill="jual", arg="Wood", before={"money": 129})
    append(r.events, ev="crash", at=r.clock[0])
    r.tick()
    block = r.facts.status_block()
    assert "129g" in block and "278g" not in block and "crash" in block and "kenapa diam" in block
    assert block.strip().count("\n") == 0
    # The reserved line survives the system-prompt trim with the ownership facts intact.
    trimmed = bridge.trim_system_prompt_for_llm("x" * 9000 + block, {"llm_system_prompt_max_chars": 100})
    assert "129g" in trimmed and "Tanaman yang kamu tanam sendiri" in trimmed
    r.facts._poll(r.cycle.parent / "c2", r.clock[0])
    assert r.facts.status_block() == ""  # a new cycle starts without inherited ownership


def test_large_journal_bootstrap_uses_recent_diagnostic_without_replaying_history(rig):
    r = rig
    trial = "day-0003-attempt-000"
    journal = r.cycle / trial / "journal" / "g1_journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b'{"ev":"old_observation"}\n' * 60000)
    append(journal, ev="day_start_blocked", reason="ui_key_uncertain", menu_type="LevelUpMenu", at=r.clock[0])
    append(r.events, ev="day_trial_started", day_trial=trial, at=r.clock[0])
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    r.tick()
    assert "ui_key_uncertain" in r.voice[-1]
    assert r.facts._tails[journal].offset == journal.stat().st_size


def test_old_history_restores_current_state_without_replaying_notifications(rig):
    r = rig
    append(r.events, ev="SAFETY_STOP", at=r.clock[0] - 1000)
    r.tick()
    assert not r.voice and "darurat" in r.facts.status_block()
    append(r.events, ev="stop_cleared", at=r.clock[0] - 900)
    r.tick()
    assert not r.voice and not r.facts.status_block()


def make_relay(r, enqueue=None, **kw):
    resolver = Live7HelpChannelResolver(r.config, active_cycles=lambda *_a, **_k:
        [ActiveCycle(r.cycle, r.cycle.parent.parent, r.clock[0])], announce=lambda _: None)
    return HelpRelay(r.config, enqueue or r.enqueue, clock=lambda: r.clock[0],
                     channel_resolver=resolver, resolve_interval_sec=0, **kw)


def test_works_with_moments_off_and_has_priority_when_on(rig):
    r = rig
    relay = make_relay(r)
    append(r.events, ev="crash", at=r.clock[0])
    relay.tick()
    assert isinstance(r.voice[-1], StatusText)
    assert "crash" in relay.status_block()
    r.config["live7_moments_enabled"] = True
    moment = r.cycle.parent.parent / "ev-10000000-1.json"
    moment.write_text(json.dumps(dict(schema="arti.stardew_moment/1", kind="planted",
        situation="[Stardew] Baru menanam.", ts=r.clock[0], ttl_s=120)), encoding="utf-8")
    os.utime(moment, (r.clock[0], r.clock[0]))
    r.clock[0] += 10
    relay.tick()
    assert len(r.voice) == 2 and all(isinstance(t, StatusText) for t in r.voice)
    assert not moment.exists()  # optional inbox still drains


def test_real_help_window_blocks_status_delivery(rig):
    r = rig
    r.config.update(live7_help_enabled=True, live7_help_speak_enabled=False)
    relay = make_relay(r)
    relay.submit(HelpRequest(request_id="f" * 32, session_id="s", reason="stuck",
        game_date="Y1 Spring 3", game_time=2200, location="Farm", skill="tidur",
        detail="menunggu", created_at=r.clock[0], expires_at=r.clock[0] + 600))
    append(r.events, ev="crash", at=r.clock[0])
    relay.tick()
    assert not r.voice
    relay.note_owner_text("tunggu sebentar")
    relay.tick()
    assert len(r.voice) == 1


def test_worker_tails_and_main_voice_reads_never_touch_files(rig, monkeypatch):
    r = rig
    append(r.events, ev="crash", at=r.clock[0])
    relay = make_relay(r)
    monkeypatch.setattr(bridge, "_live7_help_relay", relay)
    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", True)
    original = {name: getattr(Path, name) for name in ("open", "stat", "exists", "glob")}
    threads = []

    def guarded(name):
        def call(path, *a, **kw):
            threads.append(threading.current_thread().name)
            assert threading.current_thread().name == "live7-help"
            return original[name](path, *a, **kw)
        return call

    for name in original:
        monkeypatch.setattr(Path, name, guarded(name))
    relay.start()
    try:
        deadline = time.monotonic() + 4
        while not relay.status_block() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "crash" in relay.status_block()
        for _ in range(30):
            assert bridge._live7_status_block_now() == relay.status_block()
            if r.voice:
                assert not bridge._game_reaction_expired(bridge.VoiceTrigger(r.voice[-1], "game"))
        assert threads and set(threads) == {"live7-help"}
    finally:
        relay.stop()


def test_resolver_keeps_terminal_cycle_after_active_registration_disappears(rig):
    r = rig
    active = [ActiveCycle(r.cycle, r.cycle.parent.parent, r.clock[0])]
    resolver = Live7HelpChannelResolver(r.config, active_cycles=lambda *_a, **_k: active,
                                       announce=lambda _: None)
    relay = HelpRelay(r.config, r.enqueue, clock=lambda: r.clock[0], channel_resolver=resolver,
                      resolve_interval_sec=0)
    relay.tick()
    active.clear()
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    relay.tick()
    assert "darurat" in relay.status_block()


def test_start_gate_with_only_status_enabled_defers_resolver_io(monkeypatch):
    import arti_help_relay as module

    started = []
    monkeypatch.setattr(bridge, "_live7_help_relay", None)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "live7_moments_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", True)
    monkeypatch.setattr(module.Live7HelpChannelResolver, "__call__",
                        lambda _: pytest.fail("resolver I/O on startup caller"))
    monkeypatch.setattr(module.HelpRelay, "start", lambda relay: started.append(relay))
    bridge._start_live7_help_if_enabled()
    assert started == [bridge._live7_help_relay]
    bridge._stop_live7_help_relay()


@pytest.mark.parametrize("buffered", [False, True])
def test_status_survives_real_voice_queues_and_uses_own_kill_switch(rig, monkeypatch, buffered):
    r = rig
    append(r.events, ev="crash", at=r.clock[0])
    r.tick()
    text = r.voice[-1]
    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "live7_moments_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "voice_queue_enabled", buffered)
    monkeypatch.setattr(bridge, "_note_stardew_owner_ingress", lambda *_: None)
    monkeypatch.setattr(bridge, "_brain_busy", False)
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    monkeypatch.setattr(bridge.session_transcript, "log_trigger", lambda *_: "fake")
    monkeypatch.setattr(bridge, "voice_trigger_queue", queue.Queue())
    monkeypatch.setattr(bridge, "voice_trigger_buffer", bridge.arti_voice_queue.VoiceTriggerQueue())
    assert bridge.queue_voice_trigger(text, trigger_type="game")
    item = bridge.voice_trigger_buffer.dequeue() if buffered else bridge.voice_trigger_queue.get_nowait()
    trigger = bridge._normalize_voice_trigger(item)
    assert trigger.text is text and not bridge._game_reaction_expired(trigger)
    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", False)
    assert bridge._game_reaction_expired(trigger)


def test_kill_switch_has_zero_io_and_byte_identical_turn_prompt(rig, monkeypatch):
    r = rig
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    r.tick()
    monkeypatch.setattr(bridge, "_live7_help_relay", SimpleNamespace(status_block=r.facts.status_block))
    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", False)
    r.config["live7_status_facts_enabled"] = False
    monkeypatch.setattr(r.facts, "_poll", lambda *_: pytest.fail("disabled facts polled"))
    r.tick()
    monkeypatch.setattr(bridge, "_prepare_turn_start", _nothing)
    monkeypatch.setattr(bridge, "mood_block_now", lambda: "\nMOOD")
    monkeypatch.setattr(bridge, "build_today_block", lambda: "\nHARI INI: Jumat")
    monkeypatch.setattr(bridge, "viewer_block_for", lambda _: "\nVIEWER")
    monkeypatch.setattr(bridge, "discord_stories_block_now", lambda: "\nDISCORD")
    monkeypatch.setattr(bridge, "stardew_role_block_now", lambda: "\nROLE")
    captured = []

    class Captured(Exception):
        pass

    async def capture(_speech, _memories, system, *_a, **_kw):
        captured.append(system)
        raise Captured

    monkeypatch.setattr(bridge.arti_voice_pipeline, "prepare_turn_context", capture)
    with pytest.raises(Captured):
        asyncio.run(bridge._handle_voice_trigger(bridge.VoiceTrigger("kok diem?", "game"), [], "dasar — ARTI"))
    legacy = "dasar — ARTI\nMOOD\nHARI INI: Jumat\nVIEWER\nDISCORD\nROLE"
    assert captured[0].encode("utf-8") == legacy.encode("utf-8")
    assert bridge._live7_status_block_now() == ""


async def _nothing(*_a, **_kw):
    pass


def test_model_receives_facts_with_natural_honest_instruction_without_lookup(rig, monkeypatch):
    import arti_voice_pipeline as pipeline

    r = rig
    append(r.events, ev="crash", at=r.clock[0])
    r.tick()

    async def run(_lane, fn, *a, **kw):
        assert fn is history
        return fn()

    def history():
        return ""

    monkeypatch.setattr(pipeline.arti_executors, "run", run)
    ctx = asyncio.run(pipeline.prepare_turn_context(r.voice[-1], [], "system", {},
        trim_system_prompt=lambda text, _: text, append_watch_party_context=lambda text: text,
        get_categorized_history=history, extract_trigger_message=lambda text: text))
    assert "Fakta:" in ctx.prompt_content and "crash" in ctx.prompt_content
    assert "jujur ada yang salah" in ctx.target_instruction
    assert "1–2 kalimat" in ctx.prompt_content


@pytest.mark.parametrize("interrupted", [False, True])
def test_tts_completion_contract_rejects_interruption(monkeypatch, interrupted):
    monkeypatch.setattr(bridge, "tts_stop_flag", False)
    monkeypatch.setattr(bridge.screen_privacy, "current", lambda _: True)
    monkeypatch.setitem(bridge.CONFIG, "tts_jeda_antar_kalimat_sec", 0)
    spoken = []

    async def speak(chunk):
        spoken.append(chunk)
        bridge.tts_stop_flag = interrupted
        engine._last_playback_completed = not interrupted

    engine = SimpleNamespace(speak=speak)
    monkeypatch.setattr(bridge, "tts", engine, raising=False)
    assert asyncio.run(bridge._speak_reply_chunks(["Satu.", "Dua."], 1)) is not interrupted
    assert len(spoken) == (1 if interrupted else 2)


def test_swallowed_tts_failure_does_not_acknowledge_status(rig, monkeypatch):
    r = rig
    append(r.events, ev="crash", at=r.clock[0])
    r.tick()
    engine = bridge.TTSEngine.__new__(bridge.TTSEngine)

    async def failed(_text):
        raise RuntimeError("synthesis failed")

    monkeypatch.setattr(engine, "_speak_edge_tts", failed)
    monkeypatch.setattr(bridge, "tts", engine, raising=False)
    monkeypatch.setattr(bridge, "tts_stop_flag", False)
    monkeypatch.setattr(bridge.screen_privacy, "current", lambda _: True)
    monkeypatch.setitem(bridge.CONFIG, "tts_engine", "edge_tts")
    monkeypatch.setitem(bridge.CONFIG, "tts_jeda_antar_kalimat_sec", 0)
    engine._last_playback_completed = True  # previous utterance must not leak
    voiced = asyncio.run(bridge._speak_reply_chunks(["Ada masalah."], 1))
    assert not voiced and r.voice[-1].is_current()
    r.clock[0] += 10
    r.tick()
    assert len(r.voice) == 2


def test_reenable_restores_snapshot_without_io_in_reader(rig):
    r = rig
    append(r.events, ev="crash", at=r.clock[0])
    r.tick()
    before = r.facts.status_block()
    r.config["live7_status_facts_enabled"] = False
    r.tick()
    assert not r.facts.status_block()
    r.config["live7_status_facts_enabled"] = True
    r.tick()
    assert r.facts.status_block() == before


def test_stop_invalidates_speech_and_cannot_restart_on_late_config_refresh(rig):
    r = rig
    append(r.events, ev="crash", at=r.clock[0])
    r.tick()
    old = r.voice[-1]
    r.facts.stop()
    r.facts.config = r.config
    r.clock[0] += 10
    r.tick()
    assert not old.is_current() and not r.facts.status_block() and len(r.voice) == 1


def test_crash_then_rejoin_preserves_cause_and_ends_obsolete_host_wait(rig):
    r = rig
    for ev in ("host_wait", "crash", "rejoin_attempt"):
        append(r.events, ev=ev, at=r.clock[0])
    r.tick()
    assert "crash" in r.voice[-1] and "menyambung ulang" in r.voice[-1]
    assert "tempat tidur" not in r.facts.status_block()
    append(r.events, ev="rejoin_complete", at=r.clock[0] + 1)
    r.clock[0] += 1
    r.tick()
    assert "crash" in r.voice[-1] and "kembali" in r.voice[-1]
    assert r.facts.status_block() == ""


@pytest.mark.parametrize("outcome", ["voiced", "tts_failed", "silent", "resolved_during_generation",
                                     "stop_question", "emotion_leak"])
def test_full_turn_acknowledges_only_successful_current_fact(rig, monkeypatch, outcome):
    r = rig
    if outcome == "stop_question":
        # DR4: bridge observes c1 while running; STOP removes it from active
        # discovery, then an ordinary operator mic/text turn asks its status.
        r.config.update(active_profile="smoke", live7_help_dir=str(r.cycle.parent.parent),
                        stardew_command_dir=str(r.cycle.parent.parent / "command"))
        state = r.cycle / "cycle_state.json"
        state.write_text(json.dumps({"status": "running"}), encoding="utf-8")
        resolver = Live7HelpChannelResolver(r.config, r.config["live7_help_dir"],
            clock=lambda: r.clock[0], announce=lambda _: None)
        relay = HelpRelay(r.config, r.enqueue, clock=lambda: r.clock[0],
                          channel_resolver=resolver, resolve_interval_sec=0)
        relay.tick()
        assert resolver.cycle_dir == r.cycle
        (r.cycle.parent.parent / "LIVE7_STOP").touch()
        append(r.events, ev="live7_stop_file_created", source="operator_stop", at=r.clock[0])
        append(r.events, ev="cycle_stopped", at=r.clock[0])
        state.write_text(json.dumps({"status": "stopped"}), encoding="utf-8")
        r.clock[0] += 22  # DR4 asked about 22 seconds after STOP.
        relay.tick()
        assert resolver.cycle_dir == r.cycle
        # K16: STOP / no active cycle means no status/moment voice. The owner
        # question below still sees the planned-stop fact in the prompt.
        if r.voice:
            r.voice[-1].mark_voiced()
        relay.tick()
        monkeypatch.setattr(bridge, "_live7_help_relay", relay)
        text = "Arti, status game sekarang bagaimana: lokasi, jam, energi dan HP? Berhenti atau bergerak?"
    else:
        append(r.events, ev="crash", at=r.clock[0])
        r.tick()
        text = r.voice[-1]
        monkeypatch.setattr(bridge, "_live7_help_relay", SimpleNamespace(status_block=r.facts.status_block))
    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "expression_nod_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "expression_emotion_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "tts_jeda_antar_kalimat_sec", 0)
    for name in ("_pending_turn_id", "current_api_task"):
        monkeypatch.setattr(bridge, name, None)
    for name in ("hotkey_active", "tts_stop_flag"):
        monkeypatch.setattr(bridge, name, False)
    monkeypatch.setattr(bridge, "last_arti_reply_text", "")
    monkeypatch.setattr(bridge, "_prepare_turn_start", _nothing)
    monkeypatch.setattr(bridge, "PipelineTimer", Mock(return_value=Mock(stages_ms=lambda: {})))
    monkeypatch.setattr(bridge.screen_privacy, "current", lambda _: True)
    for name in ("mood_block_now", "build_today_block", "discord_stories_block_now", "stardew_role_block_now"):
        monkeypatch.setattr(bridge, name, lambda: "")
    monkeypatch.setattr(bridge, "viewer_block_for", lambda _: "")
    context = SimpleNamespace(formatted_history="", llm_system="system", prompt_content="facts",
                              target_instruction="react", rag_query="")
    monkeypatch.setattr(bridge, "_live7_reply_facts_now", lambda: "")
    prepared = AsyncMock(return_value=context)
    monkeypatch.setattr(bridge.arti_voice_pipeline, "prepare_turn_context", prepared)

    def generate(_trigger):
        if outcome == "resolved_during_generation":
            r.facts._accept(parse_event({"ev": "rejoin_complete"}), r.clock[0])
        if outcome == "stop_question":
            return "Sesi Stardew aku lagi berhenti.", True
        if outcome == "emotion_leak":
            return "Aku pegang siaran dulu. [EMOTION: excited]", True
        return ("[DIAM]" if outcome == "silent" else "Aku terputus, lagi ada masalah."), True

    monkeypatch.setattr(bridge, "_try_stardew_owner_generation", generate)
    monkeypatch.setattr(bridge, "_should_route_to_cursor", lambda _: False)
    monkeypatch.setattr(bridge, "clean_ai_reply", lambda value: value)
    monkeypatch.setattr(bridge, "post_process_response", lambda value, *_: value)
    monkeypatch.setattr(bridge, "_maybe_execute_reply_tags", lambda value, *_: value)
    monkeypatch.setattr(bridge, "vts", None)
    monkeypatch.setattr(bridge.arti_expression_runtime, "apply_speaking", _nothing)
    monkeypatch.setattr(bridge.arti_expression_runtime, "apply_turn_end", _nothing)
    monkeypatch.setattr(bridge.arti_expression_runtime, "should_nod_for_emotion", lambda *_: False)
    monkeypatch.setattr(bridge, "_media_playback_until", 0)
    for name in ("add_to_history", "_cermin_ke_chat_game", "_schedule_post_answer_cleanup"):
        monkeypatch.setattr(bridge, name, Mock())
    monkeypatch.setattr(bridge.voice_listener_worker, "_last_tts_end", 0, raising=False)
    engine = SimpleNamespace(speak=AsyncMock(), _last_playback_completed=outcome != "tts_failed")
    monkeypatch.setattr(bridge, "tts", engine, raising=False)
    asyncio.run(bridge._handle_voice_trigger(
        bridge.VoiceTrigger(text, "mic" if outcome == "stop_question" else "game"), [], "persona"))
    assert "[STARDEW STATUS]" in prepared.call_args.args[2]
    if outcome == "stop_question":
        assert "Bohan menghentikan sesi Stardew" in prepared.call_args.args[2]
        engine.speak.assert_awaited()
        assert "berhenti" in engine.speak.await_args.args[0]
    elif outcome == "emotion_leak":
        assert engine.speak.await_args.args[0] == "Aku pegang siaran dulu."
        assert bridge.add_to_history.call_args.args[1] == "Aku pegang siaran dulu."
        assert bridge._cermin_ke_chat_game.call_args.args[0] == "Aku pegang siaran dulu."
    elif outcome == "voiced":
        assert not text.is_current() and "crash" in r.facts.status_block()
        assert not r.facts._pending
    elif outcome == "resolved_during_generation":
        engine.speak.assert_not_awaited()
        assert not text.is_current() and not r.facts.status_block()
    else:
        assert text.is_current()
        if outcome == "silent":
            engine.speak.assert_not_awaited()


@pytest.mark.parametrize("row", [
    {"ev": "operator_stop"}, {"ev": "observed_stop"}, {"ev": "cycle_stopped"},
    {"ev": "live7_stop_file_created", "source": "operator_stop"},
    {"ev": "live7_stop_file_created", "source": "observed_stop"},
])
def test_review_1_planned_stop_is_neutral_and_clears_stuck_state(rig, row):
    r = rig
    trial = "day-0003-attempt-000"
    journal = r.cycle / trial / "journal" / "g1_journal.jsonl"
    append(journal, ev="old_observation")
    append(r.events, ev="day_trial_started", day_trial=trial, at=r.clock[0])
    for ev in ("SAFETY_STOP", "crash", "host_wait"):
        append(r.events, ev=ev, at=r.clock[0])
    r.tick()
    old = r.voice[-1]
    append(r.events, **row, at=r.clock[0] + 1)
    r.clock[0] += 1
    r.tick()
    assert "Bohan menghentikan sesi Stardew" in r.voice[-1]
    assert "darurat" not in r.voice[-1] and "perlu ngecek" not in r.voice[-1]
    assert "Bohan menghentikan sesi Stardew" in r.facts.status_block() and not old.is_current()
    assert "darurat" not in r.facts.status_block()
    append(journal, ev="part_live_row", outcome_class="SAFETY_STOP", reason="stop:live7_stop_file")
    r.clock[0] += 1
    r.tick()
    assert "Bohan menghentikan sesi Stardew" in r.facts.status_block()  # still a planned stop


@pytest.mark.parametrize("row", [
    {"ev": "SAFETY_STOP"},
    {"ev": "live7_stop_file_created", "source": "pad_watch"},
    {"ev": "live7_journal_alert", "outcome_class": "SAFETY_STOP"},
    {"ev": "live7_monitor_violation"},
])
def test_review_1_real_safety_stops_remain_emergencies(row):
    assert "darurat" in parse_event(row).fact


@pytest.mark.parametrize("resolution", ["new_cycle", "sentinel_cleared"])
def test_planned_stop_fact_outlives_delivery_and_expires_only_on_state_change(rig, resolution):
    r = rig
    stop = r.cycle.parent.parent / "LIVE7_STOP"
    stop.touch()
    append(r.events, ev="cycle_stopped", at=r.clock[0])
    r.tick()
    assert not r.voice
    assert "Bohan menghentikan sesi Stardew" in r.facts.status_block()
    r.clock[0] += 301
    r.tick()
    assert "Bohan menghentikan sesi Stardew" in r.facts.status_block()
    assert len(r.voice) == 0  # stop file present: no status voice (K16)
    if resolution == "new_cycle":
        new_cycle = r.cycle.parent / "c2"
        new_cycle.mkdir()
        r.facts.tick(new_cycle, r.enqueue, now=r.clock[0])
    else:
        stop.unlink()
        r.tick()
    assert not r.facts.status_block()


@pytest.mark.parametrize("ev", ["host_wait_ended", "bed_wait_ended"])
def test_review_2_host_wait_resolution_clears_without_speaking(rig, ev):
    r = rig
    append(r.events, ev="host_wait", at=r.clock[0])
    r.tick()
    assert "tempat tidur" in r.voice[-1] and "petani HOST" in r.voice[-1]
    assert "Bohan" not in r.voice[-1]
    r.voice[-1].mark_voiced()
    append(r.events, ev=ev, at=r.clock[0] + 1)
    r.clock[0] += 1
    r.tick()
    assert len(r.voice) == 1 and not r.facts.status_block()


def review_moment(r):
    path = r.cycle.parent.parent / f"ev-{int(r.clock[0] * 1000)}-1.json"
    path.write_text(json.dumps(dict(schema="arti.stardew_moment/1", kind="planted",
        situation="[Stardew] Baru menanam.", ts=r.clock[0], ttl_s=120)), encoding="utf-8")
    os.utime(path, (r.clock[0] - 1, r.clock[0] - 1))
    return path


@pytest.mark.parametrize("ev", ["host_wait", "operator_stop"])
def test_review_3_low_priority_fact_yields_to_optional_moment(rig, ev):
    r = rig
    r.config["live7_moments_enabled"] = True

    def enqueue(text, **_):
        r.voice.append(text)
        r.gates["queue"] = True
        return True

    relay = make_relay(r, enqueue=enqueue, game_trigger_waiting=lambda: r.gates["queue"])
    append(r.events, ev=ev, at=r.clock[0])
    moment = review_moment(r)
    relay.tick()
    assert r.voice == ["[Stardew] Baru menanam."]
    assert not moment.exists() and relay._moments.voiced == 1
    assert relay._moments.dropped == 0


def test_review_3_queued_low_priority_fact_preserves_unvoiced_moment(rig):
    r = rig
    r.config["live7_moments_enabled"] = True
    relay = make_relay(r, game_trigger_waiting=lambda: r.gates["queue"])
    append(r.events, ev="host_wait", at=r.clock[0])
    relay.tick()
    low = r.voice[-1]
    r.gates["queue"] = True
    moment = review_moment(r)
    relay.tick()
    assert moment.exists() and relay._moments.dropped == 0
    low.mark_voiced()
    r.gates["queue"] = False
    r.config["live7_moments_min_gap_sec"] = 90
    relay.tick()
    assert not moment.exists() and r.voice[-1] == "[Stardew] Baru menanam."


@pytest.mark.parametrize("result", ["silent", "rejected", "raises"])
def test_review_4_attempt_cap_and_progressive_backoff(rig, result):
    r = rig
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    calls, delays = [], []

    def enqueue(text, **_):
        calls.append(r.clock[0] - 10000)
        if result == "raises":
            raise RuntimeError("queue failed")
        return result != "rejected"

    for elapsed in range(301):
        r.clock[0] = 10000 + elapsed
        r.facts.tick(r.cycle, enqueue, now=r.clock[0])
        if elapsed in (0, 10, 40):
            delays.append(r.facts._pending["stop"].next_try - r.clock[0])
    assert calls == [0, 10, 40] and delays == [10, 30, 60]
    assert "darurat" in r.facts.status_block()


@pytest.mark.parametrize("age", [0, 299, 301, None])
def test_review_5_diagnostic_must_be_recent_and_is_consumed_once(rig, age):
    r = rig
    trial = "day-0003-attempt-000"
    journal = r.cycle / trial / "journal" / "g1_journal.jsonl"
    fields = {} if age is None else {"at": r.clock[0] - age}
    append(journal, ev="day_start_blocked", reason="old_menu_problem", **fields)
    append(r.events, ev="day_trial_started", day_trial=trial, at=r.clock[0])
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    r.tick()
    assert ("old_menu_problem" in r.voice[-1]) is (age is not None and age <= 300)
    append(r.events, ev="stop_cleared", at=r.clock[0] + 1)
    append(r.events, ev="SAFETY_STOP", at=r.clock[0] + 1)
    r.clock[0] += 1
    r.tick()
    assert "old_menu_problem" not in r.voice[-1]


@pytest.mark.parametrize("ev", ["day_trial_started", "day_ended", "night_saved"])
def test_review_6_day_boundaries_clear_host_wait_silently(rig, ev):
    r = rig
    append(r.events, ev="host_wait", at=r.clock[0])
    r.tick()
    r.voice[-1].mark_voiced()
    append(r.events, ev=ev, day_trial="day-0004-attempt-000", at=r.clock[0] + 1)
    r.clock[0] += 1
    r.tick()
    assert not r.facts.status_block() and len(r.voice) == 1


def test_review_7_restart_keeps_inactive_cycle_discovery_out_of_scope(rig):
    # Optional restart bootstrap is intentionally deferred: characterize the
    # retained active-only resolver, without promoting an inactive run to live.
    r = rig
    r.config["live7_help_dir"] = str(r.cycle.parent.parent)
    (r.cycle / "cycle_state.json").write_text(json.dumps({"status": "safety_stop"}), encoding="utf-8")
    append(r.events, ev="SAFETY_STOP", at=r.clock[0])
    resolver = Live7HelpChannelResolver(r.config, clock=lambda: r.clock[0], announce=lambda _: None)
    relay = HelpRelay(r.config, r.enqueue, clock=lambda: r.clock[0], channel_resolver=resolver)
    relay.tick()
    assert resolver.cycle_dir is None and not relay.status_block() and not r.voice


@pytest.mark.parametrize("help_enabled", [False, True])
def test_review_8_status_only_relay_leaves_disabled_moment_files_untouched(rig, help_enabled):
    r = rig
    r.config.update(live7_help_enabled=help_enabled, live7_moments_enabled=False)
    moment = review_moment(r)
    relay = make_relay(r)
    append(r.events, ev="crash", at=r.clock[0])
    relay.tick()
    assert moment.exists() and relay._moments.dropped == 0
    assert len(r.voice) == 1 and isinstance(r.voice[0], StatusText)

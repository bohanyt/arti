"""Live7 moments use temporary channels and injected clocks/queues only."""

from arti_runtime import ipc
import asyncio
import json
import os
from pathlib import Path
import queue
from types import SimpleNamespace

import pytest

from arti_help_bus import FileHelpChannel, HelpRequest
from arti_help_relay import HelpRelay, Live7HelpChannelResolver


@pytest.fixture
def rig(tmp_path):
    clock, voice = [10000.0], []
    cfg = {"live7_moments_enabled": True}
    waiting = {"viewer": False, "game": False}
    relay = HelpRelay(
        cfg, lambda text, **kw: voice.append((text, kw)) or True,
        file_channel=FileHelpChannel(tmp_path), clock=lambda: clock[0],
        viewer_waiting=lambda: waiting["viewer"],
        game_trigger_waiting=lambda: waiting["game"],
    )
    clock[0] += 1
    return relay, clock, voice, waiting


def write_moment(folder, now, seq=1, *, age=1, raw=None, **changes):
    folder.mkdir(parents=True, exist_ok=True)
    data = dict(schema="arti.stardew_moment/1", ts=now, game_time="Spring 2 13:40",
                kind="watered_all", situation="[Stardew] Semua tanaman sudah disiram.", ttl_s=120)
    data.update(changes)
    path = folder / f"ev-{int(now * 1000)}-{seq}.json"
    path.write_text(json.dumps(data) if raw is None else raw, encoding="utf-8")
    os.utime(path, (now - age, now - age))
    return path


def test_valid_moment_voiced_once_and_consumed(rig, tmp_path, caplog, capsys):
    relay, clock, voice, _ = rig
    path = write_moment(tmp_path, clock[0])
    relay.tick()
    relay.tick()
    assert voice == [("[Stardew] Semua tanaman sudah disiram.", {"trigger_type": "game"})]
    assert not path.exists() and relay._moments.voiced == 1
    output = capsys.readouterr().out
    assert output == "[Moments] voiced kind=watered_all\n"
    assert "Semua tanaman" not in output + caplog.text


def test_gap_is_since_last_accepted_moment(rig, tmp_path):
    relay, clock, voice, _ = rig
    for offset in (0, 89, 90):
        clock[0] = 10001 + offset
        write_moment(tmp_path, clock[0])
        relay.tick()
    assert len(voice) == 2 and relay._moments.dropped == 1
    assert not list(tmp_path.glob("ev-*.json"))


def test_bed_bypasses_gap_right_after_another_moment(rig, tmp_path, capsys):
    relay, clock, voice, _ = rig
    write_moment(tmp_path, clock[0])
    relay.tick()
    clock[0] += 1
    write_moment(tmp_path, clock[0], kind="bed", situation="[Stardew] Aku tidur dulu.")
    relay.tick()
    assert [t for t, _ in voice] == ["[Stardew] Semua tanaman sudah disiram.", "[Stardew] Aku tidur dulu."]
    assert getattr(voice[1][0], "_live7_hold", False) is True
    assert relay._moments.dropped == 0 and relay._moments._held is None
    assert capsys.readouterr().out.endswith("[Moments] voiced kind=bed\n")
    # The gap still applies to ordinary moments after the bed.
    clock[0] += 1
    write_moment(tmp_path, clock[0], 2)
    relay.tick()
    assert len(voice) == 2 and relay._moments.dropped == 1


@pytest.mark.parametrize("gate", ["viewer", "game"])
def test_bed_queues_behind_viewer_or_ptt_instead_of_dropping(rig, tmp_path, gate):
    relay, clock, voice, waiting = rig
    waiting[gate] = True
    write_moment(tmp_path, clock[0], kind="bed")
    relay.tick()
    relay.tick()
    assert not voice and relay._moments.dropped == 0
    assert not list(tmp_path.glob("ev-*.json"))
    waiting[gate] = False
    clock[0] += 5
    relay.tick()
    assert len(voice) == 1 and voice[0][1] == {"trigger_type": "game"}
    assert relay._moments.voiced == 1 and relay._moments.dropped == 0


def test_bed_waits_while_help_open_and_is_voiced_once(rig, tmp_path):
    relay, clock, voice, _ = rig
    relay._window = SimpleNamespace(closes_at=float("inf"))  # help window open
    write_moment(tmp_path, clock[0], kind="bed")
    write_moment(tmp_path, clock[0], 2, kind="bed")
    relay.tick()
    assert not voice and relay._moments._held is not None
    relay._window = None
    relay.tick()
    assert [kw for _, kw in voice] == [{"trigger_type": "game"}]
    assert relay._moments.voiced == 1 and relay._moments.dropped == 1  # older bed superseded


def test_held_bed_expires_after_hold_max(rig, tmp_path):
    relay, clock, voice, waiting = rig
    waiting["game"] = True
    write_moment(tmp_path, clock[0], kind="bed")
    relay.tick()
    clock[0] += 301
    relay.tick()
    waiting["game"] = False
    relay.tick()
    assert not voice and relay._moments.dropped == 1 and relay._moments._held is None


@pytest.mark.parametrize("kind", ["planted", "watered_all", "harvested", "rain_day", "shop_closed",
                                  "went_to_town", "bought_seeds", "bed", "stuck", "other"])
def test_all_contract_kinds_are_valid(rig, tmp_path, kind):
    relay, clock, voice, _ = rig
    write_moment(tmp_path, clock[0], kind=kind)
    relay.tick()
    assert len(voice) == 1


@pytest.mark.parametrize("ttl", [10, 120, 600])
def test_expiry_after_bridge_start_at_ttl_boundary(rig, tmp_path, ttl):
    relay, clock, voice, _ = rig
    created = clock[0]
    write_moment(tmp_path, created, ttl_s=ttl)
    clock[0] += ttl
    relay.tick()
    assert len(voice) == 1
    write_moment(tmp_path, clock[0], 2, ts=created, ttl_s=ttl)
    clock[0] += 0.01
    relay.tick()
    assert len(voice) == 1 and relay._moments.dropped == 1


@pytest.mark.parametrize("changes", [dict(ts=9800), dict(ts=9999), dict(ts=9999, ttl_s=600)])
def test_expired_or_before_bridge_start_dropped(rig, tmp_path, changes):
    relay, clock, voice, _ = rig
    write_moment(tmp_path, clock[0], **changes)
    relay.tick()
    assert voice == [] and relay._moments.dropped == 1
    assert not list(tmp_path.glob("ev-*.json"))


def test_newest_valid_wins_and_every_read_file_consumed(rig, tmp_path):
    relay, clock, voice, _ = rig
    clock[0] += 10
    write_moment(tmp_path, clock[0], 1, ts=clock[0] - 2, kind="planted")
    write_moment(tmp_path, clock[0], 2, kind="harvested", situation="[Stardew] Panen selesai.")
    write_moment(tmp_path, clock[0], 3, ts=clock[0] + 1, kind="unknown")
    write_moment(tmp_path, clock[0], 4, raw="{broken")
    relay.tick()
    assert [v[0] for v in voice] == ["[Stardew] Panen selesai."]
    assert not list(tmp_path.glob("ev-*.json")) and relay._moments.dropped == 3


def test_equal_timestamp_uses_numeric_file_sequence(rig, tmp_path):
    relay, clock, voice, _ = rig
    write_moment(tmp_path, clock[0], 10, situation="[Stardew] Terbaru.")
    write_moment(tmp_path, clock[0], 9, situation="[Stardew] Sebelumnya.")
    relay.tick()
    assert voice[0][0] == "[Stardew] Terbaru."


@pytest.mark.parametrize("changes", [
    dict(schema="arti.stardew_moment/2"), dict(kind="unknown"), dict(kind=[]),
    dict(situation="Tanpa prefix"), dict(situation=None), dict(situation="[Stardew]"),
    dict(ts="10001"), dict(ts=True), dict(ts=float("nan")), dict(ts=float("inf")),
    dict(ttl_s=9), dict(ttl_s=601), dict(ttl_s=True), dict(ttl_s="120"),
])
def test_invalid_consumed_and_logged_once_per_reason(rig, tmp_path, caplog, changes):
    relay, clock, voice, _ = rig
    write_moment(tmp_path, clock[0], 1, **changes)
    write_moment(tmp_path, clock[0], 2, **changes)
    relay.tick()
    assert voice == [] and relay._moments.dropped == 2
    assert not list(tmp_path.glob("ev-*.json"))
    assert len([r for r in caplog.records if "[Moments] dropped reason=" in r.message]) == 1
    assert "Semua tanaman" not in caplog.text


@pytest.mark.parametrize("raw", ["[]", "null", "1", "{broken", "x" * 33000],
                         ids=["array", "null", "number", "broken", "oversized"])
def test_invalid_json_or_shape_does_not_break_tick(rig, tmp_path, raw):
    relay, clock, voice, _ = rig
    write_moment(tmp_path, clock[0], raw=raw)
    relay.tick()
    assert not voice and not list(tmp_path.glob("ev-*.json"))


def test_sanitize_and_cap_situation(rig, tmp_path):
    relay, clock, voice, _ = rig
    write_moment(tmp_path, clock[0], situation=(
        "[Stardew] https://example.org www.example.org contoh.id @penonton "
        "[MC: attack] [Stardew] [tag] <tag> air\x00\u200b\n" + "a" * 220))
    relay.tick()
    text = voice[0][0]
    assert text.startswith("[Stardew] air ") and len(text) == 200
    assert text.count("[") == text.count("]") == 1
    assert not any(s in text for s in ("http", "www", "contoh.id", "@", "\x00", "\u200b", "\n", "<tag>"))


def test_missing_ttl_defaults_to_120(rig, tmp_path):
    relay, clock, voice, _ = rig
    path = write_moment(tmp_path, clock[0])
    data = json.loads(path.read_text())
    del data["ttl_s"]
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (clock[0] - 1, clock[0] - 1))
    relay.tick()
    assert len(voice) == 1


def test_default_off_leaves_moment_files_untouched(rig, tmp_path):
    relay, clock, voice, _ = rig
    relay.config.pop("live7_moments_enabled")
    write_moment(tmp_path, clock[0])
    relay.tick()
    assert not voice and relay._moments.dropped == 0
    assert list(tmp_path.glob("ev-*.json"))


def test_bridge_publishes_default_off_and_gap():
    import arti_bridge as bridge

    assert bridge.CONFIG["live7_moments_enabled"] is False
    assert bridge.CONFIG["live7_moments_min_gap_sec"] == 90


@pytest.mark.parametrize("gate", ["viewer", "game"])
def test_waiting_gate_drops_without_retry(rig, tmp_path, gate):
    relay, clock, voice, waiting = rig
    waiting[gate] = True
    write_moment(tmp_path, clock[0])
    relay.tick()
    waiting[gate] = False
    relay.tick()
    assert not voice and relay._moments.dropped == 1


def help_request(now):
    return HelpRequest(request_id="e" * 32, session_id="fake", reason="stuck",
                       game_date="Spring 2", game_time=1340, location="Farm", skill=None,
                       detail="stuck", created_at=now, expires_at=now + 600)


def test_help_file_in_same_tick_has_priority(rig, tmp_path):
    relay, clock, voice, _ = rig
    relay._channel.write_request(help_request(clock[0]))
    write_moment(tmp_path, clock[0])
    relay.tick()
    assert len(voice) == 1 and voice[0][0].startswith("[Stardew] Aku buntu:")
    assert relay._moments.voiced == 0 and relay._moments.dropped == 1


def test_young_files_wait_until_safety_age(rig, tmp_path):
    relay, clock, voice, _ = rig
    path = write_moment(tmp_path, clock[0], age=0)
    relay.tick()
    assert path.exists() and not voice
    clock[0] += 0.21
    relay.tick()
    assert not path.exists() and len(voice) == 1


def test_unlink_failure_renames_done(rig, tmp_path, monkeypatch):
    relay, clock, voice, _ = rig
    path = write_moment(tmp_path, clock[0])
    original = Path.unlink

    def unlink(item, *args, **kwargs):
        if item == path:
            raise PermissionError("private situation must never be logged")
        return original(item, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    relay.tick()
    relay.tick()
    assert not path.exists() and path.with_suffix(".json.done").exists()
    assert len(voice) == 1


def test_unconsumable_file_is_not_voiced(rig, tmp_path, monkeypatch, caplog):
    relay, clock, voice, _ = rig
    path = write_moment(tmp_path, clock[0])
    unlink, replace = Path.unlink, Path.replace

    def fail_unlink(item, *args, **kw):
        if item == path:
            raise PermissionError("private situation")
        return unlink(item, *args, **kw)

    def fail_replace(item, *args, **kw):
        if item == path:
            raise PermissionError("private situation")
        return replace(item, *args, **kw)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(ipc, "replace", fail_replace)
    relay.tick()
    relay.tick()
    assert not voice and path.exists()
    assert caplog.text.count("reason=consume") == 1 and "private situation" not in caplog.text


@pytest.mark.parametrize("raises", [False, True])
def test_enqueue_failure_drops_and_next_tick_recovers(rig, tmp_path, raises, caplog):
    relay, clock, voice, _ = rig
    calls = []

    def reject(*_args, **_kw):
        calls.append(True)
        if raises:
            raise OSError("private situation must never be logged")
        return False

    relay._enqueue = reject
    write_moment(tmp_path, clock[0])
    relay.tick()
    relay.tick()
    assert calls == [True] and relay._moments.dropped == 1
    relay._enqueue = lambda text, **kw: voice.append((text, kw)) or True
    write_moment(tmp_path, clock[0], 2)
    relay.tick()
    assert len(voice) == 1 and "private situation" not in caplog.text


def test_periodic_drop_summary_has_only_counts(rig, tmp_path, caplog, capsys):
    relay, clock, voice, waiting = rig
    waiting["viewer"] = True
    write_moment(tmp_path, clock[0])
    relay.tick()
    assert not caplog.records and not capsys.readouterr().out
    clock[0] += 60
    relay.tick()
    relay.tick()
    assert capsys.readouterr().out == "[Moments] dropped=1 by_reason={'chat_waiting': 1}\n"


@pytest.mark.parametrize("as_root", [False, True])
def test_shared_resolver_follows_active_run_dir(tmp_path, as_root):
    clock, voice = [10000.0], []
    root, run_a, run_b = tmp_path / "runs", tmp_path / "runs" / "a", tmp_path / "runs" / "b"
    cfg = {"live7_help_dir": str(root if as_root else run_a),
           "live7_moments_enabled": True, "live7_moments_min_gap_sec": 0}
    resolver = Live7HelpChannelResolver(cfg, cfg["live7_help_dir"], clock=lambda: clock[0],
                                        announce=lambda _line: None)
    relay = HelpRelay(cfg, lambda text, **kw: voice.append(text) or True,
                      channel_resolver=resolver, resolve_interval_sec=0, clock=lambda: clock[0])
    for run in (run_a, run_b):
        clock[0] += 2
        cycle = run / "live7" / "c1"
        cycle.mkdir(parents=True)
        state = cycle / "cycle_state.json"
        state.write_text(json.dumps({"status": "active"}), encoding="utf-8")
        os.utime(state, (clock[0], clock[0]))
        if not as_root:
            cfg["live7_help_dir"] = str(run)
        write_moment(run, clock[0])
        relay.tick()
        assert relay._channel.help_dir == run
        assert not list(run.glob("ev-*.json"))
    assert len(voice) == 2


@pytest.mark.parametrize("buffered", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_bridge_wires_queue_gates_and_redacts_logs(tmp_path, monkeypatch, capsys, buffered, enabled):
    import arti_bridge as bridge
    import arti_help_relay as module
    from arti_live7_moments import MomentText

    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", False)

    clock, logged = [10000.0], []
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "live7_moments_enabled", enabled)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_dir", str(tmp_path))
    monkeypatch.setitem(bridge.CONFIG, "voice_queue_enabled", buffered)
    monkeypatch.setattr(bridge, "_live7_help_relay", None)
    monkeypatch.setattr(bridge, "_stardew_runtime", None)
    monkeypatch.setattr(bridge, "voice_trigger_queue", queue.Queue())
    monkeypatch.setattr(bridge, "voice_trigger_buffer", bridge.arti_voice_queue.VoiceTriggerQueue())
    monkeypatch.setattr(bridge, "_viewer_chat_waiting", lambda: False)
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: False)
    monkeypatch.setattr(bridge, "_note_stardew_owner_ingress", lambda *_a: None)
    monkeypatch.setattr(bridge, "_brain_busy", False)
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    monkeypatch.setattr(bridge.time, "time", lambda: clock[0])
    monkeypatch.setattr(bridge.session_transcript, "log_trigger", lambda *args: logged.append(args) or "fake")
    monkeypatch.setattr(HelpRelay, "start", lambda _relay: None)
    monkeypatch.setattr(module, "HelpRelay", lambda *a, **kw: HelpRelay(
        *a, **kw, clock=lambda: clock[0]))
    bridge._start_live7_help_if_enabled()
    relay = bridge._live7_help_relay
    if not enabled:
        # Off means off: help and moments both disabled -> no relay at all
        # (test_help_bridge::test_disabled_startup_does_not_construct_relay).
        assert relay is None and not logged
        return
    clock[0] += 1
    try:
        write_moment(tmp_path, clock[0])
        relay._channel.write_request(help_request(clock[0]))
        relay.tick()
        assert relay._window is None  # help stays off in moments-only mode
        assert not list(tmp_path.glob("ev-*.json"))
        assert relay._moments.voiced == 1
        item = (bridge.voice_trigger_buffer._items[0] if buffered
                else bridge.voice_trigger_queue.queue[0])
        trigger = bridge._normalize_voice_trigger(item)
        assert isinstance(trigger.text, MomentText)
        assert not bridge._game_reaction_expired(trigger)
        assert bridge._game_trigger_waiting()
        clock[0] += 90
        write_moment(tmp_path, clock[0], 2)
        relay.tick()
        assert relay._moments.voiced == 1 and relay._moments.dropped == 1
        assert bridge._game_reaction_expired(trigger)
        monkeypatch.setitem(bridge.CONFIG, "live7_moments_enabled", False)
        assert bridge._game_reaction_expired(trigger)
        assert "Semua tanaman" not in capsys.readouterr().out
        assert all("Semua tanaman" not in args[2] for args in logged)
    finally:
        bridge._stop_live7_help_relay()


def test_pipeline_treats_moment_as_situation_without_lookup_or_text_logs(monkeypatch, capsys):
    import arti_voice_pipeline as pipeline
    from arti_live7_moments import MomentText

    async def run(_lane, fn, *args, **kw):
        assert fn is history
        return fn()

    def history():
        return ""

    monkeypatch.setattr(pipeline.arti_executors, "run", run)
    text = MomentText("[Stardew] Semua tanaman sudah disiram.")
    ctx = asyncio.run(pipeline.prepare_turn_context(
        text, [], "system", {}, trim_system_prompt=lambda text, _cfg: text,
        append_watch_party_context=lambda text: text, get_categorized_history=history,
        extract_trigger_message=lambda text: text,
    ))
    assert text in ctx.prompt_content
    assert "Stardew" in ctx.target_instruction and "Reaksi, bukan narasi" in ctx.target_instruction
    assert "karaktermu sendiri" in ctx.target_instruction
    assert "petani HOST mengikuti identitas native" in ctx.target_instruction
    assert "penonton chat berada di luar game" in ctx.target_instruction
    assert "Bohan main karakter host-nya" not in ctx.target_instruction
    assert "kebunku" not in ctx.target_instruction and "kebun kita" not in ctx.target_instruction
    assert "Semua tanaman" not in capsys.readouterr().out

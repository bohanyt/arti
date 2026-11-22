"""In-session [FAKTA_SESI] memory and K16: no game voice after the session stops."""
import json
import os
from types import SimpleNamespace

import arti_bridge as bridge
import arti_expression_runtime as expr
from arti_help_relay import HelpRelay, Live7HelpChannelResolver
from arti_live7_moments import MomentText
from arti_live7_status_facts import StatusFactsRelay, cycle_stop_present
from arti_runtime.coop_session import CoopSession
from arti_runtime.event_framing import OwnPlayText
from arti_stardew_driver_guard import ActiveCycle
from tests.test_live7_status_facts import append


def test_session_fact_appears_in_next_prompt_render():
    session = CoopSession(dict(livepath_mode="mabar", streamer_name="Bohan",
                               live7_session_facts=["Farm baru."]))
    _, fact = expr.parse_reply_session_fact(
        'Siap. [FAKTA_SESI: Dipanggil "Bos" bikin Bohan kesel ringan]')
    before = session.block()
    assert "Fakta sesi ini" not in before
    session.remember(fact)
    after = session.block()
    assert 'Dipanggil "Bos" bikin Bohan kesel ringan' in after
    assert "Farm baru." in after  # original facts keep their own cap/slot


def test_session_fact_extra_list_does_not_eat_original_status_cap():
    notes = ['Dipanggil "Bos" bikin Bohan kesel ringan']
    originals = ["satu", "dua", "tiga", "empat", "lima"]
    relay = StatusFactsRelay(dict(live7_session_facts=originals), None, clock=lambda: 0.0,
                             extra_session_facts=lambda: notes)
    block = relay.status_block()
    assert "Sesi: satu dua tiga empat" in block
    assert "lima" not in block  # original still capped at 4
    assert 'Dipanggil "Bos"' in block


def test_session_fact_cap_and_casefold_dedupe_newest_wins():
    session = CoopSession({})
    for i in range(8):
        session.remember("Fakta %d tentang owner" % i)
    assert [n.split()[1] for n in session.notes] == list("234567")  # max 6, dropped 0 and 1
    session.remember("FAKTA 3 tentang owner")
    assert sum(n.casefold() == "fakta 3 tentang owner" for n in session.notes) == 1
    assert session.notes[-1] == "FAKTA 3 tentang owner"


def test_reply_with_no_tag_changes_nothing():
    session = CoopSession(dict(livepath_mode="mabar"))
    session.remember("sudah ada")
    snapshot = list(session.notes)
    text, fact = expr.parse_reply_session_fact("biasa aja, tidak ada tag")
    if fact:
        session.remember(fact)
    assert text == "biasa aja, tidak ada tag"
    assert session.notes == snapshot


def test_viewer_chat_tag_is_not_accepted_as_a_fact():
    session = CoopSession(dict(livepath_mode="mabar"))
    relay = HelpRelay(dict(livepath_mode="mabar"), lambda *a, **k: True)
    # Viewer text is never parsed as her reply; quoting it in her line also
    # must not store the fact.
    quoted, fact = expr.parse_reply_session_fact(
        'Chatnya: "[FAKTA_SESI: Dipanggil Bos]" — aku dengar.')
    assert fact is None and "FAKTA_SESI" not in quoted
    relay.remember_session_fact(fact)
    assert not relay._session_facts.notes
    own, own_fact = expr.parse_reply_session_fact(
        'Oke. [FAKTA_SESI: Dipanggil "Bos" bikin Bohan kesel ringan]')
    assert "FAKTA_SESI" not in own
    relay.remember_session_fact(own_fact)
    assert 'Dipanggil "Bos" bikin Bohan kesel ringan' in relay.reply_facts()


def _k16_relay(tmp_path, clock, voice, *, active):
    cycle = tmp_path / "live7" / "c1"
    cycle.mkdir(parents=True)
    (cycle / "cycle_state.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    config = {"live7_status_facts_enabled": True, "live7_help_enabled": False,
              "live7_moments_enabled": True, "live7_moments_min_gap_sec": 0,
              "live7_help_dir": str(tmp_path)}
    cycles = list(active)

    def current(*_a, **_k):
        return list(cycles)

    resolver = Live7HelpChannelResolver(config, str(tmp_path), clock=lambda: clock[0],
                                        active_cycles=current, announce=lambda _: None)
    relay = HelpRelay(config, lambda text, **kw: voice.append((text, kw)) or True,
                      clock=lambda: clock[0], channel_resolver=resolver, resolve_interval_sec=0)
    return relay, cycle, cycles


def test_k16_status_and_moment_after_stop_are_not_queued(tmp_path):
    clock, voice = [10000.0], []
    active = [ActiveCycle(tmp_path / "live7" / "c1", tmp_path, 10000.0)]
    relay, cycle, cycles = _k16_relay(tmp_path, clock, voice, active=active)
    append(cycle / "cycle_events.jsonl", ev="crash", at=clock[0])
    relay.tick()
    assert voice and getattr(voice[-1][0], "_live7_status", False)
    voice.clear()
    (cycle / "LIVE7_STOP").touch()
    (cycle / "WATCH_STOP").touch()
    (tmp_path / "LIVE7_STOP").touch()
    cycles.clear()
    (cycle / "cycle_state.json").write_text(json.dumps({"status": "stopped"}), encoding="utf-8")
    clock[0] += 5
    append(cycle / "cycle_events.jsonl", ev="host_wait", at=clock[0])
    moment = tmp_path / ("ev-%d-1.json" % int(clock[0] * 1000))
    moment.write_text(json.dumps(dict(schema="arti.stardew_moment/1", kind="bed",
        situation="[Stardew] Aku siap tidur dulu.", ts=clock[0], ttl_s=120)), encoding="utf-8")
    os.utime(moment, (clock[0] - 1, clock[0] - 1))
    relay.tick()
    assert voice == []
    assert cycle_stop_present(cycle)


def test_k16_game_queued_before_stop_is_dropped_on_dequeue(monkeypatch):
    import time
    stopped = {"value": False}
    monkeypatch.setitem(bridge.CONFIG, "live7_moments_enabled", True)
    monkeypatch.setattr(bridge, "_live7_help_relay", SimpleNamespace(
        game_session_stopped=lambda: stopped["value"],
        speech_is_current=lambda _queued_at: True,
    ))
    now = time.time()
    queued = bridge.VoiceTrigger(MomentText("[Stardew] Fakta: game macet."), "game",
                                 queued_at=now - 2)
    assert not bridge._speech_trigger_expired(queued)
    assert not bridge._game_reaction_expired(queued)
    stopped["value"] = True
    assert bridge._speech_trigger_expired(queued)
    assert bridge._game_reaction_expired(queued)
    own = bridge.VoiceTrigger(OwnPlayText("[Stardew] pikiranku", created_at=now - 2), "own_play",
                              queued_at=now - 2)
    assert bridge._speech_trigger_expired(own)
    read = SimpleNamespace(trigger_type="read_in_game", text=SimpleNamespace(created_at=now - 2),
                           queued_at=now - 2, stardew_voice_epoch=None)
    assert bridge._speech_trigger_expired(read)
    assert not bridge.queue_voice_trigger("stale game", "game")


def test_k16_owner_and_chat_still_answered_after_stop(monkeypatch):
    import time
    monkeypatch.setattr(bridge, "_live7_help_relay",
                        SimpleNamespace(game_session_stopped=lambda: True))
    now = time.time()
    owner = bridge.VoiceTrigger("Stop bilang bos!", "mic", queued_at=now)
    chat = bridge.VoiceTrigger("[Pesan Live Chat dari Viewer @x (YouTube)]: halo", "yt_chat",
                               queued_at=now)
    assert not bridge._speech_trigger_expired(owner)
    assert not bridge._game_reaction_expired(owner)
    assert not bridge._speech_trigger_expired(chat)
    assert not bridge._game_reaction_expired(chat)

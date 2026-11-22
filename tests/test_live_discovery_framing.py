"""ADR 110 live/rig framing parity. Synthetic facts and providers only: UNIT_TESTED."""
import asyncio
import json
import os
import queue
import time
from unittest.mock import AsyncMock, Mock

import pytest

from arti_discovery_inbox import DiscoveryInbox
from arti_live7_moments import MomentsRelay, _decode
from arti_runtime import time_grounding as G
from tests.test_bridge_testmode import isolated  # noqa: F401


def event(**changes):
    return dict(dict(schema="arti.game_event/1", kind="discovery", id="first-sardine",
                     ts=time.time(), ttl_s=120, importance="high", count=1,
                     event_time="1:spring:9 1510", event_place="Beach",
                     situation="item_obtained: (O)131. Discovery Y1 spring 9: Sardine",
                     facts=dict(kind="item_obtained", subject="(O)131")), **changes)


def row(clock=1710, bag=1):
    return dict(observed_ts=time.time(), game="stardew",
                game_time=dict(date="Y1 spring 9", time=clock, place="Farm"),
                native_facts=dict(possession={"(O)131": bag}))


def write_event(folder, data):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "ev-1-1.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (data["ts"] - 1, data["ts"] - 1))
    return path


@pytest.mark.parametrize("kind,subject,situation", [
    ("item_obtained", "(O)131", "item_obtained: (O)131. Discovery Y1 spring 9: Sardine"),
    ("location_entered", "FarmHouse", "location_entered: FarmHouse. Discovery Y1 spring 9: House"),
    ("activity", "panen", "activity: panen. Discovery Y1 spring 9: panen: terverifikasi; hari ini panen: Parsnip 3"),
])
def test_live_and_rig_use_identical_grounded_trigger_and_instruction(tmp_path, monkeypatch, kind, subject, situation):
    import arti_bridge_testmode as T
    import arti_voice_pipeline as VP

    data = event(facts=dict(kind=kind, subject=subject), situation=situation)
    path = write_event(tmp_path, data)
    live = _decode(path.read_bytes())[3]
    inbox = DiscoveryInbox(T.Transcript(tmp_path / "transcript.jsonl"))
    inbox.poll(tmp_path)
    rig = inbox.take(time.time())
    now = row()
    current = G.current_state(now)
    assert live.events == rig.events
    live, rig = (text.grounded(current, now["native_facts"]) for text in (live, rig))
    assert str(live).encode() == str(rig).encode()

    async def run(_lane, fn, *args):
        return fn(*args)

    monkeypatch.setattr(VP.arti_executors, "run", run)
    async def prepare(text):
        return await VP.prepare_turn_context(
            text, [], "system", {}, trim_system_prompt=lambda text, _cfg: text,
            append_watch_party_context=lambda text: text, get_categorized_history=lambda: "",
            extract_trigger_message=lambda text: text)
    a, b = asyncio.run(prepare(live)), asyncio.run(prepare(rig))
    assert a.prompt_content.encode() == b.prompt_content.encode()
    assert "waktunya ada di faktanya, bisa sudah lewat" in a.target_instruction
    assert "boleh [diam]" in a.target_instruction
    frame = json.loads(str(live).split(": ", 1)[1])
    assert frame["game_minutes_ago"] == 120
    assert frame["relation"] == "past_observation" and frame["current_place"] == "Farm"
    if kind == "item_obtained":
        assert frame["fact"] == "Pertama tercatat dapat jenis ini: Sardine; riwayat lama belum diketahui"
        assert frame["in_bag_now"] == 1
    elif kind == "location_entered":
        assert frame["fact"] == "Pertama tercatat masuk: rumah utama HOST (FarmHouse); riwayat lama belum diketahui"
    else:
        assert frame["fact"] == "panen: terverifikasi; hari ini panen: Parsnip 3"


@pytest.mark.parametrize("stale", [False, True])
def test_missing_or_stale_current_does_not_claim_event_age_or_bag(isolated, monkeypatch, stale):
    now = row()
    if stale:
        now["observed_ts"] -= 60
    else:
        now = {}
    calls, reads, ended = live_turn(isolated, monkeypatch, now)
    assert reads == 2 and ended == 1
    frame = json.loads(str(calls[0]["speech"]).split(": ", 1)[1])
    assert frame["current_place"] is None and frame["current_time"] is None
    assert "game_minutes_ago" not in frame and "in_bag_now" not in frame


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("buffered", [False, True])
def test_real_live_handler_frames_at_generation_and_preserves_silence(isolated, monkeypatch, refresh, buffered):
    calls, reads, ended = live_turn(isolated, monkeypatch, row(), refresh=refresh, buffered=buffered)
    assert reads == 2  # fresh_reply's existing reads; grounding adds none
    assert ended == 1  # existing [diam] handling ends the turn without TTS/history
    assert len(calls) == (2 if refresh else 1)
    for n, call in enumerate(calls):
        frame = json.loads(str(call["speech"]).split(": ", 1)[1])
        assert frame["game_minutes_ago"] == (180 if n else 120)
        assert frame["in_bag_now"] == (0 if n else 1)
        assert str(call["speech"]) in call["prompt"]
        assert '"game_minutes_ago": 999' in call["prompt"]  # history is never rewritten
        assert "boleh [diam]" in call["prompt"] and "bisa sudah lewat" in call["prompt"]
        assert "Kabar kejadian lampau" in call["system"]
        assert "Unknown tetap unknown" in call["system"]


def live_turn(isolated, monkeypatch, now, *, refresh=False, buffered=False):
    """Execute _handle_voice_trigger with the real prompt/freshness code, no devices/provider."""
    import arti_bridge as B

    calls, reads = [], []
    ended = AsyncMock()
    monkeypatch.setitem(B.CONFIG, "bridge_test_mode", False)
    monkeypatch.setattr(B, "_prepare_turn_start", AsyncMock())
    monkeypatch.setattr(B, "_pending_turn_id", None)
    monkeypatch.setattr(B, "PipelineTimer", Mock(return_value=Mock(stages_ms=lambda: {})))
    monkeypatch.setattr(B.screen_privacy, "current", lambda _: True)
    for name in ("mood_block_now", "build_today_block", "discord_stories_block_now", "stardew_role_block_now",
                 "mind_intent_block_now", "_live7_status_block_now"):
        monkeypatch.setattr(B, name, lambda: "")
    monkeypatch.setattr(B, "viewer_block_for", lambda _: "")
    monkeypatch.setattr(B, "get_categorized_history", lambda: 'old trigger: "game_minutes_ago": 999')
    monkeypatch.setattr(B, "trim_system_prompt_for_llm", lambda text, _cfg: text)
    monkeypatch.setattr(B, "_append_live_context", lambda text: text)
    monkeypatch.setattr(B, "_try_stardew_owner_generation", lambda _: (None, False))
    monkeypatch.setattr(B, "_should_route_to_cursor", lambda _: True)
    monkeypatch.setattr(B.arti_expression_runtime, "apply_turn_end", ended)
    monkeypatch.setattr(B, "vts", None)
    monkeypatch.setattr(B, "add_to_history", Mock(side_effect=AssertionError("silent turn wrote history")))

    def read(_base):
        reads.append(1)
        if refresh and len(reads) > 1:
            latest = row(1810, 0)
            latest["game_time"]["place"] = "Beach"
            return latest
        return now

    async def reply(system, prompt, speech, **_kw):
        calls.append(dict(system=system, prompt=prompt, speech=speech))
        return "[diam]", [], "codex:fake"

    monkeypatch.setattr(B.arti_mind, "read_now", read)
    monkeypatch.setattr(B, "_cursor_reply_with_fallback", reply)
    text = _decode(json.dumps(event()).encode())[3]
    monkeypatch.setitem(B.CONFIG, "voice_queue_enabled", buffered)
    monkeypatch.setattr(B, "voice_trigger_queue", queue.Queue())
    monkeypatch.setattr(B, "voice_trigger_buffer", B.arti_voice_queue.VoiceTriggerQueue())
    monkeypatch.setattr(B, "_note_stardew_owner_ingress", lambda *_: None)
    monkeypatch.setattr(B, "_brain_busy", False)
    monkeypatch.setattr(B, "tts_is_playing", False)
    monkeypatch.setattr(B.session_transcript, "log_trigger", lambda *args: "synthetic")
    assert B.queue_voice_trigger(text, trigger_type="game")
    queued = B.voice_trigger_buffer.dequeue() if buffered else B.voice_trigger_queue.get_nowait()
    trigger = B._normalize_voice_trigger(queued)
    assert trigger.text is text  # both queues preserve the framing metadata
    asyncio.run(B._handle_voice_trigger(trigger, [], "system"))
    return calls, len(reads), ended.await_count


@pytest.mark.parametrize("generic", [False, True])
def test_live_relay_stays_lossy_and_testmode_leaves_both_schemas_alone(tmp_path, generic):
    data = event()
    if not generic:
        data.update(schema="arti.stardew_moment/1", kind="harvested", situation="[Stardew] panen: terverifikasi")
    path = write_event(tmp_path, data)
    relay = MomentsRelay(dict(live7_moments_enabled=True, bridge_test_mode=True), started_at=data["ts"]-1)
    queued = []
    enqueue = lambda text, **kw: queued.append(text) or True
    relay.tick(tmp_path, enqueue, now=data["ts"]+1)
    assert path.exists() and not queued
    relay.config["bridge_test_mode"] = False
    relay.tick(tmp_path, enqueue, now=data["ts"]+1)
    assert not path.exists() and len(queued) == 1
    assert bool(getattr(queued[0], "_generic_discovery", False)) is generic
    if generic:
        assert queued[0].events[0]["event_time"] == data["event_time"]
    # A second event inside the gap is still consumed and dropped.
    path = write_event(tmp_path, dict(data, ts=data["ts"]+2))
    relay.tick(tmp_path, enqueue, now=data["ts"]+3)
    assert not path.exists() and len(queued) == 1 and relay.dropped == 1

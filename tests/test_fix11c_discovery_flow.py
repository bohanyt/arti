"""Discovery integration: real adapter writer, generic ingress, fake persona only."""
import asyncio
import hashlib
import json
import time

import pytest

import arti_bridge as B
import arti_bridge_testmode as T
from arti_discovery_inbox import DiscoveryInbox, DiscoveryText
from arti_live7_moments import MomentsRelay
from arti_stardew_moments import MomentSource, discovery_importance
from scripts.free_play.evaluate import Evidence, discoveries_surfaced
from tests.test_bridge_testmode import isolated  # noqa: F401 - guarded, private-state-free fixture


def event(identity="high", kind="location_entered", **kw):
    return dict(id=identity, kind=kind, text="First entered an unknown place.", **kw)


def emit(folder, *events):
    source = MomentSource(folder, lambda row: None)
    for e in events:
        source.discovery(e, "fixture day")
    source.flush(enabled=False)


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("kind,tier", [("location_entered", "high"), ("location_unlocked", "high"),
    ("item_obtained", "high"), ("npc_met", "high"), ("rare_event", "high"), ("story_event", "high"),
    ("unknown", "low"), ("activity", "high"), ("future_kind", "high"), ("item_seen", "low"),
    ("connection_unlocked", "low"), ("connection_discovered", "flood")])
def test_adapter_assigns_generic_hint(kind, tier):
    assert discovery_importance(event(kind=kind)) == tier


def test_discovery_flush_independent_of_optional_moments_and_expiry(tmp_path):
    source = MomentSource(tmp_path, lambda row: None)
    source.discovery(event(), "day")
    source.pending[0][0]["ts"] -= 1000
    source.flush(enabled=False)
    data = json.loads(next(tmp_path.glob("ev-*.json")).read_text())
    assert data["importance"] == "high" and data["facts"]["text"] == event()["text"]


def test_write_failure_retries_without_losing_discovery(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("fixture")
    source = MomentSource(blocker, lambda row: None)
    source.discovery(event(), "day")
    source.flush(enabled=False)
    assert len(source.pending) == 1
    source.folder = tmp_path / "inbox"
    source.flush(enabled=False)
    assert len(list(source.folder.glob("ev-*.json"))) == 1 and not source.pending


def test_lossy_moment_reader_does_not_race_generic_consumer(tmp_path):
    emit(tmp_path, event())
    path = next(tmp_path.glob("ev-*.json"))
    now = time.time() + 1
    legacy = MomentsRelay(dict(bridge_test_mode=True, live7_moments_enabled=True), started_at=now-10)
    voice = []
    legacy.tick(tmp_path, lambda *a, **kw: voice.append(a) or True, now=now)
    assert path.exists() and not voice
    # Normal-mode semantics of the old relay remain unchanged.
    legacy.config["bridge_test_mode"] = False
    legacy.tick(tmp_path, lambda *a, **kw: voice.append(a) or True, now=now)
    assert len(voice) == 1 and not path.exists()


def test_no_interruption_flood_dedupe_bounded_batch_and_restart(tmp_path):
    transcript = T.Transcript(tmp_path / "bridge.jsonl")
    inbox = DiscoveryInbox(transcript, min_gap_sec=60)
    emit(tmp_path, *(event(str(n)) for n in range(12)), event("low", "item_seen"),
         event("flood", "connection_discovered", count=246))
    inbox.poll(tmp_path)
    assert inbox.take(0, busy=True) is None and len(inbox.pending) == 12
    text = inbox.take(1)
    assert len(text.events) == 8 and len(inbox.pending) == 4
    assert inbox.take(20) is None
    transcript.write("discovery_delivery", event_ids=text.shown_ids, status="reacted")
    restored = DiscoveryInbox(transcript)
    assert len(restored.pending) == 4 and restored.counts == dict(high=12, low=1, flood=246)
    emit(tmp_path, event("0"))
    restored.poll(tmp_path)
    assert len(restored.pending) == 4
    assert len([r for r in rows(transcript.path) if r["ev"] == "discovery_context"]) == 14
    assert len(list(tmp_path.glob("*.persona-done"))) == 15
    assert len(json.loads(inbox.context())["recent"]) == 8


def test_full_flow_high_reacts_low_and_flood_are_only_context(isolated):
    cfg = isolated["cfg"]
    cfg.update(bridge_test_probe_on_start=False, bridge_test_duration_sec=1.5)
    folder = cfg["live7_help_dir"]
    emit(folder, event(), event("low", "item_seen"), event("flood", "connection_discovered", count=246))
    assert T.main(B) == T.EXIT_OK
    transcript = rows(isolated["dir"] / T.TRANSCRIPT_NAME)
    turns = [r for r in transcript if r["ev"] == "turn"]
    assert len(turns) == len(isolated["calls"]) == 1
    assert turns[0]["shown_discovery_ids"] == ["high"]
    assert turns[0]["discoveries"][0]["facts"]["text"] == event()["text"]
    assert "low" in turns[0]["context"]["discoveries"]
    assert "flood" in turns[0]["context"]["discoveries"]
    prompt = isolated["calls"][0]["prompt"]
    assert "Nilai sendiri" in prompt and "jangan mendeskripsikan" in prompt
    assert "Ini situasi barusan di Stardew" not in prompt
    assert transcript[-1]["unanswered"] == 0


@pytest.mark.parametrize("reply,silent,pending", [("[diam]", True, False), (None, False, True)])
def test_persona_can_decline_and_failed_reply_is_retained(isolated, monkeypatch, reply, silent, pending):
    async def fake(*a, **kw):
        return reply, [], "codex"
    monkeypatch.setattr(B, "_cursor_reply_with_fallback", fake)
    mode = T.TestModeBridge(B, isolated["cfg"], T.Transcript(isolated["dir"] / T.TRANSCRIPT_NAME))
    emit(isolated["cfg"]["live7_help_dir"], event())
    mode.discoveries.poll(isolated["cfg"]["live7_help_dir"])
    text = mode.discoveries.take(0)
    asyncio.run(mode.handle(T.Turn(text, "game", "discovery", time.time())))
    assert bool(mode.discoveries.pending) is pending
    turn = next(r for r in rows(mode.transcript.path) if r["ev"] == "turn")
    assert turn["silent"] is silent
    assert bool(DiscoveryInbox(mode.transcript).pending) is pending


def test_silent_context_survives_prompt_trimming_on_an_owner_turn(isolated):
    mode = T.TestModeBridge(B, isolated["cfg"], T.Transcript(isolated["dir"] / T.TRANSCRIPT_NAME))
    isolated["cfg"]["llm_system_prompt_max_chars"] = 10
    emit(isolated["cfg"]["live7_help_dir"], event("low", "item_seen"))
    mode.discoveries.poll(isolated["cfg"]["live7_help_dir"])
    assert mode.discoveries.take(0) is None
    asyncio.run(mode.handle(T.Turn("mau ngapain?", "mic", "owner_probe", time.time())))
    assert "low" in isolated["calls"][0]["system"]


def test_evaluator_requires_exact_linked_review_not_just_any_reply(tmp_path):
    path = tmp_path / "bridge/bridge-transcript.jsonl"
    transcript = T.Transcript(path)
    inbox = DiscoveryInbox(transcript)
    emit(tmp_path, event(), event("low", "item_seen"), event("flood", "connection_discovered", count=246))
    inbox.poll(tmp_path)
    text = inbox.take(0)
    transcript.write("turn", n=1, source="discovery", discoveries=text.events,
                     shown_discovery_ids=text.shown_ids, reply="Ooh, penasaran!", provider_source="codex")
    data = rows(path)
    result = discoveries_surfaced(Evidence(tmp_path), data)
    assert result["by_tier"]["high"]["reaction_turns"] == 1
    assert result["by_tier"]["flood"]["context"] == 246
    assert result["grounded_reactions"] == 0 and result["unreviewed_reactions"] == 1
    rating = dict(turn_n=1, event_ids=["high"], grounded=True, basis="fixture explicit review", reply_sha256="wrong")
    ratings = tmp_path / "bridge/discovery-reaction-ratings.json"
    ratings.write_text(json.dumps([rating]))
    assert discoveries_surfaced(Evidence(tmp_path), data)["grounded_reactions"] == 0
    rating["reply_sha256"] = hashlib.sha256("Ooh, penasaran!".encode()).hexdigest()
    ratings.write_text(json.dumps([rating, rating]))
    assert discoveries_surfaced(Evidence(tmp_path), data)["grounded_reactions"] == 1
    data[-1]["shown_discovery_ids"] = ["not-the-event"]
    assert discoveries_surfaced(Evidence(tmp_path), data)["grounded_reactions"] == 0


def test_historical_transcript_has_zero_surfacing(tmp_path):
    r = discoveries_surfaced(Evidence(tmp_path), [dict(ev="turn", reply="some old game reply")],
        [(None, [(1, dict(ev="discovery", id="unseen"))])])
    assert r["journal_discoveries"] == 1 and r["surfaced_journal_ids"] == 0
    assert r["reaction_turns"] == r["grounded_reactions"] == 0

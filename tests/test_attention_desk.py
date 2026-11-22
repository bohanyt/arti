"""ADR 172: synthetic candidates and fake selectors only."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from arti_runtime.attention_desk import AttentionDesk, Candidate, score, native_salience


def item(kind, text="synthetic question?", at=100, **fields):
    return NS(text=text, trigger_type=kind, enqueued_at=at, turn_id=str(id(fields)),
              viewer_name=None, **fields)


def test_owner_weights_age_and_address():
    own = Candidate(item("own_play"), "own_play", "own_play", 0)
    owner = Candidate(item("mic_pasif"), "mic_pasif", "owner", 0)
    chat = Candidate(item("yt_chat"), "yt_chat", "chat", 0)
    assert score(own) > score(owner) > score(chat)
    owner.name_addressed = owner.question = True
    assert score(owner) > score(own)
    owner.age = 90
    assert score(owner) < score(chat)


def test_native_only_salience():
    assert native_salience({"kind": "own_intent", "text": "fight low HP"}) == 0.2
    assert native_salience({"kind": "activity", "subject": "lawan"}) >= 0.8
    assert native_salience({"kind": "npc_met"}) > native_salience({"kind": "walking"})


def test_selector_situation_stays_short_during_a_text_burst():
    from arti_runtime.attention_desk import bounded_lines
    lines = bounded_lines([dict(speaker="synthetic", text="x"*2048) for _ in range(100)])
    assert sum(len(row["text"]) for row in lines) <= 2048


def test_timeout_fallback_and_no_human_removal():
    async def slow(_):
        await asyncio.sleep(10)
    desk = AttentionDesk()
    pool = [item("yt_chat"), item("mic_pasif")]
    result = asyncio.run(desk.tick(pool, now=100, choose=slow, timeout=0.01))
    assert result.choice.raw is pool[1] and result.fallback
    assert len(pool) == 2


def test_focus_then_deferred_answer_only_after_actual_speech():
    desk = AttentionDesk(deferred_limit=2)
    people = [item("mic_pasif"), item("yt_chat")]
    result = asyncio.run(desk.tick(people, now=100, busy=0.95, situation={"fight": True}))
    assert result.choice.kind == "focus"
    assert '"fight": true' in result.choice.facts
    assert not desk.deferred
    desk.voiced(result.choice, now=101)
    assert len(desk.deferred) == 2
    again = asyncio.run(desk.tick(people, now=103, busy=0.95))
    assert all(c.kind != "focus" for c in again.candidates)
    later = asyncio.run(desk.tick(people + [item("own_play", at=110)], now=110, busy=0))
    assert later.choice.deferred and "previously" in later.choice.facts
    desk.voiced(later.choice, now=111)
    assert len(desk.deferred) == 1


def test_cutin_threshold_rate_and_only_overlap_candidate():
    desk = AttentionDesk()
    for n in range(26):
        desk.audio(True, 1, now=100+n)
    waiting = [item("mic_pasif")]
    result = asyncio.run(desk.tick(waiting, now=126))
    assert result.choice.kind == "cut_in"
    desk.voiced(result.choice, now=126)
    assert asyncio.run(desk.tick([], now=129)).choice is None
    for n in range(30):
        desk.audio(True, 1, now=127+n)
    assert asyncio.run(desk.tick([], now=157)).choice is None
    assert asyncio.run(desk.tick(waiting, now=157)).choice is None
    result = asyncio.run(desk.tick([item("yt_chat", at=158)], now=158))
    assert result.choice.kind == "cut_in" and "2" in result.choice.facts
    desk.audio(False, 0, now=165)
    assert asyncio.run(desk.tick([item("yt_chat", at=165)], now=165)).choice.kind == "yt_chat"


@pytest.mark.parametrize("speaking,held", [(True, False), (False, True), (True, True)])
def test_her_speech_or_owner_hold_never_selects(speaking, held):
    async def forbidden(_):
        raise AssertionError("selector called during speech/hold")
    result = asyncio.run(AttentionDesk().tick([item("mic_pasif")], now=100,
                                            speaking=speaking, held=held, choose=forbidden))
    assert result.choice is None

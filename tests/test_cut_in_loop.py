"""Empty cut-ins must not loop, and must not fall through to Groq."""
import asyncio
from types import SimpleNamespace as NS

from arti_runtime.attention_desk import AttentionDesk


def item(kind, text="synthetic waiting chat", at=100, **fields):
    return NS(text=text, trigger_type=kind, enqueued_at=at, turn_id=str(id(fields) or at),
              viewer_name=None, **fields)


def owner_talks(desk, start, seconds):
    for n in range(seconds):
        desk.audio(True, 1, now=start + n)


def test_owner_talking_sixty_seconds_with_no_candidate_never_cuts_in():
    # The loop case: owner keeps talking, tick every 2 s, no waiting candidate.
    desk = AttentionDesk()
    start = 20 * 3600
    cuts = 0
    for elapsed in range(0, 61, 2):
        desk.audio(True, 1, now=start + elapsed)
        desk.audio(True, 1, now=start + elapsed + 1)
        now = start + elapsed + 1
        ranked = desk.ranked([], now=now, config={}, busy=0, situation={})
        result = asyncio.run(desk.tick([], now=now))
        cuts += sum(1 for c in ranked if c.kind == "cut_in")
        cuts += int(result.choice is not None and result.choice.kind == "cut_in")
    assert cuts == 0


def test_selected_cut_in_never_voiced_does_not_refire_before_gap():
    desk = AttentionDesk()
    owner_talks(desk, 100, 26)
    pool = [item("yt_chat", at=110)]
    first = asyncio.run(desk.tick(pool, now=126))
    assert first.choice.kind == "cut_in"
    assert desk.last_cut_in == 126
    owner_talks(desk, 127, 5)
    second = asyncio.run(desk.tick(pool, now=132))
    assert second.choice is None
    assert desk.last_cut_in == 126


def test_new_waiting_candidate_yields_one_cut_in_until_gap_and_newer():
    desk = AttentionDesk()
    owner_talks(desk, 100, 26)
    pool = [item("yt_chat", text="first waiting", at=110)]
    first = asyncio.run(desk.tick(pool, now=126))
    assert first.choice.kind == "cut_in"
    owner_talks(desk, 127, 16)
    assert asyncio.run(desk.tick(pool, now=142)).choice is None
    newer = [item("yt_chat", text="newer waiting", at=143)]
    again = asyncio.run(desk.tick(newer, now=143))
    assert again.choice.kind == "cut_in"
    owner_talks(desk, 144, 5)
    assert asyncio.run(desk.tick(newer, now=148)).choice is None


def test_cut_in_without_cursor_route_never_calls_groq(monkeypatch):
    import arti_bridge as b

    def boom(*_a, **_k):
        raise AssertionError("groq_chat_completion must not run for attention system turns")

    monkeypatch.setattr(b, "groq_chat_completion", boom)
    monkeypatch.setattr(b, "incharacter_fallback_reply", boom)
    assert b._attention_skip_without_primary_lane("cut_in", False) is True
    assert b._attention_skip_without_primary_lane("focus", False) is True
    assert b._attention_skip_without_primary_lane("scene_ended", False) is True
    assert b._attention_skip_without_primary_lane("cut_in", True) is False
    assert b._attention_skip_without_primary_lane("yt_chat", False) is False
    if not b._attention_skip_without_primary_lane("cut_in", False):
        b.groq_chat_completion("m", "s", "u", {})


def test_cut_in_cursor_lane_does_not_fall_back_to_groq(monkeypatch):
    import arti_bridge as b
    import arti_cursor_agent as aca

    def boom(*_a, **_k):
        raise AssertionError("groq or canned fallback must not run for cut_in")

    monkeypatch.setattr(b.arti_codex_agent, "prewarm", lambda cfg: True)
    monkeypatch.setattr(b.arti_codex_agent, "send_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(aca, "prewarm", lambda cfg: True)
    monkeypatch.setattr(aca, "is_warm", lambda: True)

    class Fail:
        ok = False
        text = ""
        reason = "synthetic"
        model = ""
        latency_ms = 0
        sentences = []

    monkeypatch.setattr(aca, "send_turn", lambda *_a, **_k: Fail())
    monkeypatch.setattr(b, "pick_groq_model", boom)
    monkeypatch.setattr(b, "groq_chat_completion", boom)
    monkeypatch.setattr(b, "incharacter_fallback_reply", boom)
    cfg = {**b.CONFIG, "codex_primary_voice": True, "codex_agent_enabled": True,
           "_codex_primary_tes": True, "agy_agent_enabled": False, "agy_primary_voice": False,
           "cursor_last_resort_incharacter": True}
    reply, chunks, source = asyncio.run(
        b._cursor_reply_with_fallback("sys", "prompt", "user", cfg, "cut_in"))
    assert reply is None and chunks == [] and source == "skip-attention"

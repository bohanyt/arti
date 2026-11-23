"""Synthetic replay contracts for main's arti_bridge quiet-chat/media paths."""
import asyncio

import pytest

from arti_runtime import chat_media
from scripts.livepath import replay_policy
from scripts.livepath import replay_timeline as R


def session(tmp_path, events, end=180):
    return R.Session(tmp_path / '2026-11-15_synthetic_bridge.log', 1000, 1000+end, events, [], {})


def owner(at=1000, turn_id='owner'):
    return R.Event(at, 'owner', 'Arti, jawab dong?', 'mic_pasif',
                   queued_at=at, turn_id=turn_id, named=True)


def chat(at, line, text='ordinary words', viewer='viewer'):
    return R.Event(at, 'chat', text, 'yt_chat', viewer=viewer, line=line)


def run(s, config=None, *, model=None, chat_slot=False):
    worker = R.Replay(model or R.DurationModel.fixed(4, 0, 1), (s.start, s.end), chat_slot=chat_slot)
    asyncio.run(worker.session(s, config or R.build_config('plain', s), []))
    return worker


def result(worker):
    return R.metrics(worker.owners, worker.audio, worker.counts, worker.active, worker.chats)


def test_plain_uses_current_mabar_quiet_chat_and_media_config(tmp_path):
    cfg = R.build_config('plain', session(tmp_path, []))
    assert cfg['chat_quiet_answer_all'] is True
    assert cfg['media_watch_mode'] is True
    assert cfg['media_reaction_rank'] == .25
    assert cfg['media_summary_wait_sec'] == 2
    assert cfg['codex_thread_max_turns'] == 6


def test_raw_quiet_chat_uses_bridge_admission_and_acknowledges_overflow(tmp_path, monkeypatch):
    s = session(tmp_path, [owner()] + [chat(1001+i/10, i+1) for i in range(10)])
    cfg = {**R.build_config('plain', s), 'chat_busy_messages': 100}
    bind = replay_policy.bind
    calls = []
    def recording_bind(*args):
        b = bind(*args)
        original = b._admit_quiet_viewer_chat
        def admit(*a, **kw):
            calls.append(b.time.time())
            return original(*a, **kw)
        b._admit_quiet_viewer_chat = admit
        return b
    monkeypatch.setattr(replay_policy, 'bind', recording_bind)
    worker = run(s, cfg)
    assert len(calls) == 10
    assert result(worker)['chat_messages'] == dict(incoming=10, answered=1, share_answered=.1)
    assert result(worker)['chat_replies_total'] == 2
    [ack] = [e for _, _, kind, e in worker.audio if kind == 'yt_chat' and e.data.get('chat_acknowledgement')]
    assert len(ack.data['chat_ids']) == 9
    assert len(worker.b._quiet_chat._messages) == 10


def test_busy_chat_acknowledgement_is_not_counted_as_an_answer(tmp_path):
    s = session(tmp_path, [owner()] + [chat(1001+i/10, i+1) for i in range(10)])
    worker = run(s)
    assert result(worker)['chat_messages'] == dict(incoming=10, answered=1, share_answered=.1)
    [ack] = [e for _, _, kind, e in worker.audio if kind == 'yt_chat' and e.data.get('chat_acknowledgement')]
    assert len(ack.data['chat_ids']) == 9


def test_quiet_admission_bypasses_cooldown_and_throttle_without_w3(tmp_path):
    s = session(tmp_path, [chat(1001, 1), chat(1035, 2)])
    cfg = {**R.build_config('plain', s), 'mic_priority_mark': False,
        'yt_viewer_cooldown_sec': 900, 'yt_chat_santai_gap_sec': 900}
    worker = run(s, cfg)
    assert result(worker)['chat_messages']['answered'] == 2
    assert result(worker)['chat_replies_total'] == 2
    assert worker.b._last_yt_trigger_by_viewer['viewer'] == 1035


@pytest.mark.parametrize('chat_slot', [False, True])
def test_superseded_quiet_chat_is_requeued_and_still_answered(tmp_path, chat_slot):
    s = session(tmp_path, [chat(1000, 1), owner(1001)])
    worker = run(s, chat_slot=chat_slot)
    assert result(worker)['chat_messages'] == dict(incoming=1, answered=1, share_answered=1)
    assert worker.counts['cancelled']['yt_chat'] == 1
    event = next(e for a, b, kind, e in worker.audio if kind == 'yt_chat')
    assert event.data['chat_ids'] == [f'{s.path.name}:1']
    assert worker.b.QUIET_CHAT_MAX_REQUEUES == 3


def test_merging_into_requeued_chat_retains_original_message_identities(tmp_path):
    worker = run(session(tmp_path, [chat(1000, 1), owner(1001), chat(1005, 2)]))
    assert result(worker)['chat_messages'] == dict(incoming=2, answered=2, share_answered=1)
    assert result(worker)['chat_replies_total'] == 1


def test_quiet_chat_requeue_limit_bounds_generation_after_repeated_owner_speech(tmp_path):
    worker = run(session(tmp_path, [chat(1000, 1)] + [owner(at, f'o-{at}') for at in (1001, 1011, 1021, 1031)]))
    assert worker.counts['cancelled']['yt_chat'] == 4
    assert sum(g.kind == 'yt_chat' for g in worker.generations) == 4
    assert result(worker)['chat_messages']['answered'] == 0


def test_human_ttl_ages_only_outside_media_holds(tmp_path):
    s = session(tmp_path, [owner(), chat(1001, 1),
        R.Event(1002, 'media_hold', data={'seconds': 100})])
    cfg = {**R.build_config('plain', s), 'chat_quiet_answer_all': False,
           'yt_chat_queue_ttl_sec': 8, 'live_reply_queue_max_age_sec': 8,
           'live_reply_chat_keep': False}
    worker = run(s, cfg)
    assert worker.holds.age(1001, 1102) == 1
    assert result(worker)['chat_messages']['answered'] == 1
    assert not worker.counts['expired']


def test_media_reaction_rank_comes_from_bridge_queue_factory(tmp_path):
    s = session(tmp_path, [owner(), chat(1001, 1),
        R.Event(1001, 'enqueue', '[Live7 moment]', 'game', queued_at=1001, turn_id='game'),
        R.Event(1001, 'enqueue', 'video', 'video', queued_at=1001, turn_id='video')])
    cfg = {**R.build_config('plain', s), 'voice_queue_priority_overrides': {'video': 99, 'game': .5}}
    worker = run(s, cfg)
    assert worker.q._priority['video'] == .25
    assert [kind for a, b, kind, e in worker.audio] == ['mic_pasif', 'video', 'game', 'yt_chat']


def test_hold_state_is_isolated_and_restored_between_sessions(tmp_path):
    original = chat_media.holds
    previous = original.age(1000, 1180)
    first = run(session(tmp_path, [R.Event(1000, 'media_hold', data={'seconds': 100})]))
    second = run(session(tmp_path, []))
    assert first.holds.age(1000, 1180) == 80
    assert second.holds.age(1000, 1180) == 180
    assert chat_media.holds is original
    assert original.age(1000, 1180) == previous


@pytest.mark.parametrize('attribute,name', [('FUNCTIONS', '_missing_replay_seam'), ('CONSTANTS', 'MISSING_REPLAY_CONSTANT')])
def test_missing_policy_seams_still_fail_loudly(monkeypatch, attribute, name):
    replay_policy.compiled_policy.cache_clear()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(replay_policy, attribute, getattr(replay_policy, attribute) + (name,))
            with pytest.raises(KeyError, match=name):
                replay_policy.compiled_policy()
    finally:
        replay_policy.compiled_policy.cache_clear()

"""Tests for arti_voice_queue.VoiceTriggerQueue."""

from __future__ import annotations

import time

import arti_voice_queue as vq


def test_yt_fifo_max_depth_drops_oldest():
    q = vq.VoiceTriggerQueue(max_yt=2, ttl_sec=60.0)
    q.enqueue(vq.QueuedVoiceTrigger("a", trigger_type="yt_chat", viewer_name="A"))
    q.enqueue(vq.QueuedVoiceTrigger("b", trigger_type="yt_chat", viewer_name="B"))
    q.enqueue(vq.QueuedVoiceTrigger("c", trigger_type="yt_chat", viewer_name="C"))
    assert q.depth_for("yt_chat") == 2
    first = q.dequeue()
    assert first is not None
    assert first.viewer_name == "B"


def test_priority_owner_mic_before_yt():
    q = vq.VoiceTriggerQueue(max_yt=2, ttl_sec=60.0)
    q.enqueue(vq.QueuedVoiceTrigger("mic", trigger_type="mic"))
    time.sleep(0.01)
    q.enqueue(vq.QueuedVoiceTrigger("yt", trigger_type="yt_chat", viewer_name="V"))
    item = q.dequeue()
    assert item is not None
    assert item.trigger_type == "mic"


def test_overdue_chat_orders_viewers_behind_owner_and_donation():
    q = vq.VoiceTriggerQueue(max_yt=3, play_active=lambda: True)
    now = time.time()
    arrivals = [
        vq.QueuedVoiceTrigger("newer", "yt_chat", "A", now - 75),
        vq.QueuedVoiceTrigger("oldest", "yt_chat", "B", now - 90),
        vq.QueuedVoiceTrigger("game", "game", enqueued_at=now - 100),
        vq.QueuedVoiceTrigger("paid", "donation"),
        vq.QueuedVoiceTrigger("owner", "mic"),
    ]
    for item in arrivals:
        assert q.enqueue(item)
    assert [q.dequeue().text for _ in arrivals] == ["owner", "paid", "oldest", "newer", "game"]


def test_per_viewer_dedup_replaces_old():
    q = vq.VoiceTriggerQueue(max_yt=2, ttl_sec=60.0)
    q.enqueue(vq.QueuedVoiceTrigger("old", trigger_type="yt_chat", viewer_name="Alice"))
    q.enqueue(vq.QueuedVoiceTrigger("new", trigger_type="yt_chat", viewer_name="Alice"))
    assert q.depth_for("yt_chat") == 1
    item = q.dequeue()
    assert item is not None
    assert item.text == "new"


def test_curious_deferred_when_yt_pending():
    q = vq.VoiceTriggerQueue(max_yt=2, ttl_sec=60.0)
    q.enqueue(vq.QueuedVoiceTrigger("yt", trigger_type="yt_chat", viewer_name="V"))
    ok = q.enqueue(vq.QueuedVoiceTrigger("curious", trigger_type="curious"))
    assert not ok
    assert q.depth_for("curious") == 0


def test_ttl_expires_stale_items():
    q = vq.VoiceTriggerQueue(max_yt=2, ttl_sec=0.05)
    q.enqueue(vq.QueuedVoiceTrigger("stale", trigger_type="mic"))
    time.sleep(0.08)
    assert q.dequeue() is None


def test_game_tidak_dipurge_ttl_generik_buffer():
    """TTL reaksi game ditentukan bridge (biasa/penting/kematian berbeda)."""
    q = vq.VoiceTriggerQueue(max_yt=2, ttl_sec=0.01)
    q.enqueue(vq.QueuedVoiceTrigger(
        "game lama", trigger_type="game", enqueued_at=time.time() - 10
    ))

    item = q.dequeue()

    assert item is not None
    assert item.text == "game lama"


def test_item_buffer_membawa_turn_id_sendiri():
    item = vq.QueuedVoiceTrigger(
        "halo", trigger_type="yt_chat", viewer_name="V", turn_id="turn-V"
    )

    assert item.turn_id == "turn-V"

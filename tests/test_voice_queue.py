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


def test_priority_yt_before_mic():
    q = vq.VoiceTriggerQueue(max_yt=2, ttl_sec=60.0)
    q.enqueue(vq.QueuedVoiceTrigger("mic", trigger_type="mic"))
    time.sleep(0.01)
    q.enqueue(vq.QueuedVoiceTrigger("yt", trigger_type="yt_chat", viewer_name="V"))
    item = q.dequeue()
    assert item is not None
    assert item.trigger_type == "yt_chat"


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

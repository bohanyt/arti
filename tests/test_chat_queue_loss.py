"""UNIT_TESTED: a queued viewer message never leaves without a trace, and owner talk alone does not remove it.

Queued chat messages could never get a turn, with no log line: each one was followed by owner speech first, and
`_note_owner_speech` cleared the queue on every plain owner utterance.
"""
import time

import arti_bridge as bridge
from arti_voice_queue import QueuedVoiceTrigger, VoiceTriggerQueue


def drain():
    while not bridge.voice_trigger_queue.empty():
        bridge.voice_trigger_queue.get_nowait()
    bridge.voice_trigger_buffer.clear()


def chat(text="halo", viewer="@viewer"):
    return QueuedVoiceTrigger(text, trigger_type="yt_chat", viewer_name=viewer)


def test_clear_logs_counts_by_type_and_never_the_viewer(capsys):
    drain()
    try:
        bridge.voice_trigger_buffer.enqueue(chat("rahasia viewer", "@namaviewer"))
        bridge.voice_trigger_buffer.enqueue(QueuedVoiceTrigger("inisiatif", trigger_type="curious"))
        bridge.voice_trigger_queue.put(bridge.VoiceTrigger("legacy", "mic", None, 0.0))
        capsys.readouterr()
        bridge.clear_trigger_queue()
        out = capsys.readouterr().out
        assert "[Queue] Dibuang:" in out and "mic=1" in out
        assert "namaviewer" not in out and "rahasia viewer" not in out
    finally:
        drain()


def test_clearing_nothing_stays_quiet(capsys):
    drain()
    capsys.readouterr()
    bridge.clear_trigger_queue()
    assert "Dibuang" not in capsys.readouterr().out


def test_ttl_expiry_logs_a_count_only(capsys):
    q = VoiceTriggerQueue(ttl_sec=60.0)
    old = chat("rahasia viewer", "@namaviewer")
    old.enqueued_at = time.time() - 120
    q.enqueue(old)
    capsys.readouterr()
    assert len(q) == 0
    out = capsys.readouterr().out
    assert "Kedaluwarsa" in out and "1 dibuang" in out
    assert "namaviewer" not in out and "rahasia viewer" not in out


def test_paid_and_game_items_still_ignore_ttl_and_log_nothing(capsys):
    q = VoiceTriggerQueue(ttl_sec=60.0)
    for kind in ("donation", "video", "game"):
        q.enqueue(QueuedVoiceTrigger("x", trigger_type=kind, enqueued_at=time.time() - 999))
    capsys.readouterr()
    assert len(q) == 3
    assert "Kedaluwarsa" not in capsys.readouterr().out


def test_plain_owner_speech_keeps_chat_but_still_clears_initiative():
    drain()
    try:
        bridge.voice_trigger_buffer.enqueue(chat())
        bridge.voice_trigger_buffer.enqueue(QueuedVoiceTrigger("inisiatif", trigger_type="curious"))
        bridge.voice_trigger_queue.put(bridge.VoiceTrigger("legacy", "yt_chat", "@lain", time.time()))
        assert bridge._note_owner_speech("oke oke lanjut aja", "mic_pasif", None) is None
        assert bridge.voice_trigger_queue.empty()  # FIFO: chat kept here would answer before the owner
        assert bridge.voice_trigger_buffer.depth_for("yt_chat") == 1
        assert bridge.voice_trigger_buffer.depth_for("curious") == 0
    finally:
        drain()


def test_owner_speech_is_still_served_before_kept_chat():
    q = VoiceTriggerQueue()
    q.enqueue(chat())
    q.enqueue(QueuedVoiceTrigger("omongan", trigger_type="mic_pasif"))
    assert q.dequeue().trigger_type == "mic_pasif"
    assert q.dequeue().trigger_type == "yt_chat"


def test_stop_command_and_ptt_clear_still_drop_chat():
    drain()
    try:
        bridge.voice_trigger_buffer.enqueue(chat())
        bridge.clear_trigger_queue()
        assert bridge.voice_trigger_buffer.depth_for("yt_chat") == 0
        bridge.voice_trigger_buffer.enqueue(chat())
        try:
            bridge._note_owner_speech("diam", "mic", None)
            assert bridge.voice_trigger_buffer.depth_for("yt_chat") == 0
        finally:
            bridge._note_owner_speech("boleh ngomong lagi", "mic", None)
    finally:
        drain()

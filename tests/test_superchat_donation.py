"""D0 (v0.7): Super Chat YouTube = trigger 'donation' — tanpa wake word.

Temuan rekon: liveChatPaidMessageRenderer SUDAH diterima parse_action tapi
purchaseAmountText DIBUANG, dan Super Chat tanpa "arti" di teksnya diabaikan.
Kunci desain:
1. Donasi dijawab TANPA syarat wake word.
2. Donasi TIDAK PERNAH di-drop saat sibuk (orang sudah bayar).
3. Prioritas antrean tertinggi (donation > yt_chat > mic > curious).
4. Super Chat tanpa teks (sticker/nominal saja) tetap direspon.
"""
from __future__ import annotations

import queue as _queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_voice_queue as avq
import hermes_vtuber_bridge as b


# --- extract_superchat (pure) ---------------------------------------------------


def _paid_item(amount="Rp 20.000", text="semangat arti!", renderer="liveChatPaidMessageRenderer"):
    r = {
        "id": "x1",
        "authorName": {"simpleText": "@donatur99"},
        "purchaseAmountText": {"simpleText": amount},
    }
    if text is not None:
        r["message"] = {"runs": [{"text": text}]}
    return {renderer: r}


def test_extract_superchat_paid_message():
    out = b.extract_superchat(_paid_item())
    assert out == {"name": "@donatur99", "message": "semangat arti!",
                   "paid_amount": "Rp 20.000"}


def test_extract_superchat_sticker_without_text():
    out = b.extract_superchat(_paid_item(text=None, renderer="liveChatPaidStickerRenderer"))
    assert out["paid_amount"] == "Rp 20.000"
    assert out["message"] == ""


def test_extract_superchat_ignores_normal_message():
    item = {"liveChatTextMessageRenderer": {"authorName": {"simpleText": "@x"},
                                            "message": {"runs": [{"text": "halo"}]}}}
    assert b.extract_superchat(item) is None


# --- wiring parse_action & process_message (source) ------------------------------


def test_parse_action_keeps_paid_and_textless_superchat():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "liveChatPaidStickerRenderer" in src
    assert "if not msg and not paid: return None" in src, (
        "Super Chat tanpa teks tidak boleh di-drop"
    )
    assert "out['paid_amount'] = paid" in src


def test_process_message_donation_before_wake_call():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    i_hist = src.index("[SUPER CHAT {paid}]")
    i_don = src.index("schedule_donation_trigger(", i_hist)
    i_wake = src.index("if is_arti_wake_call(chat_msg):", i_hist)
    assert i_hist < i_don < i_wake, (
        "urutan: history (dengan nominal) -> jadwalkan donation -> baru wake call biasa"
    )


# --- tunda reaksi sampai alert overlay selesai (Bohan 2026-08-02) ----------------


def test_alert_delay_scales_with_message_length():
    cfg = {"donation_alert_base_sec": 5.0, "donation_alert_per_char_sec": 0.055,
           "donation_alert_max_sec": 20.0}
    assert b.donation_alert_delay_sec("", cfg) == 5.0, "tanpa pesan = base saja"
    d100 = b.donation_alert_delay_sec("x" * 100, cfg)
    assert 10.0 < d100 < 11.0, "pesan 100 char ~ base + 5,5 dtk baca"
    assert b.donation_alert_delay_sec("x" * 2000, cfg) == 20.0, "wajib di-cap"


def test_alert_delay_zero_base_means_instant():
    assert b.donation_alert_delay_sec("halo", {"donation_alert_base_sec": 0}) == 0.0


def test_schedule_fires_after_delay(monkeypatch):
    import time as _t

    calls = []
    monkeypatch.setattr(b, "queue_voice_trigger",
                        lambda text, trigger_type, viewer_name: calls.append(trigger_type))
    monkeypatch.setitem(b.CONFIG, "donation_alert_base_sec", 0.05)
    monkeypatch.setitem(b.CONFIG, "donation_alert_per_char_sec", 0.0)
    b.schedule_donation_trigger("[DONASI ...]: hai", "@x", "hai")
    assert calls == [], "tidak boleh langsung — nunggu alert"
    _t.sleep(0.3)
    assert calls == ["donation"], "harus terkirim setelah tundaan"


# --- donasi tidak pernah di-drop saat sibuk ---------------------------------------


def test_donation_not_dropped_when_busy(monkeypatch):
    monkeypatch.setattr(b.session_transcript, "log_trigger", lambda *a, **k: None)
    monkeypatch.setattr(b, "_brain_busy", True)
    # kosongkan antrean dulu
    while True:
        try:
            b.voice_trigger_queue.get_nowait()
        except _queue.Empty:
            break

    b.queue_voice_trigger("[DONASI Super Chat Rp 20.000 dari @donatur99]: mantap",
                          trigger_type="donation", viewer_name="@donatur99")
    item = b.voice_trigger_queue.get_nowait()
    assert item.trigger_type == "donation"

    # mic saat sibuk tetap di-drop (perilaku lama tidak berubah)
    b.queue_voice_trigger("halo", trigger_type="mic")
    try:
        left = b.voice_trigger_queue.get_nowait()
        raise AssertionError(f"mic harusnya di-drop saat sibuk, dapat: {left}")
    except _queue.Empty:
        pass


# --- prioritas antrean buffer ------------------------------------------------------


def test_donation_highest_priority_in_buffer():
    buf = avq.VoiceTriggerQueue()
    buf.enqueue(avq.QueuedVoiceTrigger(text="chat", trigger_type="yt_chat",
                                       viewer_name="@a"))
    buf.enqueue(avq.QueuedVoiceTrigger(text="donasi", trigger_type="donation",
                                       viewer_name="@b"))
    first = buf.dequeue()
    assert first is not None and first.trigger_type == "donation"


# --- prompt donasi -----------------------------------------------------------------


def test_prepare_turn_context_donation_instruction():
    import asyncio

    import arti_voice_pipeline as vp

    ctx = asyncio.run(
        vp.prepare_turn_context(
            "[DONASI Super Chat Rp 20.000 dari @donatur99]: semangat terus!",
            [],
            "system base",
            {"vault_rag_live_enabled": False},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "[history]",
            extract_trigger_message=lambda s: s,
        )
    )
    assert "DONASI" in ctx.target_instruction
    assert "terima kasih" in ctx.target_instruction.lower()
    assert '"donatur"' in ctx.target_instruction, (
        "nickname donatur (tanpa angka ekor) harus disuntik"
    )
    assert "2-3 kalimat penuh" not in ctx.prompt_content, (
        "donasi bukan jalur streamer — jangan pakai instruksi default"
    )


def test_shipped_default_trigger_types_unchanged():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"cursor_trigger_types": ["yt_chat"],' in src, (
        "default shipped tetap yt_chat saja; donation dinyalakan via config_local"
    )

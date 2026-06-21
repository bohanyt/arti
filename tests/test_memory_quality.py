"""Tests for arti_memory_quality."""
import arti_memory_quality as mq


def test_sanitize_strips_thinking_blocks():
    tag = "think"
    raw = f"<{tag}>chain of thought here</{tag}>\nRingkasan bersih dalam bahasa Indonesia."
    out = mq.sanitize_model_text(raw)
    assert "chain of thought" not in out
    assert "Ringkasan bersih" in out


def test_should_save_learning_rejects_noise():
    assert not mq.should_save_learning("tidak ditemukan")
    assert not mq.should_save_learning("short")
    assert mq.should_save_learning("Streamer suka nasi goreng pedas level 3")


def test_append_not_duplicate():
    assert mq.is_duplicate_learning(
        "Reflection: Arti suka membantu stream",
        ["- [2026-06-01] Reflection: Arti suka membantu stream"],
    )


def test_filter_memories_for_startup_today_only():
    mems = [
        "- [2026-06-03] lama",
        "- [2026-06-04] hari ini satu",
        "- [2026-06-04] hari ini dua",
    ]
    out = mq.filter_memories_for_startup(mems, today="2026-06-04")
    assert len(out) == 2
    assert all("[2026-06-04]" in m for m in out)


def test_strip_history_echo_truncates_at_leak():
    raw = 'Ya, sudah tahu dong. Streamer [11:04:23] coba:"Arti:" Ini lo.'
    out = mq.strip_history_echo(raw)
    assert out == "Ya, sudah tahu dong."
    assert "[11:04:23]" not in out
    assert "Arti:" not in out


def test_strip_history_echo_preserves_clean_reply():
    clean = "Halo Streamer, apa kabar?"
    assert mq.strip_history_echo(clean) == clean


def test_normalize_stutter_words():
    assert mq.normalize_stutter_words("Nama co-host diberikan diberikan oleh Streamer") == (
        "Nama co-host diberikan oleh Streamer"
    )


def test_should_save_learning_skips_debut_canon():
    assert not mq.should_save_learning("Arti debut co-host 27 Mei 2026 bersama streamer")
    assert not mq.should_save_learning("Nama Arti diberikan oleh streamer saat debut")

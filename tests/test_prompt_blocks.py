"""Golden prompt blocks captured from the bridge before the split."""

import calendar
import time
from types import SimpleNamespace

import pytest

import arti_live_safety
import arti_memory_quality
from arti_runtime import prompt_blocks


_DEBUT_CONFIG = {"arti_debut_label": "1 Januari 2026", "arti_debut_date": "2026-01-01"}
_TODAY_TAIL = (
    " Ini alat hitung DI KEPALAMU saja: cuplikan memori berlabel [YYYY-MM-DD] kamu terjemahkan jadi waktu relatif "
    "('tiga hari lalu', 'minggu kemarin'). JANGAN menyebut atau mengumumkan tanggal di jawaban kecuali memang "
    "DITANYA tanggal/hari — tidak ada 'berdasarkan tanggal...', tidak ada tanggal di akhir jawaban. "
    "Tanggal debut tetap dijawab sesuai [ASAL USUL ARTI]."
)
_MEMORY_FALLBACK = (
    "\n\n[MEMORI JANGKA PANJANG: catatan sesi lama dipanggil otomatis saat jawab. Jangan sebut nama teknis "
    "internal (database, RAG, vault, nama berkas, nama model). Pakai ingatan itu supaya nyambung dengan "
    "obrolan sekarang; cara kerjanya nggak perlu diceritakan. Urusan teknis ditangani Bohan; "
    "jangan membuat klaim pekerjaan internal.]"
)


@pytest.fixture
def fixed_clock():
    return SimpleNamespace(
        strftime=lambda fmt: "2026-11-21",
        strptime=time.strptime,
        time=lambda: calendar.timegm((2026, 11, 21, 12, 0, 0)),
        mktime=calendar.timegm,
    )


def test_origin_block_matches_original():
    assert prompt_blocks.build_origin_context(_DEBUT_CONFIG) == (
        "\n\n[ASAL USUL ARTI]\nfakta contoh live: 1 Januari 2026 (2026-01-01).\n"
        "Kalau ditanya sejak kapan Arti ada: jawab 1 Januari 2026, bukan tanggal sesi hari ini."
    )
    assert prompt_blocks.build_origin_context({"arti_debut_label": "", "arti_debut_date": ""}) == (
        "\n\n[ASAL USUL ARTI]\nfakta contoh live: debut date (YYYY-MM-DD).\n"
        "Kalau ditanya sejak kapan Arti ada: jawab debut date, bukan tanggal sesi hari ini."
    )


def test_today_block_matches_original(fixed_clock):
    assert prompt_blocks.build_today_block(_DEBUT_CONFIG, time=fixed_clock) == (
        "\n\n[HARI INI]\nTanggal sesi sekarang: 2026-11-21. Kamu sudah 324 hari jadi co-host." + _TODAY_TAIL
    )
    assert prompt_blocks.build_today_block({"arti_debut_date": "invalid"}, time=fixed_clock) == (
        "\n\n[HARI INI]\nTanggal sesi sekarang: 2026-11-21." + _TODAY_TAIL
    )


def test_startup_memory_block_matches_original(fixed_clock, monkeypatch):
    monkeypatch.setattr(arti_memory_quality, "time", fixed_clock)
    config = {"memory_startup_max_bullets": 2}
    memories = [
        "- [2026-11-21] Menyapa penonton.",
        "- [2026-11-20] Belanja bibit.",
        "- [2026-11-21] Panen stroberi.",
        "- [2026-11-21] Memperbaiki API.",
        "- [2026-11-21] Pergi ke pasar.",
    ]
    assert prompt_blocks.build_startup_memory_block(memories, config, arti_live_safety=arti_live_safety) == (
        "\n\n[MEMORI TERBARU (hari ini):]\n- [2026-11-21] Panen stroberi.\n- [2026-11-21] Pergi ke pasar."
    )
    assert prompt_blocks.build_startup_memory_block(
        ["- [2026-11-20] Belanja bibit."], config, arti_live_safety=arti_live_safety,
    ) == _MEMORY_FALLBACK


@pytest.mark.parametrize(
    ("cap", "rag_enabled", "expected"),
    [(0, True, _MEMORY_FALLBACK), (0, False, ""), (2, False, "")],
)
def test_startup_memory_fallback_matches_original(cap, rag_enabled, expected, fixed_clock, monkeypatch):
    monkeypatch.setattr(arti_memory_quality, "time", fixed_clock)
    config = {"memory_startup_max_bullets": cap, "vault_rag_enabled": rag_enabled}
    assert prompt_blocks.build_startup_memory_block(
        ["- [2026-11-20] Belanja bibit."], config, arti_live_safety=arti_live_safety,
    ) == expected

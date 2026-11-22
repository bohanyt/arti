"""Tests keluar-dari-air.

Bohan melihat Arti TENGGELAM 2026-08-07. Sebabnya bukan sekadar "[recorded remark omitted]": mineflayer-pathfinder MENOLAK simpul di dalam air
(`if (blockC.liquid) return // dont go underwater` di movements.js). Jadi
begitu dia tercebur, tidak ada jalur yang bisa dihitung sama sekali — dia
mematung di dalam air sampai napasnya habis. Pathfinder tidak bisa
menolongnya; yang bisa cuma kendali langsung.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402

BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")


def _kode(blok: str) -> str:
    return "\n".join(b for b in blok.splitlines()
                     if b.strip() and not b.strip().startswith("//"))


def _blok_renang() -> str:
    i = BOT_JS.index("setInterval(() => {\n  if (!bot.entity) return\n  if (!diAir())")
    return _kode(BOT_JS[i:BOT_JS.index("}, 1000)", i)])


# --- kenapa pathfinder tidak dipakai --------------------------------------

def test_pathfinder_goal_is_cleared_in_water():
    """Membiarkan goal lama hidup = pathfinder terus mencoba jalur yang mustahil
    sambil kita menahan tombol; dua pengendali berebut satu badan."""
    blok = _blok_renang()
    assert "bot.pathfinder.setGoal(null)" in blok


def test_uses_direct_controls_not_pathfinding():
    """Lompat = naik ke permukaan (perilaku vanilla). Ini saja sudah
    menghentikan tenggelam, bahkan kalau daratnya tidak ketemu."""
    blok = _blok_renang()
    assert "bot.setControlState('jump', true)" in blok
    assert "bot.setControlState('forward', true)" in blok


def test_controls_are_released_on_every_exit():
    """Tombol yang tertinggal ditekan = dia lompat-lompat maju terus di darat."""
    assert "function lepasKendaliRenang" in BOT_JS
    blok = _blok_renang()
    assert blok.count("lepasKendaliRenang()") >= 2, "jalur sampai-darat & menyerah"


def test_checked_every_second_not_on_the_slow_ticker():
    """Napas habis dalam ~15 detik; telat satu putaran ticker 5 dtk saja sudah
    setengah nyawa."""
    i = BOT_JS.index("if (!diAir())")
    assert "}, 1000)" in BOT_JS[i:i + 2200]


def test_gives_up_instead_of_flailing_forever():
    blok = _blok_renang()
    assert "renangSampai" in blok and "menyerah" in blok


def test_land_scan_requires_solid_floor_and_headroom():
    """Tanpa cek dua blok udara di atas pijakan, dia berenang ke dinding batu
    yang kebetulan permukaannya kelihatan seperti darat."""
    i = BOT_JS.index("function arahDarat")
    blok = _kode(BOT_JS[i:BOT_JS.index("function lepasKendaliRenang", i)])
    assert "boundingBox === 'block'" in blok
    assert "kaki.name === 'air'" in blok and "atas.name === 'air'" in blok
    assert "bawah.name !== 'water'" in blok


def test_swimming_is_not_mistaken_for_being_stuck_or_overridden():
    """Berenang memakai kendali langsung — ticker 5 dtk tidak boleh menimpanya,
    dan deteksi nyangkut tidak boleh menyeretnya pergi."""
    assert "if (currentTask === 'renang') return" in BOT_JS
    i = BOT_JS.index("currentTask !== 'idle' &&")
    assert "currentTask !== 'renang' &&" in BOT_JS[i:i + 260]


# --- sisi Python ----------------------------------------------------------

def test_swim_start_speaks_swim_end_does_not():
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction({"ev": "swim_start", "oxygen": 8}, lim, 100.0, {})
    assert r and "kecebur" in r
    assert am.map_event_to_reaction(
        {"ev": "swim_end", "reason": "sampai_darat"}, lim, 200.0, {}) is None


def test_swim_events_reach_the_context():
    out = am.format_context(
        None, [(99.0, {"ev": "swim_start"}),
               (99.5, {"ev": "swim_end", "reason": "sampai_darat"})],
        ttl_sec=120.0, now=100.0)
    assert "kecebur" in out and "daratan" in out
    out2 = am.format_context(
        None, [(99.0, {"ev": "swim_end", "reason": "menyerah"})],
        ttl_sec=120.0, now=100.0)
    assert "berhenti berusaha" in out2


def test_task_label_is_human():
    assert am.task_label("renang") == "lagi berusaha keluar dari air"

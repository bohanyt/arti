"""Tests kemampuan MAKAN (Phase 2, potongan pertama).

Di log 6-7 Agustus Arti mengeluh lapar berkali-kali sambil MEMEGANG roti dari
Bohan tanpa bisa memakannya. Ini potongan pertama yang disepakati:
makan -> nyerang -> pegang item -> craft -> nambang/naruh blok.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402

BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")


def _kode(blok: str) -> list[str]:
    return [b.strip() for b in blok.splitlines()
            if b.strip() and not b.strip().startswith("//")]


# --- protokol -------------------------------------------------------------

def test_eat_tag_is_accepted_and_stripped():
    bersih, cmds = am.parse_mc_tags("Laper banget nih [MC: eat]", {})
    assert cmds == [{"cmd": "eat"}]
    assert "[MC:" not in bersih and bersih == "Laper banget nih"


def test_eat_encodes_without_arguments():
    assert am.encode_command({"cmd": "eat"}) == '{"cmd": "eat"}'


def test_eat_is_an_action_not_owner_only():
    """Makan bukan perubahan sesi — Arti boleh memutuskannya sendiri, dan
    penonton yang menyuruh makan tidak perlu ditolak gate pemilik."""
    assert am.TAG_CATEGORIES["eat"] == "action"
    assert am.is_owner_only({"cmd": "eat"}) is False


def test_eat_cannot_collide_with_another_movement_command():
    """Satu tag per kategori: kalau LLM menulis dua aksi, yang pertama menang
    — bukan dua perintah yang bertabrakan."""
    _, cmds = am.parse_mc_tags("[MC: eat] terus [MC: roam]", {})
    assert cmds == [{"cmd": "eat"}]


# --- event & reaksi -------------------------------------------------------

def test_ate_event_becomes_a_reaction_without_numbers():
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction(
        {"ev": "ate", "item": "bread", "food_before": 8, "food_after": 13},
        lim, 100.0, {})
    assert r and "bread" in r
    assert "8" not in r and "13" not in r, "angka mentah bocor ke prompt"
    assert "jangan sebut angka" in r.lower()


def test_ate_appears_in_context_summary():
    out = am.format_context(
        None, [(99.0, {"ev": "ate", "item": "cooked_beef"})],
        ttl_sec=120.0, now=100.0)
    assert "cooked_beef" in out and "makan" in out


def test_eat_failures_have_human_labels():
    """Tanpa label, alasan mentah seperti 'tidak_punya_makanan' masuk prompt
    sebagai jargon dan bisa terucap apa adanya."""
    for alasan in ("sudah_kenyang", "tidak_punya_makanan", "gagal_makan"):
        out = am.format_context(
            None, [(99.0, {"ev": "task_failed", "task": "eat", "reason": alasan})],
            ttl_sec=120.0, now=100.0)
        assert alasan not in out, f"jargon bocor: {alasan}"
        assert out.strip(), alasan


# --- bot.js ---------------------------------------------------------------

def test_food_lookup_uses_name_not_id():
    """REGRESI: `item.type` roti tidak sama dengan kunci `registry.foods` —
    tabel makanan TIDAK diindeks id item, jadi `foods[it.type]` selalu
    undefined dan Arti selalu merasa tidak punya makanan walau tasnya penuh
    roti. Unit test tidak akan pernah menangkap ini; hanya server sungguhan
    yang bisa."""
    i = BOT_JS.index("async eat(")
    blok = chr(10).join(_kode(BOT_JS[i:BOT_JS.index("\n  },", i)]))
    assert "foodsByName" in blok
    assert "bot.registry.foods[it.type]" not in blok, "pakai lookup id yang rusak lagi"


def test_eat_reports_every_failure_path():
    """Gagal diam-diam = Arti mengira sudah makan lalu bercerita begitu."""
    i = BOT_JS.index("async eat(")
    blok = BOT_JS[i:BOT_JS.index("\n  },", i)]
    for alasan in ("sudah_kenyang", "tidak_punya_makanan", "gagal_makan"):
        assert alasan in blok, alasan
    assert "try {" in blok and "catch" in blok


def test_eat_picks_the_most_filling_first():
    """Kalau punya steak dan roti, jangan habiskan steak untuk 1 bar."""
    i = BOT_JS.index("async eat(")
    blok = BOT_JS[i:BOT_JS.index("\n  },", i)]
    assert "makanan.sort(" in blok and "foodPoints" in blok


def test_eat_refuses_when_full():
    i = BOT_JS.index("async eat(")
    blok = BOT_JS[i:BOT_JS.index("\n  },", i)]
    assert "bot.food >= 20" in blok


# --- prompt ---------------------------------------------------------------

def test_prompt_offers_eating_and_no_longer_denies_it():
    assert "[MC: eat]" in BRIDGE
    i = BRIDGE.index("BATAS KEMAMPUANMU SEKARANG")
    blok = BRIDGE[i:i + 900]
    bisa = blok[blok.index("BISA:"):blok.index("BELUM BISA:")]
    assert "makan" in bisa.lower(), "batas kemampuan masih bilang dia tidak bisa makan"
    belum = blok[blok.index("BELUM BISA:"):blok.index("Jadi menambang")]
    assert "makan" not in belum.lower(), "makan masih terdaftar sebagai belum bisa"

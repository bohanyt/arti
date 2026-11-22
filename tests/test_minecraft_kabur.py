"""Tests kemampuan KABUR (Phase 2).

Tanpa kabur, Arti bisa mati berkali-kali sambil terus berjalan ditembaki
skeleton — dia tidak pernah menoleh, mundur, atau melawan.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402

BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")


def _kode(blok: str) -> str:
    return "\n".join(b for b in blok.splitlines()
                     if b.strip() and not b.strip().startswith("//"))


# --- protokol -------------------------------------------------------------

def test_kabur_tag_and_encoding():
    bersih, cmds = am.parse_mc_tags("Kabur dulu ah [MC: kabur]", {})
    assert cmds == [{"cmd": "kabur"}] and "[MC:" not in bersih
    assert am.encode_command({"cmd": "kabur"}) == '{"cmd": "kabur"}'
    assert am.TAG_CATEGORIES["kabur"] == "action"
    assert am.is_owner_only({"cmd": "kabur"}) is False


def test_flee_events_become_context_and_reaction():
    lim = am.ReactionLimiter()
    r = am.map_event_to_reaction({"ev": "flee_start", "from": "skeleton"},
                                 lim, 100.0, {})
    assert r and "skeleton" in r and "KABUR" in r
    # selesai kabur bukan berita — cukup jadi konteks
    assert am.map_event_to_reaction({"ev": "flee_end", "reason": "aman"},
                                    lim, 200.0, {}) is None
    out = am.format_context(
        None, [(99.0, {"ev": "flee_start", "from": "creeper"}),
               (99.5, {"ev": "flee_end", "reason": "aman"})],
        ttl_sec=120.0, now=100.0)
    assert "kabur dari creeper" in out and "lepas dari kejaran" in out


def test_task_label_is_human():
    assert "kabur" in am.task_label("kabur")
    assert am.task_label("kabur") != "kabur", "label mentah bocor ke prompt"


def test_no_threat_is_reported_not_silent():
    out = am.format_context(
        None, [(99.0, {"ev": "task_failed", "task": "kabur",
                       "reason": "tidak_ada_ancaman"})],
        ttl_sec=120.0, now=100.0)
    assert "tidak_ada_ancaman" not in out and out.strip()


# --- bot.js ---------------------------------------------------------------

def test_flee_uses_inverted_goal_and_is_dynamic():
    """GoalInvert = balik arah goal "dekati dia". `dynamic: true` supaya dia
    terus menjauh saat mobnya mengejar, bukan lari ke satu titik lalu berhenti
    jadi sasaran lagi."""
    i = BOT_JS.index("function mulaiKabur")
    blok = _kode(BOT_JS[i:BOT_JS.index("\nfunction selesaiKabur", i)])
    assert "goals.GoalInvert(" in blok and "goals.GoalFollow(" in blok
    assert "), true)" in blok, "goal kabur harus dinamis"


def test_flee_deadline_is_set_once_not_refreshed():
    """REGRESI TERUKUR (server nyata 2026-08-07): menyegarkan batas waktu tiap
    tick membuatnya TIDAK PERNAH tercapai — selama mob mengejar dalam 14 blok,
    Arti kabur selamanya dan tidak pernah kembali menemani/menjelajah."""
    i = BOT_JS.index("function mulaiKabur")
    blok = _kode(BOT_JS[i:BOT_JS.index("\nfunction selesaiKabur", i)])
    baris = blok.splitlines()
    i_gate = min(k for k, b in enumerate(baris) if "currentTask !== 'kabur'" in b)
    i_batas = min(k for k, b in enumerate(baris) if "fleeUntil = Date.now()" in b)
    assert i_gate < i_batas, "batas waktu dipasang di luar gerbang mulai episode"


def test_new_damage_extends_the_flee():
    """Yang boleh memperpanjang cuma luka BARU — bukan sekadar dikejar."""
    i = BOT_JS.index("function cobaKaburOtomatis")
    blok = _kode(BOT_JS[i:BOT_JS.index("bot.on('health'", i)])
    assert "fleeUntil = Date.now() + FLEE_MS" in blok
    assert "currentTask === 'kabur'" in blok


def test_flee_ends_when_safe_or_time_is_up():
    i = BOT_JS.index("if (currentTask === 'kabur') {")
    blok = _kode(BOT_JS[i:i + 700])
    assert "selesaiKabur('aman')" in blok
    assert "selesaiKabur('cukup_jauh')" in blok
    assert "FLEE_AMAN" in blok


def test_flee_branch_runs_before_follow_and_roam():
    """Kalau cabang follow/roam duluan, tujuan kabur langsung ditimpa."""
    i = BOT_JS.index("if (currentTask === 'kabur') {")
    for lain in ("if (currentTask === 'follow') {",
                 "if (currentTask === 'roam') {"):
        assert i < BOT_JS.index(lain), lain


def test_auto_flee_triggers_on_damage_not_only_on_command():
    """Yang menyelamatkan dia adalah pemicu OTOMATIS — menunggu LLM
    mengeluarkan tag terlalu lambat saat sedang ditembaki."""
    panggilan = [b.strip() for b in BOT_JS.splitlines()
                 if "cobaKaburOtomatis(h)" in b
                 and not b.strip().startswith(("function", "//"))]
    assert panggilan == ["cobaKaburOtomatis(h)"] * 2, panggilan


def test_auto_flee_can_be_turned_off():
    assert "if (!(FLEE_HP > 0) || h > FLEE_HP) return" in BOT_JS
    assert '"minecraft_flee_hp": 10,' in BRIDGE
    py = (ROOT / "arti_minecraft.py").read_text(encoding="utf-8")
    assert '"--flee-hp", str(cfg.get("minecraft_flee_hp", 10))' in py


def test_prompt_offers_fleeing():
    assert "[MC: kabur]" in BRIDGE
    i = BRIDGE.index("BATAS KEMAMPUANMU SEKARANG")
    blok = BRIDGE[i:i + 900]
    bisa = blok[blok.index("BISA:"):blok.index("BELUM BISA:")]
    assert "kabur" in bisa.lower()

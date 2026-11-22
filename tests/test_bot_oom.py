"""OBAT OOM 4GB bot (2026-08-10) — akar + tiga lapis pertahanan.

Reproduksi terukur di server: `mine stone 32` saat batunya tak terjangkau /
roam saat terkurung memicu badai alokasi pathfinder ~100 MB/dtk (goal tak
terjangkau -> A* sinkron dihitung ulang tiap path kosong; default
thinkTimeout 5000 ms + searchRadius tanpa batas + movements boleh menggali).
Di limit heap default ~4GB, mark-compact V8 keteteran -> "Ineffective
mark-compacts" dalam 30 dtk - 18 menit (4x di live 13.48, SEMUA persis
sesudah "[recorded remark omitted]"). Diagnosis lama "[recorded remark omitted]" cuma separuh
cerita — hari ini POV mati total dan bot tetap tumbang.

Obatnya berlapis, dan yang MENYEMBUHKAN adalah cap heap:
1. Cap heap V8 (runner --max-old-space-size): GC dipaksa rajin — terukur
   264-274 MB DATAR >4 menit di kondisi identik yang membunuh 4 bot.
2. Rem pathfinder (thinkTimeout 750, searchRadius 64): tiap pencarian
   dibatasi, event loop tetap bernapas (watchdog tidak kelaparan lagi).
3. Watchdog 3 lapis, ambang = persentase limit NYATA (v8.getHeapStatistics):
   korbankan POV -> batalkan tugas rakus -> restart terkendali.
4. mine hanya blok TEREKSPOS — menggali ke kedalaman itu tugas `turun`.
"""
from pathlib import Path

import arti_minecraft as am
import arti_bridge as bridge

ROOT = Path(__file__).resolve().parent.parent
BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
RUNNER = (ROOT / "arti_minecraft.py").read_text(encoding="utf-8")


# --- lapis 1: cap heap ----------------------------------------------------

def test_runner_memasang_cap_heap():
    i = RUNNER.index("_default_open_proc")
    blok = RUNNER[i:i + 2200]
    assert "--max-old-space-size=" in blok, "tanpa cap, GC malas dan bot mati 4GB"
    assert "minecraft_bot_heap_mb" in blok


def test_config_cap_heap_ada():
    assert int(bridge.CONFIG["minecraft_bot_heap_mb"]) == 2048


# --- lapis 2: rem pathfinder ---------------------------------------------

def test_pathfinder_direm():
    assert "bot.pathfinder.thinkTimeout = 200" in BOT_JS, (
        "default 5000 ms = A* sinkron 5 detik per hitung-ulang goal buntu; "
        "750 pun masih kalah cepat dari badai (bot uji #3 tewas 48 dtk)")
    assert "bot.pathfinder.searchRadius = 64" in BOT_JS, (
        "default -1 = ruang pencarian tanpa batas saat goal tak terjangkau")


def test_goal_buntu_dibuang():
    """Lapis SUMBER: 5 hasil non-success beruntun tanpa kemajuan posisi =
    goal dibuang — terlihat menembak di uji ("jalur buntu (partial x5)")."""
    i = BOT_JS.index("jalurBuntu")
    blok = BOT_JS[i - 800:i + 1800]
    assert "path_update" in blok
    assert "jalurBuntu.n < 5" in blok or "jalurBuntu.n >= 5" in blok
    assert "distanceTo(jalurBuntu.pos) < 2" in blok, (
        "tanpa syarat kemajuan posisi, perjalanan jauh yang sah ikut dibuang")
    assert "'jalur_buntu'" in BOT_JS


# --- lapis 3: watchdog 3 tingkat -----------------------------------------

def _watchdog() -> str:
    i = BOT_JS.index("HEAP_LIMIT_MB")
    return BOT_JS[i - 600:i + 2400]


def test_ambang_watchdog_dari_limit_nyata():
    w = _watchdog()
    assert "getHeapStatistics().heap_size_limit" in w, (
        "angka absolut jadi bohong begitu cap-nya berubah")
    for nama in ("HEAP_POV_MB", "HEAP_BATAL_MB", "HEAP_KRITIS_MB"):
        assert nama in w


def test_watchdog_membatalkan_tugas_rakus():
    w = _watchdog()
    assert "cancelTask" in w, "collectBlock yang mengamuk harus bisa disetop"
    assert "setGoal(null)" in w
    assert "'memori_bengkak'" in w


def test_watchdog_restart_terkendali_dan_patroli_rapat():
    w = _watchdog()
    assert "process.exit(1)" in w, (
        "sekarat 4GB dengan GC 7 detik lebih buruk daripada respawn bersih")
    assert "bot_restart_memori" in w
    assert "}, 5000)" in w, "patroli 30 dtk terbukti KELEWATAN ledakan 30-detik"


# --- lapis 4: mine hanya blok terekspos ----------------------------------

def test_mine_menolak_blok_terkubur():
    i = BOT_JS.index("async mine(c)")
    blok = BOT_JS[i:i + 4200]
    assert "terekspos" in blok
    assert "boundingBox === 'empty'" in blok
    assert "'terkubur_semua'" in blok


def test_mine_pra_cek_jalur_dan_per_target():
    """Target wajib TERVERIFIKASI berjalur (A* 60ms, wajib success) dan
    collect satu-per-satu dengan batas 10 dtk/target — SATU target tak
    terjangkau terukur menyembur ~120 MB/dtk (bot uji #5 tewas 30 dtk)."""
    i = BOT_JS.index("async mine(c)")
    blok = BOT_JS[i:i + 5200]
    assert "GoalGetToBlock" in blok
    assert "hasil.status === 'success'" in blok, (
        "partial tidak cukup — jalur separuh = badai di ujungnya")
    assert "for (const b of blok)" in blok, "collect per-target, bukan borongan"
    assert "cancelTask" in blok
    assert "}, 10000)" in blok


def test_label_kegagalan_baru_dikenal_llm():
    src = (ROOT / "arti_minecraft.py").read_text(encoding="utf-8")
    assert '"memori_bengkak"' in src
    assert '"terkubur_semua"' in src
    assert "[MC: turun]" in src.split('"terkubur_semua"')[1][:200], (
        "kegagalan terkubur harus MENUNJUKKAN jalan keluarnya (turun)")

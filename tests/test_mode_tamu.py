"""MODE TAMU — mabar di server orang (16 Agu 2026, sesi mendadak e4mc).

Permintaan Bohan: (1) "[recorded remark omitted]";
(2) dilepas jalan-jalan aman + bisa respawn di bed; (3) teman-teman TIDAK
mendengar TTS Arti — chat Minecraft adalah SATU-SATUNYA kanal dua arah.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am
import arti_bridge as bridge

_BOT_JS = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Bot: gerbang dunia
# ---------------------------------------------------------------------------

def test_daftar_terlarang_memuat_semua_pengubah_dunia():
    blok = _BOT_JS.split("const TAMU_TERLARANG")[1].split("])")[0]
    for verb in ("mine", "place", "bangun", "turun", "jembatan", "menara",
                 "lubang_aman", "mundur_tembok", "masak", "craft",
                 "siapkan_alat", "simpan", "ambil", "portal", "portal_cor"):
        assert f"'{verb}'" in blok, f"{verb} lolos dari gerbang tamu"


def test_tidur_sengaja_TIDAK_terlarang():
    """Bed = respawn point — Bohan eksplisit minta Arti bisa respawn di bed."""
    blok = _BOT_JS.split("const TAMU_TERLARANG")[1].split("])")[0]
    assert "'tidur'" not in blok


def test_dispatcher_menolak_bersuara():
    """Invariant kejujuran: perintah LLM ditolak DENGAN task_failed, supaya
    dia tidak mengaku menambang yang tidak pernah terjadi."""
    assert "reason: 'mode_tamu'" in _BOT_JS


def test_refleks_pembangun_digerbang_senyap():
    """4 cabang refleks pengubah dunia + gali-keluar wajib cek MODE_TAMU."""
    assert _BOT_JS.count("!MODE_TAMU") >= 5
    assert "if (MODE_TAMU) return false" in _BOT_JS  # galiKeluar


def test_status_membawa_bendera_tamu():
    assert "tamu: MODE_TAMU," in _BOT_JS


def test_arg_tamu_dialirkan_dari_config():
    src = (ROOT / "arti_minecraft.py").read_text(encoding="utf-8")
    assert '"--tamu"' in src
    assert "minecraft_mode_tamu" in src


# ---------------------------------------------------------------------------
# 2. Nudge, takdir, reaksi
# ---------------------------------------------------------------------------

def test_nasihat_tamu_tidak_mendorong_menambang():
    baris = am.nasihat({"tamu": True, "health": 20, "food": 20})
    assert len(baris) == 1
    assert "TAMU" in baris[0]
    assert "nambang" not in baris[0].lower()


def test_nasihat_biasa_tidak_berubah():
    hasil = am.nasihat({"health": 20, "food": 20, "tamu": False})
    assert all("TAMU" not in b for b in hasil)


def test_reaksi_mode_tamu_sekali_per_sesi():
    lim = am.ReactionLimiter()
    ev = {"ev": "task_failed", "task": "mine", "reason": "mode_tamu"}
    pertama = am.map_event_to_reaction(ev, lim, 100.0, {})
    kedua = am.map_event_to_reaction(ev, lim, 200.0, {})
    assert pertama is not None and "TAMU" in pertama
    assert kedua is None


def test_takdir_digerbang_saat_tamu():
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    blok = src.split('if not CONFIG.get("minecraft_takdir_enabled", True):')[1][:400]
    assert 'CONFIG.get("minecraft_mode_tamu", False)' in blok


# ---------------------------------------------------------------------------
# 3. Chat dua arah dengan teman
# ---------------------------------------------------------------------------

class _RunnerPalsu(am.MinecraftRunner):
    def __init__(self, config, hooks):
        # Jalur minimum: tanpa proses bot. Atribut yang dibaca _handle_event.
        self._config = config
        self._hooks = hooks
        self._limiter = am.ReactionLimiter()
        self._events = []
        self._chat_balas_ts = 0.0

    def _emit_reaction(self, ev, now=None):
        pass


def _runner(hooks, **cfg):
    dasar = {"minecraft_streamer_name": "examplemcplayer",
             "minecraft_bot_name": "arti_berarti",
             "minecraft_spectator_name": "cam_sample",
             "minecraft_chat_reply_gap_sec": 0.0}
    dasar.update(cfg)
    return _RunnerPalsu(dasar, hooks)


def test_chat_teman_memicu_balasan_dengan_nama():
    panggilan = []
    r = _runner({
        "add_history": lambda who, msg: panggilan.append(("hist", who, msg)),
        "streamer_chat": lambda teks, nama=None: panggilan.append(("balas", nama, teks)),
    })
    r._handle_line(json.dumps({"ev": "chat", "from": "TemanA", "text": "arti lagi ngapain?"}))
    assert ("hist", "TemanA", "(chat Minecraft) arti lagi ngapain?") in panggilan
    assert ("balas", "TemanA", "arti lagi ngapain?") in panggilan


def test_chat_streamer_tetap_jalur_lama():
    panggilan = []
    r = _runner({
        "add_history": lambda who, msg: panggilan.append(("hist", who)),
        "streamer_chat": lambda teks, nama=None: panggilan.append(("balas", nama)),
    })
    r._handle_line(json.dumps({"ev": "chat", "from": "examplemcplayer", "text": "halo arti"}))
    assert ("hist", "Streamer") in panggilan
    assert ("balas", None) in panggilan


def test_kamera_bot_sendiri_dan_command_disaring():
    panggilan = []
    r = _runner({
        "add_history": lambda who, msg: panggilan.append(who),
        "streamer_chat": lambda teks, nama=None: panggilan.append(f"balas:{nama}"),
    })
    r._handle_line(json.dumps({"ev": "chat", "from": "cam_sample", "text": "halo"}))
    r._handle_line(json.dumps({"ev": "chat", "from": "arti_berarti", "text": "gema"}))
    r._handle_line(json.dumps({"ev": "chat", "from": "TemanB", "text": "!nasi"}))
    assert panggilan == []


def test_hook_lama_satu_argumen_tidak_meledak():
    """Kompat: pemasangan lama balas(teks) — chat teman tetap terkirim."""
    panggilan = []
    r = _runner({
        "add_history": lambda who, msg: None,
        "streamer_chat": lambda teks: panggilan.append(teks),  # tanpa nama
    })
    r._handle_line(json.dumps({"ev": "chat", "from": "TemanC", "text": "woi arti"}))
    assert panggilan == ["woi arti"]


# ---------------------------------------------------------------------------
# 4. Cermin balasan ke chat game
# ---------------------------------------------------------------------------

def test_bagi_chat_game_per_kalimat():
    hasil = am.bagi_chat_game("Halo TemanA! Aku lagi nemenin Bohan aja. Kalian main apa?")
    assert hasil == ["Halo TemanA! Aku lagi nemenin Bohan aja. Kalian main apa?"]


def test_bagi_chat_game_memecah_yang_panjang():
    teks = ("Kalimat pertama yang lumayan panjang supaya jelas terpisah dari yang lain. " * 3).strip()
    hasil = am.bagi_chat_game(teks, maks=100)
    assert len(hasil) >= 2
    assert all(len(p) <= 100 for p in hasil)


def test_bagi_chat_game_membuang_tag_mc():
    hasil = am.bagi_chat_game("Oke aku ikut! [MC: follow]")
    assert hasil == ["Oke aku ikut!"]


def test_cermin_dipanggil_di_akhir_giliran():
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert '_cermin_ke_chat_game(ai_reply)' in src


def test_config_produksi_mode_tamu_mati_default():
    assert bridge.CONFIG["minecraft_mode_tamu"] is False
    assert bridge.CONFIG["minecraft_chat_mirror"] == "tamu"

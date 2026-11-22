"""Tests MISI ARAH-TETAP — misi yang tidak punya garis finis ("survive").

Bohan (2026-08-08): "[recorded remark omitted]"

Jawabannya menyingkap bug rancangan. Blok misi lama ditulis untuk misi yang BISA
selesai dan menyuruhnya menutup dengan `[MC: goal_done]` kalau tercapai — dan
`goal_done` MENGELUARKANNYA dari dunia. Misi "survive" tidak pernah selesai, jadi
dengan blok itu dia bisa merasa "aku selamat!" lalu meninggalkan dunia di tengah
siaran. Dua jenis misi karena itu harus dipisah, dan `goal_done` dipagari di sisi
eksekusi juga — prompt saja tidak cukup, model tetap bisa mengeluarkan tagnya.

Tes ini juga mengunci kesalahan yang HAMPIR lolos waktu fitur ini dibuat:
percabangan pertama ditulis sebagai `if/else` di TENGAH rangkaian string literal,
dan karena penggabungan literal mengikat lebih kuat dari `if/else`, satu cabang
kehilangan header misinya sementara cabang lain menelan seluruh blok
"BATAS KEMAMPUANMU". Prompt tetap terbentuk tanpa error — cuma isinya salah.
Karena itu tes di bawah memeriksa PROMPT YANG JADI, bukan sekadar flag-nya.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402


# --- pengenalan misi tanpa garis finis -----------------------------------

def test_survive_dikenali_sebagai_arah_tetap():
    for teks in ("survive", "SURVIVE", "coba survive selama mungkin",
                 "bertahan hidup di hutan", "jangan mati ya",
                 "tetap hidup sampai aku balik", "stay alive"):
        assert am.misi_tanpa_finis(teks), teks


def test_misi_biasa_tetap_punya_garis_finis():
    """Misi yang bisa dicentang selesai TIDAK boleh ikut terjaring — kalau ikut,
    dia kehilangan satu-satunya cara mengakhiri misi dan tidak pernah keluar
    game meski tujuannya sudah tercapai."""
    for teks in ("cari stronghold", "bikin rumah kecil", "kumpulkan 10 kayu",
                 "temukan diamond", "", "   "):
        assert not am.misi_tanpa_finis(teks), teks


def test_misi_kosong_bukan_arah_tetap():
    assert not am.misi_tanpa_finis("")
    assert not am.misi_tanpa_finis(None)  # type: ignore[arg-type]


# --- prompt yang benar-benar terbentuk -----------------------------------

class _RunnerPalsu:
    last_status = {"health": 20, "food": 20, "pos": {"x": 0, "y": 64, "z": 0}, "inv": []}

    def events_snapshot(self):
        return []


def _prompt(monkeypatch, misi: str) -> str:
    import arti_bridge as b

    monkeypatch.setitem(b.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(b, "_mc_runner_active", lambda: True)
    monkeypatch.setattr(b, "_minecraft_runner", _RunnerPalsu())
    monkeypatch.setattr(b, "add_to_history", lambda *a, **k: None)
    b._set_minecraft_goal(misi)
    return b._append_minecraft_context("")


def test_arah_tetap_melarang_goal_done_di_prompt(monkeypatch):
    out = _prompt(monkeypatch, "survive, jangan sampai mati")
    assert "[ARAH TETAP DARI BOHAN]" in out
    assert "survive" in out
    assert "JANGAN PERNAH memakai tag [MC: goal_done]" in out
    # Kalimat yang menawarkan jalan keluar TIDAK boleh ada di cabang ini.
    assert "kamu akan keluar dari game" not in out


def test_misi_biasa_masih_menawarkan_goal_done(monkeypatch):
    out = _prompt(monkeypatch, "cari stronghold")
    assert "[MISI DARI BOHAN]" in out
    assert "cari stronghold" in out
    assert "kamu akan keluar dari game" in out
    assert "[ARAH TETAP DARI BOHAN]" not in out


def test_blok_kemampuan_ikut_di_KEDUA_cabang(monkeypatch):
    """Ini yang hampir hilang: percabangan di tengah rangkaian string literal
    membuat blok kemampuan cuma menempel di satu cabang. Tanpa blok itu dia
    kehilangan seluruh pagar kejujuran — termasuk larangan mengaku membangun."""
    for misi in ("cari stronghold", "survive"):
        out = _prompt(monkeypatch, misi)
        assert "BATAS KEMAMPUANMU SEKARANG" in out, misi
        assert "membangun bangunan berbentuk bebas" in out, misi


def test_teks_misi_selalu_sampai_ke_model(monkeypatch):
    """Header yang hilang berarti model tahu ada aturan tapi tidak tahu misinya."""
    out = _prompt(monkeypatch, "survive di pulau ini")
    assert "survive di pulau ini" in out


# --- pagar di sisi eksekusi (prompt saja tidak cukup) --------------------

def test_goal_done_tidak_mengeluarkannya_dari_arah_tetap(monkeypatch):
    """Model tetap bisa mengeluarkan tag yang dilarang. Kalau itu terjadi di
    tengah siaran solo, akibatnya dia meninggalkan dunia dan stream jadi diam."""
    import arti_bridge as b

    # Tes unit harus independen dari config_local mesin. Bohan sengaja
    # mematikan Minecraft untuk sesi non-game; jalur eksekusi yang diuji di
    # sini tetap perlu dinyalakan secara lokal di dalam tes.
    monkeypatch.setitem(b.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(b, "add_to_history", lambda *a, **k: None)
    b._set_minecraft_goal("survive")

    dipanggil = []
    monkeypatch.setattr(b, "_complete_minecraft_goal",
                        lambda: dipanggil.append(True))
    b._execute_mc_tag({"cmd": "goal_done"})
    assert dipanggil == [], "goal_done tidak boleh dieksekusi untuk arah tetap"

    b._set_minecraft_goal("cari stronghold")
    b._execute_mc_tag({"cmd": "goal_done"})
    assert dipanggil == [True], "misi biasa harus tetap bisa diselesaikan"


def test_flag_ikut_berubah_saat_misi_diganti(monkeypatch):
    """Ganti dari survive ke misi biasa harus MENGEMBALIKAN garis finisnya —
    flag yang nyangkut bikin misi berikutnya tidak bisa diselesaikan."""
    import arti_bridge as b

    monkeypatch.setattr(b, "add_to_history", lambda *a, **k: None)
    b._set_minecraft_goal("survive")
    assert b._minecraft_goal_terus
    b._set_minecraft_goal("cari diamond")
    assert not b._minecraft_goal_terus
    b._set_minecraft_goal("")
    assert not b._minecraft_goal_terus

"""Tests kamera penonton — klien Minecraft terpisah yang men-spectate Arti.

POV siaran memakai klien asli, karena renderer web itu alat debug pengembang bot
(tanpa animasi, tanpa siang-malam, mesh mob rusak, tanpa indikator blok pecah —
semuanya gratis di klien asli).

Satu-satunya kelemahan jalur itu yang WAJIB ditutup: spectate LEPAS begitu
targetnya mati, dan Arti bisa mati berkali-kali dalam beberapa menit.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402
import arti_spectator as sp  # noqa: E402
import arti_bridge as bridge  # noqa: E402

SRC = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
CFG = {"minecraft_spectator_name": "CamBot", "minecraft_bot_name": "arti_berarti"}


# --- kill switch ----------------------------------------------------------

def test_off_unless_a_camera_is_named():
    """Nama kosong = tidak ada klien kamera = tidak ada yang perlu dijaga.
    Satu setelan, tidak bisa setengah-nyala."""
    assert sp.is_enabled(CFG) is True
    assert sp.is_enabled({}) is False
    assert sp.is_enabled({"minecraft_spectator_name": "   "}) is False
    assert sp.is_enabled(None) is False
    assert sp.build_commands({}) == []
    # Yang dijaga adalah default BAWAAN di source — bukan CONFIG hasil merge.
    # config_local sebuah setup boleh mengisinya (kalau punya klien kamera);
    # yang tidak boleh adalah repo mengirim fitur ini menyala.
    assert '"minecraft_spectator_name": "",' in SRC, "default WAJIB mati di source"


def test_camera_cannot_be_arti_herself():
    """Menyuruh Arti men-spectate dirinya sendiri = kamera hilang tanpa pesan."""
    assert sp.build_commands(
        {"minecraft_spectator_name": "arti_berarti",
         "minecraft_bot_name": "arti_berarti"}) == []
    # '@' yang sering ikut tersalin tidak boleh membuat perbandingan meleset
    assert sp.build_commands(
        {"minecraft_spectator_name": "@arti_berarti",
         "minecraft_bot_name": "arti_berarti"}) == []


# --- perintah -------------------------------------------------------------

def test_commands_order_and_shape():
    """`spectate` diabaikan diam-diam kalau pemainnya belum spectator, jadi
    gamemode HARUS lebih dulu. Dan `spectate` wajib atas nama si penonton —
    dari console polos server menolaknya (console bukan entitas)."""
    c = sp.build_commands(CFG)
    assert c == ["clear CamBot",
                 "gamemode spectator CamBot",
                 "execute as CamBot run spectate arti_berarti"]


def test_names_are_normalized():
    c = sp.build_commands({"minecraft_spectator_name": " @CamBot ",
                           "minecraft_bot_name": " arti_berarti "})
    assert c[0] == "clear CamBot"
    assert c[1] == "gamemode spectator CamBot"
    assert c[2].endswith("spectate arti_berarti")


# --- kapan perlu dikunci ulang -------------------------------------------

def test_respawn_and_spawn_trigger():
    """respawn = pemicu utama. `death` sendiri TIDAK cukup: saat itu dia belum
    ada di dunia lagi, jadi mengunci ulang di detik kematian pasti gagal."""
    assert sp.should_resync({"ev": "respawn"}, "overworld")[0] is True
    assert sp.should_resync({"ev": "spawned"}, None)[0] is True
    assert sp.should_resync({"ev": "death"}, "overworld")[0] is False


def test_dimension_change_triggers_but_steady_state_does_not():
    """Pindah dimensi juga melepas kamera dan tidak punya event sendiri —
    dibaca dari perubahan `dim` di status."""
    perlu, dim = sp.should_resync({"ev": "status", "dim": "overworld"}, None)
    assert perlu is False and dim == "overworld", "status pertama cukup dicatat"
    assert sp.should_resync({"ev": "status", "dim": "overworld"}, "overworld") == (
        False, "overworld")
    perlu, dim = sp.should_resync({"ev": "status", "dim": "the_nether"}, "overworld")
    assert perlu is True and dim == "the_nether"


def test_noisy_events_never_trigger():
    """Kalau tiap event memicu perintah, kameranya menyentak terus di depan
    penonton — lebih buruk daripada lepas sesekali."""
    for ev in ({"ev": "hurt"}, {"ev": "chat"}, {"ev": "roam_leg"},
               {"ev": "task_done"}, {"ev": "low_health"}, {"ev": "status"},
               {}, None, "bukan dict"):
        assert sp.should_resync(ev, "overworld")[0] is False, ev


def test_burst_is_collapsed_into_one_command():
    """Server mengirim respawn + status beruntun dalam milidetik yang sama."""
    assert sp.cooled(0.0, 100.0) is True, "0.0 = BELUM PERNAH, bukan barusan"
    assert sp.cooled(100.0, 100.5) is False
    assert sp.cooled(100.0, 103.0) is True


# --- membaca balasan server ----------------------------------------------

def test_server_rejection_is_detected():
    """Server membalas kalimat error dengan status yang sama seperti sukses —
    satu-satunya penanda adalah isinya."""
    assert sp.result_failed(["", "Now spectating arti_berarti"]) == ""
    assert "No player was found" in sp.result_failed(["No player was found", ""])
    assert sp.result_failed(["Unknown or incomplete command"])
    assert sp.result_failed([]) == "" and sp.result_failed(None) == ""


# --- wiring ke bridge -----------------------------------------------------

def test_runner_calls_the_camera_hook_for_every_event():
    """Hook terpisah dari refleks: kegagalan salah satu tidak menyeret yang
    lain."""
    seen = []
    runner = am.MinecraftRunner(
        {}, {"spectator": lambda ev: seen.append(ev.get("ev")),
             "queue_reaction": lambda t: None},
        open_proc=lambda: None,
    )
    for line in ('{"ev": "respawn"}', '{"ev": "status", "dim": "overworld"}',
                 'bukan json'):
        runner._handle_line(line)
    assert seen == ["respawn", "status"], seen


def test_camera_hook_failure_never_kills_the_reader(capsys):
    runner = am.MinecraftRunner(
        {}, {"spectator": lambda ev: 1 / 0, "queue_reaction": lambda t: None},
        open_proc=lambda: None,
    )
    runner._handle_line('{"ev": "respawn"}')      # tidak boleh melempar
    assert "hook gagal" in capsys.readouterr().out


def test_bridge_registers_the_hook():
    i = SRC.index("def _start_minecraft_runner")
    blok = SRC[i:SRC.index("\ndef ", i + 10)]
    # 2026-08-10: hook kamera kini lewat hub (takdir + kamera satu pintu);
    # niat tesnya tetap — event bot HARUS sampai ke jalur spectator.
    assert (chr(34) + 'spectator' + chr(34) + ': _mc_event_hub') in blok


def test_rcon_runs_off_the_reader_thread():
    """RCON bisa menggantung, dan pemanggilnya adalah thread pembaca event.
    Menahan reader = menahan SELURUH aliran event game."""
    i = SRC.index("def _spectator_lock")
    blok = SRC[i:SRC.index("\ndef ", i + 10)]
    assert "threading.Thread(" in blok and "daemon=True" in blok
    assert "rcon(perintah)" in blok


def test_failures_warn_once_not_every_death():
    i = SRC.index("def _spectator_lock")
    blok = SRC[i:SRC.index("\ndef ", i + 10)]
    assert "_spectator_warned" in blok
    assert "_spectator_warned = False" in blok, "sukses harus mengizinkan keluhan lagi"


def test_disabled_camera_does_nothing(monkeypatch):
    dipanggil = []
    monkeypatch.setitem(bridge.CONFIG, "minecraft_spectator_name", "")
    monkeypatch.setattr(bridge, "_spectator_lock", lambda r: dipanggil.append(r))
    bridge._spectator_on_event({"ev": "respawn"})
    assert dipanggil == []


def test_enabled_camera_relocks_on_respawn(monkeypatch):
    dipanggil = []
    monkeypatch.setitem(bridge.CONFIG, "minecraft_spectator_name", "CamBot")
    monkeypatch.setattr(bridge, "_spectator_lock", lambda r: dipanggil.append(r))
    monkeypatch.setattr(bridge, "_spectator_last_ts", 0.0)
    monkeypatch.setattr(bridge, "_spectator_dim", "overworld")
    bridge._spectator_on_event({"ev": "respawn"})
    assert dipanggil == ["respawn"]
    # yang kedua dalam sedetik ditelan (respawn + status beruntun)
    bridge._spectator_on_event({"ev": "respawn"})
    assert dipanggil == ["respawn"]


def test_heartbeat_never_relocks_blindly(monkeypatch):
    """Tiap perintah spectate berpotensi menyentak kamera di depan penonton —
    itu alasan detaknya DULU dimatikan sama sekali. Sekarang detaknya nyala
    tapi cuma MEMERIKSA (baca NBT, tidak mengganggu); kunci ulang hanya kalau
    kamera memang sudah lepas."""
    i = SRC.index('_spectator_check()')
    j = SRC.index('def _spectator_check')
    blok = SRC[j:SRC.index(chr(10) + "def ", j + 10)]
    assert "gamemode_lepas" in blok, "detak mengunci tanpa memeriksa dulu"
    assert "_spectator_lock" in blok
    dipanggil = []
    monkeypatch.setitem(bridge.CONFIG, "minecraft_spectator_name", "CamBot")
    monkeypatch.setitem(bridge.CONFIG, "minecraft_spectator_heartbeat_sec", 0.0)
    monkeypatch.setattr(bridge, "_spectator_lock",
                        lambda r, **k: dipanggil.append(r))
    monkeypatch.setattr(bridge, "_spectator_dim", "overworld")
    monkeypatch.setattr(bridge, "_spectator_last_ts", 1.0)
    for _ in range(5):
        bridge._spectator_on_event({"ev": "status", "dim": "overworld"})
    assert dipanggil == []


# --- pemulihan otomatis (2026-08-07) --------------------------------------

def test_camera_bag_is_emptied_on_every_relock():
    """Kalau kamera sempat lepas ke survival dia MEMUNGUT barang — kejadian
    2026-08-07, 8 barang. Barang di tas kamera muncul sebagai ikon MELAYANG di
    layar "[recorded remark omitted]", karena resource pack menghapus kotak latarnya."""
    cmds = sp.build_commands({"minecraft_spectator_name": "CamBot",
                           "minecraft_bot_name": "arti_berarti"})
    assert cmds[0] == "clear CamBot"
    assert cmds.index("clear CamBot") < next(
        i for i, c in enumerate(cmds) if "spectate" in c)


def test_clear_only_ever_targets_the_camera():
    """Salah sasaran = seluruh isi tas Arti lenyap di depan penonton."""
    for c in sp.build_commands({"minecraft_spectator_name": "CamBot",
                             "minecraft_bot_name": "arti_berarti"}):
        if c.startswith("clear "):
            assert c.split()[1] == "CamBot"


def test_heartbeat_is_on_by_default():
    """Kunci ulang lewat event cuma menangkap respawn & pindah dimensi. Kamera
    bisa lepas karena sebab lain (gamemode diubah, relog, mati) dan dulu TIDAK
    ADA yang mengembalikannya — untuk siaran AFK seharian itu berarti layar
    mati sampai Bohan sadar sendiri. Terjadi 2026-08-07."""
    import re
    m = re.search(r'"minecraft_spectator_heartbeat_sec":\s*([\d.]+)', SRC)
    assert m and float(m.group(1)) > 0, "detak jantung kamera mati lagi"


def test_a_relogged_camera_is_relocked():
    """Kamera relog: mode spectator-nya ikut TERSIMPAN tapi target tontonannya
    hilang. Tidak terlihat dari gamemode, jadi harus dari event join."""
    cfg = {"minecraft_spectator_name": "CamBot", "minecraft_bot_name": "arti_berarti"}
    assert sp.should_resync({"ev": "player_join", "name": "CamBot"}, "overworld", cfg)[0]
    assert not sp.should_resync({"ev": "player_join", "name": "orang"}, "overworld", cfg)[0]


def test_an_unreadable_gamemode_is_not_treated_as_loose():
    """Salah baca -> kunci ulang -> kamera menyentak di depan penonton. Lebih
    baik diam saat RCON gagal atau pemainnya offline."""
    assert sp.gamemode_lepas("CamBot has the following entity data: 3") is False
    assert sp.gamemode_lepas("CamBot has the following entity data: 0") is True
    for buruk in ("", None, "No entity was found", "error"):
        assert sp.gamemode_lepas(buruk) is False, buruk


# --- mode ORBIT (2026-08-09 sore, diverifikasi di server nyata) ----------

def test_orbit_offset_melingkar_dan_periodik():
    """Jarak datar konstan = radius; satu period penuh kembali ke titik awal.
    Terverifikasi live: jarak kamera-arti stabil 5.5 sepanjang putaran."""
    import math
    import arti_spectator as sp
    for t in (0, 7, 33, 74.9):
        dx, dy, dz = sp.orbit_offset(t, 5.5, 75, 2.5)
        assert abs(math.hypot(dx, dz) - 5.5) < 1e-6
        assert dy == 2.5
    a = sp.orbit_offset(3, 5.5, 75, 2.5)
    b = sp.orbit_offset(3 + 75, 5.5, 75, 2.5)
    assert all(abs(x - y) < 1e-6 for x, y in zip(a, b))


def test_orbit_command_relatif_dan_menghadap_arti():
    """`execute at` = ikut posisi & DIMENSI Arti tiap tick; saat dia mati,
    entity-nya hilang dan perintahnya gagal diam-diam — begitu respawn, kamera
    nempel lagi sendiri (terverifikasi live: jarak balik ke 5.5). Ini yang
    menggantikan /spectate: tidak ada kunci yang bisa putus."""
    import arti_spectator as sp
    cfg = {"minecraft_bot_name": "arti_berarti",
           "minecraft_spectator_name": "CamBot"}
    cmd = sp.orbit_command(cfg, 5.5, 2.5, 0.0)
    assert cmd.startswith("execute at arti_berarti run tp CamBot ~")
    assert cmd.endswith("facing entity arti_berarti eyes")
    assert sp.orbit_command({}, 1, 1, 1) == ""


def test_mode_orbit_mematikan_seluruh_jalur_spectate():
    """Dua pemegang kamera = perang tarik-menarik di depan penonton."""
    import arti_spectator as sp
    assert sp.is_orbit({"minecraft_camera_mode": "orbit"})
    assert not sp.is_orbit({"minecraft_camera_mode": "spectate"})
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    i = src.index("def _spectator_lock")
    assert "is_orbit(CONFIG)" in src[i:i + 900]
    i = src.index("def _spectator_lepas_saat_mati")
    assert "is_orbit(CONFIG)" in src[i:i + 900]
    assert "_orbit_start()" in src and "_orbit_stop_now()" in src


def test_worker_orbit_pakai_koneksi_persisten():
    """5 tp/dtk lewat rcon() biasa = 5 handshake TCP+auth per detik."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    i = src.index("def _orbit_worker")
    blok = src[i:i + 2600]
    assert "create_connection" in blok
    assert "stop.wait" in blok
    assert "gamemode spectator" in blok


# --- auto-F5 & default kamera (verdict Bohan 2026-08-09 malam) -----------

def test_default_kamera_kembali_spectate():
    """Verdict live: orbit "[recorded remark omitted]" — default spectate + auto
    F5; orbit tetap tersedia lewat config."""
    import arti_bridge as b
    assert b.CONFIG["minecraft_camera_mode"] == "spectate"
    import arti_spectator as sp
    assert not sp.is_orbit(b.CONFIG)


def test_f5_ditekan_ulang_sesudah_kunci_ulang():
    """Tiap /spectate me-reset perspektif ke first-person — F5 otomatis
    dipasang persis di titik itu. Efek visual = konfirmasi mata Bohan."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert "def _kamera_f5" in src
    i = src.index("Terkunci ke Arti lagi")
    assert "_kamera_f5" in src[i:i + 400]
    i = src.index("def _kamera_f5")
    blok = src[i:i + 2600]
    assert "PostMessageW" in blok and "0x74" in blok
    assert "minecraft_kamera_window_hint" in blok, (
        "dua window Minecraft (Bohan ikut main) tidak boleh salah sasaran")


def test_kunci_ulang_dicoba_lagi_saat_arti_masih_mati():
    """Live 23.35: dua kunci-ulang gagal "No player was found" (menembak saat
    Arti mati, belum respawn) dan TIDAK ada percobaan ulang — detak jantung
    cuma memeriksa gamemode, jadi kamera menggantung bebas. "[recorded remark omitted]" (Bohan)."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    i = src.index("def _spectator_lock")
    blok = src[i:i + 3000]
    assert "coba: int = 0" in blok
    assert '"No player" in str(gagal) and coba < 4' in blok
    assert "coba=coba + 1" in blok


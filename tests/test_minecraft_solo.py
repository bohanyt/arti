"""Tests sesi solo Arti: steering obrolan, mode roam, misi, scene OBS.

Spek Bohan 2026-08-04: Arti bisa ambil alih satu stream sendirian — nyetir
obrolan penonton balik ke game, jalan sendiri kalau Bohan tak ada, mengejar
misi yang Bohan kasih, dan pindah scene OBS otomatis.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curious as ac  # noqa: E402
import arti_minecraft as am  # noqa: E402
import arti_obs  # noqa: E402
import arti_bridge as bridge  # noqa: E402

CFG = {"minecraft_reaction_cooldown_sec": 60.0, "minecraft_mine_allowlist": []}


class _StubRunner:
    def __init__(self, task="roam"):
        self.sent: list[dict] = []
        self.stopped = False
        self.last_status = {
            "ev": "status", "health": 20, "food": 18, "task": task, "solo": True,
            "dim": "overworld", "is_night": False, "pos": {"x": 5, "y": 70, "z": 5},
            "nearby_players": [], "nearby_hostiles": [],
        }

    def is_active(self):
        return not self.stopped

    def send_command(self, cmd):
        self.sent.append(cmd)
        return True

    def stop(self):
        self.stopped = True

    def events_snapshot(self):
        return []


# --- roam / solo ----------------------------------------------------------

def test_roam_verbs_accepted_end_to_end():
    _, cmds = am.parse_mc_tags("Aku jalan-jalan dulu ya [MC: roam]", CFG)
    assert cmds == [{"cmd": "roam"}]
    assert am.encode_command({"cmd": "roam"}) == '{"cmd": "roam"}'


def test_roam_start_announced_once_per_episode():
    lim = am.ReactionLimiter()
    ev = {"ev": "roam_start", "reason": "streamer_absent"}
    first = am.map_event_to_reaction(ev, lim, 100.0, CFG)
    assert first and "sendirian" in first
    assert am.map_event_to_reaction(ev, lim, 200.0, CFG) is None
    # Bohan balik -> episode solo tutup; kalau dia pergi lagi, diumumkan lagi
    assert am.map_event_to_reaction({"ev": "roam_end"}, lim, 210.0, CFG) is None
    assert am.map_event_to_reaction(ev, lim, 220.0, CFG) is not None


def test_roam_leg_is_context_only():
    lim = am.ReactionLimiter()
    ev = {"ev": "roam_leg", "pos": {"x": 1, "y": 2, "z": 3}}
    assert am.map_event_to_reaction(ev, lim, 100.0, CFG) is None
    out = am.format_context(None, [(99.0, ev)], ttl_sec=120.0, now=100.0)
    assert "titik jelajah" in out


def test_status_note_reads_roam_as_solo():
    note = am.status_note(_StubRunner().last_status)
    assert "jelajah sendiri" in note and "sendirian" in note


def test_bot_js_has_solo_mode_and_auto_return():
    """Perilaku solo hidup di bot.js — kunci agar tidak hilang saat diedit."""
    src = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
    assert "startRoam" in src and "roam_start" in src and "roam_leg" in src
    assert "streamerHere()" in src and "roamManual" in src
    # setFollow tanpa streamer TIDAK boleh mematung
    assert "startRoam('streamer_absent')" in src


# --- misi (goal) ----------------------------------------------------------

def test_goal_set_clear_and_context(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "add_to_history", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "_minecraft_runner", _StubRunner())
    bridge._set_minecraft_goal("cari stronghold")
    try:
        out = bridge._append_minecraft_context("SYS")
        assert "[MISI DARI BOHAN] cari stronghold" in out
        assert "[MC: goal_done]" in out
        mats = bridge._initiative_materials()
        assert mats["minecraft_goal"] == "cari stronghold"
    finally:
        bridge._set_minecraft_goal("")
    assert "[MISI DARI BOHAN]" not in bridge._append_minecraft_context("SYS")


def test_goal_done_leaves_game_only_when_goal_active(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "add_to_history", lambda *a, **k: None)
    stops = []
    monkeypatch.setattr(bridge, "_stop_minecraft_runner", lambda: stops.append(1))
    # Tanpa misi aktif: tag diabaikan (anti halusinasi "aku udah nemu!")
    bridge._set_minecraft_goal("")
    bridge._execute_mc_tag({"cmd": "goal_done"})
    assert stops == []
    # Dengan misi: umumkan selesai -> keluar game (mode ngobrol)
    bridge._set_minecraft_goal("bikin rumah")
    bridge._execute_mc_tag({"cmd": "goal_done"})
    assert stops == [1]
    assert bridge._minecraft_goal == ""


def test_goal_console_commands(monkeypatch, capsys):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "add_to_history", lambda *a, **k: None)
    triggers: list[tuple] = []
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(
        "mc goal cari stronghold\nmc goal\nmc goal clear\n"
    ))
    monkeypatch.setattr(
        bridge, "queue_voice_trigger",
        lambda text, trigger_type="mic", viewer_name=None, **kw: triggers.append(text),
    )
    bridge.text_input_worker()
    out = capsys.readouterr().out
    assert triggers == []  # perintah console, bukan omongan ke Arti
    assert "Misi dipasang: cari stronghold" in out
    assert "Misi sekarang: cari stronghold" in out
    assert "Misi dikosongkan" in out
    assert bridge._minecraft_goal == ""


def test_narration_prompt_mentions_goal():
    p = ac.build_minecraft_narration_prompt(
        "HP 20/20, siang, mode jelajah sendiri, lagi sendirian",
        goal="cari stronghold", angle_idx=0,
    )
    assert "cari stronghold" in p and "Misi yang lagi kamu kejar" in p


# --- steering -------------------------------------------------------------

def test_context_tells_arti_to_steer_talk_back_to_game(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "_minecraft_runner", _StubRunner())
    out = bridge._append_minecraft_context("SYS")
    assert "[ARAHKAN OBROLAN KE GAME]" in out
    assert "LAYANI dulu" in out          # jangan cuek ke penonton
    assert "hal konkret" in out          # pivot pakai detail nyata, bukan basa-basi
    assert "Jangan dipaksakan" in out    # ada rem


def test_steering_absent_when_not_in_game(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "_minecraft_runner", None)
    assert "[ARAHKAN OBROLAN KE GAME]" not in bridge._append_minecraft_context("SYS")


# --- OBS ------------------------------------------------------------------

def test_obs_auth_matches_spec():
    """Bandingkan dengan rumus yang DIEJA ULANG dari spek, bukan output modul.

    Dokumen resmi obs-websocket v5 ("[recorded remark omitted]") tidak
    memuat nilai jadi, cuma langkahnya. Jadi langkah itu ditulis lagi di sini
    secara mandiri; kalau implementasi modul menyimpang, angkanya beda.
    Nilai contoh = contoh Hello di dokumen tsb.
    """
    import base64
    import hashlib

    password = "supersecretpassword"
    salt = "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI="
    challenge = "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="

    # Langkah 1-2: sha256(password + salt) -> base64 = "base64 secret"
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode()).digest()
    ).decode()
    # Langkah 3-4: sha256(base64 secret + challenge) -> base64 = authentication
    expected = base64.b64encode(
        hashlib.sha256((secret + challenge).encode()).digest()
    ).decode()

    assert arti_obs.build_auth(password, salt, challenge) == expected
    assert len(expected) == 44 and expected.endswith("=")  # 32 byte -> base64


def test_obs_identify_with_and_without_auth():
    hello_open = {"op": 0, "d": {"rpcVersion": 1}}
    msg = arti_obs.build_identify(hello_open, "")
    assert msg["op"] == 1 and "authentication" not in msg["d"]
    hello_auth = {"op": 0, "d": {"rpcVersion": 1,
                                 "authentication": {"salt": "s", "challenge": "c"}}}
    msg = arti_obs.build_identify(hello_auth, "pw")
    assert msg["d"]["authentication"]


def test_obs_identify_without_password_raises():
    hello_auth = {"op": 0, "d": {"rpcVersion": 1,
                                 "authentication": {"salt": "s", "challenge": "c"}}}
    try:
        arti_obs.build_identify(hello_auth, "")
    except PermissionError:
        return
    raise AssertionError("password kosong seharusnya ditolak")


def test_obs_scene_request_shape():
    req = arti_obs.build_scene_request("Arti Main")
    assert req["op"] == 6
    assert req["d"]["requestType"] == "SetCurrentProgramScene"
    assert req["d"]["requestData"] == {"sceneName": "Arti Main"}


def test_obs_switch_disabled_or_unconfigured_is_noop(capsys):
    assert arti_obs.switch_scene({"obs_scene_switch_enabled": False}, "game") is False
    arti_obs._warned.clear()
    cfg = {"obs_scene_switch_enabled": True, "obs_scene_game": ""}
    assert arti_obs.switch_scene(cfg, "game") is False
    assert "belum diisi" in capsys.readouterr().out


def test_obs_failure_never_raises(capsys):
    arti_obs._warned.clear()
    cfg = {
        "obs_scene_switch_enabled": True,
        "obs_scene_game": "Game",
        "obs_ws_url": "ws://127.0.0.1:1",  # tidak ada yang dengar
        "obs_ws_timeout_sec": 0.2,
    }
    assert arti_obs.switch_scene(cfg, "game") is False
    assert "siaran lanjut" in capsys.readouterr().out


def test_scene_for_mode_maps_both():
    cfg = {"obs_scene_game": "Main", "obs_scene_chat": "Ngobrol"}
    assert arti_obs.scene_for_mode(cfg, "game") == "Main"
    assert arti_obs.scene_for_mode(cfg, "chat") == "Ngobrol"


# --- regresi dari tes live 2026-08-05 ------------------------------------

def test_join_reports_game_mode_before_process_exists(monkeypatch):
    """Bug live: scene OBS pindah ke scene SALAH saat Arti masuk game.

    `runner.start()` cuma menyalakan thread; proses bot belum ada di baris
    berikutnya, jadi mode terbaca 'duet' bukan 'duet_game'.
    """
    seen = []
    monkeypatch.setattr(bridge, "_host_mode", False)
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: False)  # belum spawn
    monkeypatch.setattr(arti_obs, "switch_scene", lambda cfg, mode: seen.append(mode))
    bridge._apply_session_mode_change("minecraft_join", in_game=True)
    import time as _t
    for _ in range(50):
        if seen:
            break
        _t.sleep(0.02)
    assert seen == ["duet_game"], f"scene mengikuti mode salah: {seen}"


def test_hurt_without_source_is_not_called_an_attack():
    """Bug live: '[recorded remark omitted]' bikin Arti mengarang 'penyerang invisible'."""
    out = am.format_context(
        None, [(99.0, {"ev": "hurt", "source": "unknown", "health": 17})],
        ttl_sec=120.0, now=100.0,
    )
    assert "diserang" not in out
    # kondisi darah tetap ikut, tapi sebagai frasa manusia — bukan angka
    # (keluhan Bohan 2026-08-05: "[recorded remark omitted]")
    assert "jatuh" in out and "darah kepotong dikit" in out
    assert "17" not in out
    # sumber yang JELAS tetap disebut sebagai serangan
    out2 = am.format_context(
        None, [(99.0, {"ev": "hurt", "source": "creeper", "health": 5})],
        ttl_sec=120.0, now=100.0,
    )
    assert "diserang creeper" in out2


def test_bot_waits_before_claiming_streamer_absent():
    """Bug live: bot lapor 'Bohan nggak ada' padahal Bohan sudah 2 menit di dunia.

    Perbaikan pertama memakai daftar tab pemain sebagai penahan — dan audit
    2026-08-05 membuktikan itu SALAH ARAH: selama Bohan online di mana pun
    (Nether/End/jauh), penahannya tidak pernah lepas dan bot mematung
    selamanya. Acuan yang benar = kapan entity-nya TERAKHIR TERLIHAT.
    """
    src = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
    assert "STREAMER_GRACE_MS" in src
    assert "streamerSeenAt" in src, "acuan tunggu harus 'terakhir terlihat'"
    assert "wait_streamer" in src, "status tunggu terpisah dari 'idle' (anti-mematung)"
    # ticker WAJIB mencoba lagi saat menunggu, kalau tidak bot mematung selamanya
    i = src.index("currentTask === 'wait_streamer'")
    assert "setFollow()" in src[i:i + 200]


def test_wait_streamer_shown_readably_in_status():
    note = am.status_note({
        "health": 20, "food": 20, "task": "wait_streamer", "is_night": False,
        "nearby_players": [], "nearby_hostiles": [], "pos": {},
    })
    assert "nunggu dunia termuat" in note and "wait_streamer" not in note


# --- jangan bohong soal kemampuan (pertanyaan Bohan 2026-08-06) -------------

def test_goal_block_states_the_real_limits(monkeypatch):
    """Misi "bikin rumah" tidak bisa dieksekusi (Phase 2 belum ada). Tanpa
    batas yang eksplisit dia akan bercerita sudah membangun — bohong ke
    penonton, dan bisa memicu [MC: goal_done] untuk rumah yang tidak ada."""
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "add_to_history", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "_minecraft_runner", _StubRunner())
    bridge._set_minecraft_goal("[recorded remark omitted]")
    try:
        blok = bridge._append_minecraft_context("SYS")
        assert "BATAS KEMAMPUANMU" in blok
        # Nambang, craft, naruh SATU blok, dan MELAWAN musuh sudah bisa sejak
        # 2026-08-07. Yang tersisa cuma MEMBANGUN bangunan utuh — dan bedanya
        # penting: satu blok bisa, menyusun rumah belum.
        belum = blok[blok.index("BELUM BISA:"):blok.index("Jadi menambang")]
        assert "membangun bangunan berbentuk" in belum
        for x in ("menambang", "craft", "menyerang"):
            assert x not in belum, f"melarang hal yang sudah bisa: {x}"
        bisa = blok[blok.index("BISA:"):blok.index("BELUM BISA:")]
        assert "MENARUH SATU BLOK" in bisa and "MELAWAN musuh" in bisa
        assert "jangan pernah mengaku sudah membangun" in blok.lower()
        assert ("JANGAN memakai [MC: goal_done] untuk misi yang butuh MEMBANGUN"
                in blok)
    finally:
        bridge._set_minecraft_goal("")


def test_unknown_bot_command_reports_failure_not_silence():
    """mine/place/give ada di whitelist Python tapi belum ada handler-nya di
    bot.js. Dulu dibuang diam-diam -> Arti mengira aksinya jalan."""
    src = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
    i = src.index("const fn = handlers[c && c.cmd]")
    blok = src[i:i + 600]
    assert "task_failed" in blok and "belum_bisa" in blok
    import arti_minecraft as am
    ringkas = am.format_context(
        None, [(99.0, {"ev": "task_failed", "task": "mine", "reason": "belum_bisa"})],
        ttl_sec=120.0, now=100.0)
    assert "belum bisa" in ringkas.lower() and "belum_bisa" not in ringkas

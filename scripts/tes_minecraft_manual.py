"""Uji Minecraft TANPA audio/LLM — jalur produksi, mulutnya saja dicabut.

Dipakai sepanjang 2026-08-07 untuk menguji craft, naruh blok, bangun, serang,
kasih barang, dan momen "[recorded remark omitted]". Dipindahkan dari folder sementara ke sini
karena tiap fitur Minecraft baru butuh alat ini lagi.

Yang ASLI (bukan tiruan): `MinecraftRunner`, `bridge._spectator_on_event`,
`bridge._craft_panel_on_event`, seluruh config dari config_local.json.
Yang DICABUT: `queue_reaction` jadi print, `add_history` jadi no-op — jadi
tidak ada panggilan LLM dan tidak ada TTS sama sekali.

    ./venv/Scripts/python.exe scripts/tes_minecraft_manual.py
    ./venv/Scripts/python.exe scripts/tes_minecraft_manual.py diam

Kirim perintah dengan menulis satu baris JSON ke file perintah (bawaan:
$CLAUDE_JOB_DIR/tmp/perintah.txt), dibaca sekali lalu dikosongkan:

    echo {"cmd":"craft","item":"stone_pickaxe"} > perintah.txt

Env opsional:
    STREAMER=<nama>  pinjam akun lain sebagai "Bohan" (untuk uji chat in-game)
    LINGER=<detik>   tahan panel craft lebih lama supaya sempat dinilai
"""
import os, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import arti_minecraft as am
import arti_bridge as bridge

DIAM = "diam" in sys.argv

# Mode tinjau: panel ditahan lama supaya Bohan sempat menilainya. Craft itu
# sendiri cuma ~40 ms, jadi tanpa ini yang bisa dilihat cuma kedipan.
if os.environ.get("LINGER"):
    bridge.CONFIG["minecraft_craft_panel_linger_sec"] = float(os.environ["LINGER"])
    bridge.CONFIG["minecraft_craft_panel_max_sec"] = float(os.environ["LINGER"]) + 120
cfg = dict(bridge.CONFIG)
cfg["minecraft_pov_enabled"] = False
cfg["minecraft_status_interval_sec"] = 5
if os.environ.get("STREAMER"):   # uji: pinjam akun lain sbg "Bohan"
    cfg["minecraft_streamer_name"] = os.environ["STREAMER"]

import json, pathlib
# $CLAUDE_JOB_DIR hanya ada di sesi background job; sesi biasa jatuh ke
# ./tmp — dan foldernya WAJIB dibuat sendiri, kalau tidak status.json &
# tas.json gagal ditulis DIAM-DIAM (try/except di catat()) dan agen yang
# memakai harness ini kehilangan alat ukurnya tanpa tahu kenapa.
_DIR_TMP = pathlib.Path(os.environ.get('CLAUDE_JOB_DIR', '.')) / 'tmp'
_DIR_TMP.mkdir(parents=True, exist_ok=True)
BERKAS_TAS = _DIR_TMP / 'tas.json'

def catat(ev):
    if ev.get('ev') == 'status' and ev.get('inv') is not None:
        try:
            BERKAS_TAS.write_text(json.dumps(ev['inv']), encoding='utf-8')
        except Exception:
            pass
    # Status LENGKAP ikut ditulis. Tanpa ini field baru (oxygen/light/armor)
    # tidak bisa diperiksa dari luar sama sekali -- dan "tidak bisa dilihat"
    # sudah dua kali kusalahartikan sebagai "tidak jalan".
    if ev.get('ev') == 'status':
        try:
            (BERKAS_TAS.parent / 'status.json').write_text(
                json.dumps(ev), encoding='utf-8')
        except Exception:
            pass
    if ev.get("ev") in ("death", "respawn", "swim_start", "swim_end",
                        "flee_start", "flee_end", "collect_done", "ate"):
        print(f"[event] {ev.get('ev')} {str(ev)[:100]}", flush=True)
    if ev.get("ev") in ("roam_start", "swim_start") or (
            ev.get("ev") == "task_failed" and ev.get("reason") == "stuck_timeout"):
        print(f"[jalan] {str(ev)[:110]}", flush=True)
    if ev.get("ev") in ("build_start", "build_done") or (
            ev.get("ev") == "task_failed" and ev.get("task") == "bangun"):
        print(f"[bangun] {str(ev)[:150]}", flush=True)
    if ev.get("ev") in ("fight_start", "killed", "fight_lost", "fight_end") or (
            ev.get("ev") == "task_failed" and ev.get("task") == "serang"):
        print(f"[tarung] {str(ev)[:150]}", flush=True)
    if ev.get("ev") in ("gave", "give_walk") or (ev.get("ev") == "task_failed" and ev.get("task") == "give"):
        print(f"[kasih] {str(ev)[:150]}", flush=True)
    if ev.get("ev") == "placed" or (ev.get("ev") == "task_failed" and ev.get("task") == "place"):
        print(f"[naruh] {str(ev)[:150]}", flush=True)
    if ev.get("ev") in ("window_open", "window_close"):
        print(f"[JENDELA] {ev.get('ev')} {ev.get('kind','')} {ev.get('title','')}", flush=True)
    # Refleks yang dijalankan BOT sendiri. Ikut dicetak karena "[recorded remark omitted]" untuk jenis baru sudah TIGA KALI kusalahartikan sebagai "fiturnya
    # tidak jalan" -- padahal daftar cetak ini yang belum memuatnya.
    if ev.get("ev") == "refleks":
        print(f"[refleks] {str(ev)[:150]}", flush=True)
    # Bertahan hidup (2026-08-08). WAJIB ikut dicetak: tanpa baris ini log
    # DIAM untuk turun/masak/tidur, dan "[recorded remark omitted]" mudah disalahartikan
    # sebagai "fiturnya tidak jalan" — itu sudah sekali menyesatkanku.
    # GENERIK, bukan daftar putih. Daftar nama event sudah ENAM KALI tertinggal
    # dari bot.js dan tiap kali "[recorded remark omitted]" kusalahartikan sebagai
    # "fiturnya tidak jalan". Sekarang: semua event yang belum ditangani
    # kategori lain dicetak apa adanya.
    _SUDAH = {"status", "roam_start", "roam_leg", "roam_end", "swim_start",
              "swim_end", "flee_start", "flee_end", "death", "respawn",
              "collect_start", "collect_done", "ate", "build_start",
              "build_done", "fight_start", "killed", "fight_lost", "fight_end",
              "gave", "give_walk", "placed", "window_open", "window_close",
              "craft_walk", "craft_start", "crafted", "player_join",
              "player_leave", "chat", "task_failed"}
    if ev.get("ev") not in _SUDAH:
        print(f"[hidup] {str(ev)[:170]}", flush=True)
    if ev.get("ev") == "task_failed" and ev.get("task") in (
            "turun", "masak", "tidur", "tembok", "portal", "masuk_portal",
            "menara"):
        print(f"[hidup] {str(ev)[:170]}", flush=True)
    if ev.get("ev") in ("craft_walk", "craft_start", "crafted") or (
            ev.get("ev") == "task_failed" and ev.get("task") == "craft"):
        print(f"[craft] {str(ev)[:170]}", flush=True)
    # Hub (takdir + kamera) kalau ada — bridge lama cuma punya spectator.
    (getattr(bridge, "_mc_event_hub", None) or bridge._spectator_on_event)(ev)
    bridge._craft_panel_on_event(ev)   # jalur panel yang SUNGGUHAN

# Reaksi internal bridge (takdir dsb.) ikut tercetak — tanpa ini pengumuman
# takdir masuk antrean suara bridge yang di harness tidak ada konsumennya.
bridge._queue_game_reaction = lambda t: print(f"[reaksi] {t[:110]}", flush=True)

r = am.MinecraftRunner(cfg, {
    "queue_reaction": lambda t: print(f"[reaksi] {t[:110]}", flush=True),
    "add_history": lambda *a: None,
    "spectator": catat,
    "streamer_chat": lambda t: print(f"[CHAT-BALAS] {t[:110]}", flush=True),
})
# Perintah manual dari file: tulis satu baris JSON ke perintah.txt, dibaca
# sekali lalu dikosongkan. Supaya craft bisa dites tanpa mic dan tanpa LLM.
SURUH = _DIR_TMP / 'perintah.txt'
def cek_perintah():
    try:
        if not SURUH.exists():
            return
        baris = SURUH.read_text(encoding='utf-8').strip()
        SURUH.write_text('', encoding='utf-8')
        if baris:
            print(f"[suruh] {baris}", flush=True)
            r.send_command(json.loads(baris))
    except Exception as e:
        print(f"[suruh] gagal: {type(e).__name__}: {e}", flush=True)
if not r.start():
    print("gagal start"); raise SystemExit(1)
print("Arti masuk; penjaga kamera aktif.", flush=True)
if DIAM:
    time.sleep(6)
    # `stop` = diam di tempat TANPA efek status, jadi FOV kamera tidak berubah
    # (Slowness amplifier tinggi mengubah FOV — pelajaran 2026-08-07).
    r.send_command({"cmd": "stop"})
    print("disuruh DIAM (stop) — FOV tidak tersentuh.", flush=True)
try:
    while True:
        time.sleep(1)
        cek_perintah()
        if DIAM and (r.last_status or {}).get("task") not in ("idle", None, "craft"):
            r.send_command({"cmd": "stop"})   # jaga tetap diam
except KeyboardInterrupt:
    pass
finally:
    r.stop()

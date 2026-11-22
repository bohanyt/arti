"""Curious proactive trigger — comment on screen when idle."""

from __future__ import annotations

import time

import arti_benang
import arti_renungan
import arti_screen_context as sc
import arti_vision_client
from arti_reply_policy import inisiatif_max_sentences
from arti_screen_privacy import screen_privacy, bind_context_epoch

_last_curious_ts = 0.0
_last_interval_check_ts = 0.0
_recent_hooks: list[str] = []
_MAX_RECENT_HOOKS = 3

_GENERIC_HOOK_MARKERS = (
    "streamer sedang",
    "layar menampilkan",
    "kayaknya lagi",
    "sedang melihat",
    "ngecek sesuatu",
)


_init_last_fire_ts = 0.0
_init_streak = 0

# Bahan inisiatif yang SUDAH dipakai sesi ini — anti "arti looping" (topik yang
# sama bisa diangkat berulang dalam beberapa menit kalau bullet acak tidak
# dilacak). Ring kecil: setelah 12 topik lain, bahan lama boleh muncul lagi.
_used_init_materials: list[str] = []
_MAX_USED_INIT_MATERIALS = 12
# Summary scouter yang terakhir diangkat. Disimpan TERPISAH dari ring di atas:
# teks summary panjang membuat _same_topic_as_used memblok bullet memori yang
# kebetulan berbagi satu kata (REVIEW_BRAIN_FIXES N2).
_last_scouter_summary_used = ""
_SCOUTER_KEY = "<scouter_summary>"

# ---- GERAKAN DIALOG (16 Agu 2026) --------------------------------------------
# Kalau prompt cuma mengenal dua gerakan (beropini, bertanya), giliran curious
# jatuh ke satu template reporter yang membuka dengan pola yang sama. Manusia
# ngobrol pakai banyak gerakan. Satu gerakan disuntikkan per giliran, dirotasi
# acak tanpa ulang sampai satu siklus habis (pola yang sama dengan fallback
# in-character yang sudah terbukti menghapus pengulangan).
#
# Daftar & larangannya PARAMETER, bukan hardcode (permintaan Bohan 16 Agu):
# timpa `curious_gerakan_dialog` / `curious_larangan` dari config_local.json —
# tanpa menyentuh kode. List kosong = fitur mati (kill switch).
# Gerakan mengubah BENTUK omongan; topiknya tetap dari momen (layar/hook/
# obrolan barusan), dan tiap suntikan diberi pintu keluar "momen menang".
DEFAULT_GERAKAN_DIALOG: list[str] = [
    "Godain/ledek ringan yang nyambung sama momen barusan — pedas tapi sayang.",
    "Boleh punya pendirian sendiri bila nyambung, sambil tetap mendengar maksud lawan bicara.",
    "Ungkit satu hal dari obrolan tadi atau sesi sebelumnya yang nyambung — tunjukkan kamu ingat.",
    "Bagikan pengalaman/koleksi momenmu sendiri yang nyambung — bukan tentang streamer.",
    "Kalau ada chat penonton yang relevan dengan momen ini, boleh sambung langsung "
    "ke orangnya; nama dan isi pesannya harus cocok. Pengamatan layar bukan pesan darinya.",
    "Pilih reaksi yang cocok dengan niat dan suasana lawan bicara; ketidakpastian boleh ditanyakan, jangan mengarang sebab kejadian.",
    "Kalau lawan bicara masih ingin melanjutkan, boleh sambung cerita yang menggantung; beri ruang saat lelah.",
]
DEFAULT_LARANGAN: str = (
    "Jangan merangkum aktivitas streamer — semua orang sudah melihatnya; "
    "langsung timpali, ledek, atau sambungkan.\n"
    "Jangan membuka kalimat dengan nama streamer terus-terusan.\n"
    "Jangan mengklaim emosi pakai kata kaleng (deg-degan, penasaran) — "
    "tunjukkan lewat isi omonganmu. Helaan napas 'hhh' justru BOLEH — "
    "itu bagian suaramu."
)
_gerakan_sisa: list[str] = []


def _butuh_penonton(gerakan: str) -> bool:
    """Gerakan yang menyapa chat tidak boleh keluar saat penonton nol —
    bug ngoceh-ke-hantu jangan balik lewat pintu lain."""
    g = gerakan.lower()
    return "chat" in g or "penonton" in g


def gerakan_dialog(rng=None, ada_penonton: bool = True, config: dict | None = None) -> str:
    """Satu instruksi gerakan untuk giliran ini; acak tanpa ulang per siklus.

    TEPAT SATU tarikan `rng()` per panggilan. Versi pertama memakai shuffle
    (6 tarikan tiap awal siklus) dan itu meledakkan tes lama yang menyodorkan
    iterator rng berjatah pas — StopIteration di test_initiative.
    Return "" kalau daftarnya dikosongkan (fitur dimatikan dari config).
    """
    global _gerakan_sisa
    import random as _random
    if rng is None:
        rng = _random.random
    # CONFIG produksi menaruh kuncinya = None ("pakai default") — .get() dengan
    # default TIDAK menolong karena kuncinya ADA. Aturan proyek no. 7: uji
    # terhadap CONFIG produksi, bukan dict kosong — inilah kenapa.
    moves = (config or {}).get("curious_gerakan_dialog")
    if moves is None:
        moves = DEFAULT_GERAKAN_DIALOG
    if not moves:
        return ""
    if not _gerakan_sisa:
        _gerakan_sisa = [str(m) for m in moves]
    kandidat = [
        i for i, g in enumerate(_gerakan_sisa)
        if ada_penonton or not _butuh_penonton(g)
    ]
    if not kandidat:
        _gerakan_sisa = [str(m) for m in moves if not _butuh_penonton(str(m))]
        if not _gerakan_sisa:
            return ""
        kandidat = list(range(len(_gerakan_sisa)))
    pilih = kandidat[int(rng() * len(kandidat)) % len(kandidat)]
    return _gerakan_sisa.pop(pilih)


def reset_session() -> None:
    global _last_curious_ts, _last_interval_check_ts, _recent_hooks
    global _init_last_fire_ts, _init_streak
    _last_curious_ts = 0.0
    _last_interval_check_ts = 0.0
    _recent_hooks.clear()
    _init_last_fire_ts = 0.0
    _init_streak = 0
    _used_init_materials.clear()
    global _last_scouter_summary_used
    _last_scouter_summary_used = ""
    _gerakan_sisa.clear()
    arti_benang.reset_session()
    arti_renungan.reset_session()


def _vision_effective(config: dict) -> bool:
    """Manual toggle OR scouter auto-window (mirrors bridge is_vision_active)."""
    if not screen_privacy.allows_screen():
        return False
    if not config.get("vision_enabled", config.get("screen_context_enabled", False)):
        return False
    if config.get("vision_runtime_on", False):
        return True
    return time.time() < float(config.get("vision_auto_until", 0))


def _hook_too_similar(new_hook: str, old_hook: str) -> bool:
    a = new_hook.lower().strip()
    b = old_hook.lower().strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    aw = set(a.split())
    bw = set(b.split())
    if not aw or not bw:
        return False
    return len(aw & bw) / max(len(aw | bw), 1) >= 0.7


def _is_generic_hook(hook: str) -> bool:
    h = hook.lower().strip()
    if len(h) < 20:
        return True
    return any(m in h for m in _GENERIC_HOOK_MARKERS)


def should_fire(
    config: dict,
    *,
    brain_busy: bool,
    tts_playing: bool,
    ptt_active: bool,
    yt_cooling: bool = False,
    yt_queue_pending: bool = False,
    streamer_recent: bool = False,
    stardew_live: bool = False,
) -> bool:
    """True if curious may queue a proactive turn.

    `yt_queue_pending` = a viewer chat is waiting (queued or held, see
    viewer_chat_waiting); `streamer_recent` = the same anti-interrupt gap as
    initiative. Curious is OFF while Stardew is live (owner 2026-10-21).
    """
    if not config.get("curious_enabled", False):
        return False
    if stardew_live:
        return False
    if not _vision_effective(config):
        return False
    if brain_busy or tts_playing or ptt_active or yt_cooling:
        return False
    if yt_queue_pending:
        return False
    if streamer_recent:
        return False

    now = time.time()
    interval = float(config.get("curious_interval_sec", 75))
    cooldown = float(config.get("curious_cooldown_sec", 120))

    global _last_interval_check_ts
    if now - _last_interval_check_ts < interval:
        return False

    if _last_curious_ts and (now - _last_curious_ts) < cooldown:
        return False

    manual_vision = bool(config.get("vision_runtime_on", False))
    scouter = config.get("scouter_last_result") or {}
    curious_worthy = bool(scouter.get("curious_worthy", False))
    hook = (scouter.get("curious_hook") or "").strip()

    if not manual_vision and not curious_worthy:
        return False

    if hook and _is_generic_hook(hook):
        return False

    # Hook yang mengutip log bridge sendiri = backstage (live 2026-08-02:
    # "[recorded remark omitted]" jadi seed dan Arti narasi dapurnya).
    if hook and sc.looks_like_bridge_log(hook):
        return False

    if hook and any(_hook_too_similar(hook, prev) for prev in _recent_hooks):
        return False

    if config.get("curious_requires_fresh_screen", True):
        if not arti_vision_client.is_vision_fresh(config):
            return False

    latest = sc.screen_ring.latest()
    if not latest or not latest.scene.strip():
        return False

    _last_interval_check_ts = now
    return True


def mark_fired(config: dict | None = None) -> None:
    global _last_curious_ts, _recent_hooks
    _last_curious_ts = time.time()
    cfg = config or {}
    scouter = cfg.get("scouter_last_result") or {}
    hook = (scouter.get("curious_hook") or "").strip()
    if hook:
        _recent_hooks.append(hook)
        if len(_recent_hooks) > _MAX_RECENT_HOOKS:
            _recent_hooks.pop(0)


# --------------------------------------------------------------------------- #
# INISIATIF — buka topik sendiri saat hening (Fitur A, rencana v0.7)
#
# Jalur SAUDARA curious, bukan pengganti: curious butuh layar menarik (vision +
# scouter curious_worthy), inisiatif justru hidup saat TIDAK ada apa-apa —
# 30 detik tanpa chat dan tanpa omongan streamer, Arti membuka topik sendiri
# dari memorinya / penonton yang hadir / obrolan terakhir.
# --------------------------------------------------------------------------- #


def _policy(mode: str, config: dict, minecraft: bool = True) -> dict:
    """Kebijakan proaktif mode ini (import lokal — modul murni, nol siklus)."""
    import arti_session_mode  # noqa: PLC0415

    return arti_session_mode.mode_policy(mode, config, minecraft=minecraft)


def is_dormant(
    config: dict, *, now: float, last_human_ts: float, mode: str = "duet",
    minecraft: bool = True,
) -> bool:
    """Ruangan mati total? (revisi spek Bohan 2026-08-03)

    BERGANTUNG MODE (spek Bohan 2026-08-04): aturan "[recorded remark omitted]" hanya
    berlaku di mode `duet` — Bohan hadir dan tidak ada acara. Begitu dia AFK
    (`host_chat`) atau Arti lagi main game (`duet_game`/`host_game`), Arti-lah
    acaranya dan dia HARUS terus bicara. Kebijakannya di arti_session_mode.

    Spek lama "[recorded remark omitted]" ternyata
    kebablasan: live seharian 3/8 ada ~1 jam tanpa satu pun viewer dan Arti
    monolog terus. Kini: tanpa tanda kehidupan manusia (chat viewer / suara
    streamer) selama initiative_dormant_after_idle_sec, SEMUA jalur proaktif
    (inisiatif + curious layar) tidur — bangun otomatis begitu ada chat
    masuk / streamer bersuara (timestamp-nya maju sendiri).
    <= 0 = fitur mati (perilaku lama). last_human_ts 0/None = belum ada
    data (startup) -> jangan blokir. Jumlah penonton NAIK juga dihitung
    tanda kehidupan (bridge bump timestamp via telemetri arti_yt_viewers);
    spek final Bohan: turun + 5 menit tanpa chat/mic = off.

    `minecraft` False = game-nya Stardew: `duet_game` ikut dormansi duet
    (`stardew_dormancy_enabled`, REVIEW_BRAIN F3) — lihat mode_policy."""
    if not _policy(mode, config, minecraft)["dormancy_applies"]:
        return False
    dormant_sec = float(config.get("initiative_dormant_after_idle_sec", 300.0))
    if dormant_sec <= 0 or not last_human_ts:
        return False
    return now - float(last_human_ts) > dormant_sec


# Layar yang isinya "tidak ada apa-apa" — bukan bahan obrolan: layar gelap
# yang dibahas berulang-ulang bukan topik.
_BORING_SCREEN_MARKERS = (
    "layar gelap", "layar hitam", "layar kosong", "layar hanya",
    "latar belakang hitam", "latar belakang gelap", "background hitam",
    "tidak menampilkan apa", "tidak ada yang ditampilkan",
    "hanya menampilkan latar",
)


def is_boring_screen_hook(text: str) -> bool:
    """Hook/scene layar kosong/gelap = BUKAN topik — jangan jadi bahan."""
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in _BORING_SCREEN_MARKERS)


def should_fire_initiative(
    config: dict,
    *,
    now: float,
    last_arti_ts: float,
    last_streamer_ts: float,
    tts_playing: bool,
    brain_busy: bool,
    ptt_active: bool,
    provider_fail_until: float = 0.0,
    last_human_ts: float = 0.0,
    mode: str = "duet",
    minecraft: bool = True,
    viewer_waiting: bool = False,
) -> bool:
    """Boleh buka topik sendiri sekarang? Spek final Bohan 2026-08-02:

    1. `initiative_quiet_sec` (30) sejak ARTI terakhir bicara — bales chat,
       bales streamer, atau monolognya sendiri, semuanya menghitung.
    2. `initiative_streamer_gap_sec` (5) sejak streamer terakhir BERSUARA
       APAPUN di mic (termasuk omongan pasif) — pagar anti-motong: "[recorded remark omitted]".
    Chat penonton yang ngobrol sendiri TIDAK ngeblok — kalau ruangan kosong,
    justru Arti yang harus banyak ngomong.

    Cadence: `initiative_backoff_base_sec` <= 0 (setelan Bohan) = FLAT tiap 30
    detik; > 0 = eskalasi eksponensial (dobel, cap backoff_max) untuk yang mau
    Arti makin kalem di ruangan kosong.

    MODE ACARA (`mode` != "duet", spek Bohan 2026-08-04): saat Arti pegang
    siaran atau lagi main game, jeda dipakai dari kebijakan mode (lebih rapat),
    dormansi tidak berlaku, dan eskalasi backoff dilewati — pembawa acara tidak
    boleh makin diam justru saat dia yang harus mengisi. Pagar anti-motong
    streamer, TTS/busy/PTT, dan rehat provider TETAP dihormati di semua mode.

    `viewer_waiting` (owner 2026-10-21): chat VIEWER belum dijawab — antre atau
    tertahan keran/cooldown (viewer_chat_waiting) — = jangan bicara di atasnya.
    Renungan numpang slot ini, jadi ikut tertahan.

    `minecraft` False = game-nya Stardew: jeda/dormansi/backoff dari
    mode_policy (REVIEW_BRAIN F3), bukan tempo narasi Minecraft.
    """
    global _init_streak
    if not config.get("initiative_enabled", False):
        return False
    if tts_playing or brain_busy or ptt_active:
        return False
    if viewer_waiting:
        return False
    # Rehat pasca semua-provider-gagal: tanpa rehat, tembakan sia-sia tiap
    # 30 dtk memperparah kuota.
    if now < float(provider_fail_until or 0.0):
        return False
    # Detektor kehidupan: ruangan mati total = tidur (0.0 = data belum ada).
    # Di mode acara dormansi dilewati — lihat docstring is_dormant.
    policy = _policy(mode, config, minecraft)
    if is_dormant(config, now=now, last_human_ts=last_human_ts, mode=mode,
                  minecraft=minecraft):
        return False
    quiet_sec = float(policy["proactive_gap_sec"])
    if now - float(last_arti_ts or 0.0) < quiet_sec:
        return False
    if now - float(last_streamer_ts or 0.0) < float(
        config.get("initiative_streamer_gap_sec", 5.0)
    ):
        return False
    if _init_last_fire_ts and last_streamer_ts > _init_last_fire_ts:
        _init_streak = 0
    # Rem darurat (regresi live 2026-08-02 sore): kalau turn inisiatif MATI
    # (exception/skip) Arti tidak pernah bicara -> last_arti_ts beku -> gate
    # quiet_sec lolos terus -> 180 tembakan dalam 5 menit. Cadence flat tetap
    # wajib berjarak quiet_sec dari TEMBAKAN terakhir, apapun nasib turn-nya.
    if _init_last_fire_ts and now - _init_last_fire_ts < quiet_sec:
        return False
    base = float(config.get("initiative_backoff_base_sec", 180.0))
    if not policy["backoff_applies"]:
        base = 0.0  # pembawa acara tidak boleh makin diam saat dia yang isi
    if _init_streak > 0 and base > 0:
        cap = float(config.get("initiative_backoff_max_sec", 720.0))
        backoff = min(base * (2 ** (_init_streak - 1)), cap)
        if now - _init_last_fire_ts < backoff:
            return False
    return True


def mark_initiative_fired(now: float | None = None) -> None:
    global _init_last_fire_ts, _init_streak
    _init_last_fire_ts = float(now if now is not None else time.time())
    _init_streak += 1


def viewer_chat_waiting(
    config: dict,
    *,
    now: float,
    yt_queued: bool,
    last_viewer_chat_ts: float,
    last_yt_trigger_ts: float,
) -> bool:
    """Ada chat VIEWER yang menunggu jawaban? (owner 2026-10-21)

    - `yt_queued`: yt_chat masih di antrean bridge (buffer atau queue).
    - Tertahan: chat yang cuma masuk history (keran santai, cooldown pribadi,
      atau di-skip karena Arti sibuk) = chat viewer terakhir LEBIH BARU dari
      trigger yt_chat terakhir yang diterima. Audit: 28 giliran proaktif S1
      menembak di atas chat tertahan.
    Chat tertahan berhenti menahan setelah `proactive_viewer_wait_sec`
    (default 60 = keran santai): tidak ada yang akan menjawabnya otomatis,
    jadi tanpa batas ini satu chat bisa membungkam siaran selamanya.
    Semua ts = jam dinding (time.time()); 0 = belum ada.
    """
    if yt_queued:
        return True
    chat_ts = float(last_viewer_chat_ts or 0.0)
    if chat_ts <= 0 or chat_ts <= float(last_yt_trigger_ts or 0.0):
        return False
    wait = float((config or {}).get("proactive_viewer_wait_sec", 60.0))
    return now - chat_ts < wait


# Bullet learning yang MENCERITAKAN sistem sendiri ("Sistem mendorong inisiatif
# mengangkat fakta X") — kalau jadi bahan inisiatif, Arti buka topik meta yang
# bikin penonton bingung (live sore2 2026-08-02: "[recorded remark omitted]").
# Marker sengaja sempit: fakta stream normal ("[recorded remark omitted]") aman.
_META_BULLET_MARKERS = (
    "sistem mendorong",
    "inisiatif",
    "scouter",
    "kurator",
    "trigger",
    "prompt",
    "provider",
    "fallback",
    "vault rag",
    # 2026-08-03: kurator sempat menyimpan "[recorded remark omitted]" & "[recorded remark omitted]" sebagai stream fact — dapur bridge bukan ilmu.
    "whisper",
    "bridge session",
)


def is_meta_learning_bullet(line: str) -> bool:
    """Bullet learning bercerita tentang sistem Arti sendiri? (bukan bahan topik)."""
    low = (line or "").lower()
    if not low.strip():
        return True
    if any(m in low for m in _META_BULLET_MARKERS):
        return True
    return sc.looks_like_bridge_log(line)


# Kata umum yang muncul di hampir semua bullet — bukan penanda topik.
_TOPIC_STOPWORDS = frozenset({
    "bohan", "arti", "stream", "fact", "live", "chat", "viewer", "penonton",
    "streamer", "kemarin", "hari", "yang", "untuk", "dengan", "karena",
    "adalah", "sedang", "sudah", "belum", "masih", "juga", "atau", "pada",
    "livestream", "sesi", "catatan", "bilang", "sempat",
})


def _topic_words(text: str) -> set[str]:
    """Kata bermakna (len>=4, non-stopword) — penanda topik bullet/bahan."""
    import re as _re

    low = _re.sub(r"^-?\s*\[\d{4}-\d{2}-\d{2}\]\s*", "", (text or "").strip().lower())
    low = low.replace("stream fact:", " ")
    return {
        w for w in _re.findall(r"[a-z0-9!]{4,}", low) if w not in _TOPIC_STOPWORDS
    }


def _same_topic_as_used(candidate: str) -> bool:
    """Live pagi 2026-08-03: inisiatif #2 & #3 dua-duanya soal 'nasi' — bullet
    BEDA teks, topik sama (kurator menghasilkan varian duplikat satu fakta).
    Anti-ulang teks-persis tidak cukup: satu kata topik yang sama dengan bahan
    yang sudah dipakai sesi ini = anggap topik sama, skip."""
    cand = _topic_words(candidate)
    if not cand:
        return False
    for used in _used_init_materials:
        if cand & _topic_words(used):
            return True
    return False


# Sudut komentar game — DIROTASI bergilir (bukan acak) supaya dalam satu sesi
# main semua sisi kebagian: kejadian, aksi sekarang, rencana, suasana, ajakan
# ngobrol. Acak murni sempat bikin Arti mengulang sudut yang sama beruntun.
_MC_NARRATION_ANGLES = (
    "Komentari apa yang BARUSAN terjadi ke kamu di game (lihat daftar kejadian "
    "di blok [DI MINECRAFT]) — reaksikan kayak orang yang lagi ngalamin, bukan "
    "melapor.",
    "Reaksikan apa yang lagi kamu lakuin SEKARANG — gimana rasanya, bukan "
    "laporan langkah.",
    "Umumkan rencanamu BERIKUTNYA — mau ngapain habis ini, dan kenapa.",
    "Reaksikan suasana di sekitarmu: tempatnya, siang/malam, bahaya yang "
    "kelihatan, atau kondisi badanmu (HP/lapar) kalau memang lagi genting.",
    "Ajak ngobrol soal permainan ini — lempar celetukan ke penonton atau ke "
    "Bohan tentang apa yang lagi terjadi di dunia game.",
)
_mc_angle_idx = 0


def build_minecraft_narration_prompt(
    minecraft_note: str,
    *,
    goal: str = "",
    greet_note: str = "",
    angle_idx: int | None = None,
) -> str:
    """Turn proaktif SAAT MAIN GAME — komentator, bukan "[recorded remark omitted]".

    Framing sengaja beda dari inisiatif biasa: tidak ada kalimat "stream lagi
    hening" (dia lagi main, bukan nunggu), dan dia didorong bicara soal
    kejadian/aksi/rencana — persis permintaan Bohan "[recorded remark omitted]".
    """
    global _mc_angle_idx
    if angle_idx is None:
        angle_idx = _mc_angle_idx
        _mc_angle_idx = (_mc_angle_idx + 1) % len(_MC_NARRATION_ANGLES)
    angle = _MC_NARRATION_ANGLES[angle_idx % len(_MC_NARRATION_ANGLES)]
    sapaan = f"\nSelipkan dulu sapaan singkat: {greet_note}" if greet_note else ""
    misi = (
        f"\nMisi yang lagi kamu kejar: {goal} — kaitkan omonganmu ke misi ini "
        "(kemajuan, hambatan, atau langkah berikutnya)."
        if goal else ""
    )
    return (
        "[Komentar main game]\n"
        f"Kamu lagi MAIN Minecraft sekarang — kondisimu: {minecraft_note}.{misi} "
        "Kamu yang pegang mic: penonton lagi nonton kamu main, jadi jangan "
        "bengong. Bicara sendiri seperti streamer yang lagi asyik main.\n"
        f"Sudut kali ini: {angle}{sapaan}\n"
        "Maksimal 3 kalimat, Bahasa Indonesia, penuh karakter kamu. JANGAN "
        "bilang stream-nya sepi/hening atau nanya 'ada yang mau aku lakuin?' — "
        "kamu punya inisiatif sendiri. Kalau mau bertindak, tutup dengan SATU "
        "tag aksi [MC: ...] (jangan pernah dibaca/disebut). Jangan menyebut "
        "sistem, log, status teknis, atau angka koordinat mentah."
    )


# Sudut siaran saat Arti pegang mic sendirian — dirotasi bergilir (alasan sama
# dengan sudut game: acak murni mengulang sudut yang sama beruntun).
_HOST_ANGLES = (
    "Angkat satu bahan di bawah jadi cerita/opini kamu sendiri.",
    "Lempar pertanyaan ke penonton soal bahan itu dan pancing mereka jawab di "
    "chat — sebut kalau kamu bakal baca jawabannya.",
    "Sambung lagi obrolan yang tadi sempat jalan, atau belokkan ke sisi lain "
    "yang masih nyambung.",
    "Tanggapi apa yang lagi terdengar/kelihatan sekarang kayak temen "
    "nyeletuk — jangan jelasin isinya.",
    "Umumkan rencana kamu habis ini — mau ngobrolin apa atau ngapain — biar "
    "penonton punya alasan nungguin.",
)
_host_angle_idx = 0


@bind_context_epoch
def build_host_prompt(
    material: str, *, greet_note: str = "", angle_idx: int | None = None
) -> str:
    """Turn proaktif saat ARTI PEGANG SIARAN (Bohan AFK).

    Beda framing dari inisiatif biasa: dia bukan "[recorded remark omitted]", dia PEMBAWA ACARANYA. Marker "[Arti pegang siaran]" dibaca
    ulang arti_voice_pipeline untuk memilih instruksi turn yang tepat.
    """
    global _host_angle_idx
    if angle_idx is None:
        angle_idx = _host_angle_idx
        _host_angle_idx = (_host_angle_idx + 1) % len(_HOST_ANGLES)
    angle = _HOST_ANGLES[angle_idx % len(_HOST_ANGLES)]
    sapaan = f"\nSelipkan dulu sapaan singkat: {greet_note}" if greet_note else ""
    return (
        "[Arti pegang siaran]\n"
        "Bohan lagi AFK — kamu yang pegang mic sekarang, penonton nontonin "
        "KAMU. Bicara duluan, jangan nunggu siapa pun.\n"
        f"Sudut kali ini: {angle}\n"
        f"Bahan: {material}{sapaan}\n"
        "Maksimal 3 kalimat, Bahasa Indonesia, penuh karakter kamu. JANGAN "
        "mengeluh sepi, JANGAN mengulang-ulang bahwa Bohan lagi pergi, dan "
        "JANGAN mengarang seolah dia menjawab kamu. Jangan menyebut sistem, "
        "log, atau istilah teknis."
    )


# Stardew (plan 2026-10-22): sudut host yang TIDAK menyuruh bikin rencana —
# indeks _HOST_ANGLES tanpa "[recorded remark omitted]" (agency-first, #329).
_STARDEW_HOST_ANGLES = (0, 1, 3)
_stardew_host_angle_idx = 0


def build_stardew_material(stardew_note: str, *, ada_penonton: bool = True) -> str:
    """Bahan inisiatif dari game Stardew yang ARTI mainkan sendiri.

    Aturan 11 "Reaksi, bukan narasi" + larangan bikin rencana/daftar tugas
    (owner #329: dia main dengan caranya sendiri; faktanya cuma pemantik).
    """
    pemantik = "opini, perasaan, candaan" + (
        ", atau pertanyaan ke penonton" if ada_penonton else "")
    return (
        f"Fakta dari Stardew: {stardew_note}\n"
        "Ini fakta dari game yang KAMU mainkan (karaktermu sendiri). Jangan "
        "dibacakan/dilaporkan. Pakai sebagai pemantik: "
        f"{pemantik}. Reaksi, bukan narasi. "
        "Jangan bikin rencana/daftar tugas buat dirimu."
    )


def _next_stardew_host_angle() -> int:
    global _stardew_host_angle_idx
    idx = _STARDEW_HOST_ANGLES[_stardew_host_angle_idx % len(_STARDEW_HOST_ANGLES)]
    _stardew_host_angle_idx += 1
    return idx


# Dipakai saat semua bahan habis/terblokir. Sengaja beberapa dan bervariasi —
# satu kalimat tunggal terbukti bikin Arti mengulang hal yang sama berpuluh kali.
_FALLBACK_MATERIALS = (
    "Bebas: ceritakan satu hal kecil yang bikin kamu penasaran akhir-akhir ini.",
    "Bebas: tanya kabar siapa pun yang lagi nonton, dan pancing mereka cerita.",
    "Bebas: celetukin sesuatu yang lagi kamu pikirin barusan, sependek apa pun.",
    "Bebas: ajak penonton main tebak-tebakan atau pilih-pilihan singkat.",
    "Bebas: ceritakan satu hal yang menurutmu overrated atau underrated.",
    "Bebas: bikin pengakuan receh soal kebiasaan atau kesukaanmu.",
    "Bebas: tanya penonton lagi ngapain sekarang, lalu tanggapi dengan pendapatmu.",
    "Bebas: bahas satu hal kecil yang bikin kamu kesal belakangan ini.",
)


@bind_context_epoch
def build_initiative_prompt(
    config: dict,
    *,
    memory_bullets: list[str] | None = None,
    present_viewers: list[str] | None = None,
    scouter_summary: str = "",
    screen_hook: str = "",
    viewer_join_note: str = "",
    minecraft_note: str = "",
    minecraft_goal: str = "",
    mode: str = "duet",
    vault_topic: str = "",
    streamer_baru_bicara: bool = False,
    ada_penonton: bool = True,
    heard_note: str = "",
    web_topic: str = "",
    stardew_note: str = "",
    stardew_live: bool = False,
    rng=None,
) -> str:
    """User message untuk turn inisiatif — pilih SATU bahan acak-berbobot.

    `mode` menentukan BUNGKUS akhirnya (inisiatif biasa / komentar game /
    siaran solo) dan bahan tambahan mana yang ikut. `rng` = callable tanpa
    argumen yang mengembalikan float [0,1) — diinjeksi di test supaya
    deterministik; default random.random.
    """
    from arti_live_safety import public_context
    if config.get("stardew_initiative_game_first") and stardew_live:
        # This lane cannot fall through into viewers, renungan, old sessions or Bebas.
        # Use the existing game framing; an empty game snapshot skips the slot.
        note = public_context(stardew_note)
        if not note:
            return ""
        material = build_stardew_material(note, ada_penonton=ada_penonton)
        if mode in ("host_chat", "host_game"):
            return build_host_prompt(material, angle_idx=_next_stardew_host_angle())
        return "[Inisiatif — buka topik sendiri]\n" + material
    memory_bullets = [public_context(b) for b in (memory_bullets or []) if public_context(b)]
    scouter_summary = public_context(scouter_summary)
    screen_hook = public_context(screen_hook)
    vault_topic = public_context(vault_topic)
    heard_note = public_context(heard_note)
    web_topic = public_context(web_topic)
    if rng is None:
        import random  # noqa: PLC0415

        rng = random.random

    memory_bullets = [
        b for b in (memory_bullets or [])
        if not is_meta_learning_bullet(b)
        and b not in _used_init_materials
        and not _same_topic_as_used(b)
    ]
    present_viewers = [
        v for v in (present_viewers or []) if v not in _used_init_materials
    ]
    if screen_hook in _used_init_materials:
        screen_hook = ""
    if viewer_join_note in _used_init_materials:
        viewer_join_note = ""  # event yang sama jangan disapa dua kali
    global _last_scouter_summary_used
    if scouter_summary and scouter_summary == _last_scouter_summary_used:
        scouter_summary = ""  # summary yang sama jangan diangkat lagi (F4/N2)

    # LAGI MAIN GAME = mode komentator (spek Bohan 2026-08-04 "[recorded remark omitted]"). Bahan lain (memori sesi lama, hook layar OBS, ringkasan
    # scouter) SENGAJA tidak ikut: penonton lagi nonton dia main, bukan dengar
    # dia melamun soal kemarin. Sapaan penonton baru tetap menang sekali —
    # event langka ber-TTL, dan menyambut orang lebih penting.
    if minecraft_note:
        if viewer_join_note:
            _used_init_materials.append(viewer_join_note)
            if len(_used_init_materials) > _MAX_USED_INIT_MATERIALS:
                _used_init_materials.pop(0)
            return build_minecraft_narration_prompt(
                minecraft_note, goal=minecraft_goal, greet_note=viewer_join_note
            )
        return build_minecraft_narration_prompt(minecraft_note, goal=minecraft_goal)

    # STARDEW (plan 2026-10-22): giliran game sudah dipilih bridge (rasio
    # stardew_initiative_game_ratio). Sapaan penonton baru dan streamer yang
    # baru bicara tetap menang; bahan lain menunggu giliran "other".
    stardew_material = ""
    if stardew_note and not viewer_join_note and not streamer_baru_bicara:
        stardew_material = build_stardew_material(stardew_note, ada_penonton=ada_penonton)

    # RENUNGAN (Bohan 2026-08-20): busur mikir multi-giliran numpang slot
    # inisiatif. Momen menang tetap nomor satu — penonton baru masuk atau
    # streamer baru bicara = renungan ngalah giliran ini, disambung nanti.
    # Seed dipilih DETERMINISTIK (bullet pertama yang lolos filter, bukan
    # tarikan rng) — tarikan rng ekstra meledakkan tes lama yang menyodorkan
    # iterator berjatah pas (pelajaran gerakan_dialog, StopIteration).
    if not stardew_material and not viewer_join_note and not streamer_baru_bicara:
        seed_renungan = ""
        if memory_bullets:
            seed_renungan = memory_bullets[0].lstrip("- ").strip()
        elif screen_hook:
            seed_renungan = screen_hook
        elif scouter_summary:
            seed_renungan = scouter_summary
        prompt_renungan = arti_renungan.giliran_renungan(
            config or {}, mode=mode, ada_penonton=ada_penonton,
            seed=seed_renungan,
        )
        if prompt_renungan:
            if seed_renungan and seed_renungan == scouter_summary:
                _last_scouter_summary_used = scouter_summary
            elif seed_renungan:
                # Seed yang jadi busur jangan diangkat lagi sebagai celetukan.
                _used_init_materials.append(seed_renungan)
                if len(_used_init_materials) > _MAX_USED_INIT_MATERIALS:
                    _used_init_materials.pop(0)
            return prompt_renungan

    # (bobot, teks prompt, kunci anti-ulang — "" = tidak dilacak)
    candidates: list[tuple[float, str, str]] = []
    if viewer_join_note:
        # Penonton baru masuk = event paling segar — bobot tertinggi.
        candidates.append((4.0, viewer_join_note, viewer_join_note))
    if memory_bullets:
        b = memory_bullets[int(rng() * len(memory_bullets)) % len(memory_bullets)]
        candidates.append((3.0, (
            f"Kenangan dari catatanmu (SESI LAMA, bukan barusan; bukan bukti hadir): "
            f"{b.lstrip('- ').strip()} — kalau diangkat, sebut asalnya secara "
            "kasual TANPA tanggal persis, dan VARIASIKAN frasanya (jangan "
            "selalu 'pas live kemarin' — bisa 'waktu itu', 'aku pernah "
            "denger', 'dulu sempet kepikiran', atau langsung kaitkan ke hari "
            "ini). Jangan bahas seolah baru saja terjadi."
        ), b))
    if present_viewers:
        v = present_viewers[int(rng() * len(present_viewers)) % len(present_viewers)]
        candidates.append((2.0, (
            f"Penonton {v} tadi hadir di chat — sapa dia, atau bahas hal yang "
            "kamu ingat tentang dia."
        ), v))
    if screen_hook:
        candidates.append((2.0, f"Ada yang menarik di layar: {screen_hook} — "
                           "tanggapi, jangan jelasin ulang isinya.",
                           screen_hook))
    if scouter_summary:
        # Summary scouter cuma berganti tiap ~5 menit (scouter_interval_sec
        # 300) — dilacak supaya tidak diulang giliran demi giliran (F4).
        candidates.append((1.0, (
            f"Obrolan terakhir sebelum hening (ringkasan, bukan bukti hadir): {scouter_summary} — lanjutkan, "
            "atau belokkan ke topik baru yang masih nyambung."
        ), _SCOUTER_KEY))
    # Bahan KHUSUS mode siaran solo. Tanpa ini dia cuma punya learnings vault
    # (satu berkas) dan mengulang topik — akar keluhan "[recorded remark omitted]".
    if mode in ("host_chat", "host_game"):
        if vault_topic and vault_topic not in _used_init_materials:
            candidates.append((3.0, (
                f"Sesuatu dari catatanmu sendiri (ingatan, bukan bukti hadir): {vault_topic} — ceritakan "
                "dengan kalimatmu, kasih pendapatmu, jangan dibacakan mentah."
            ), vault_topic))
        if heard_note and heard_note not in _used_init_materials:
            candidates.append((2.5, (
                f"Yang barusan kedengaran dari yang lagi diputar: {heard_note} "
                "— tanggapi kayak temen yang ikut dengerin, jangan ulang isinya; "
                "ini yang KAMU DENGAR, bukan yang kamu lihat."
            ), heard_note))
        if web_topic and web_topic not in _used_init_materials:
            candidates.append((2.5, (
                f"Kabar dari internet yang kamu intip barusan: {web_topic} — "
                "angkat satu sisi yang menarik menurutmu, jangan baca semua."
            ), web_topic))

    if stardew_material:
        material = stardew_material  # tanpa tarikan rng/ring bahan lain
    elif not candidates:
        # Audit 2026-08-05: dulu satu kalimat generik yang IDENTIK, dan karena
        # tidak pernah dicatat ke ring, `_same_topic_as_used` memblokir semua
        # bahan tersisa selamanya — 109 dari 120 giliran memakai kalimat yang
        # persis sama (≈50 menit mengulang satu kalimat tiap 25 dtk).
        # Sekarang: ring dikosongkan dulu (beri kesempatan bahan lama berputar
        # lagi), dan fallback-nya bervariasi + ikut dicatat.
        # JANGAN clear() total. Audit verifikasi 2026-08-05: itu ikut menghapus
        # jejak bahan NYATA yang barusan dipakai, jadi potongan vault yang sama
        # diangkat lagi tiap ~50 detik (ring seharusnya menahan 12 giliran,
        # nyatanya cuma 2). Cukup lepaskan separuh tertua supaya bahan lama
        # perlahan boleh berputar lagi.
        del _used_init_materials[: max(1, len(_used_init_materials) // 2)]
        material = _FALLBACK_MATERIALS[
            int(rng() * len(_FALLBACK_MATERIALS)) % len(_FALLBACK_MATERIALS)
        ]
        _used_init_materials.append(material)
    else:
        total = sum(w for w, _, _ in candidates)
        r = rng() * total
        material, used_key = candidates[-1][1], candidates[-1][2]
        acc = 0.0
        for w, m, key in candidates:
            acc += w
            if r <= acc:
                material, used_key = m, key
                break
        if used_key == _SCOUTER_KEY:
            _last_scouter_summary_used = scouter_summary
        elif used_key:
            _used_init_materials.append(used_key)
            if len(_used_init_materials) > _MAX_USED_INIT_MATERIALS:
                _used_init_materials.pop(0)

    if mode in ("host_chat", "host_game"):
        # Bohan AFK: bungkusnya "[recorded remark omitted]", bukan "[recorded remark omitted]".
        # host_game sampai sini cuma tanpa minecraft_note (Stardew live).
        return build_host_prompt(
            material,
            angle_idx=_next_stardew_host_angle() if stardew_material else None,
        )

    # Pembukaan JUJUR terhadap keadaan. Live 14 Agu: pagar anti-motong cuma
    # 5 detik, jadi Bohan yang baru selesai bicara tetap dapat kalimat
    # "[recorded remark omitted]" — padahal
    # omongannya ADA di history dan Arti mengutipnya. Dia melihat Bohan
    # tapi dilarang menjawabnya, lalu keluar monolog yang menyebut-nyebut
    # dia tanpa berdialog ("[recorded remark omitted]").
    if streamer_baru_bicara:
        pembuka = (
            "[Inisiatif — nyambung obrolan]\n"
            "Streamer BARU SAJA bicara. Tanggapi dulu yang dia bilang "
            "(lihat riwayat), baru lanjutkan dengan pendapatmu sendiri. "
            "Jangan membuka topik baru yang tidak nyambung.\n"
        )
    else:
        pembuka = (
            "[Inisiatif — buka topik sendiri]\n"
            "Stream lagi hening — tidak ada chat maupun omongan streamer. "
            "Kamu MEMULAI obrolan, bukan menjawab siapa pun.\n"
        )
    # Menyuruh melempar ke "penonton" saat penontonnya NOL bikin Arti
    # mengoceh ke ruangan kosong padahal siaran offline.
    if ada_penonton:
        sasaran = (
            "Pertanyaan penutup OPSIONAL; kalau pakai, lebih sering "
            "lempar ke penonton daripada ke streamer. "
        )
    else:
        sasaran = (
            "TIDAK ADA penonton yang menyimak — jangan menyapa atau "
            "melempar pertanyaan ke 'penonton'. Ngobrol saja dengan "
            "streamer, atau bergumam sendiri. "
        )
    gerakan = gerakan_dialog(rng, ada_penonton, config)
    baris_gerakan = (
        # "Momen menang": gerakan cuma bentuk — kekhawatiran Bohan 16 Agu,
        # jangan sampai Arti sibuk pamer koleksi pas ada kejadian di stream.
        f"Gerakan giliran ini (SKIP kalau nggak nyambung sama momen yang lagi "
        f"terjadi — momen selalu menang): {gerakan}\n"
        if gerakan else ""
    )
    benang = arti_benang.blok_prompt()
    return (
        pembuka
        + (f"{benang}\n" if benang else "")
        + f"Bahan: {material}\n"
        + baris_gerakan
        # Persona 2026-08-03 (Bohan: "[recorded remark omitted]"): kewajiban 1-pertanyaan dicabut — opini dulu.
        # Angka ikut batas pemotong (arti_reply_policy) — dulu "3" vs cap 1.
        + f"Maksimal {inisiatif_max_sentences(config)} kalimat berisi "
        "OPINI/celetukan kamu sendiri — kamu "
        "punya pendirian, bukan asisten yang minta arahan. "
        # SATU PIKIRAN (paket kohesi 17 Agu, baseline: 3 kalimat = 3 topik
        # terpisah di 84% giliran): satu giliran = satu benang, bukan daftar.
        + "SATU PIKIRAN saja: dari semua bahan pilih SATU dan dalami — sisanya "
        "abaikan, masih ada giliran berikutnya. Kalimat berikutnya harus "
        "MELANJUTKAN kalimat sebelumnya, bukan ganti topik. "
        + sasaran
        + "Bahasa Indonesia. "
        "Jangan menyebut sistem, memori, log, atau keheningan secara teknis."
    )


def build_curious_system_addon(config: dict) -> str:
    """System prompt block khusus turn proaktif."""
    # Larangan (negative prompt) = PARAMETER: timpa `curious_larangan` dari
    # config_local.json tanpa menyentuh kode. Default-nya anti-reporter
    # (pembuka kaleng yang sama tiap giliran). String kosong = tanpa larangan.
    larangan = config.get("curious_larangan")
    if larangan is None:  # None = pakai default; "" = sengaja tanpa larangan
        larangan = DEFAULT_LARANGAN
    larangan = str(larangan).strip()
    return (
        "\n\n[MODE PENASARAN — PROAKTIF]\n"
        "Kamu memulai obrolan karena penasaran, bukan karena dipanggil.\n"
        "Reaksi pada satu detail konkret dengan opini/pendirian kamu sendiri; "
        "pertanyaan boleh tapi TIDAK wajib — jangan tiap giliran nanya streamer.\n"
        "Reaksi, bukan narasi: jangan jelasin isi layar atau mengulang hook scouter kata per kata."
        + (f"\n{larangan}" if larangan else "")
    )


@bind_context_epoch
def build_prompt(config: dict) -> str:
    """User message for curious LLM turn."""
    if not screen_privacy.allows_screen():
        return ""
    latest = sc.screen_ring.latest()
    scene = (latest.scene if latest else "").strip()
    if not scene:
        return ""
    playback = latest.playback_mmss if latest else None
    scouter = config.get("scouter_last_result") or {}
    if any(sc.looks_like_bridge_log(text) for text in (scene, scouter.get("curious_hook"), scouter.get("curious_question"))):
        return ""
    hook = (scouter.get("curious_hook") or "").strip()
    if is_boring_screen_hook(hook):
        hook = ""  # layar gelap/kosong bukan sudut penasaran
    question = (scouter.get("curious_question") or "").strip()

    parts = ["[Curious — reaksi proaktif]"]
    if hook:
        parts.append(f"Sudut penasaran (seed, jangan copy verbatim): {hook}")
    if question:
        parts.append(f"Ide pertanyaan (adaptasi, jangan copy verbatim): {question}")
    parts.append(f"Layar saat ini: {scene}")
    if playback:
        parts.append(f"Posisi video: {playback}")
    benang = arti_benang.blok_prompt()
    if benang:
        parts.append(benang)
    gerakan = gerakan_dialog(config=config)
    if gerakan:
        parts.append(
            "Gerakan giliran ini (SKIP kalau nggak nyambung sama momen yang "
            f"lagi terjadi — momen selalu menang): {gerakan}"
        )
    # Larangan anti-reporter TIDAK diulang di sini — dia hidup di
    # build_curious_system_addon (kunci config `curious_larangan`), satu pintu
    # supaya Bohan bisa menimpanya tanpa berburu duplikat.
    parts.append(
        "Tunjuk 1 detail spesifik yang menarik perhatianmu, hubungkan ke konteks stream singkat, "
        "dan kasih komentar/opini khas kamu — kamu punya pendirian sendiri. "
        "SATU PIKIRAN saja: satu giliran = satu benang — kalimat berikutnya "
        "MELANJUTKAN kalimat sebelumnya, bukan buka topik baru. "
        "Maksimal 3 kalimat; pertanyaan penutup opsional (kalau ada, variasikan "
        "targetnya — penonton juga, bukan streamer melulu). "
        "Bahasa Indonesia. Reaksi, bukan narasi — jangan jelasin isi layar."
    )
    return "\n".join(parts)


def prepare_for_fire(config: dict) -> bool:
    """Refresh vision if stale; return True if ready."""
    if not _vision_effective(config):
        return False
    if config.get("curious_requires_fresh_screen", True):
        if not arti_vision_client.is_vision_fresh(config):
            snap, provider = arti_vision_client.refresh_if_stale(config)
            if snap and snap.scene:
                sc.update_watch_state_from_snapshot(
                    snap,
                    event_id=str(config.get("watch_party_event_id") or ""),
                )
                print(f"[Curious] Refreshed vision via {provider}")
            if not arti_vision_client.is_vision_fresh(config):
                return False
    latest = sc.screen_ring.latest()
    if not (latest and latest.scene.strip()):
        return False
    # Layar gelap/kosong + hook scouter juga kosong/boring = tidak ada topik:
    # batalkan turn (audit 3/8: build_prompt buang hook boring tapi scene
    # "layar gelap" tetap terinjeksi + disuruh "tunjuk 1 detail spesifik").
    hook = ((config.get("scouter_last_result") or {}).get("curious_hook") or "")
    if is_boring_screen_hook(latest.scene) and is_boring_screen_hook(hook):
        return False
    return True

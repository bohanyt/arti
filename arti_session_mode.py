"""Mode sesi Arti — satu sumber kebenaran "sekarang dia lagi jadi apa".

Stream punya 4 kombinasi (spek Bohan 2026-08-04) = (Bohan hadir / Bohan AFK) x
(main game / tidak):

    duet       Bohan nemenin, tidak main  -> Arti temen ngobrol (perilaku lama)
    duet_game  Bohan nemenin, main game   -> main bareng, Arti nguntit
    host_chat  Bohan AFK, tidak main      -> ARTI PEGANG SIARAN (mode baru)
    host_game  Bohan AFK, main game       -> Arti solo gaming stream

Modul ini MURNI (tanpa state global, tanpa I/O) supaya bisa dites tanpa bridge.
Bridge menyimpan satu bool `_host_mode`; "lagi main" = runner Minecraft aktif
ATAU Stardew live (arti_stardew_driver_guard.stardew_live — definisi yang sama
dengan pagar dua-driver). Sisanya diturunkan di sini.
"""

from __future__ import annotations

import re

DUET = "duet"
DUET_GAME = "duet_game"
HOST_CHAT = "host_chat"
HOST_GAME = "host_game"
MODES = (DUET, DUET_GAME, HOST_CHAT, HOST_GAME)

_LABELS = {
    DUET: "ngobrol bareng Bohan",
    DUET_GAME: "main game bareng Bohan",
    HOST_CHAT: "Arti pegang siaran (Bohan AFK)",
    HOST_GAME: "Arti main game sendirian (Bohan AFK)",
}


def game_active(*, minecraft: bool, stardew: bool) -> bool:
    """Arti lagi main game? Stardew live ATAU runner Minecraft aktif.

    Dulu cuma Minecraft, jadi stream Stardew selalu terbaca duet/host_chat
    (audit 2026-10-21). Minecraft diparkir tapi tetap dihitung kalau nyala.
    """
    return bool(minecraft) or bool(stardew)


def resolve_mode(host_mode: bool, in_game: bool) -> str:
    if host_mode:
        return HOST_GAME if in_game else HOST_CHAT
    return DUET_GAME if in_game else DUET


def is_host(mode: str) -> bool:
    return mode in (HOST_CHAT, HOST_GAME)


def is_game(mode: str) -> bool:
    return mode in (DUET_GAME, HOST_GAME)


def mode_policy(mode: str, config: dict | None = None, *,
                minecraft: bool = True) -> dict:
    """Kebijakan proaktif per mode — dibaca gate inisiatif & bridge.

    `dormancy_applies` False = aturan "sepi total 5 menit -> diam" TIDAK
    berlaku. Itu benar untuk semua mode kecuali `duet`: kalau Bohan ada dan
    ruangan mati total, diam memang jawabannya (keluhan Bohan 2026-08-03,
    "[recorded remark omitted]"). Begitu dia AFK atau
    lagi main, Arti-lah acaranya — dia harus terus bicara.

    `minecraft` = game-nya runner Minecraft (bridge: `_mc_runner_active()`).
    False = Stardew (REVIEW_BRAIN F3, default konservatif sampai owner
    memutuskan): TIDAK mewarisi tempo narasi Minecraft. `duet_game` Stardew
    pakai jeda `stardew_initiative_gap_sec` (default = `initiative_quiet_sec`)
    dan dormansi/backoff duet (`stardew_dormancy_enabled`, default True);
    `host_game` Stardew = tempo `host_chat` (seperti sebelum batch brain).
    """
    cfg = config or {}
    stardew_duet = mode == DUET_GAME and not minecraft
    return {
        "label": _LABELS.get(mode, mode),
        "dormancy_applies": mode == DUET or (
            stardew_duet and bool(cfg.get("stardew_dormancy_enabled", True))),
        "backoff_applies": mode == DUET or stardew_duet,
        "proactive_gap_sec": _gap_for(mode, cfg, minecraft=minecraft),
        # Komentar layar OBS dibungkam saat in-game (anti dobel dengan event
        # game); di mode ngobrol layar tetap jadi bahan.
        "screen_curious_allowed": not is_game(mode),
        "obs_scene_key": f"obs_scene_{mode}",
    }


def _gap_for(mode: str, cfg: dict, *, minecraft: bool = True) -> float:
    if cfg.get("stardew_initiative_game_first") and is_game(mode) and not minecraft:
        from arti_runtime.reactive_play import number
        classic = _gap_for(mode, dict(cfg, stardew_initiative_game_first=False), minecraft=False)
        return max(classic, number(cfg, "stardew_own_topic_gap_sec", 180))
    if is_game(mode) and minecraft:
        return float(cfg.get("minecraft_narration_gap_sec", 20.0))
    if mode in (HOST_CHAT, HOST_GAME):
        return float(cfg.get("host_narration_gap_sec", 25.0))
    quiet = float(cfg.get("initiative_quiet_sec", 30.0))
    if mode == DUET_GAME:  # Stardew (minecraft=False); None = ikut quiet
        raw = cfg.get("stardew_initiative_gap_sec")
        return quiet if raw is None else float(raw)
    return quiet


# ---------------------------------------------------------------------------
# Siapa yang boleh nyuruh (keputusan Bohan 2026-08-04: cuma dia)
# ---------------------------------------------------------------------------

def normalize_handle(handle: str) -> str:
    """Samakan bentuk handle. Chat YT asli TANPA '@' (authorName.simpleText),
    sedangkan config `yt_default_viewer` PAKAI '@' — tanpa normalisasi,
    perbandingan pemilik tidak akan pernah cocok."""
    return (handle or "").strip().lstrip("@").strip().lower()


def owner_handles(config: dict | None = None) -> set[str]:
    cfg = config or {}
    raw = list(cfg.get("owner_yt_handles") or [])
    if not raw:
        # Fallback: handle yang sudah dipakai fitur "yt <pesan>" di console.
        one = cfg.get("yt_default_viewer") or ""
        if one:
            raw = [one]
    return {h for h in (normalize_handle(x) for x in raw) if h}


# Turn yang lahir dari Arti sendiri (proaktif). Tag pengubah-sesi harus boleh
# di sini, kalau tidak dia tak akan pernah bisa menyatakan misinya selesai.
_SELF_TRIGGERS = frozenset({"curious", "game"})
# Suara/ketikan streamer. "mic" BUKAN mikrofon — itu ketikan console (jalur
# suara asli selalu 'ptt'/'wake_word'); dua-duanya sama-sama Bohan.
# "mc_chat" = Bohan mengetik di chat DALAM GAME. Dihitung sebagai giliran
# streamer karena pengirimnya sudah disaring terhadap
# `minecraft_streamer_name` sebelum trigger dibuat — jadi memang dia, dan dia
# harus bisa menyuruh keluar/ganti misi lewat chat sama seperti lewat mic.
_STREAMER_TRIGGERS = frozenset({"ptt", "wake_word", "mic", "mc_chat"})


def is_owner_turn(
    trigger_type: str, viewer_name: str | None, config: dict | None = None
) -> bool:
    """Boleh mengubah mode/sesi/misi dari turn ini?

    Viewer biasa SENGAJA ditolak: kalau tidak, satu penonton iseng bisa
    menyuruh Arti keluar dari game atau ganti misi di tengah jalan. Mereka
    tetap bisa memengaruhi aksi kecil (ikutin/jalan-jalan) — itu tidak lewat
    sini.
    """
    ttype = (trigger_type or "").strip()
    if ttype in _STREAMER_TRIGGERS or ttype in _SELF_TRIGGERS:
        return True
    if ttype == "yt_chat":
        return normalize_handle(viewer_name or "") in owner_handles(config)
    return False


# ---------------------------------------------------------------------------
# Jaring pengaman: Bohan pamit AFK tapi Arti tidak mengeluarkan tag
# ---------------------------------------------------------------------------

_AFK_PATTERNS = (
    r"\bafk\b",
    r"\baku pergi\b",
    r"\baku cabut\b",
    r"\bku tinggal\b",
    r"\bkutinggal\b",
    r"\btinggal dulu\b",
    r"\bditinggal dulu\b",
    r"\bkamu (yang )?pegang\b",
    r"\bambil alih\b",
    r"\bgantiin aku\b",
    r"\bgantiin gue\b",
    r"\baku keluar dulu\b",
    r"\bpergi dulu\b",
)
_AFK_RE = re.compile("|".join(_AFK_PATTERNS), re.IGNORECASE)
# "nanti aku afk" / "kalau aku afk" = RENCANA, bukan pamit sekarang.
# Diperluas sesudah audit verifikasi 2026-08-05 (7 dari 12 bukan-pamit lolos):
# "[recorded remark omitted]" bahkan MEMASANG jaring — persis kalimat pembatalannya.
_AFK_NEGATORS = re.compile(
    r"\b(nanti|kalau|kalo|jangan|belum|habis ini|nggak|ngga|gak|enggak|tidak"
    r"|bukan|kemarin|tadi|besok|sempat)\b",
    re.IGNORECASE,
)
# Negator bisa datang SESUDAH frasanya juga ("aku afk dulu... eh nggak jadi").
_AFK_NEGATORS_SESUDAH = re.compile(
    r"\b(nggak jadi|ngga jadi|gak jadi|enggak jadi|batal|becanda|bercanda"
    r"|belum jadi|jangan|bukan|artinya)\b",
    re.IGNORECASE,
)


def detect_afk_intent(text: str) -> bool:
    """Streamer barusan pamit pergi? (deterministik, bukan tebakan LLM)

    Dipakai sebagai JARING: kalau Arti gagal mengeluarkan [MODE: host] dan
    Bohan benar-benar pergi, stream mati sampai dia balik — konsekuensinya
    terlalu mahal untuk mengandalkan LLM saja.

    Negator dicek HANYA di depan frasa pamitnya. Audit 2026-08-05: dulu
    dicari di seluruh kalimat, dan kata "nanti"/"kalau" muncul alami di pamit
    sungguhan — "[recorded remark omitted]" jadi tidak terdeteksi.
    7 dari 9 kalimat pamit natural lolos begitu saja.
    """
    t = (text or "").strip()
    if not t:
        return False
    m = _AFK_RE.search(t)
    if not m:
        return False
    sebelum = t[max(0, m.start() - 30):m.start()]
    if _AFK_NEGATORS.search(sebelum):
        return False
    # "aku afk dulu... eh nggak jadi deng" — pembatalan menyusul di belakang.
    return not _AFK_NEGATORS_SESUDAH.search(t[m.end():m.end() + 40])


# ---------------------------------------------------------------------------
# Tag [MODE: ...] di jawaban LLM
# ---------------------------------------------------------------------------

_MODE_TAG_RE = re.compile(r"\[\s*MODE\s*:([^\]]*)\]", re.IGNORECASE)
_MODE_VERBS = {
    "host": HOST_CHAT,   # "Bohan AFK, aku pegang" — game-nya urusan tag [MC:]
    "duet": DUET,        # "Bohan balik"
}


# ---------------------------------------------------------------------------
# Perintah Minecraft dari kalimat streamer (jaring deterministik)
# ---------------------------------------------------------------------------
# Perintah eksplisit streamer bisa tidak pernah dijalankan. Sebabnya bukan bug logika: giliran yang
# dipicu omongan streamer sengaja dirutekan ke Groq (butuh instan), dan model
# yang kepilih llama-3.1-8b MENGABAIKAN instruksi tag [MC: join] — dia cuma
# menjawab ngobrol. Giliran proaktif yang lewat composer memang mengeluarkan
# tag, tapi perintah langsung tidak boleh bergantung pada model yang kebetulan
# menang routing. Jadi: perintah eksplisit dideteksi deterministik.
_MC_JOIN_RE = re.compile(
    # Akhiran -nya ikut diterima (20 Agu): "minecraftnya"/"gamenya" itu bentuk
    # yang wajar diucapkan Bohan, dan tanpa ini perintah aslinya lolos diam2.
    r"\b(buka|main|mainin|masuk|join|gabung|ikut(?:\s+main)?)\b[^.!?]{0,20}"
    r"\b(minecraft|game|mc)(?:nya)?\b"
    r"|\b(minecraft|game)(?:nya)?\b[^.!?]{0,15}\b(yuk|dong|sana|gih)\b",
    re.IGNORECASE,
)
_MC_LEAVE_RE = re.compile(
    r"\b(keluar|udahan|udah(?:an)?|berhenti|stop|cukup|selesai)\b[^.!?]{0,20}"
    r"\b(minecraft|game|main|mc)(?:nya)?\b",
    re.IGNORECASE,
)
_MC_NEGATORS = re.compile(
    r"\b(nanti|jangan|belum|nggak|ngga|gak|enggak|tidak|kalau|kalo|besok"
    r"|kemarin|tadi|dulu\s+pernah|waktu\s+itu"
    # Pengandaian/wacana — Bohan sedang MIKIR, bukan menyuruh. Log 20 Agu
    # 23.29: "[recorded remark omitted]" dan "[recorded remark omitted]" menyeret bot masuk game 3x (servernya bahkan
    # mati). Perintah berbunyi "buka minecraft", bukan "bisa sambil main".
    r"|bisa|sambil|kayaknya|mungkin|pengen|pengin|enaknya|andai|seandainya"
    r"|berapa|apakah|semoga|pas\s+lagi)\b",
    re.IGNORECASE,
)


def detect_minecraft_intent(text: str) -> str | None:
    """Perintah masuk/keluar Minecraft dari kalimat streamer, atau None.

    Sengaja SEMPIT: cuma join/leave, yang paling sering diucapkan dan paling
    mahal kalau meleset. Misi tetap lewat `mc goal` / tag.
    """
    t = (text or "").strip()
    if not t:
        return None
    for pola, hasil in ((_MC_LEAVE_RE, "leave"), (_MC_JOIN_RE, "join")):
        m = pola.search(t)
        if not m:
            continue
        # Jendela pengecekan diperlebar 25 -> 40 huruf (20 Agu): "[recorded remark omitted]" lolos karena pengandaiannya duduk lebih jauh dari
        # kata kerjanya. Pengandaian di MANA PUN sebelum kata kerja = batal.
        if _MC_NEGATORS.search(t[max(0, m.start() - 40):m.start()]):
            return None
        return hasil
    return None


def parse_mode_tags(reply: str) -> tuple[str, str | None]:
    """(teks_bersih, "host"|"duet"|None).

    SEMUA bentuk [MODE:...] dibuang dari teks — valid maupun tidak — supaya
    TTS tidak pernah mengucapkan tag (aturan sama dengan [MC:]).
    """
    if not reply:
        return "", None
    found: list[str] = []

    def _swallow(m: re.Match) -> str:
        verb = (m.group(1) or "").strip().split()[:1]
        if verb and verb[0].lower() in _MODE_VERBS and not found:
            found.append(verb[0].lower())
        return " "

    clean = _MODE_TAG_RE.sub(_swallow, reply)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, (found[0] if found else None)

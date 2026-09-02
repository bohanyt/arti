"""Mode sesi Arti — satu sumber kebenaran "sekarang dia lagi jadi apa".

Stream punya 4 kombinasi (spek streamer 2026-08-04) = (streamer hadir / streamer AFK) x
(main Minecraft / tidak):

    duet       streamer nemenin, tidak main  -> Arti temen ngobrol (perilaku lama)
    duet_game  streamer nemenin, main game   -> main bareng, Arti nguntit
    host_chat  streamer AFK, tidak main      -> ARTI PEGANG SIARAN (mode baru)
    host_game  streamer AFK, main game       -> Arti solo gaming stream

Modul ini MURNI (tanpa state global, tanpa I/O) supaya bisa dites tanpa bridge.
Bridge menyimpan satu bool `_host_mode`; "lagi main" dibaca dari runner
Minecraft. Sisanya diturunkan di sini.
"""

from __future__ import annotations

import re

DUET = "duet"
DUET_GAME = "duet_game"
HOST_CHAT = "host_chat"
HOST_GAME = "host_game"
MODES = (DUET, DUET_GAME, HOST_CHAT, HOST_GAME)

_LABELS = {
    DUET: "ngobrol bareng streamer",
    DUET_GAME: "main Minecraft bareng streamer",
    HOST_CHAT: "Arti pegang siaran (streamer AFK)",
    HOST_GAME: "Arti main Minecraft sendirian (streamer AFK)",
}


def resolve_mode(host_mode: bool, in_game: bool) -> str:
    if host_mode:
        return HOST_GAME if in_game else HOST_CHAT
    return DUET_GAME if in_game else DUET


def is_host(mode: str) -> bool:
    return mode in (HOST_CHAT, HOST_GAME)


def is_game(mode: str) -> bool:
    return mode in (DUET_GAME, HOST_GAME)


def mode_policy(mode: str, config: dict | None = None) -> dict:
    """Kebijakan proaktif per mode — dibaca gate inisiatif & bridge.

    `dormancy_applies` False = aturan "sepi total 5 menit -> diam" TIDAK
    berlaku. Itu benar untuk semua mode kecuali `duet`: kalau streamer ada dan
    ruangan mati total, diam memang jawabannya (keluhan streamer 2026-08-03,
    "1 jam gaada viewer trus si arti ngomong sendiri"). Begitu dia AFK atau
    lagi main, Arti-lah acaranya — dia harus terus bicara.
    """
    cfg = config or {}
    return {
        "label": _LABELS.get(mode, mode),
        "dormancy_applies": mode == DUET,
        "proactive_gap_sec": _gap_for(mode, cfg),
        # Komentar layar OBS dibungkam saat in-game (anti dobel dengan event
        # game); di mode ngobrol layar tetap jadi bahan.
        "screen_curious_allowed": not is_game(mode),
        "obs_scene_key": f"obs_scene_{mode}",
    }


def _gap_for(mode: str, cfg: dict) -> float:
    if is_game(mode):
        return float(cfg.get("minecraft_narration_gap_sec", 20.0))
    if mode == HOST_CHAT:
        return float(cfg.get("host_narration_gap_sec", 25.0))
    return float(cfg.get("initiative_quiet_sec", 30.0))


# ---------------------------------------------------------------------------
# Siapa yang boleh nyuruh (keputusan operator [date removed]: cuma dia)
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
# suara asli selalu 'ptt'/'wake_word'); dua-duanya sama-sama operator.
# "mc_chat" = operator mengetik di chat DALAM GAME. Dihitung sebagai giliran
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
# Jaring pengaman: operator pamit AFK tapi Arti tidak mengeluarkan tag
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
# Diperluas sesudah audit verifikasi [date removed] (7 dari 12 bukan-pamit lolos):
# "nggak jadi afk deh" bahkan MEMASANG jaring — persis kalimat pembatalannya.
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
    streamer benar-benar pergi, stream mati sampai dia balik — konsekuensinya
    terlalu mahal untuk mengandalkan LLM saja.

    Negator dicek HANYA di depan frasa pamitnya. Audit 2026-08-05: dulu
    dicari di seluruh kalimat, dan kata "nanti"/"kalau" muncul alami di pamit
    sungguhan — "aku afk dulu ya, nanti aku balik" jadi tidak terdeteksi.
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
    "host": HOST_CHAT,   # "operator AFK, aku pegang" — game-nya urusan tag [MC:]
    "duet": DUET,        # "operator balik"
}


# ---------------------------------------------------------------------------
# Perintah Minecraft dari kalimat streamer (jaring deterministik)
# ---------------------------------------------------------------------------
# Live [date removed] malam: operator tiga kali menyuruh "arti, coba buka minecraft
# deh" dan Arti TIDAK PERNAH masuk. Sebabnya bukan bug logika: giliran yang
# dipicu omongan streamer sengaja dirutekan ke Groq (butuh instan), dan model
# yang kepilih llama-3.1-8b MENGABAIKAN instruksi tag [MC: join] — dia cuma
# menjawab ngobrol. Giliran proaktif yang lewat composer memang mengeluarkan
# tag, tapi perintah langsung tidak boleh bergantung pada model yang kebetulan
# menang routing. Jadi: perintah eksplisit dideteksi deterministik.
_MC_JOIN_RE = re.compile(
    # Akhiran -nya ikut diterima ([date removed]): "minecraftnya"/"gamenya" itu bentuk
    # yang wajar diucapkan operator, dan tanpa ini perintah aslinya lolos diam2.
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
    # Pengandaian/wacana — operator sedang MIKIR, bukan menyuruh. Log [date removed]
    # [time removed]: "bisa sambil main Minecraft kali ya biar aman" dan "berapa yang
    # main game nya harus..." menyeret bot masuk game 3x (servernya bahkan
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
        # Jendela pengecekan diperlebar 25 -> 40 huruf ([date removed]): "bisa sambil
        # main Minecraft" lolos karena pengandaiannya duduk lebih jauh dari
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

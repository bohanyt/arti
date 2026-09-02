"""Kebijakan panjang jawaban Arti — terutama YouTube chat adaptif."""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

# Sapaan / noise → jawaban pendek
_GREETING_RE = re.compile(
    r"^(?:halo+|hai+|hi+|hey+|heyo+|p+|woy+|woi+|test(?:ing)?|"
    r"apa\s+kabar|hallo+|selamat\s+\w+)\b[!?.\s~]*$",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"^(?:lol+|wk+w+|wkw+k*|haha+|xd+|ok+|oke+|sip+|mantap+|nice+)[!?.~\s]*$",
    re.IGNORECASE,
)
_DEEP_MARKERS = (
    "kenapa",
    "mengapa",
    "gimana",
    "bagaimana",
    "menurut",
    "jelasin",
    "jelaskan",
    "apa itu",
    "apa beda",
    "bedanya",
    "perbedaan",
    "opini",
    "menurutmu",
    "artinya",
    "maksudnya",
    "berapa",
    "kapan",
    "siapa yang",
    "apakah",
    "bisa ga",
    "bisa gak",
    "cara ",
    "kenapa sih",
    "gimana cara",
    "menurut kamu",
    "menurut arti",
    "jelasin dong",
    "ceritain",
    "pendapat",
)

# DIRAPATKAN [date removed] (permintaan operator: "untuk celetukan santai 1 kalimat
# sudah cukup; 2-3 kalimat simpan buat penjelasan serius"). Terukur di sesi
# [date removed]: tts_play p50 15,7 dtk dari total giliran 29,7 dtk — lebih dari
# separuh giliran habis buat Arti bicara, 37 dari 44 jawaban 2 kalimat.
# Dulu: brief (1,2) normal (2,3) deep (4,5).
_SENT_BY_KIND = {
    "brief": (1, 1),
    "normal": (2, 2),
    "deep": (3, 4),
}

# Penanda giliran inisiatif/curious — Arti membuka topik sendiri, bukan
# menjawab siapa pun. Ini celetukan, bukan penjelasan.
_INISIATIF_RE = re.compile(r"^\s*\[\s*inisiatif", re.IGNORECASE)


def jenis_giliran(user_speech: str) -> str:
    """yt | inisiatif | streamer.

    Dipakai supaya kebijakan panjang berlaku di SEMUA jalur, bukan cuma chat
    YouTube. Di sesi 26 Agu: curious 25, yt_chat 13, mic 6 — jadi 31 dari 44
    giliran dulu memakai batas datar 5 kalimat tanpa adaptasi sama sekali.
    """
    u = user_speech or ""
    if is_youtube_trigger(u):
        return "yt"
    if _INISIATIF_RE.match(u):
        return "inisiatif"
    return "streamer"
_TOKENS_BY_SENT = {1: 110, 2: 150, 3: 200, 4: 260, 5: 320, 6: 380, 7: 440, 8: 500}


@dataclass(frozen=True)
class YtReplyPlan:
    sentences: int
    max_chars: int
    max_tokens: int
    mode: str
    message_preview: str = ""


_NICK_SUFFIXES = ("official", "channel", "gaming", "yt", "tv")
_NICK_STOPWORDS = {"the", "its", "im", "mr", "mrs", "si"}
_HANDLE_IN_WRAPPER_RE = re.compile(r"Viewer\s+(@\S+)")


def viewer_nickname(handle: str, max_len: int = 8) -> str:
    """Nama panggilan pendek (±2-3 suku kata) dari handle viewer.

    Concern Bohan 2026-08-02: TTS membaca handle utuh termasuk angka ekor —
    "abdmanlifyou241" jadi panjang dan kaku. Aturan: buang @, buang angka/
    separator ekor, ambil kata pertama (pecah camelCase & _-.), buang suffix
    platform (bohanyt -> bohan), potong maksimal `max_len` huruf.
    """
    h = (handle or "").strip().lstrip("@")
    if not h:
        return ""
    h = re.sub(r"[\d_\-.]+$", "", h) or h
    parts = [p for p in re.split(r"[_\-.\s]+", h) if p]
    parts = [p for p in parts if p.lower() not in _NICK_STOPWORDS] or parts
    first = parts[0] if parts else h
    m = re.match(r"[A-Z]?[a-z]+", first)
    if m and len(m.group(0)) >= 4:
        first = m.group(0)
    low = first.lower()
    for suf in _NICK_SUFFIXES:
        if low.endswith(suf) and len(first) - len(suf) >= 4:
            first = first[: len(first) - len(suf)]
            break
    return first[:max_len] if first else handle.lstrip("@")[:max_len]


def extract_viewer_handle(user_speech: str) -> str:
    """Handle viewer dari wrapper '[Pesan Live Chat dari Viewer @x (YouTube)]'."""
    m = _HANDLE_IN_WRAPPER_RE.search(user_speech or "")
    return m.group(1) if m else ""


def is_youtube_trigger(user_speech: str) -> bool:
    u = user_speech or ""
    return "Pesan Live Chat" in u or "(YouTube)" in u


def extract_chat_message(user_speech: str) -> str:
    """Teks chat asli dari wrapper YT atau kutipan PTT."""
    if not user_speech:
        return ""
    m = re.search(r'\]:\s*(.+?)"?\s*$', user_speech.strip().strip('"'))
    if m:
        return m.group(1).strip()
    m2 = re.search(
        r'\[Pesan/Panggilan Sekarang:\]\s*\n\s*"(.+?)"\s*$',
        user_speech,
        re.DOTALL,
    )
    if m2:
        return m2.group(1).strip()
    m3 = re.search(r'"\s*(.+?)\s*"\s*$', user_speech, re.DOTALL)
    if m3 and "Pesan/Panggilan" in user_speech:
        return m3.group(1).strip()
    return user_speech.strip().strip('"')


def classify_yt_message(message: str) -> str:
    """
    brief | normal | deep | gacha
    gacha = tidak yakin → panjang 1–5 kalimat (deterministik per teks pesan).
    """
    t = (message or "").strip()
    if not t:
        return "gacha"
    low = t.lower()
    words = re.sub(r"[^\w\s?]", " ", low).split()
    n = len(words)

    if n <= 2 and (_GREETING_RE.match(low) or _NOISE_RE.match(low)):
        return "brief"
    if n <= 3 and "?" not in t and len(t) < 30:
        return "brief"

    has_q = "?" in t
    has_deep = any(m in low for m in _DEEP_MARKERS)

    if has_q or has_deep:
        if len(t) >= 55 or n >= 10:
            return "deep"
        if len(t) >= 28 or n >= 5:
            return "normal"

    if len(t) >= 95 or n >= 15:
        return "deep"
    if len(t) >= 50 or n >= 9:
        return "normal"

    return "gacha"


def _pick_sentences_deterministic(
    message: str, smin: int, smax: int, salt: str
) -> int:
    if smin >= smax:
        return smin
    payload = f"{salt}|{message}".encode("utf-8")
    h = zlib.adler32(payload) & 0xFFFFFFFF
    return smin + (h % (smax - smin + 1))


def _rant_roll(message: str, chance: float) -> bool:
    """Dadu rant DETERMINISTIK per teks pesan (bukan random.random).

    Dua alasan: (1) get_arti_reply_limits dan live_max_tokens_for_trigger
    masing-masing menghitung plan untuk turn yang sama — dadu sungguhan bisa
    membuat keduanya tidak sepakat (token brief, kalimat rant); (2) testable.
    """
    if chance <= 0:
        return False
    h = zlib.adler32(f"rant|{message}".encode("utf-8")) & 0xFFFFFFFF
    return (h % 1000) < int(chance * 1000)


def resolve_yt_reply_plan(
    user_speech: str, config: dict, *, quiet: bool = False
) -> YtReplyPlan:
    msg = extract_chat_message(user_speech)
    preview = msg[:48] + ("…" if len(msg) > 48 else "")

    if not config.get("arti_reply_yt_adaptive", True):
        sent = int(config.get("arti_reply_max_sentences_yt", 2))
        mode = "fixed"
    else:
        kind = classify_yt_message(msg)
        # RANT MODE (permintaan operator [date removed]): sesekali pertanyaan biasa
        # dijawab panjang banget — tapi HANYA saat chat sepi (`quiet` dari
        # bridge; saat rame antrean sudah drop 213 panggilan di live 11,5 jam,
        # rant cuma memperparah). `deep` dikecualikan — dia sudah punya jatah
        # panjang sendiri; rant justru buat pertanyaan garing yang tiba-tiba
        # dijawab niat, itu komedinya.
        if (
            quiet
            and kind != "deep"
            and _rant_roll(msg, float(config.get("arti_reply_rant_chance", 0.10)))
        ):
            lo = int(config.get("arti_reply_rant_min_sentences", 6))
            hi = int(config.get("arti_reply_rant_max_sentences", 8))
            sent = _pick_sentences_deterministic(msg, lo, hi, "rantlen")
            chars = min(
                int(config.get("arti_reply_rant_chars_cap", 900)),
                int(config.get("arti_reply_yt_chars_base", 40))
                + sent * int(config.get("arti_reply_yt_chars_per_sentence", 95)),
            )
            return YtReplyPlan(
                sentences=sent,
                max_chars=chars,
                max_tokens=_TOKENS_BY_SENT.get(sent, 440),
                mode=f"rant→{sent}kal",
                message_preview=preview,
            )
        if kind == "gacha":
            lo = int(config.get("arti_reply_yt_gacha_min_sentences", 1))
            hi = int(config.get("arti_reply_yt_gacha_max_sentences", 3))
            sent = _pick_sentences_deterministic(msg, lo, hi, "gacha")
            mode = f"gacha→{sent}kal"
        else:
            smin, smax = _SENT_BY_KIND[kind]
            sent = _pick_sentences_deterministic(msg, smin, smax, kind)
            mode = f"{kind}→{sent}kal"

    char_cap = int(config.get("arti_reply_max_chars_yt_cap", 500))
    char_base = int(config.get("arti_reply_yt_chars_base", 40))
    char_per = int(config.get("arti_reply_yt_chars_per_sentence", 95))
    chars = min(char_cap, char_base + sent * char_per)
    tokens = _TOKENS_BY_SENT.get(sent, 200)

    return YtReplyPlan(
        sentences=sent,
        max_chars=chars,
        max_tokens=tokens,
        mode=mode,
        message_preview=preview,
    )


def resolve_reply_plan(
    user_speech: str, config: dict, *, quiet: bool = False
) -> YtReplyPlan:
    """Rencana panjang untuk SEMUA jalur, bukan cuma chat YouTube.

    - yt        : ladder adaptif lama (brief/normal/deep/gacha/rant)
    - inisiatif : celetukan Arti sendiri -> pendek, tetap (bawaan 1 kalimat)
    - streamer  : ucapan Bohan -> ladder yang sama, dinilai dari kalimatnya
    """
    jenis = jenis_giliran(user_speech)
    if jenis == "yt":
        return resolve_yt_reply_plan(user_speech, config, quiet=quiet)

    if jenis == "inisiatif":
        sent = max(1, int(config.get("arti_reply_inisiatif_max_sentences", 1)))
        return YtReplyPlan(
            sentences=sent,
            max_chars=min(
                int(config.get("arti_reply_max_chars_yt_cap", 500)),
                int(config.get("arti_reply_yt_chars_base", 40))
                + sent * int(config.get("arti_reply_yt_chars_per_sentence", 95)),
            ),
            max_tokens=_TOKENS_BY_SENT.get(sent, 110),
            mode=f"inisiatif→{sent}kal",
            message_preview="",
        )

    # streamer
    if not config.get("arti_reply_streamer_adaptive", True):
        sent = int(config.get("arti_reply_max_sentences", 5))
        mode = "streamer-fixed"
    else:
        msg = extract_chat_message(user_speech)
        kind = classify_yt_message(msg)
        if kind == "gacha":
            lo = int(config.get("arti_reply_yt_gacha_min_sentences", 1))
            hi = int(config.get("arti_reply_yt_gacha_max_sentences", 3))
            sent = _pick_sentences_deterministic(msg, lo, hi, "gacha")
        else:
            smin, smax = _SENT_BY_KIND[kind]
            sent = _pick_sentences_deterministic(msg, smin, smax, kind)
        mode = f"streamer-{kind}→{sent}kal"
    return YtReplyPlan(
        sentences=sent,
        max_chars=min(
            int(config.get("arti_reply_max_chars_yt_cap", 500)),
            int(config.get("arti_reply_yt_chars_base", 40))
            + sent * int(config.get("arti_reply_yt_chars_per_sentence", 95)),
        ),
        max_tokens=_TOKENS_BY_SENT.get(sent, 200),
        mode=mode,
        message_preview=extract_chat_message(user_speech)[:48],
    )


def kelas_dari_mode(mode: str) -> str:
    """Ambil KELAS dari string mode yang sudah dipakai di log.

    Bentuk yang mungkin: "deep->3kal", "streamer-deep->3kal", "rant->7kal",
    "gacha->2kal", "inisiatif->1kal", "fixed", "streamer-fixed".

    Kelas sengaja dibaca dari mode, bukan disimpan sebagai field baru di
    YtReplyPlan: dataclass itu dipakai di banyak jalur dan menambah field
    berarti menyentuh semuanya. Mode sudah jadi kontrak yang dicetak ke log.
    """
    m = (mode or "").strip()
    if m.startswith("streamer-"):
        m = m[len("streamer-"):]
    for pemisah in ("→", "->"):
        if pemisah in m:
            return m.split(pemisah, 1)[0].strip()
    return m


def format_reply_instruction(plan: YtReplyPlan) -> str:
    """Tail prompt panjang jawaban untuk jalur NON-YouTube.

    Jalur ini dulu hardcoded "Jawab dalam 2-3 kalimat" dan MENABRAK plan —
    kesalahan yang sama persis yang sudah pernah diperbaiki untuk YouTube
    (komentar di arti_voice_pipeline: 58% jawaban kena potong di live 11,5
    jam). Sekarang prompt, token, dan filter sepakat di semua jalur.
    """
    n = plan.sentences
    if n <= 1:
        return (
            "Balas SATU kalimat saja dalam Bahasa Indonesia — celetukan santai, "
            "bukan penjelasan. DILARANG: monolog, menjelaskan prompt, "
            "atau bilang 'sebagai Arti'."
        )
    if n <= 2:
        return (
            f"Balas max {n} kalimat dalam Bahasa Indonesia, langsung ke point. "
            "DILARANG: monolog, menjelaskan prompt, atau bilang 'sebagai Arti'."
        )
    return (
        f"Ini pertanyaan yang layak dijawab niat — max {n} kalimat "
        "(Bahasa Indonesia), berisi tapi tetap natural seperti co-host. "
        "DILARANG: menjelaskan prompt, meta AI, atau bilang 'sebagai Arti'."
    )


def format_yt_reply_instruction(plan: YtReplyPlan) -> str:
    """Tail prompt untuk LLM (YouTube)."""
    n = plan.sentences
    if plan.mode.startswith("rant"):
        return (
            f"\n\nChat lagi sepi — SEKALI INI boleh ngelantur: jawab panjang sampai "
            f"{n} kalimat (Bahasa Indonesia), bawa cerita/opini/tangen yang seru dari "
            "pertanyaannya, tetap in-character dan menghibur. "
            "DILARANG: menjelaskan prompt, meta AI, atau bilang 'sebagai Arti'."
        )
    if plan.mode.startswith("brief") or n <= 2:
        return (
            f"\n\nJawab singkat: max {n} kalimat ke viewer (Bahasa Indonesia), "
            "langsung ke point + sebut nama mereka. "
            "DILARANG: monolog, menjelaskan prompt, atau bilang 'sebagai Arti'."
        )
    if plan.mode.startswith("deep") or n >= 4:
        return (
            f"\n\nPertanyaan viewer cukup bermakna — jawab sampai {n} kalimat (Bahasa Indonesia): "
            "ada jawaban/opini yang berisi, tetap natural seperti co-host. "
            "DILARANG: menjelaskan prompt, meta AI, atau bilang 'sebagai Arti'."
        )
    return (
        f"\n\nJawab max {n} kalimat ke viewer (Bahasa Indonesia), relevan dan hidup. "
        "DILARANG: monolog panjang, menjelaskan prompt, atau bilang 'sebagai Arti'."
    )

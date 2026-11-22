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

# DIRAPATKAN: lebih dari separuh giliran bisa habis buat Arti bicara, dan
# kebanyakan jawaban 2 kalimat.
# Dulu: brief (1,2) normal (2,3) deep (4,5).
_SENT_BY_KIND = {
    "brief": (1, 1),
    "normal": (2, 2),
    "deep": (3, 4),
}

# Penanda giliran inisiatif/curious — Arti membuka topik sendiri, bukan
# menjawab siapa pun. Ini celetukan, bukan penjelasan.
_INISIATIF_RE = re.compile(r"^\s*\[\s*inisiatif", re.IGNORECASE)


def inisiatif_max_sentences(config: dict | None) -> int:
    """Batas kalimat giliran inisiatif — satu sumber untuk pemotong jawaban DAN
    prompt inisiatif (arti_curious). Dulu prompt minta 3 kalimat sementara
    pemotong menyisakan 1: 32/32 jawaban terpotong, 73% teks dibuang."""
    return max(1, int((config or {}).get("arti_reply_inisiatif_max_sentences", 2)))


def jenis_giliran(user_speech: str) -> str:
    """yt | inisiatif | streamer.

    Dipakai supaya kebijakan panjang berlaku di SEMUA jalur, bukan cuma chat
    YouTube; dulu jalur lain memakai batas datar 5 kalimat tanpa adaptasi.
    """
    u = user_speech or ""
    if is_youtube_trigger(u):
        return "yt"
    if _INISIATIF_RE.match(u):
        return "inisiatif"
    return "streamer"
_TOKENS_BY_SENT = {1: 110, 2: 150, 3: 200, 4: 260, 5: 320, 6: 380, 7: 440, 8: 500}

# ---------------------------------------------------------------------------
# Target vs batas aman (permintaan Bohan 2026-10-23: "[recorded remark omitted]").
#
# Jumlah kalimat di rencana = TARGET yang disebut di prompt, bukan gunting.
# Pemotong pasca-proses cuma memakai BATAS AMAN jauh di atas target:
#   kalimat = max(target x 2, arti_reply_hard_cap_sentences)
#   karakter = max(arti_reply_hard_cap_chars,
#                  min(target_chars x 1,5, arti_reply_hard_cap_chars_max))
# dan memotong HANYA di batas kalimat. Budget max_tokens tiap rencana dinaikkan
# supaya selalu menampung batas aman itu (max_tokens = plafon, bukan ongkos:
# jawaban normal tetap pendek karena targetnya di prompt tidak berubah).
# ---------------------------------------------------------------------------
_HARD_CAP_SENTENCES_FLOOR = 6
_HARD_CAP_CHARS = 900
# Plafon batas aman karakter (rant): angka yang dijanjikan = 1200, bukan
# 900 x 1,5 = 1350 saat arti_reply_rant_chars_cap ikut menggigit.
_HARD_CAP_CHARS_MAX = 1200
# Bahasa Indonesia ~3,5-4,5 char/token di tokenizer Llama/gpt-oss/Gemma —
# pakai 3 (konservatif) + ruang untuk tag [EMOTION:]/[MC:]/[MEMORY_SAVE:].
_CHARS_PER_TOKEN_CONSERVATIVE = 3
_TAG_TOKEN_HEADROOM = 64


def reply_hard_cap(
    target_sentences: int, target_chars: int, config: dict | None = None
) -> tuple[int, int]:
    """(kalimat, karakter) batas aman pemotong — jauh di atas target."""
    cfg = config or {}
    floor = int(cfg.get("arti_reply_hard_cap_sentences", _HARD_CAP_SENTENCES_FLOOR))
    chars = int(cfg.get("arti_reply_hard_cap_chars", _HARD_CAP_CHARS))
    ceiling = int(cfg.get("arti_reply_hard_cap_chars_max", _HARD_CAP_CHARS_MAX))
    return (
        max(int(target_sentences) * 2, floor, 1),
        max(chars, min(-(-int(target_chars) * 3 // 2), ceiling), 1),
    )


def tokens_for_chars(chars: int) -> int:
    """Budget token yang pasti cukup untuk `chars` karakter jawaban."""
    return -(-int(chars) // _CHARS_PER_TOKEN_CONSERVATIVE) + _TAG_TOKEN_HEADROOM


def _plan_tokens(sent: int, chars: int, config: dict | None, default: int) -> int:
    """Token rencana: tabel lama, tapi tidak pernah di bawah batas aman."""
    _, hard_chars = reply_hard_cap(sent, chars, config)
    return max(_TOKENS_BY_SENT.get(sent, default), tokens_for_chars(hard_chars))


# Singkatan yang titiknya BUKAN akhir kalimat. Gelar (Dr. Stone) tidak pernah
# memisah; singkatan daftar (dll./dst.) hanya tidak memisah bila kata
# berikutnya huruf kecil — "makan, minum, dll. Terus…" tetap dua kalimat.
# "Pak."/"Bu." SENGAJA tidak masuk: di obrolan Indonesia "Siap, pak." justru
# akhir kalimat yang umum.
_ABBREV_TITLE = frozenset({"dr", "drs", "prof", "ir", "mr", "mrs", "ms", "jl", "bpk"})
_ABBREV_LIST = frozenset({"dll", "dst", "dsb", "dkk", "tsb"})
_WORD_BEFORE_RE = re.compile(r"([A-Za-z]+)$")
_SPLIT_CANDIDATE_RE = re.compile(r"[.!?]\s+")


def _dot_is_abbreviation(text: str, dot_index: int) -> bool:
    """Titik di `dot_index` milik singkatan (bukan akhir kalimat)?"""
    if text[dot_index] != ".":
        return False
    m = _WORD_BEFORE_RE.search(text[:dot_index])
    if not m:
        return False
    word = m.group(1).lower()
    if word in _ABBREV_TITLE:
        return True
    if word in _ABBREV_LIST:
        nxt = text[dot_index + 1:].lstrip()[:1]
        return bool(nxt) and nxt.islower()
    return False


def split_sentences(text: str) -> list[str]:
    """Pecah teks di [.!?] + spasi, kecuali titik singkatan (Dr., dll. …)."""
    t = (text or "").strip()
    parts: list[str] = []
    start = 0
    for m in _SPLIT_CANDIDATE_RE.finditer(t):
        if _dot_is_abbreviation(t, m.start()):
            continue
        parts.append(t[start:m.start() + 1])
        start = m.end()
    parts.append(t[start:])
    return [p.strip() for p in parts if p.strip()]


# Akhir kalimat untuk membuang kalimat terakhir yang terpotong max_tokens:
# tanda baca biasa, ATAU akhiran santai yang memang menutup kalimat di chat
# Indonesia — emoji, ":)"/":D", "~", tawa "wkwk"/"haha"/"hehe". Boleh diikuti
# kutip/kurung tutup dan tag utuh ([EMOTION: …]) yang ikut dipertahankan.
_EMOJI = "\U0001F300-\U0001FAFF☀-➿️‍"
_SOFT_END = (
    r"(?:[.!?…]+|~+|[:;]-?[)(DPp]+|\^\^"
    r"|\b(?:wk)+w?k*\b|\b(?:ha){2,}h?\b|\b(?:he){2,}\b|\b(?:hi){2,}\b|\b(?:xi){2,}\b"
    rf"|[{_EMOJI}]+)"
)
_TERMINAL_RE = re.compile(
    _SOFT_END + r"[\"'”’)\]]*(?:\s*\[[^\[\]]*\])*(?=\s|$)", re.IGNORECASE
)
_ONLY_TAGS_RE = re.compile(r"^(?:\s*\[[^\[\]]*\])+\s*$")
_CUT_FINISH = {"length", "max_tokens", "max_output_tokens"}


def drop_partial_tail(text: str) -> str:
    """Buang ekor setelah kalimat utuh terakhir (kalimat yang terpotong).

    Tidak ada kalimat utuh sama sekali -> teks dikembalikan apa adanya
    (lebih baik sepotong daripada diam; budget token membuat ini langka).
    Ekor yang cuma tag utuh ("[EMOTION: senang]") dianggap lengkap; akhiran
    emoji/":)"/"~"/"wkwk" dihitung akhir kalimat.
    """
    t = (text or "").rstrip()
    end = 0
    for m in _TERMINAL_RE.finditer(t):
        if _dot_is_abbreviation(t, m.start()):
            continue
        end = m.end()
    if end <= 0:
        return t
    tail = t[end:]
    if not tail.strip() or _ONLY_TAGS_RE.match(tail):
        return t
    return t[:end].rstrip()


def is_length_finish(finish_reason) -> bool:
    return str(finish_reason or "").strip().lower() in _CUT_FINISH


def trim_if_length_cut(text, finish_reason, source: str = "") -> str:
    """Provider melapor finish=length/MAX_TOKENS -> jangan ucapkan potongan."""
    if not text or not is_length_finish(finish_reason):
        return text
    out = drop_partial_tail(str(text))
    if out != str(text).rstrip():
        print(
            f"[Filter] {source or 'LLM'} finish={finish_reason}: kalimat terakhir "
            f"yang kepotong dibuang ({len(str(text))}→{len(out)}ch)"
        )
    return out


def looks_token_cut(text: str, max_tokens: int) -> bool:
    """Heuristik bila finish_reason tidak tersedia: jawaban mendekati budget
    (>= 2 char/token, sangat pesimis) DAN tidak berakhir dengan tanda baca."""
    t = (text or "").rstrip()
    if not t or len(t) < 2 * int(max_tokens):
        return False
    return drop_partial_tail(t) != t


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
    "exampleviewr586" jadi panjang dan kaku. Aturan: buang @, buang angka/
    separator ekor, ambil kata pertama (pecah camelCase & _-.), buang suffix
    platform (sampleyt -> sample), potong maksimal `max_len` huruf.
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
        # RANT MODE (permintaan Bohan 2026-08-01): sesekali pertanyaan biasa
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
                max_tokens=_plan_tokens(sent, chars, config, 440),
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
    tokens = _plan_tokens(sent, chars, config, 200)

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
        sent = inisiatif_max_sentences(config)
        chars = min(
            int(config.get("arti_reply_max_chars_yt_cap", 500)),
            int(config.get("arti_reply_yt_chars_base", 40))
            + sent * int(config.get("arti_reply_yt_chars_per_sentence", 95)),
        )
        return YtReplyPlan(
            sentences=sent,
            max_chars=chars,
            max_tokens=_plan_tokens(sent, chars, config, 110),
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
    chars = min(
        int(config.get("arti_reply_max_chars_yt_cap", 500)),
        int(config.get("arti_reply_yt_chars_base", 40))
        + sent * int(config.get("arti_reply_yt_chars_per_sentence", 95)),
    )
    return YtReplyPlan(
        sentences=sent,
        max_chars=chars,
        max_tokens=_plan_tokens(sent, chars, config, 200),
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


# Kalimat penutup instruksi panjang: angka rencana = TARGET, bukan gunting.
# Pemotong pasca-proses cuma batas aman (reply_hard_cap), jadi model boleh
# menuntaskan pikiran — yang dilarang berhenti di tengah kalimat.
# "Boleh lebih" HANYA untuk target >= 3 kalimat (review 2026-10-23): di target
# 1-2 kalimat (mayoritas giliran YT) undangan itu memanjangkan jawaban, dan
# jawaban panjang menahan antrean chat (TTL 60 dtk) / momen Live7 (28 dtk).
SELESAIKAN_PIKIRAN = "Selesaikan pikiranmu — jangan berhenti di tengah kalimat."
BOLEH_LEBIH = (
    "Boleh lebih kalau memang perlu, tapi selesaikan pikiranmu — "
    "jangan berhenti di tengah kalimat."
)


def _tutup_panjang(n: int) -> str:
    return BOLEH_LEBIH if n >= 3 else SELESAIKAN_PIKIRAN


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
            "Balas biasanya cukup SATU kalimat dalam Bahasa Indonesia — celetukan "
            f"santai, bukan penjelasan. {_tutup_panjang(n)} DILARANG: monolog, "
            "menjelaskan prompt, atau bilang 'sebagai Arti'."
        )
    if n <= 2:
        return (
            f"Balas biasanya {n} kalimat dalam Bahasa Indonesia, langsung ke point. "
            f"{_tutup_panjang(n)} "
            "DILARANG: monolog, menjelaskan prompt, atau bilang 'sebagai Arti'."
        )
    return (
        f"Ini pertanyaan yang layak dijawab niat — biasanya {n} kalimat "
        "(Bahasa Indonesia), berisi tapi tetap natural seperti co-host. "
        f"{_tutup_panjang(n)} "
        "DILARANG: menjelaskan prompt, meta AI, atau bilang 'sebagai Arti'."
    )


def format_yt_reply_instruction(plan: YtReplyPlan) -> str:
    """Tail prompt untuk LLM (YouTube)."""
    n = plan.sentences
    if plan.mode.startswith("rant"):
        return (
            f"\n\nChat lagi sepi — SEKALI INI boleh ngelantur: jawab panjang sekitar "
            f"{n} kalimat (Bahasa Indonesia), bawa cerita/opini/tangen yang seru dari "
            "pertanyaannya, tetap in-character dan menghibur. Selesaikan pikiranmu — "
            "jangan berhenti di tengah kalimat. "
            "DILARANG: menjelaskan prompt, meta AI, atau bilang 'sebagai Arti'."
        )
    if plan.mode.startswith("brief") or n <= 2:
        return (
            f"\n\nJawab singkat: biasanya {n} kalimat ke viewer (Bahasa Indonesia), "
            f"langsung ke point + sebut nama mereka. {_tutup_panjang(n)} "
            "DILARANG: monolog, menjelaskan prompt, atau bilang 'sebagai Arti'."
        )
    if plan.mode.startswith("deep") or n >= 4:
        return (
            f"\n\nPertanyaan viewer cukup bermakna — jawab sekitar {n} kalimat (Bahasa Indonesia): "
            f"ada jawaban/opini yang berisi, tetap natural seperti co-host. {_tutup_panjang(n)} "
            "DILARANG: menjelaskan prompt, meta AI, atau bilang 'sebagai Arti'."
        )
    return (
        f"\n\nJawab biasanya {n} kalimat ke viewer (Bahasa Indonesia), relevan dan hidup. "
        f"{_tutup_panjang(n)} "
        "DILARANG: monolog panjang, menjelaskan prompt, atau bilang 'sebagai Arti'."
    )

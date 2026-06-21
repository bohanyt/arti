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

_SENT_BY_KIND = {
    "brief": (1, 2),
    "normal": (2, 3),
    "deep": (4, 5),
}
_TOKENS_BY_SENT = {1: 110, 2: 150, 3: 200, 4: 260, 5: 320}


@dataclass(frozen=True)
class YtReplyPlan:
    sentences: int
    max_chars: int
    max_tokens: int
    mode: str
    message_preview: str = ""


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


def resolve_yt_reply_plan(user_speech: str, config: dict) -> YtReplyPlan:
    msg = extract_chat_message(user_speech)
    preview = msg[:48] + ("…" if len(msg) > 48 else "")

    if not config.get("arti_reply_yt_adaptive", True):
        sent = int(config.get("arti_reply_max_sentences_yt", 2))
        mode = "fixed"
    else:
        kind = classify_yt_message(msg)
        if kind == "gacha":
            lo = int(config.get("arti_reply_yt_gacha_min_sentences", 1))
            hi = int(config.get("arti_reply_yt_gacha_max_sentences", 5))
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


def format_yt_reply_instruction(plan: YtReplyPlan) -> str:
    """Tail prompt untuk LLM (YouTube)."""
    n = plan.sentences
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

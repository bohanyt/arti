"""Reply text filters and sentence handling for the voice path."""

import re

import arti_groq_stream
import arti_reply_policy


_TTS_EXPRESSION_TAG_RE = re.compile(
    r"<\s*(laugh|sigh|breath|chuckle|giggle)\s*>",
    re.IGNORECASE,
)
_ANY_ANGLE_TAG_RE = re.compile(r"<\s*[a-zA-Z][a-zA-Z0-9_-]*\s*>")


def strip_tts_expression_tags(text: str) -> str:
    """Tag <laugh> dll → fonetik TTS; sigh/breath → hhh/hah (bukan kata 'sigh')."""
    if not text:
        return ""

    laugh_idx = 0
    laugh_variants = ("haha", "hehe", "hihi")

    def _repl_tag(match: re.Match) -> str:
        nonlocal laugh_idx
        tag = match.group(1).lower()
        if tag in ("laugh", "chuckle", "giggle"):
            word = laugh_variants[laugh_idx % len(laugh_variants)]
            laugh_idx += 1
            return f" {word}"
        if tag == "sigh":
            return " hhh"
        if tag == "breath":
            return " hah"
        return ""

    out = _TTS_EXPRESSION_TAG_RE.sub(_repl_tag, text)
    out = _ANY_ANGLE_TAG_RE.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.!?])", r"\1", out)
    out = re.sub(
        r"\b(haha|hehe|hihi)(\s+\1\b)+",
        r"\1",
        out,
        flags=re.IGNORECASE,
    )
    return out.strip()


def clean_ai_reply(text):
    """Membersihkan yapping bahasa Inggris, membuang teks di dalam tanda bintang (bintang tunggal/ganda), dan menyisakan jawaban Indonesia murni menggunakan filter bahasa statistik."""
    if not text:
        return ""
    # 0. Hapus blok <think>...</think> dari model yang support "thinking" (Qwen3, dll)
    #    Handle juga kasus truncated (max_tokens motong di tengah <think>)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL).strip()  # Truncated: no closing tag
    # 1. Hapus teks di dalam bintang ganda (bold) atau bintang tunggal (italic)
    text = re.sub(r"\*\*.*?\*\*", "", text)
    text = re.sub(r"\*.*?\*", "", text)
    
    # 2. Split menjadi kalimat-kalimat untuk memfilter yapping per kalimat
    sentences = re.split(r'(?<=[.!?])\s+', text)
    cleaned_sentences = []
    
    # Kumpulan kata bahasa Inggris umum (stop words) untuk penyaringan bahasa statistik
    english_words = {
        # Grammatical particles / pronouns
        "is", "it", "it's", "ready", "for", "immediate", "use", "the", "to", "and", "of", "in", 
        "that", "this", "with", "from", "you", "are", "not", "no", "shut", "up", "i", "have", "devis", 
        "respond", "interpret", "streamer", "parameter", "context", "analyze", "utterance", "witty", 
        "cute", "proceed", "response", "believe", "got", "my", "profile", "playful", "nonsensical", 
        "opportunity", "making", "good", "progress", "active", "ready", "confirm", "available", 
        "availability", "will", "would", "should", "could", "can", "do", "does", "did", "have", 
        "has", "had", "been", "was", "were", "be", "am", "are", "your", "yours", "me", "my", "myself",
        "an", "but", "or", "as", "if", "so", "than", "then", "there", "their", "them", "they", "we",
        "i've", "rare", "leaning", "persona", "crafting", "emphasizes", "qualities", "precise", "aim",
        "concise", "maintain", "existing", "memory", "emojis", "avoided", "kept", "previous", "feedback",
        # Conversational English words (very common in yapping)
        "here", "let's", "make", "amazing", "i'm", "bad", "bro", "hello", "hi", "great", "good", "fine",
        "stream", "co-host", "ai", "intelligent", "talk", "speak", "chat", "conversation", "dialogue",
        "words", "shutting", "talking", "yapping", "thought", "think", "thought", "how", "what", "why",
        "when", "where", "who", "which", "whose", "whom", "about", "above", "below", "under", "over"
    }
    
    for sentence in sentences:
        words = [w.strip(".,!?\"'()").lower() for w in sentence.split() if w.strip()]
        if not words:
            continue
            
        # Hitung rasio kata bahasa Inggris dalam kalimat
        english_count = sum(1 for w in words if w in english_words)
        
        # Jika kalimat MAYORITAS bahasa Inggris (>=60% kata Inggris DAN minimal 4 kata Inggris),
        # baru buang sebagai yapping. Threshold rendah sebelumnya (2 kata/30%) terlalu agresif
        # karena bahasa Indonesia sering campur kata Inggris ("aku lagi chat di YouTube").
        if english_count >= 4 and (english_count / len(words)) >= 0.6:
            continue
            
        cleaned_sentences.append(sentence.strip())
        
    final_text = " ".join(cleaned_sentences).strip()
    return final_text


_BAD_ARTICULATION_PATTERNS = re.compile(
    r"|".join(
        [
            r"membaca\s+\d*\s*catatan",
            r"membaca\s+(sejarah|history|log)",
            r"catatan\s+sejarah",
            r"buku\s+sejarah",
            r"sejarah\s+(stream|chat|percakapan)",
            r"mengingat\s+(chat|sejarah|percakapan|dari\s+konteks)",
            r"aku\s+ingat",
            r"(kamu\s+)?ingat\s+viewer",
            r"bilang.*ingat",
            r"history\s+stream",
            r"\d+\s+catatan",
            r"log\s+(chat|stream)",
            r"sebagai\s+arti",
            r"arti\s+menerima",
            r"menerima\s+panggilan",
            r"menanggapi\s+panggilan",
            r"giliran\s+aku\s+sebagai",
            r"aku\s+harus\s+respon",
            r"aku\s+perlu\s+(memilih|ingat|menjawab)",
            r"bukan\s+sebagai\s+ai",
            r"langsung\s+sebagai\s+arti",
            r"sekarang\s+arti\s+(lagi|menerima)",
            r"perlu\s+ingat\s+aturan",
            r"ini\s+adalah\s+lanjutan",
            r"aturan\s*:\s*\d",
            r"arti\s+ini\s+dipanggil",
            r"pertanyaan\s+atau\s+panggilan",
            r"panggilan\s+sekarang\s+adalah",
            r"harus\s+merespons",
            r"ini\s+kan\s+ucapan",
            r"dengan\s+gaya\s+yang\s+sesuai",
            r"karena\s+aku\s+ingat\s+dari\s+konteks",
            r"langsung\s+merespons\s+sebagai",
        ]
    ),
    re.IGNORECASE,
)


def _sentence_is_bad_articulation(sentence: str) -> bool:
    return bool(_BAD_ARTICULATION_PATTERNS.search(sentence))


def filter_meta_history_talk(text: str) -> tuple[str, int]:
    """Buang kalimat meta; return (teks bersih, jumlah kalimat dibuang)."""
    if not text:
        return "", 0
    kept = []
    removed = 0
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        s = sentence.strip()
        if not s:
            continue
        if _sentence_is_bad_articulation(s):
            removed += 1
            print(f"[Filter] Meta/narrator dihapus: {s[:72]}...")
            continue
        kept.append(s)
    return " ".join(kept).strip(), removed


def _trim_stream_reply(full: str, finish_reason, max_tokens: int, source: str) -> str:
    """Jawaban stream: pakai finish_reason dari chunk SSE terakhir kalau ada.

    Heuristik (dekat budget + tanpa tanda baca akhir) hanya cadangan saat
    stream tidak melapor finish_reason — heuristik itu meleset di gpt-oss,
    yang nalarnya berbagi budget sehingga content berhenti di bawah ambang.
    """
    if finish_reason:
        return arti_reply_policy.trim_if_length_cut(full, finish_reason, source)
    if arti_reply_policy.looks_token_cut(full, max_tokens):
        return arti_reply_policy.trim_if_length_cut(full, "length", f"{source} (heuristik)")
    return full


def _truncate_reply_length(
    text: str, max_sentences: int, max_chars: int, runon_chars: int | None = None
) -> str:
    """Batas AMAN jawaban — potong HANYA di batas kalimat (2026-10-23).

    Dulu memotong di jumlah kalimat target lalu di koma/kata kalau karakter
    lewat — Arti berhenti di tengah pikiran. Sekarang `max_sentences` /
    `max_chars` adalah batas aman (arti_reply_policy.reply_hard_cap) dan:
    - kalimat yang akan terbelah batas karakter dibuang UTUH;
    - kalimat pertama (atau pembuka pendek "Iya!" + satu kalimat panjang)
      dipertahankan utuh, kecuali lebih dari 2x `runon_chars` (keluaran kabur
      tanpa tanda baca) — baru di situ pemotong klausa lama dipakai sebagai
      rem darurat, memotong ke ~`runon_chars`. `runon_chars` = batas aman
      dasar (900), bukan batas rant, jadi kalimat kabur tidak pernah > 1800
      dan hasil rem <= 900.
    """
    sentences = arti_reply_policy.split_sentences(text)
    sentences = [re.sub(r"\s+", " ", s).strip() for s in sentences]
    if max_chars <= 0 or max_sentences <= 0 or not sentences:
        return ""
    runon = max_chars if runon_chars is None else max(1, min(runon_chars, max_chars))
    kept: list[str] = []
    for sentence in sentences[:max_sentences]:
        joined = " ".join(kept + [sentence])
        if kept and len(joined) > max_chars:
            if len(" ".join(kept)) < 0.25 * max_chars:
                # Yang tersisa cuma pembuka ("Iya!"): jangan jadi celetukan.
                # Aturan sama dengan kalimat pertama — utuh sampai 2x runon,
                # rem darurat hanya di atas itu.
                if len(joined) > 2 * runon:
                    return _runaway_clause_cut(joined, runon)
                kept.append(sentence)
            break
        kept.append(sentence)
    result = " ".join(kept)
    if len(result) <= 2 * runon or max(len(s) for s in kept) <= 2 * runon:
        return result
    return _runaway_clause_cut(result, runon)


def _runaway_clause_cut(result: str, max_chars: int) -> str:
    """Rem darurat untuk SATU kalimat kabur > 2x batas aman (tanpa titik).

    Ini pemotong lama (klausa/kata + "…"); sekarang hanya untuk keluaran
    kabur, bukan jawaban normal yang panjang.
    """
    if len(result) <= max_chars:
        return result
    if max_chars <= 0:
        return ""
    cut = result[:max_chars - 1]
    # Prefer the last complete sentence or clause, but only when it keeps at
    # least 60% of the budget: an opener like "Hah," / "Iya!" must never
    # become the whole reply. Boundaries are judged on the full text, so the
    # "." in "Rp10.000" or the "," in "3,5" never counts as one.
    boundary = 0
    for m in re.finditer(r"[.!?,;:](?=\s|$)", result):
        if m.end() > len(cut):
            break
        boundary = m.end()
    if boundary >= 0.6 * max_chars and " " in cut[:boundary]:
        cut = cut[:boundary]
        if cut.endswith((".", "!", "?")):
            return cut
    elif not result[len(cut)].isspace():
        head = cut.rsplit(" ", 1)[0] if " " in cut else ""
        # One leftover word ("Hah,") would be a lone interjection; keep the
        # hard cut instead.
        cut = head if not head or " " in head else cut
    return cut.rstrip(" ,;:") + "…"


def _sentences_or_empty(text: str) -> list[str]:
    """Kontrak sama dengan jalur Groq: list hanya diisi kalau >1 kalimat."""
    try:
        parts = arti_groq_stream.split_indonesian_sentences(text)
        return parts if len(parts) > 1 else []
    except Exception:  # noqa: BLE001
        return []


# qwen di Groq sesekali menyelipkan aksara CJK ke jawaban Indonesia; TTS
# membacanya kacau. Rentang: kana, CJK unified, hangul, fullwidth forms.
_CJK_RE = re.compile(r"[　-ヿ㐀-鿿가-힯＀-￯]+")


def _shorten_viewer_handles(text: str, user_speech: str | None) -> str:
    """Ganti handle panjang di jawaban dengan nama panggilan pendek.

    Jaring pengaman kedua (yang pertama: instruksi nick di prompt) — TTS tidak
    boleh membaca "exampleviewr586" bulat-bulat. Dua sasaran:
    1. Handle viewer TURN INI (dengan/tanpa @, case-insensitive).
    2. Token gaya-handle eksplisit ber-@ dengan >=2 digit ekor di mana pun.
    """
    handle = arti_reply_policy.extract_viewer_handle(user_speech or "")
    if handle:
        nick = arti_reply_policy.viewer_nickname(handle)
        bare = re.escape(handle.lstrip("@"))
        if nick and nick.lower() != handle.lstrip("@").lower():
            text = re.sub(rf"@?{bare}\b", nick, text, flags=re.IGNORECASE)
    def _nick_at(m: re.Match) -> str:
        return arti_reply_policy.viewer_nickname(m.group(0)) or m.group(0)
    return re.sub(r"@[A-Za-z][\w.\-]*\d{2,}\b", _nick_at, text)

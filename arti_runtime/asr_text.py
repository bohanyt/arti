"""Text normalization and hallucination filtering for ASR transcripts."""

import collections
import re


# Pola literal output Whisper dari noise — sering persis begini (kapital + titik)
_ASR_LITERAL_PHANTOMS = frozenset({
    "Arti, Bohan, Stardew Valley.",
    "Terima kasih.",
    "Thank you.",
    "Thanks.",
    "Selamat menikmati.",
    "Terima kasih telah menonton.",
    "Terima kasih sudah menonton.",
    "Thank you for watching.",
    "Thanks for watching.",
    "Like and subscribe.",
    "Sampai jumpa.",
})
# Tanpa titik / lowercase: cek lagi pakai durasi + jarak dari TTS
_ASR_BARE_THANKS = frozenset({"terima kasih", "thank you", "thanks", "Terima kasih", "Thank you", "Thanks"})
_ASR_ALWAYS_NOISE = frozenset({
    "selamat menikmati", "terima kasih telah menonton", "selamat datang", "halo halo",
    "ya ya ya", "oke oke", "terima kasih sudah menonton", "like and subscribe",
    "sampai jumpa", "goodbye", "bye bye", "i mean",
    "thank you for watching", "thanks for watching",
})
_ASR_NOISE_SUBSTRINGS = (
    "terima kasih telah menonton",
    "terima kasih sudah menonton",
    "like and subscribe",
    "thank you for watching",
)


def _normalize_asr_text(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def filter_whisper_hallucination(text, is_passive_monitoring=True):
    """Menyaring halusinasi khas Whisper dari noise/ambient.
    
    Lebih smart dari versi lama:
    - Filter kata tunggal yang meaningless TAPI hanya kalau itu hasil transkrip sendiri
      (bukan bagian dari percakutan nyata — cek via is_passive_monitoring)
    - Filter repetitive patterns (Whisper ngulang kata karena bingung noise)
    """
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""
    
    # Phrases yang SELALU jadi hallucination Whisper (ignoring context selalu)
    # Ini pola yang dikeluarkan Whisper sendiri kalau mendengar noise/static.
    phantom_phrases = {
        "subscribe", "like and subscribe", "thank you for watching",
        "thanks for watching", "see you next time", "salam sejahtera",
        "wassalamualaikum", "wassalamu'alaikum",
        "selamat menikmati",
    }
    text_clean = text.lower().strip(".,!?")
    if text_clean in phantom_phrases:
        return ""
    
    # Phrases yang HANYA hallucination kalau ini hasil transkrip pasif (bukan Bohan ngomong langsung)
    # Ini kata yang Whisper salah tangkap dari noise, tapi BISA jadi real speech
    contextual_hallucinations = {
        "terima kasih kerana menonton", "terima kasih kerana menonton!",
        "terima kasih guys", "sampai jumpa lagi",
    }
    if is_passive_monitoring and text_clean in contextual_hallucinations:
        return ""
    
    # Filter kata berulang (repetitive hallucination)
    # Whisper kadang ngulang kata yang sama berkali-kali kalau bingung noise
    words = text.split()
    if len(words) > 6:
        counter = collections.Counter(words)
        most_common_word, count = counter.most_common(1)[0]
        if count / len(words) > 0.6:
            return ""  # Return empty, bukan kata itu sendiri
    
    # Filter kata tunggal yang meaningless TAPI ekornya aja
    # Kalau Bohan beneran bilang "tidak" atau "bye" sendiri, biarin masuk
    # (handle via context di LLM, bukan di filter)
    meaningless_single = {"ah", "oh", "uh", "eh", "hm", "hmm"}
    if len(words) == 1 and text_clean.rstrip(".!?") in meaningless_single:
        return ""
    
    return text

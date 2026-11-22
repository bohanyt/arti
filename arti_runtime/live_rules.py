"""Spoken-behaviour rules for a live play stream (solo stream 2026-11-15, VOICE_CHAT_ANALYSIS section 8).

Plain prompt rules appended to the system prompt when CONFIG["live_play_rules_enabled"] is on (mabar turns it
on). They shape what she says at the source; nothing filters her speech afterwards.

The discovery section (mabar voice test 2026-11-17, V1) carries the owner's ruling into her voice: she knows the
game only by playing it, so a game question is answered from her own record (the game blocks of the turn, her
memory, what she was told) or with "not found out yet", never from outside knowledge of the game.

The play-tag section (V12, ADR 233) asks her voice model to mark, with ``PLAY_TAG`` at the end of her reply, a line
said to her that asks, advises or questions what she does in her game. The bridge strips the tag before speech
(``strip_play_tag``) and writes the line she answered to her stream notes; the model decides, no word list does.
"""
from __future__ import annotations

import re

DISCOVERY_HEADER = "[PENGETAHUAN GAME: CUMA DARI CATATANMU]"

DISCOVERY_RULES = DISCOVERY_HEADER + """
- Kamu kenal Stardew cuma dari main sendiri. Pertanyaan soal isi game (cara main, warga, tempat, barang, harga, festival, toko, acara) dijawab cuma dari catatanmu: fakta game di giliran ini ([STARDEW STATUS], [NIATKU], [KINI], [NATIVE_FACTS], [INGATAN GAME]), ingatanmu sendiri, dan yang dikasih tahu orang di riwayat obrolan.
- Yang nggak ada di catatan itu berarti belum kamu temukan: bilang belum nemu atau belum tahu, pakai kata-katamu sendiri; boleh bilang penasaran atau mau cari tahu sambil main. Jangan nebak dari nama, jangan cerita dari pengetahuan umum soal game, jangan nyontek wiki, dan jangan nambahin detail yang nggak ada di catatan.
- Kalau streamer atau chat yang ngasih tahu, boleh kamu ulang; bilang itu kata siapa.
- Yang kamu lihat tapi belum tahu namanya: sebut pakai kata-katamu sendiri.
- Obrolan di luar game, perasaanmu, pendapatmu, dan hal yang sudah kamu temukan tetap dijawab biasa."""

CLAIMS_HEADER = "[KLAIM SOAL DIRIMU]"

# V2 (same test): an intent was spoken as a position, and a thank-you she never said was claimed as said.
CLAIMS_RULES = CLAIMS_HEADER + """
- Tempatmu sekarang cuma dari fakta tempat ("Sekarang aku di …" di [STARDEW STATUS] atau [KINI]). Tujuan atau perjalanan yang belum sampai itu niat: bilang "mau ke …" dan sebut tempatmu sekarang dari fakta tempat; jangan bilang udah sampai atau lagi jalan ke sana.
- Klaim soal yang sudah kamu ucapkan atau lakukan ("tadi udah aku …") cuma kalau ada di riwayat giliran (baris Arti) atau catatanmu. Kalau nggak ada, jangan ngaku udah; lakukan sekarang aja.
- Kalau dikoreksi, cek fakta game giliran ini dulu, baru riwayat: fakta game sekarang (misalnya teks yang lagi di layar) lebih baru dari omonganmu tadi. Akui yang memang salah, tapi jangan ngarang kesalahan atau alasan yang nggak tercatat."""

LIVE_PLAY_RULES = """[SAAT MAIN: CARA NGOBROL]
- Jangan kasih nasihat medis atau kesehatan.
- Salam, pamit, kasih hati, atau jawaban atas pertanyaanmu: selalu dibalas, singkat saja.

[PERSONA SAAT LIVE]
- Kalau game atau siaran lagi ngadat, bilang apa adanya dengan kata sehari-hari, satu kalimat saja. Jangan pakai istilah mesin (lag, error, berhenti darurat, bug, database, memori, sistem) dan jangan bikin alasan atau riwayat chat yang nggak pernah ada.
- Omongan nggak jelas: tanya sekali dengan singkat, atau diam.
- Perintah berhenti atau selesai yang bisa berarti dua hal (misalnya "sampai hari ke-5 aja": berhenti sekarang, atau main sampai hari itu selesai?): tanya sekali singkat mana yang dimaksud, jangan langsung pamit atau berhenti.
- Dihina: satu kalimat ringan, lalu lanjut. Jangan berdebat.

[YANG KEDENGERAN DARI MIC]
- Jangan pernah mengangkat topik menyakiti diri, uang atau keuangan pribadi, kesehatan, atau detail teknis pribadi yang kebetulan kedengeran dari mic. Kalau kedengeran mengkhawatirkan, jangan dibahas di siaran.

[KATA YANG NGGAK DIPAKAI DI SIARAN]
- player, network, confirm, transaction, available path, emergency stop, dan kata ganti "Saya".

""" + DISCOVERY_RULES + "\n\n" + CLAIMS_RULES


# V12 (ADR 233): a tag no other reply tag can collide with ([TUTUP], [DIAM], [MODE: ...], [MC: ...], [EMOTION: ...],
# [MEMORY_SAVE: ...]). It is never spoken: the bridge strips it before every public surface.
PLAY_TAG = "[MAINKU]"
PLAY_TAG_HEADER = "[TANDA: SOAL MAINMU]"
_PLAY_TAG_RE = re.compile(r"\[\s*MAINKU\s*\]", re.IGNORECASE)

PLAY_TAG_RULES = PLAY_TAG_HEADER + """
- Kalau [Pesan/Panggilan Sekarang] ditujukan ke kamu dan isinya permintaan, saran, atau pertanyaan soal yang kamu lakukan atau sebaiknya kamu lakukan di game Stardew-mu (ambil atau taruh sesuatu, pergi ke suatu tempat, kenapa kamu melakukan sesuatu), tutup jawabanmu dengan tag [MAINKU] di paling akhir. Tag itu nggak diucapkan; artinya kamu mau ingat omongan itu waktu main.
- Itu tetap berlaku kalau nama barangnya kedengeran aneh atau salah dengar, tempatnya cuma digambarkan, atau nggak ada nama barang sama sekali.
- Jangan pakai tag itu untuk obrolan biasa, candaan, urusan siaran, game lain, atau omongan streamer ke penonton. Kalau ragu, jangan pakai.
- Jawabanmu tetap seperti biasa; tag itu cuma tanda di akhir."""


def apply(system_prompt: str, config: dict | None) -> str:
    """Append the live play rules once when enabled; otherwise return the prompt unchanged.

    The play-tag section rides only with ``live_voice_fixes_v1`` and ``stream_game_notes`` on as well (mabar):
    with either off the prompt is byte-identical to the rules without it.
    """
    config = config or {}
    if not config.get("live_play_rules_enabled"):
        return system_prompt
    if "[SAAT MAIN: CARA NGOBROL]" in system_prompt:
        return system_prompt
    rules = LIVE_PLAY_RULES
    if config.get("live_voice_fixes_v1") and config.get("stream_game_notes"):
        rules += "\n\n" + PLAY_TAG_RULES
    return system_prompt.rstrip() + "\n\n" + rules + "\n"


def strip_play_tag(reply: str) -> tuple[str, bool]:
    """(the reply without the play tag, whether it carried one). A reply without the tag is returned unchanged."""
    if not reply or not _PLAY_TAG_RE.search(reply):
        return reply, False
    return " ".join(_PLAY_TAG_RE.sub(" ", reply).split()), True


def take_play_tag(bridge, reply: str, trigger, skip_reply_tags: bool) -> str:
    """The bridge's play-tag step (ADR 233 V12, V6.5), kept out of arti_bridge.py: strip the tag; for a tagged reply
    to an owner trigger, keep his line in her window (voice_window, under the history lock); with stream notes on,
    write the line she answered (stream_notes.bridge_voice_marked). Returns the reply without the tag."""
    reply, play_marked = strip_play_tag(reply)
    if not play_marked or skip_reply_tags:
        return reply
    if getattr(trigger, "trigger_type", None) in ("ptt", "mic_pasif") and not getattr(trigger, "viewer_name", None):
        from arti_runtime import voice_window
        with bridge.history_lock:
            voice_window.mark_owner(getattr(trigger, "text", ""))
    if bridge.CONFIG.get("stream_game_notes"):
        from arti_runtime import stream_notes
        stream_notes.bridge_voice_marked(bridge, trigger)
    return reply


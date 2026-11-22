"""Penjaga: kebijakan panjang jawaban berlaku di SEMUA jalur, bukan cuma YT.

Latar: lebih dari separuh giliran bisa habis untuk Arti BICARA. Sumbernya:
kebijakan adaptif cuma dipakai jalur chat YouTube; jalur lain memakai batas
datar 5 kalimat DAN prompt yang hardcoded "2-3 kalimat".
"""

import arti_reply_policy as P
import arti_bridge as H

_INISIATIF = "[Inisiatif — buka topik sendiri]\nStream lagi hening..."
_YT = "[Pesan Live Chat dari Viewer @ContohViewer (YouTube)]: halo"


def test_jenis_giliran_membedakan_tiga_jalur():
    assert P.jenis_giliran(_YT) == "yt"
    assert P.jenis_giliran(_INISIATIF) == "inisiatif"
    assert P.jenis_giliran("iya gas") == "streamer"
    assert P.jenis_giliran("") == "streamer"


def test_inisiatif_target_dua_kalimat():
    """Arti membuka topik sendiri = celetukan, bukan penjelasan.

    Ini sumber terbesar jawaban panjang — dulu target 1, sekarang target 2
    karena Arti terasa terlalu diam.
    Angka ini target di prompt; pemotong cuma batas aman (reply_hard_cap).
    """
    p = P.resolve_reply_plan(_INISIATIF, H.CONFIG)
    assert p.sentences == 2, p.mode


def test_celetukan_streamer_pendek_pertanyaan_bermakna_panjang():
    pendek = P.resolve_reply_plan("iya", H.CONFIG)
    dalam = P.resolve_reply_plan(
        "menurut kamu gimana cara bikin setting grafis minecraft biar lancar",
        H.CONFIG,
    )
    assert pendek.sentences == 1, pendek.mode
    assert dalam.sentences >= 3, dalam.mode
    assert dalam.sentences > pendek.sentences


def test_tidak_ada_jalur_yang_masih_boleh_lima_kalimat():
    """Batas datar 5 kalimat tidak boleh muncul lagi di jalur mana pun.

    Kecuali kalau Bohan sengaja mematikan adaptif streamer.
    """
    for teks in (_INISIATIF, "iya", "coba dong", _YT, "kenapa kamu suka minecraft"):
        p = P.resolve_reply_plan(teks, H.CONFIG)
        assert p.sentences <= 4, (teks, p.mode, p.sentences)


def test_saklar_mati_kembali_ke_batas_datar():
    cfg = dict(H.CONFIG)
    cfg["arti_reply_streamer_adaptive"] = False
    cfg["arti_reply_max_sentences"] = 5
    p = P.resolve_reply_plan("iya", cfg)
    assert p.sentences == 5 and p.mode == "streamer-fixed"


def test_prompt_token_dan_filter_sepakat():
    """Pelajaran mahal yang sudah pernah dibayar sekali (58% jawaban kepotong).

    Kalau prompt menyuruh N kalimat tapi filter memotong di M != N, model
    membuang waktu menulis yang langsung dibuang. Ketiganya harus sejalan.
    """
    for teks in (_INISIATIF, "iya", "kenapa kamu suka minecraft sih jelasin dong"):
        p = P.resolve_reply_plan(teks, H.CONFIG)
        max_sent, max_chars = H.get_arti_reply_limits(teks, H.CONFIG)
        tok = H.live_max_tokens_for_trigger(teks, H.CONFIG)
        assert max_sent == p.sentences, (teks, max_sent, p.sentences)
        assert max_chars == p.max_chars
        assert tok == p.max_tokens
        # Instruksi ke model harus menyebut angka yang sama.
        instr = P.format_reply_instruction(p)
        if p.sentences == 1:
            assert "SATU kalimat" in instr
        else:
            assert str(p.sentences) in instr


def test_instruksi_non_yt_tidak_lagi_hardcoded():
    """Cabang non-YT dulu selalu mengirim '2-3 kalimat' apa pun rencananya."""
    import inspect

    import arti_voice_pipeline as VP

    src = inspect.getsource(VP)
    assert "resolve_reply_plan" in src, "cabang non-YT tidak memakai rencana"
    assert "Jawab dalam 2-3 kalimat penuh" not in src, (
        "instruksi panjang hardcoded kembali ke cabang non-YouTube"
    )

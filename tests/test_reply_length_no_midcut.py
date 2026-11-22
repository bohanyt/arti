"""Jawaban Arti tidak boleh kepotong di tengah pikiran (Bohan 2026-10-23:
"[recorded remark omitted]").

Desain: jumlah kalimat rencana = TARGET di prompt; pemotong pasca-proses cuma
BATAS AMAN (arti_reply_policy.reply_hard_cap) dan hanya memotong di batas
kalimat; budget max_tokens tiap rencana menampung batas aman itu; kalau
provider melapor finish=length, kalimat terakhir yang kepotong dibuang.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import arti_bridge as B
import arti_curious as AC
import arti_openrouter as OR
import arti_reply_policy as P

_YT = "[Pesan Live Chat dari Viewer @seka (YouTube)]: {}"
_INISIATIF = "[Inisiatif — buka topik sendiri]\nStream lagi hening..."
_TERMINAL = ("!", ".", "?", "…")


def _kalimat(i: int, panjang: int = 100) -> str:
    """Kalimat Indonesia sintetis, tepat `panjang` karakter, diakhiri titik."""
    dasar = f"Kalimat ke-{i} ini cerita soal kebun dan ayam di Stardew "
    isi = (dasar + "yang lucu banget " * 10)[: panjang - 1].rstrip()
    return (isi + "x" * (panjang - 1 - len(isi))) + "."


@pytest.fixture(autouse=True)
def _chat_tidak_sepi(monkeypatch):
    # Rant hanya saat chat sepi; tes ini butuh rencana deterministik.
    monkeypatch.setattr(B, "_last_yt_chat_gap_sec", 0.0)


# --- batas aman -------------------------------------------------------------


def test_hard_cap_defaults_and_config():
    assert B.CONFIG["arti_reply_hard_cap_sentences"] == 6
    assert B.CONFIG["arti_reply_hard_cap_chars"] == 900
    assert P.reply_hard_cap(1, 135, {}) == (6, 900)
    assert P.reply_hard_cap(2, 230, B.CONFIG) == (6, 900)
    assert P.reply_hard_cap(4, 420, B.CONFIG) == (8, 900)
    # rant 8 kalimat / 800 ch: batas aman ikut melebar (target x 1,5)
    assert P.reply_hard_cap(8, 800, B.CONFIG) == (16, 1200)
    # ...tapi tidak pernah lewat plafon 1200 (dulu 900 x 1,5 = 1350)
    assert B.CONFIG["arti_reply_hard_cap_chars_max"] == 1200
    assert P.reply_hard_cap(8, 900, B.CONFIG) == (16, 1200)
    assert P.reply_hard_cap(8, 900, {}) == (16, 1200)
    cfg = {"arti_reply_hard_cap_sentences": 3, "arti_reply_hard_cap_chars": 400}
    assert P.reply_hard_cap(1, 135, cfg) == (3, 400)


def test_rant_plan_hard_cap_never_above_1200():
    for i in range(300):
        p = P.resolve_reply_plan(_YT.format(f"pesan iseng {i}"), B.CONFIG, quiet=True)
        assert P.reply_hard_cap(p.sentences, p.max_chars, B.CONFIG)[1] <= 1200
    wide = dict(B.CONFIG, arti_reply_yt_chars_per_sentence=200)  # rant_chars_cap 900 menggigit
    for i in range(300):
        p = P.resolve_reply_plan(_YT.format(f"pesan iseng {i}"), wide, quiet=True)
        assert P.reply_hard_cap(p.sentences, p.max_chars, wide)[1] <= 1200


def test_hard_cap_cuts_only_whole_sentences_by_count():
    teks = " ".join(_kalimat(i, 60) for i in range(10))  # 10 kalimat, ~610 ch
    out = B._truncate_reply_length(teks, 6, 900)
    assert out == " ".join(_kalimat(i, 60) for i in range(6))
    assert teks.startswith(out) and out.endswith(".")


def test_char_cap_drops_the_whole_partial_sentence():
    kalimat = [_kalimat(i) for i in range(8)] + [_kalimat(8, 200)]
    teks = " ".join(kalimat)  # 8 x 100 + 200 + spasi = 1008 ch
    out = B._truncate_reply_length(teks, 20, 900)
    # Kalimat ke-9 akan terbelah di 900 ch -> dibuang UTUH, bukan dipotong.
    assert out == " ".join(kalimat[:8])
    assert len(out) <= 900 and out.endswith(".")
    assert "…" not in out


def test_every_hard_cap_cut_ends_on_a_sentence_boundary():
    kalimat = [_kalimat(i, 70 + 13 * i) for i in range(14)]
    teks = " ".join(kalimat)
    for cap_sent, cap_chars in ((6, 900), (3, 900), (20, 500), (20, 333)):
        out = B._truncate_reply_length(teks, cap_sent, cap_chars)
        assert out and teks.startswith(out)
        assert out.endswith(".")
        n = len(re.split(r"(?<=[.!?])\s+", out))
        assert out == " ".join(kalimat[:n]), "hanya kalimat utuh"
        assert n <= cap_sent and len(out) <= cap_chars


def test_first_sentence_kept_whole_unless_runaway():
    panjang = "Aku " + "suka banget sama kebun ini, " * 40 + "pokoknya."  # ~1130 ch
    assert 900 < len(panjang) <= 1800
    assert B._truncate_reply_length(panjang, 6, 900) == panjang
    kabur = "aku " + "terus ngomong tanpa titik " * 80  # > 2x batas aman
    out = B._truncate_reply_length(kabur, 6, 900)
    assert len(out) <= 900 and out.endswith("…"), "rem darurat keluaran kabur"


# --- finish=length / max_tokens -------------------------------------------


@pytest.mark.parametrize("finish", ["length", "max_tokens", "MAX_TOKENS"])
def test_partial_trailing_sentence_dropped_on_length_finish(finish):
    teks = "Aku suka kopi susu. Tapi kalau pagi-pagi aku biasanya"
    assert P.trim_if_length_cut(teks, finish) == "Aku suka kopi susu."


def test_length_finish_edge_cases():
    utuh = "Aku suka kopi susu. Kamu juga kan?"
    assert P.trim_if_length_cut(utuh, "length") == utuh
    assert P.trim_if_length_cut("Kopi enak. Tapi kalau", "stop") == "Kopi enak. Tapi kalau"
    assert P.trim_if_length_cut("Kopi enak. Tapi kalau", None) == "Kopi enak. Tapi kalau"
    # Tidak ada kalimat utuh sama sekali: lebih baik sepotong daripada diam.
    assert P.trim_if_length_cut("Tapi kalau pagi aku", "length") == "Tapi kalau pagi aku"
    # Tag utuh di ekor bukan kalimat yang kepotong.
    tag = "Seru banget! [EMOTION: senang]"
    assert P.trim_if_length_cut(tag, "length") == tag
    assert P.trim_if_length_cut("Seru banget! [EMOTION: sen", "length") == "Seru banget!"
    # Angka bukan batas kalimat.
    assert P.trim_if_length_cut("Harganya Rp10.000 lho. Terus aku beli 3", "length") == (
        "Harganya Rp10.000 lho."
    )
    assert P.trim_if_length_cut('Dia bilang "oke." Terus', "length") == 'Dia bilang "oke."'


def test_no_finish_reason_heuristic_only_near_budget():
    pendek = "Aku suka kopi. Kamu juga kan"
    assert not P.looks_token_cut(pendek, 364)
    panjang = " ".join(_kalimat(i) for i in range(8)) + " Terus aku mikir kalau"
    assert P.looks_token_cut(panjang, 364)
    assert not P.looks_token_cut(" ".join(_kalimat(i) for i in range(8)), 364)


def test_groq_chat_completion_drops_partial_sentence(monkeypatch):
    body = {"choices": [{
        "message": {"content": "Ini jawaban utuh. Dan ini kalimat yang kepo"},
        "finish_reason": "length",
    }]}
    sent = []

    def post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return SimpleNamespace(status_code=200, json=lambda: body, text="")

    monkeypatch.setattr(B.requests, "post", post)
    teks, model = B.groq_chat_completion("unit-model", "sys", "iya", {"groq_api_key": "k"})
    assert (teks, model) == ("Ini jawaban utuh.", "unit-model")
    assert sent[0]["max_tokens"] >= P.tokens_for_chars(900)


def test_openrouter_live_drops_partial_sentence(monkeypatch):
    body = {"choices": [{
        "message": {"content": "Halo Seka! Aku lagi nanem stroberi terus"},
        "finish_reason": "length",
    }]}
    monkeypatch.setattr(OR.requests, "post",
                        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: body))
    assert OR.openrouter_chat("k", "m", [], title="Arti Live") == "Halo Seka!"
    # Jalur lain (mis. refleksi) tidak disentuh.
    assert OR.openrouter_chat("k", "m", [], title="Arti Lain") == (
        "Halo Seka! Aku lagi nanem stroberi terus"
    )


# --- jawaban normal utuh, log hanya kalau batas aman memotong ---------------


def test_normal_length_reply_untouched_before_after(capsys):
    speech = _YT.format("halo arti")  # brief -> target 1 kalimat
    target_sent, target_chars = B.get_arti_reply_limits(speech, B.CONFIG)
    assert target_sent == 1
    jawaban = (
        "Halo Seka, selamat datang di stream! Aku lagi nyiram kebun nih. "
        "Kamu udah makan belum?"
    )
    # SEBELUM: pemotong lama memakai target sebagai gunting -> 1 kalimat.
    sebelum = " ".join(re.split(r"(?<=[.!?])\s+", jawaban)[:target_sent])
    assert sebelum == "Halo Seka, selamat datang di stream!"
    # SESUDAH: utuh, tanpa log potong.
    capsys.readouterr()
    assert B.post_process_response(jawaban, speech) == jawaban
    assert "Jawaban dipotong" not in capsys.readouterr().out


def test_cut_log_only_when_hard_cap_cuts(capsys):
    speech = "iya"
    capsys.readouterr()
    B.post_process_response("Iya   dong.  Gas   aja.", speech)  # cuma spasi
    assert "Jawaban dipotong" not in capsys.readouterr().out
    panjang = " ".join(_kalimat(i, 60) for i in range(9))
    out = B.post_process_response(panjang, speech)
    log = capsys.readouterr().out
    assert out == " ".join(_kalimat(i, 60) for i in range(6))
    assert "[Filter] Jawaban dipotong" in log and "max 6 kal / 900ch" in log


# --- target di prompt --------------------------------------------------------


def test_inisiatif_target_is_two():
    assert B.CONFIG["arti_reply_inisiatif_max_sentences"] == 2
    assert P.inisiatif_max_sentences({}) == 2
    assert P.resolve_reply_plan(_INISIATIF, B.CONFIG).sentences == 2
    prompt = AC.build_initiative_prompt({}, rng=lambda: 0.0)
    assert "Maksimal 2 kalimat berisi OPINI" in prompt
    assert P.resolve_reply_plan(prompt, {}).sentences == 2


def test_prompt_states_target_and_finish_your_thought():
    for teks in (_INISIATIF, "iya", "kenapa kamu suka minecraft sih jelasin dong"):
        p = P.resolve_reply_plan(teks, B.CONFIG)
        instr = P.format_reply_instruction(p)
        assert "selesaikan pikiranmu" in instr.lower()
        assert "max " not in instr
        assert ("SATU kalimat" if p.sentences == 1 else f"{p.sentences} kalimat") in instr
    for msg in ("halo", "arti kamu suka main game apa aja?",
                "arti menurut kamu kenapa sistem memori penting dan gimana cara kerjanya?"):
        p = P.resolve_yt_reply_plan(_YT.format(msg), B.CONFIG)
        instr = P.format_yt_reply_instruction(p)
        assert f"{p.sentences} kalimat" in instr and "max " not in instr
        assert "selesaikan pikiranmu" in instr.lower()


# --- token budget per rencana -------------------------------------------------

_PLAN_SAMPLES = [
    (_YT.format("halo"), False),
    (_YT.format("aku baru pulang kerja nih"), False),
    (_YT.format("arti kamu suka main game apa aja?"), False),
    (_YT.format("arti menurut kamu kenapa sistem memori penting dan gimana cara kerjanya?"), False),
    (_INISIATIF, False),
    ("iya", False),
    ("menurut kamu gimana cara bikin setting grafis minecraft biar lancar", False),
] + [(_YT.format(f"pesan iseng {i}"), True) for i in range(300)]


def test_token_budget_covers_hard_cap_for_every_plan():
    seen = set()
    for speech, quiet in _PLAN_SAMPLES:
        p = P.resolve_reply_plan(speech, B.CONFIG, quiet=quiet)
        seen.add(P.kelas_dari_mode(p.mode))
        hard_sent, hard_chars = P.reply_hard_cap(p.sentences, p.max_chars, B.CONFIG)
        assert hard_sent >= max(2 * p.sentences, 6)
        assert hard_chars >= 900
        assert p.max_tokens >= P.tokens_for_chars(hard_chars), (p.mode, p.max_tokens)
        # ~3 char/token (konservatif untuk Bahasa Indonesia) + ruang tag
        assert p.max_tokens >= hard_chars / 3
        # tidak pernah di bawah tabel lama
        assert p.max_tokens >= P._TOKENS_BY_SENT.get(p.sentences, 0)
    assert {"brief", "normal", "deep", "gacha", "rant", "inisiatif"} <= seen
    fixed = dict(B.CONFIG, arti_reply_streamer_adaptive=False)
    p = P.resolve_reply_plan("iya", fixed)
    assert p.max_tokens >= P.tokens_for_chars(P.reply_hard_cap(p.sentences, p.max_chars, fixed)[1])


def test_live_token_budget_matches_plan():
    for speech in (_YT.format("halo"), _INISIATIF, "iya"):
        assert B.live_max_tokens_for_trigger(speech, B.CONFIG) == (
            P.resolve_reply_plan(speech, B.CONFIG).max_tokens
        )
    # model nalar dapat ruang tambahan, tidak di bawah lantai lama
    assert B._reply_tokens_with_thinking("iya", 512) >= max(
        512, P.tokens_for_chars(900) + B.REPLY_THINKING_HEADROOM_TOKENS
    )
    assert B._reply_tokens_with_thinking("iya", 200) >= 200


# --- target YouTube tidak berubah -------------------------------------------


def test_yt_targets_unchanged():
    assert P._SENT_BY_KIND == {"brief": (1, 1), "normal": (2, 2), "deep": (3, 4)}
    assert B.CONFIG["arti_reply_rant_min_sentences"] == 6
    assert B.CONFIG["arti_reply_rant_max_sentences"] == 8
    assert B.CONFIG["arti_reply_max_sentences"] == 5
    assert B.CONFIG["arti_reply_max_chars"] == 580
    assert P.resolve_yt_reply_plan(_YT.format("halo"), B.CONFIG).sentences == 1
    assert P.resolve_yt_reply_plan(
        _YT.format("arti kamu suka main game apa aja?"), B.CONFIG).sentences == 2
    deep = P.resolve_yt_reply_plan(_YT.format(
        "arti menurut kamu kenapa sistem memori penting dan gimana cara kerjanya?"), B.CONFIG)
    assert deep.sentences in (3, 4)
    gacha = {P.resolve_yt_reply_plan(_YT.format(f"oke lanjut {i} ya"), B.CONFIG).sentences
             for i in range(60)}
    assert gacha <= {1, 2, 3}
    fixed = P.resolve_yt_reply_plan(_YT.format("halo"), {"arti_reply_yt_adaptive": False})
    assert fixed.sentences == 2
    # target karakter (dipakai prompt/log) tetap formula lama
    assert deep.max_chars == min(500, 40 + deep.sentences * 95)


# --- tindak lanjut review 2026-10-23 -----------------------------------------


def _runon(panjang: int) -> str:
    """Satu kalimat panjang berkoma, tanpa titik di tengah, tepat `panjang` ch."""
    isi = ("aku tuh mikirin kebun ini terus, soalnya stroberinya lucu banget, " * 40)
    isi = isi[: panjang - 1].rstrip(" ,")
    return isi + "x" * (panjang - 1 - len(isi)) + "."


@pytest.mark.parametrize("panjang", [950, 1300, 1790])
def test_short_opener_plus_runon_below_2x_cap_stays_whole(panjang, capsys):
    teks = "Iya! " + _runon(panjang)
    assert B._truncate_reply_length(teks, 6, 900, runon_chars=900) == teks
    out = B.post_process_response(teks, "iya")
    assert out == teks and "…" not in out
    assert "Jawaban dipotong" not in capsys.readouterr().out


def test_runaway_brake_only_above_2x_base_cap_and_cuts_to_base():
    teks = "Iya! " + _runon(2000)
    out = B._truncate_reply_length(teks, 6, 900, runon_chars=900)
    assert out.endswith("…") and len(out) <= 900
    # Rencana rant (batas 1200) tetap direm ke batas dasar 900, bukan 1200,
    # dan ambangnya 2 x 900 = 1800, bukan 2 x 1200.
    tunggal = _runon(1850)
    out = B._truncate_reply_length(tunggal, 16, 1200, runon_chars=900)
    assert out.endswith("…") and len(out) <= 900
    assert B._truncate_reply_length(_runon(1700), 16, 1200, runon_chars=900) == _runon(1700)


@pytest.mark.parametrize("teks", [
    "Aku menang lagi wkwkwk",
    "Seru banget 😆",
    "Seru banget 😆 [EMOTION: senang]",
    "Mantap kak :)",
    "Oke kak~",
    "Hehe makasih yaa hahaha [EMOTION: senang]",
])
def test_soft_endings_count_as_complete_sentence_on_length_finish(teks):
    assert P.trim_if_length_cut(teks, "length") == teks
    assert not P.looks_token_cut(teks * 1, 1)


@pytest.mark.parametrize("teks,expected", [
    ("Seru banget 😆 [EMOTION: senang] Terus aku mau",
     "Seru banget 😆 [EMOTION: senang]"),
    ("Aku menang wkwk [EMOTION: senang] Abis itu kita", "Aku menang wkwk [EMOTION: senang]"),
    ("Makasih kak :) Nanti aku", "Makasih kak :)"),
])
def test_soft_ending_keeps_sentence_and_tags_when_tail_is_cut(teks, expected):
    assert P.trim_if_length_cut(teks, "length") == expected


def test_abbreviations_do_not_split_sentences():
    assert P.split_sentences("Kata Dr. Stone itu keren. Aku setuju.") == [
        "Kata Dr. Stone itu keren.", "Aku setuju."]
    assert P.split_sentences("Ada apel, jeruk, dll. yang seger.") == [
        "Ada apel, jeruk, dll. yang seger."]
    assert P.split_sentences("Makan, minum, dll. Terus tidur.") == [
        "Makan, minum, dll.", "Terus tidur."]
    # "Pak."/"Bu." sengaja tetap akhir kalimat ("Siap, pak.")
    assert P.split_sentences("Siap, pak. Aku jalan.") == ["Siap, pak.", "Aku jalan."]
    assert P.trim_if_length_cut("Dr. Stone bilang gitu. Terus Dr", "length") == (
        "Dr. Stone bilang gitu."
    )
    teks = " ".join(["Kata Prof. Oak itu " + "x" * 80 + "."] * 8)
    assert B._truncate_reply_length(teks, 6, 900).count("Prof. Oak") == 6


def test_long_sentence_split_into_tts_chunks_text_intact():
    teks = _runon(1000)
    chunks = B._sentences_or_empty(teks)
    assert len(chunks) > 1
    assert all(len(c) <= 280 for c in chunks), [len(c) for c in chunks]
    assert " ".join(chunks).split() == teks.split(), "kata harus sama persis"
    assert all(c.endswith((",", ".")) for c in chunks), "pecah di batas klausa"
    # Teks jawaban (subtitle/history) tidak dipecah/diubah.
    assert B.post_process_response(teks, "iya") == teks
    # Jawaban normal tidak terpengaruh batas chunk.
    biasa = " ".join(_kalimat(i) for i in range(3))
    import arti_groq_stream as GS
    assert GS.split_indonesian_sentences(biasa) == GS.split_indonesian_sentences(biasa, max_chars=0)
    # Tanpa koma: jatuh ke batas spasi, tetap <= 280.
    polos = "kata " * 200
    assert all(len(c) <= 280 for c in GS.split_indonesian_sentences(polos))


def test_boleh_lebih_only_for_targets_of_three_or_more():
    for msg in ("halo", "arti kamu suka main game apa aja?"):
        p = P.resolve_yt_reply_plan(_YT.format(msg), B.CONFIG)
        assert p.sentences <= 2
        instr = P.format_yt_reply_instruction(p)
        assert "boleh lebih" not in instr.lower()
        assert "selesaikan pikiranmu" in instr.lower()
    deep = P.resolve_yt_reply_plan(_YT.format(
        "arti menurut kamu kenapa sistem memori penting dan gimana cara kerjanya?"), B.CONFIG)
    assert "boleh lebih" in P.format_yt_reply_instruction(deep).lower()
    for n, ada in ((1, False), (2, False), (3, True), (4, True)):
        plan = P.YtReplyPlan(sentences=n, max_chars=0, max_tokens=0, mode=f"streamer-x→{n}kal")
        assert ("boleh lebih" in P.format_reply_instruction(plan).lower()) is ada


@pytest.mark.parametrize("finish", ["length", "stop", None])
def test_stream_collector_exposes_finish_reason(finish):
    import json as _json

    import arti_groq_stream as GS

    chunks = [
        {"choices": [{"delta": {"content": "Halo! "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "Apa kabar"}, "finish_reason": None}]},
    ]
    if finish:
        chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    lines = [f"data: {_json.dumps(c)}".encode() for c in chunks] + [b"data: [DONE]"]
    meta: dict = {}
    full, _ = GS.collect_streaming_reply(iter(lines), meta=meta)
    assert full == "Halo! Apa kabar"
    assert meta == {"finish_reason": finish}
    # Bentuk nilai kembalian lama tetap (tanpa meta).
    assert GS.collect_streaming_reply(iter(lines))[0] == full

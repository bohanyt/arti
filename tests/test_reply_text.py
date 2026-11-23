"""Golden voice text behavior captured from the bridge before the split."""

import ast
from pathlib import Path

import pytest

from arti_runtime import reply_text


_BRIDGE_NAMES = (
    "_TTS_EXPRESSION_TAG_RE",
    "_ANY_ANGLE_TAG_RE",
    "strip_tts_expression_tags",
    "clean_ai_reply",
    "_BAD_ARTICULATION_PATTERNS",
    "_sentence_is_bad_articulation",
    "filter_meta_history_talk",
    "_trim_stream_reply",
    "_truncate_reply_length",
    "_runaway_clause_cut",
    "_sentences_or_empty",
    "_CJK_RE",
    "_shorten_viewer_handles",
)


def test_bridge_reexports_the_same_objects():
    import arti_bridge

    for name in _BRIDGE_NAMES:
        assert getattr(arti_bridge, name) is getattr(reply_text, name), name


def test_reply_text_does_not_import_the_bridge():
    source = Path(reply_text.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert "arti_bridge" not in (node.module or "").split(".")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert all("arti_bridge" not in alias.name.split(".") for alias in node.names)


def test_expression_tags_become_tts_sounds():
    text = "Ayo <LAUGH> <chuckle> <giggle> <sigh> <breath> <unknown> !"
    assert reply_text.strip_tts_expression_tags(text) == "Ayo haha hehe hihi hhh hah!"


def test_clean_reply_drops_reasoning_and_keeps_indonesian_with_loanwords():
    text = (
        "<think>internal.\nnotes</think> This is the response I would use. "
        "**pelan** Aku lagi chat di YouTube! *mikir* Yuk lanjut."
    )
    assert reply_text.clean_ai_reply(text) == "Aku lagi chat di YouTube! Yuk lanjut."
    assert reply_text.clean_ai_reply("Aku sudah siap. <think>belum selesai") == "Aku sudah siap."


def test_meta_history_sentences_are_removed_and_counted():
    assert reply_text._sentence_is_bad_articulation("Aku membaca 2 catatan.") is True
    assert reply_text._sentence_is_bad_articulation("Aku menyiram tanaman.") is False

    text = "Aku membaca 2 catatan. Yuk kita tanam benih! Sebagai Arti aku harus respon."
    assert reply_text.filter_meta_history_talk(text) == ("Yuk kita tanam benih!", 2)


@pytest.mark.parametrize(
    ("finish_reason", "token_budget", "expected"),
    [
        ("length", 620, "Ini jawaban utuh."),
        ("stop", 10, "Ini jawaban utuh. Ini baru separuh"),
        (None, 10, "Ini jawaban utuh."),
    ],
)
def test_stream_reply_respects_finish_reason(finish_reason, token_budget, expected):
    text = "Ini jawaban utuh. Ini baru separuh"
    assert reply_text._trim_stream_reply(text, finish_reason, token_budget, "unit") == expected


def test_reply_length_preserves_the_whole_first_sentence():
    text = "Prof. Oak membawa Rp10.000 untuk membeli bibit. Lalu kami pulang melewati pasar yang ramai."
    assert reply_text._truncate_reply_length(text, 3, 60) == "Prof. Oak membawa Rp10.000 untuk membeli bibit."


def test_runaway_cut_prefers_a_clause_boundary():
    text = "Aku sudah beli benih, lalu aku melewati pasar dan pulang ke kebun"
    assert reply_text._runaway_clause_cut(text, 29) == "Aku sudah beli benih…"


def test_sentence_chunks_require_more_than_one_chunk():
    text = (
        "Kami menyiram semua tanaman di kebun sebelum matahari mulai tenggelam. "
        "Sesudah itu kami membawa hasil panen ke pasar dan makan bersama."
    )
    assert reply_text._sentences_or_empty(text) == [
        "Kami menyiram semua tanaman di kebun sebelum matahari mulai tenggelam.",
        "Sesudah itu kami membawa hasil panen ke pasar dan makan bersama.",
    ]
    assert reply_text._sentences_or_empty("Aku sudah siap.") == []


def test_viewer_handles_are_shortened():
    text = "Hai @EXAMPLEVIEWER241, ajak exampleviewer241 dan @sample77 ya."
    user_speech = "[Pesan Live Chat dari Viewer @exampleviewer241 (YouTube)]: Halo!"
    assert reply_text._shorten_viewer_handles(text, user_speech) == "Hai examplev, ajak examplev dan sample ya."


def test_cjk_regex_removes_cjk_hangul_and_fullwidth_text():
    assert reply_text._CJK_RE.sub(" ", "kita放送 lalu 한글 dan ＡＢ lanjut") == "kita  lalu   dan   lanjut"

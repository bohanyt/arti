from __future__ import annotations

import arti_expression_runtime as expr


def test_parse_reply_emotion_strips_known_tag() -> None:
    clean, emotion = expr.parse_reply_emotion("iya dong [EMOTION:marah]")
    assert clean == "iya dong"
    assert emotion == "marah"


def test_parse_reply_emotion_unknown_tag_fails_neutral() -> None:
    clean, emotion = expr.parse_reply_emotion("tes [EMOTION:unknown]")
    assert clean == "tes"
    assert emotion == "neutral"


def test_explicit_face_request_wins_over_reply_tag() -> None:
    emotion = expr.resolve_turn_emotion("coba pasang muka sedih", "marah")
    assert emotion == "sedih"


def test_hint_can_select_emotion_when_reply_is_neutral() -> None:
    emotion = expr.resolve_turn_emotion("kok kamu bingung?", "neutral")
    assert emotion == "bingung"


def test_nod_remains_config_gated() -> None:
    assert expr.should_nod_for_emotion("senang", {"expression_nod_enabled": False}) is False
    assert expr.should_nod_for_emotion("senang", {"expression_nod_enabled": True}) is True

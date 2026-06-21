"""Tests for arti_expression_runtime parser."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_expression_runtime as expr


def test_parse_reply_emotion_strips_tag():
    text, emotion = expr.parse_reply_emotion("halo [EMOTION:senang] dunia")
    assert text == "halo dunia"
    assert emotion == "senang"


def test_parse_reply_emotion_invalid_tag_neutral():
    _, emotion = expr.parse_reply_emotion("halo [EMOTION:galau]")
    assert emotion == "neutral"


def test_emotion_prompt_appended_when_enabled():
    out = expr.emotion_prompt_for_system("base", {"expression_emotion_enabled": True})
    assert "[EMOTION:" in out
    assert expr.emotion_prompt_for_system("base", {"expression_emotion_enabled": False}) == "base"


def test_parse_reply_emotion_missing_tag():
    text, emotion = expr.parse_reply_emotion("halo saja")
    assert text == "halo saja"
    assert emotion == "neutral"


def test_resolve_turn_emotion_from_user_speech():
    assert expr.resolve_turn_emotion("coba muka sedih", "neutral") == "sedih"
    assert expr.resolve_turn_emotion("halo", "senang") == "senang"
    assert expr.resolve_turn_emotion("Arti coba muka bingung", "neutral") == "bingung"


def test_should_nod_for_emotion():
    cfg = {"expression_nod_enabled": True}
    assert expr.should_nod_for_emotion("sedih", cfg) is False
    assert expr.should_nod_for_emotion("marah", cfg) is True
    assert expr.should_nod_for_emotion("bingung", cfg) is True
    assert expr.should_nod_for_emotion("sedih", {"expression_nod_enabled": False}) is False


_BLOCKED_MOOD_IDS = frozenset({
    "ParamMouthOpenY",
    "ParamMouthForm",
    "Param48",
    "Param122",
    "Param125",
    "Param183",
    "Param186",
    "Param130",
    "Param96",
    "Param97",
    "Param2",
})
_EMOTION_FILES = ("ArtiBingung", "ArtiSedih", "ArtiMarah", "ArtiSenyum")


def test_emotion_templates_mouth_and_lamp_free():
    """Mood overlay must not lock lip-sync or lamp (Param130)."""
    templates = ROOT / "templates"
    for name in _EMOTION_FILES:
        path = templates / f"{name}.exp3.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        ids = {p.get("Id") for p in data.get("Parameters", [])}
        blocked = ids & _BLOCKED_MOOD_IDS
        assert not blocked, f"{name} still has blocked params: {blocked}"


def test_emotion_marah_no_param_angle_y_in_template():
    path = ROOT / "templates" / "ArtiMarah.exp3.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = {p.get("Id") for p in data.get("Parameters", [])}
    assert "ParamAngleY" not in ids


def test_emotion_marah_differs_from_bicara():
    """ArtiMarah must deform brows/eyes — identical to ArtiBicara = invisible mood."""
    marah_path = ROOT / "templates" / "ArtiMarah.exp3.json"
    bicara_path = ROOT / "templates" / "ArtiBicara.exp3.json"
    if not marah_path.is_file() or not bicara_path.is_file():
        return
    marah = {
        p["Id"]: p["Value"]
        for p in json.loads(marah_path.read_text(encoding="utf-8"))["Parameters"]
    }
    bicara = {
        p["Id"]: p["Value"]
        for p in json.loads(bicara_path.read_text(encoding="utf-8"))["Parameters"]
    }
    shared_diff = {k for k in marah if k in bicara and marah[k] != bicara[k]}
    brow_eye = {
        k
        for k in marah
        if k.startswith("ParamBrow") or k.startswith("ParamEye")
    }
    assert shared_diff or brow_eye, "ArtiMarah must not be a clone of ArtiBicara"

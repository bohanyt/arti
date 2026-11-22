"""Tests for arti_vision_client chain (mocked providers)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_screen_context as sc
import arti_vision_client as vc


def _good_json() -> str:
    return json.dumps(
        {
            "scene": "Layar YouTube dengan video game.",
            "playback_mmss": "01:23",
            "ocr_text": "judul",
        }
    )


@pytest.fixture
def vision_config():
    return {
        "vision_enabled": True,
        "vision_runtime_on": True,
        "vision_provider_chain": ["nvidia", "groq"],
        "vision_max_tokens": 256,
        "vision_scene_max_chars": 300,
        "vision_ocr_max_chars": 200,
        "vision_github_enabled": False,
    }


def test_parse_null_playback_string():
    raw = json.dumps({"scene": "Desktop.", "playback_mmss": "null", "ocr_text": ""})
    snap = sc.parse_vision_response(raw)
    assert snap.playback_mmss is None


def test_parse_ocr_truncated():
    raw = json.dumps({"scene": "Log.", "playback_mmss": None, "ocr_text": "x" * 500})
    snap = sc.parse_vision_response(raw, ocr_max_chars=200)
    assert len(snap.ocr_text) == 200


def test_prompt_contract_keywords():
    p = sc.build_vision_prompt()
    assert "MAKS 200" in p
    assert "null JSON" in p


def test_chain_failover_to_groq(vision_config):
    calls: list[str] = []

    def fake_nvidia(prompt, jpeg_b64, config):
        calls.append("nvidia")
        raise RuntimeError("HTTP 429")

    def fake_groq(prompt, jpeg_b64, config):
        calls.append("groq")
        return _good_json(), 50

    patched = {"nvidia": fake_nvidia, "groq": fake_groq}
    with patch.object(vc, "_resolve_chain", return_value=["nvidia", "groq"]), patch.dict(
        vc._PROVIDERS, patched, clear=False
    ), patch.object(vc.arti_vision_capture, "capture_jpeg_b64", return_value=("abc", b"jpeg")), patch.object(vc, "_tangkap_bergerbang", return_value=("abc", b"sidik")):
        vc.vision_uptime.consecutive_failures = 0
        snap, provider = vc.describe_with_chain(vision_config)

    assert calls == ["nvidia", "groq"]
    assert provider == "groq"
    assert snap is not None
    assert "YouTube" in snap.scene


def test_chain_all_fail(vision_config):
    def fail(*_a, **_k):
        raise RuntimeError("down")

    with patch.object(vc, "_resolve_chain", return_value=["nvidia", "groq"]), patch.dict(
        vc._PROVIDERS, {"nvidia": fail, "groq": fail}, clear=False
    ), patch.object(vc.arti_vision_capture, "capture_jpeg_b64", return_value=("abc", b"jpeg")), patch.object(vc, "_tangkap_bergerbang", return_value=("abc", b"sidik")):
        vc.vision_uptime.consecutive_failures = 0
        snap, provider = vc.describe_with_chain(
            {**vision_config, "vision_provider_chain": ["nvidia", "groq"]}
        )

    assert snap is None
    assert provider == ""
    assert vc.vision_uptime.consecutive_failures >= 1


def test_provider_pensiun_ditolak_di_rantai_vision(vision_config):
    """Cermin dari tes yang sama di test_scouter_client.

    GitHub Models pensiun 30 Juli 2026. Rantai vision memakainya di posisi 6.
    Ditolak paksa supaya config_local basi tidak bisa menghidupkannya.
    """
    cfg = {
        **vision_config,
        "vision_provider_chain": ["github", "nvidia"],
        "vision_github_enabled": True,
        "github_models_token": "token-palsu-untuk-tes",
    }
    chain = vc._resolve_chain(cfg)
    assert "github" not in chain, "provider pensiun lolos ke rantai vision"


def test_default_chain_vision_bersih_dari_provider_pensiun():
    assert vc.PROVIDER_PENSIUN, "daftar provider pensiun kosong"
    assert not (set(vc.DEFAULT_CHAIN) & vc.PROVIDER_PENSIUN)

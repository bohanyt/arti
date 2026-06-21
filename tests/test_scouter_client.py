"""Tests for arti_scouter_client chain (mocked providers)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_scouter_client as sc


def _good_json() -> str:
    return json.dumps(
        {
            "summary": "Streamer dan chat membahas boss di layar.",
            "emotion": "excited",
            "topic": "boss fight",
            "important_facts": ["Chat bilang lihat layar"],
            "screen_relevant": True,
            "screen_hint": "pembicaraan tentang boss di game",
            "curious_worthy": True,
            "curious_hook": "komentari pose boss",
        }
    )


@pytest.fixture
def scouter_config():
    return {
        "scouter_provider_chain": ["nvidia", "openrouter"],
        "scouter_max_tokens": 350,
        "scouter_temperature": 0.2,
        "scouter_timeout_sec": 30,
        "nvidia_api_key": "test-nvidia",
        "openrouter_api_key": "test-or",
        "vision_github_enabled": False,
    }


def test_parse_scouter_response():
    result = sc.parse_scouter_response(_good_json())
    assert result is not None
    assert result.screen_relevant is True
    assert result.curious_worthy is True
    assert result.screen_hint == "pembicaraan tentang boss di game"


def test_parse_null_strings():
    raw = json.dumps(
        {
            "summary": "Hai.",
            "emotion": "neutral",
            "topic": "chat",
            "important_facts": [],
            "screen_relevant": False,
            "screen_hint": "null",
            "curious_worthy": False,
            "curious_hook": "none",
        }
    )
    result = sc.parse_scouter_response(raw)
    assert result is not None
    assert result.screen_hint is None
    assert result.curious_hook is None


def test_resolve_chain_skips_groq(scouter_config):
    cfg = {**scouter_config, "scouter_provider_chain": ["groq", "nvidia"]}
    chain = sc._resolve_chain(cfg)
    assert "groq" not in chain
    assert "nvidia" in chain


def test_has_screen_keywords():
    assert sc.has_screen_keywords("chat: lihat layar dong")
    assert not sc.has_screen_keywords("halo arti")


def test_chain_failover_to_openrouter(scouter_config):
    calls: list[str] = []

    def fake_nvidia(prompt, config):
        calls.append("nvidia")
        raise RuntimeError("HTTP 429")

    def fake_openrouter(prompt, config):
        calls.append("openrouter")
        return _good_json(), 40

    patched = {"nvidia": fake_nvidia, "openrouter": fake_openrouter}
    with patch.object(sc, "_resolve_chain", return_value=["nvidia", "openrouter"]), patch.dict(
        sc._PROVIDERS, patched, clear=False
    ):
        sc.scouter_uptime.consecutive_failures = 0
        result = sc.run_chain("streamer: wow\nchat: lihat layar", scouter_config)

    assert calls == ["nvidia", "openrouter"]
    assert result is not None
    assert result.provider == "openrouter"
    assert result.screen_relevant is True


def test_run_dict_compat(scouter_config):
    def fake_nvidia(prompt, config):
        return _good_json(), 10

    with patch.object(sc, "_resolve_chain", return_value=["nvidia"]), patch.dict(
        sc._PROVIDERS, {"nvidia": fake_nvidia}, clear=False
    ):
        data = sc.run("chat line", scouter_config)

    assert data is not None
    assert data["summary"].startswith("Streamer")

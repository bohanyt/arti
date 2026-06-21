"""Unit tests for arti_nvidia_client (mocked HTTP)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_nvidia_client as nvidia


def test_resolve_api_key_from_env(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    assert nvidia.resolve_api_key({}) == ""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    assert nvidia.resolve_api_key({}) == "nvapi-test"
    assert nvidia.resolve_api_key({"nvidia_api_key": "from-config"}) == "from-config"


def test_chat_completion_returns_text_and_timing(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "Halo Kak!"}}],
    }
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp

    text, ms = nvidia.chat_completion(
        [{"role": "user", "content": "halo"}],
        config={"nvidia_api_key": "nvapi-x"},
        session=mock_session,
    )
    assert text == "Halo Kak!"
    assert isinstance(ms, int)
    assert ms >= 0
    mock_session.post.assert_called_once()


def test_chat_completion_missing_key():
    with pytest.raises(ValueError, match="NVIDIA API key"):
        nvidia.chat_completion([{"role": "user", "content": "x"}], config={})

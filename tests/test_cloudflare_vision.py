"""Kontrak adapter Cloudflare untuk tugas JSON pendek Arti."""

from __future__ import annotations

import arti_cloudflare_vision as cf
import arti_text_openai


_CFG = {
    "cloudflare_api_token": "token-test",
    "cloudflare_account_id": "account-test",
}
_TANPA_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


def test_text_chat_mematikan_thinking_gemma(monkeypatch):
    tertangkap = {}

    def fake_text_chat(*_args, **kwargs):
        tertangkap.update(kwargs)
        return "{}", 1

    monkeypatch.setattr(arti_text_openai, "text_chat", fake_text_chat)

    cf.text_chat([{"role": "user", "content": "JSON saja"}], config=_CFG)

    assert tertangkap["extra_payload"] == _TANPA_THINKING


def test_vision_chat_mematikan_thinking_gemma(monkeypatch):
    tertangkap = {}

    def fake_vision_chat(*_args, **kwargs):
        tertangkap.update(kwargs)
        return "{}", 1

    monkeypatch.setattr(cf.oai, "vision_chat", fake_vision_chat)

    cf.vision_chat("jelaskan layar", "jpeg-b64", config=_CFG)

    assert tertangkap["extra_payload"] == _TANPA_THINKING

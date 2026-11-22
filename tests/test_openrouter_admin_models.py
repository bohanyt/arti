"""Model admin OpenRouter: reasoning off + nano-30b dibuang (17 Agu 2026).

Probe transkrip-jebakan di budget scouter asli (350):
- Tanpa param reasoning, SEMUA Nemotron menghabiskan budget untuk CoT
  (finish=length) — incumbent 120B mengembalikan CoT Inggris mentah, dan
  jalur live bahkan bisa MENGUCAPKANNYA (fallback content->reasoning di
  openrouter_chat). effort=low ikut diuji dan diabaikan provider free
  (CoT tetap 666-700 token di budget 700).
- nano-30b menghalusinasi fakta kanon + contoh terlarang ke important_facts
  (tulisan permanen ke vault) -> dibuang dari slot pembawa-fakta.
"""

from __future__ import annotations

import json

import arti_openrouter
import arti_scouter_client as scout
import arti_text_openai
import arti_bridge as bridge

_NANO = "nvidia/nemotron-3-nano-30b-a3b:free"
_LIGHTNING = "nvidia/nemotron-3.5-lightning:free"
_MATIKAN = {"reasoning": {"enabled": False}}


def test_daftar_scouter_produksi_tanpa_nano_ada_lightning():
    models = bridge.CONFIG["scouter_openrouter_models"]
    assert _NANO not in models, "nano-30b menghalusinasi important_facts — jangan kembali"
    assert _LIGHTNING in models
    assert models[0] == "nvidia/nemotron-3-super-120b-a12b:free"  # kualitas fakta terbaik duluan


def test_summarizer_fallback_produksi_bukan_nano():
    assert bridge.CONFIG["openrouter_summarizer_fallback"] == _LIGHTNING


def test_call_openrouter_scouter_mematikan_reasoning(monkeypatch):
    """EKSEKUSI _call_openrouter asli: payload harus membawa reasoning off."""
    terekam = {}

    def _palsu(url, key, model, messages, **kwargs):
        terekam["extra_payload"] = kwargs.get("extra_payload")
        terekam["model"] = model
        return '{"summary": "tes"}', 10

    monkeypatch.setattr(scout.text_oai, "text_chat", _palsu)
    cfg = dict(bridge.CONFIG)
    cfg["openrouter_api_key"] = "kunci-tes"
    text, _ms = scout._call_openrouter("halo", cfg)
    assert text == '{"summary": "tes"}'
    assert terekam["extra_payload"] == _MATIKAN
    assert terekam["model"] == bridge.CONFIG["scouter_openrouter_models"][0]


def test_text_chat_meneruskan_extra_payload(monkeypatch):
    """Jembatan kwargs -> body HTTP: tanpa ini param reasoning cuma hiasan."""
    badan = {}

    class _Res:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class _Sesi:
        def post(self, url, headers=None, json=None, timeout=None):
            badan.update(json or {})
            return _Res()

    monkeypatch.setattr(arti_text_openai, "get_session", lambda: _Sesi())
    arti_text_openai.text_chat(
        "https://openrouter.ai/api/v1/chat/completions", "k", "m",
        [{"role": "user", "content": "x"}], extra_payload=_MATIKAN,
    )
    assert badan["reasoning"] == {"enabled": False}


def test_live_openrouter_mematikan_reasoning(monkeypatch):
    """Jalur live (budget 110-380): reasoning wajib mati — selain memotong
    jawaban, fallback content->reasoning bisa membuat Arti mengucapkan CoT."""
    badan = {}

    class _Res:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "Jawaban Arti."}}]}

    def _post(url, headers=None, json=None, timeout=None):
        badan.update(json or {})
        return _Res()

    monkeypatch.setattr(arti_openrouter.requests, "post", _post)
    cfg = dict(bridge.CONFIG)
    cfg["openrouter_api_key"] = "kunci-tes"
    text, label = arti_openrouter.openrouter_live_completion("sistem", "halo arti", cfg)
    assert text == "Jawaban Arti."
    assert badan["reasoning"] == {"enabled": False}
    assert label and label.startswith("openrouter/")

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


# --- SCREEN_KEYWORDS: "liat" polos BUKAN pertanyaan layar ----------------------
# Live 2026-08-02: "pasang muka sedih deh, pmau liat" & "aku mau liat" membuka
# vision window -> turn memblokir 80-85 dtk nunggu nvidia timeout + fallback.
# "liat/lihat" hanya dihitung kalau menyinggung layar/objek visual di layar.


def test_bare_liat_is_not_screen_question():
    for t in (
        "pasang muka sedih deh, pmau liat",
        "aku mau liat",
        "coba pasang muka marah, aku mau liat",
        "liat dong ekspresinya",
        "aku pengen lihat kamu senyum",
    ):
        assert sc.has_screen_keywords(t) is False, t


def test_real_screen_questions_still_detected():
    for t in (
        "arti liat layar dong",
        "kamu lihat apa di layar?",
        "apa yang kamu liat sekarang?",
        "coba jelasin tampilan di screen",
        "arti lagi nonton video apa itu?",
        "di layar ada apa",
        "kamu bisa liat game ini ga?",
    ):
        assert sc.has_screen_keywords(t) is True, t


# --- parser tahan gaya composer (audit 2026-08-03) -------------------------------


def test_parse_json_blob_tolerates_composer_output_styles():
    """Scouter kini dilayani composer-2.5 (model agen-koding: sering fence /
    kalimat pengantar). Parse gagal = curious_worthy selamanya False =
    curious layar mati DIAM-DIAM, jadi toleransi ini bukan kosmetik."""
    inner = '{"summary":"ok","mood":"lazy"}'
    for name, raw in {
        "polos": inner,
        "fence": f"```json\n{inner}\n```",
        "fence tanpa label": f"```\n{inner}\n```",
        "prosa pengantar": f"Berikut hasilnya:\n{inner}",
        "prosa penutup": f"{inner}\nSemoga membantu ya.",
        "prosa ber-kurung": "Catatan {penting}: hasil analisis di bawah.\n" + inner,
        "dua objek": f"{inner}\n{{\"summary\":\"kedua\"}}",
        "kurung di dalam string": '{"summary":"dia bilang {aneh} banget","mood":"lazy"}',
    }.items():
        data = sc._parse_json_blob(raw)
        assert isinstance(data, dict) and data.get("summary"), f"gagal: {name}"


def test_parse_json_blob_first_object_wins_not_last_brace():
    """Cara lama ({ pertama .. } terakhir) menghasilkan None untuk dua objek."""
    raw = '{"summary":"pertama","mood":"lazy"}\n{"summary":"kedua"}'
    assert sc._parse_json_blob(raw)["summary"] == "pertama"


def test_parse_json_blob_gives_up_on_real_garbage():
    assert sc._parse_json_blob("maaf, aku tidak bisa menjawab") is None
    assert sc._parse_json_blob("") is None
    assert sc._parse_json_blob("{ rusak: tanpa kutip }") is None


def test_cursor_telemetry_labels_subsystem_and_model_honestly(monkeypatch):
    """Dulu panggilan observer ikut tercatat subsystem="scouter" (89 baris
    dobel 3/8) dan kolom model diisi nama ROLE — merusak audit biaya."""
    import arti_api_telemetry as tel
    import arti_cursor_agent as ca

    rec = []
    monkeypatch.setattr(tel, "record_call", lambda **kw: rec.append(kw))

    class _R:
        ok, text, latency_ms, model, reason = True, "{}", 11, "", ""

    monkeypatch.setattr(ca, "send_task", lambda *a, **k: _R())
    sc._call_cursor("p", {})
    sc._call_cursor("p", {"cursor_role": "observer"})

    assert [r["subsystem"] for r in rec] == ["scouter", "observer"]
    assert rec[0]["model"] == "composer-2.5"
    assert rec[1]["model"] == "grok-4.5/high", "model asli, bukan nama role"


def test_parse_json_blob_survives_ghost_brace_in_quoted_prose():
    """Audit ronde-3: '{' TAK BERPASANGAN di dalam kutipan prosa membuka
    "objek hantu" yang menelan JSON asli di belakangnya."""
    raw = 'Catatan "{" penting.\n{"summary":"ok","mood":"lazy"}'
    data = sc._parse_json_blob(raw)
    assert data and data["summary"] == "ok"
    raw2 = 'Kata dia "}" doang.\n{"summary":"ok2"}'
    assert sc._parse_json_blob(raw2)["summary"] == "ok2"

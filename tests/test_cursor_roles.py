"""Sesi Cursor per-role: scout (composer), observer (grok-4.5/high), vision
(composer + gambar).

Keputusan Bohan 2026-08-01 (Cursor tulang punggung, chain gratis = fallback),
DIREVISI 2026-08-03 setelah CSV usage: grok-high 23,5M token = 49% konsumsi
sehari dari 671 call scouter — scouter turun ke composer non-fast, grok-high
tinggal untuk observer (ringkas akhir live saja).

Fakta terverifikasi spike_grok_vision.py (list_models resmi):
- grok-4.5: param effort low/medium/high + fast; id "-high" TIDAK ada
- composer-2.5: param fast SAJA (effort ke composer = tidak valid)
- default variant composer-2.5 = fast=TRUE -> fast wajib eksplisit false
- gambar: UserMessage(text, images=[SDKImage]) — Agent.send tanpa kwarg images

Semua test tanpa SDK dan tanpa jaringan.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_cursor_agent as ca
import arti_scouter_client as scouter
import arti_vision_client as vision


# --- resolve_role_model: pure -------------------------------------------------


def test_voice_role_stays_composer_no_effort():
    model, effort = ca.resolve_role_model("voice", {"cursor_model": "composer-2.5"})
    assert model == "composer-2.5"
    assert effort is None, "composer tidak punya param effort"


def test_scout_role_defaults_composer_cheap():
    """Revisi Bohan 2026-08-03 (CSV usage: grok-high 23,5M token = 49%
    konsumsi sehari dari 671 call scouter): "composer 2.5 NOT FAST is
    enough" — scouter per menit turun ke composer."""
    model, effort = ca.resolve_role_model("scout", {})
    assert model == "composer-2.5"
    assert effort is None


def test_observer_role_keeps_grok_high():
    """"mungkin buat ringkas di akhir live gapapa" — kualitas kurasi > biaya,
    dan observer cuma hidup sekali di shutdown."""
    model, effort = ca.resolve_role_model("observer", {})
    assert model == "grok-4.5"
    assert effort == "high"
    assert ca.role_timeout_sec("observer", {}) >= 60.0, (
        "grok-high per segmen terukur 12-32 dtk + cold start (tanpa prewarm)"
    )


def test_vision_role_defaults_composer_without_effort():
    model, effort = ca.resolve_role_model("vision", {})
    assert model == "composer-2.5"
    assert effort is None, "effort kosong TIDAK boleh dikirim ke composer"


def test_role_model_overridable_via_config():
    cfg = {"cursor_scout_model": "composer-2.5", "cursor_scout_effort": ""}
    assert ca.resolve_role_model("scout", cfg) == ("composer-2.5", None)


def test_role_timeouts_are_looser_than_voice():
    cfg = {"cursor_timeout_sec": 7.0}
    assert ca.role_timeout_sec("voice", cfg) == 7.0
    assert ca.role_timeout_sec("scout", cfg) >= 40.0, (
        "composer scout TERUKUR cold 27,2 dtk (spike 2026-08-03) — timeout "
        "harus punya margin, sesi didaur ulang berkali-kali sepanjang live"
    )


# --- send_task: gerbang & breaker per role ------------------------------------


@pytest.fixture(autouse=True)
def _reset_role_state():
    with ca._registry_lock:
        ca._role_sessions.clear()
        ca._role_breakers.clear()
    yield
    with ca._registry_lock:
        ca._role_sessions.clear()
        ca._role_breakers.clear()


def test_send_task_rejects_voice_role():
    with pytest.raises(ValueError):
        ca.send_task("voice", "s", "u", {})


def test_send_task_unavailable_when_cursor_off():
    r = ca.send_task("scout", "s", "u", {"cursor_agent_enabled": False})
    assert r.ok is False
    assert r.reason == ca.REASON_UNAVAILABLE


def test_role_breaker_opens_and_does_not_touch_voice(monkeypatch, tmp_path):
    cfg = {
        "cursor_agent_enabled": True, "cursor_api_key": "x",
        "cursor_scratch_dir": str(tmp_path),
        "cursor_max_consecutive_failures": 2,
        "cursor_breaker_cooldown_sec": 900,
    }
    monkeypatch.setattr(
        ca.CursorSession, "send_collect",
        lambda self, s, u, **k: ca.CursorResult(reason=ca.REASON_ERROR),
    )
    assert ca.send_task("scout", "s", "u", cfg).reason == ca.REASON_ERROR
    assert ca.send_task("scout", "s", "u", cfg).reason == ca.REASON_ERROR
    # breaker scout terbuka
    assert ca.send_task("scout", "s", "u", cfg).reason == ca.REASON_DISABLED
    # breaker voice TIDAK ikut terbuka
    assert ca._breaker_open is False, "kegagalan scout tidak boleh menutup jalur suara"


def test_role_breaker_half_open_after_cooldown(monkeypatch, tmp_path):
    cfg = {
        "cursor_agent_enabled": True, "cursor_api_key": "x",
        "cursor_scratch_dir": str(tmp_path),
        "cursor_max_consecutive_failures": 1,
        "cursor_breaker_cooldown_sec": 900,
    }
    monkeypatch.setattr(
        ca.CursorSession, "send_collect",
        lambda self, s, u, **k: ca.CursorResult(reason=ca.REASON_ERROR),
    )
    ca.send_task("scout", "s", "u", cfg)
    assert ca.send_task("scout", "s", "u", cfg).reason == ca.REASON_DISABLED
    ca._role_breaker("scout")["opened_at"] = time.monotonic() - 9999
    r = ca.send_task("scout", "s", "u", cfg)
    assert r.reason == ca.REASON_ERROR, "setelah cooldown harus MENCOBA lagi, bukan disabled"


def test_send_task_success_passes_images_and_resets_failures(monkeypatch, tmp_path):
    cfg = {
        "cursor_agent_enabled": True, "cursor_api_key": "x",
        "cursor_scratch_dir": str(tmp_path),
    }
    seen = {}

    def _fake(self, s, u, **kw):
        seen.update(kw)
        return ca.CursorResult(text="halo", ok=True, reason=ca.REASON_OK)

    monkeypatch.setattr(ca.CursorSession, "send_collect", _fake)
    r = ca.send_task("vision", "", "apa di layar?", cfg, image_paths=["x.jpg"])
    assert r.ok and r.text == "halo"
    assert seen.get("image_paths") == ["x.jpg"]
    assert ca._role_breaker("vision")["failures"] == 0


# --- scouter provider "cursor" ------------------------------------------------


def test_scouter_has_cursor_provider_registered():
    assert "cursor" in scouter._PROVIDERS


def test_scouter_call_cursor_returns_text(monkeypatch):
    monkeypatch.setattr(
        ca, "send_task",
        lambda role, s, u, cfg, **k: ca.CursorResult(
            text='{"summary":"ok"}', ok=True, reason=ca.REASON_OK,
            latency_ms=1234, model="grok-4.5/high"),
    )
    raw, ms = scouter._call_cursor("prompt", {})
    assert raw == '{"summary":"ok"}'
    assert ms == 1234


def test_scouter_call_cursor_raises_on_failure_so_chain_continues(monkeypatch):
    monkeypatch.setattr(
        ca, "send_task",
        lambda *a, **k: ca.CursorResult(reason=ca.REASON_TIMEOUT),
    )
    with pytest.raises(RuntimeError):
        scouter._call_cursor("prompt", {})


def test_scouter_chain_skips_cursor_when_unavailable(monkeypatch):
    monkeypatch.setattr(ca, "is_available", lambda cfg: (False, "off"))
    chain = scouter._resolve_chain({"scouter_provider_chain": ["cursor"]})
    assert "cursor" not in chain


def test_scouter_chain_keeps_cursor_when_available(monkeypatch):
    monkeypatch.setattr(ca, "is_available", lambda cfg: (True, "siap"))
    chain = scouter._resolve_chain({"scouter_provider_chain": ["cursor"]})
    assert chain == ["cursor"]


# --- vision provider "cursor" -------------------------------------------------


def test_vision_has_cursor_provider_registered():
    assert "cursor" in vision._PROVIDERS


def test_vision_call_cursor_writes_temp_jpeg_then_cleans(monkeypatch):
    import base64
    import os

    captured = {}

    def _fake(role, s, u, cfg, image_paths=None, **k):
        captured["role"] = role
        captured["paths"] = list(image_paths or [])
        captured["existed"] = all(os.path.isfile(p) for p in (image_paths or []))
        return ca.CursorResult(text="layar berisi editor", ok=True, reason=ca.REASON_OK)

    monkeypatch.setattr(ca, "send_task", _fake)
    b64 = base64.b64encode(b"\xff\xd8\xff-fake-jpeg").decode()
    raw, _ms = vision._call_cursor("apa ini?", b64, {})
    assert raw == "layar berisi editor"
    assert captured["role"] == "vision"
    assert captured["existed"] is True, "berkas jpeg harus ADA saat dipakai"
    assert all(not os.path.exists(p) for p in captured["paths"]), (
        "berkas sementara harus dibersihkan setelah selesai"
    )


def test_vision_chain_skips_cursor_when_unavailable(monkeypatch):
    monkeypatch.setattr(ca, "is_available", lambda cfg: (False, "off"))
    chain = vision._resolve_chain({"vision_provider_chain": ["cursor", "nvidia"]})
    assert "cursor" not in chain


# --- observer memakai registry scouter → cursor ikut --------------------------


def test_observer_uses_cursor_via_scouter_registry(monkeypatch):
    import arti_observer_client as obs

    monkeypatch.setitem(
        scouter._PROVIDERS, "cursor",
        lambda prompt, cfg: ('{"summary": "ringkasan segmen", "worth_embed": true}', 10),
    )
    data = obs.summarize_segment("transcript segmen", {"observer_provider_chain": ["cursor"]})
    assert data.get("summary") == "ringkasan segmen"
    assert data.get("provider") == "cursor"


# --- default shipped tetap aman ----------------------------------------------


def test_shipped_default_chains_have_no_cursor():
    """Repo publik: chain default TIDAK berisi cursor — nyalakan via config_local."""
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    for key in ("cursor_scout_model", "cursor_scout_effort", "cursor_vision_model"):
        assert f'"{key}"' in src, f"CONFIG kehilangan {key}"
    import re

    for chain_key in ("vision_provider_chain", "scouter_provider_chain",
                      "observer_provider_chain"):
        m = re.search(rf'"{chain_key}": \[(.*?)\]', src, re.DOTALL)
        assert m, f"{chain_key} tidak ketemu di CONFIG"
        assert '"cursor"' not in m.group(1), (
            f"chain default {chain_key} tidak boleh berisi cursor (repo publik)"
        )


# --- KEBIJAKAN BOHAN (2026-08-01): dua model saja, tidak pernah Fast ----------


def test_policy_only_composer_and_grok_never_fast():
    """"pokoknya composer 2.5 NOT FAST sama grok 4.5 high NOT FAST aja, oke?"

    Terkunci di sini. Kalau test ini merah, ada kode/config yang mencoba model
    lain atau varian Fast — 6x lebih mahal, dan pool API pihak ketiga SUDAH habis.
    """
    allowed = {"composer-2.5", "grok-4.5"}
    for role in ("voice", "scout", "vision", "observer"):
        model, effort = ca.resolve_role_model(role, {})
        assert model in allowed, f"role {role} resolve ke model terlarang: {model}"
        if model == "grok-4.5":
            assert effort == "high", "grok wajib effort=high (keputusan eksplisit)"

    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"cursor_fast_param": False,' in src, "fast TIDAK PERNAH boleh default True"
    assert '"cursor_model": "composer-2.5",' in src
    # Revisi 2026-08-03: scouter (tiap menit) = composer; grok cuma observer.
    assert '"cursor_scout_model": "composer-2.5",' in src
    assert '"cursor_observer_model": "grok-4.5",' in src
    assert '"cursor_observer_effort": "high",' in src
    assert '"cursor_vision_model": "composer-2.5",' in src

    agent_src = (ROOT / "arti_cursor_agent.py").read_text(encoding="utf-8")
    assert "ModelSelection(id=model_id, params=params)" in agent_src, (
        "ModelSelection wajib eksplisit — id string polos resolve ke FAST "
        "(varian default composer-2.5 = fast=true, terverifikasi list_models)"
    )

# --- routing role scouter vs observer (revisi biaya 2026-08-03) -------------------


def test_call_cursor_role_from_config(monkeypatch):
    """_call_cursor default role 'scout'; observer menimpa via cursor_role —
    satu jalur chain yang sama, dua sesi/model berbeda."""
    import arti_scouter_client as scl

    seen = []

    class _R:
        ok = True
        text = "t"
        latency_ms = 5
        model = "m"
        reason = ""

    monkeypatch.setattr(
        ca, "send_task", lambda role, sys_p, prompt, cfg: (seen.append(role), _R())[1]
    )
    scl._call_cursor("p", {})
    scl._call_cursor("p", {"cursor_role": "observer"})
    assert seen == ["scout", "observer"]


def test_observer_client_wires_observer_role():
    src = (ROOT / "arti_observer_client.py").read_text(encoding="utf-8")
    assert '"cursor_role": "observer"' in src, (
        "observer wajib pakai sesi grok-nya sendiri — scouter sudah composer"
    )


def test_unknown_role_is_loud_not_silent(capsys, tmp_path):
    """Typo cursor_role dulu jatuh diam-diam ke composer — observer bisa
    turun kelas tanpa peringatan (audit 2026-08-03)."""
    assert "observer" in ca.KNOWN_ROLES and "scout" in ca.KNOWN_ROLES
    ca.send_task("obsrever", "s", "u", {"cursor_agent_enabled": False})
    out = capsys.readouterr().out
    assert "role tidak dikenal" in out and "obsrever" in out


def test_known_role_stays_quiet(capsys):
    ca.send_task("observer", "s", "u", {"cursor_agent_enabled": False})
    assert "role tidak dikenal" not in capsys.readouterr().out


def test_prewarm_does_not_conflate_observer_chain_into_scout():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    i = src.index("role_chains = {")
    seg = src[i:i + 300]
    assert "observer_provider_chain" not in seg, (
        "chain observer jangan jadi syarat pemanas role scout — bisa "
        "memanaskan role yang salah"
    )

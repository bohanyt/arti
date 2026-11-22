"""Rem kuota Gemini free tier.

Latar: tanpa ingatan kuota, google_gemini di posisi 1 rantai scouter bisa
ditembak terus walau dibalas HTTP 429 — free tier flash-lite cuma belasan RPM,
dan provider yang baru ditolak tetap ditembak giliran depannya. Tujuan:
(1) jangan spam error di terminal, (2) rem preventif di bawah pagar Google
(jatah token & panggilan per menit dibatasi sendiri).
"""

from __future__ import annotations

import json

import pytest

import arti_gemini_budget as budget
import arti_gemini_vision
import arti_scouter_client
import arti_vision_client
import arti_bridge as bridge


@pytest.fixture(autouse=True)
def _bersih():
    budget.reset()
    yield
    budget.reset()


class _JamPalsu:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def jam(monkeypatch):
    j = _JamPalsu()
    monkeypatch.setattr(budget, "_now", j)
    return j


# --------------------------------------------------------------------------
# Logika rem murni
# --------------------------------------------------------------------------

def test_rpm_menahan_panggilan_berikutnya(jam):
    cfg = {"gemini_rpm_budget": 3}
    for _ in range(3):
        assert budget.boleh("m", 10, cfg)[0]
        budget.catat_panggilan("m")
    ok, alasan = budget.boleh("m", 10, cfg)
    assert not ok
    assert "panggilan" in alasan


def test_tpm_menahan_sebelum_pagar_google(jam):
    cfg = {"gemini_tpm_budget": 200_000}
    budget.catat_token("m", 199_000)
    ok, alasan = budget.boleh("m", 5_000, cfg)
    assert not ok
    assert "token" in alasan


def test_jendela_60_detik_bergulir(jam):
    cfg = {"gemini_rpm_budget": 1, "gemini_tpm_budget": 100}
    budget.catat_panggilan("m")
    budget.catat_token("m", 100)
    assert not budget.boleh("m", 1, cfg)[0]
    jam.t += 61.0
    assert budget.boleh("m", 1, cfg)[0]


def test_429_memicu_istirahat_lalu_pulih(jam):
    cfg = {"gemini_429_cooldown_sec": 60.0}
    budget.kena_429("m", cfg)
    ok, alasan = budget.boleh("m", 1, cfg)
    assert not ok
    assert "429" in alasan
    jam.t += 61.0
    assert budget.boleh("m", 1, cfg)[0]


def test_429_daily_quota_parks_through_minute_cooldowns(jam, capsys):
    budget.kena_429("m", {"gemini_429_cooldown_sec": 60}, "PerDay RequestsPerDay quota")
    assert not budget.boleh("m", 1, {})[0]
    jam.t += 61
    assert not budget.boleh("m", 1, {})[0]
    assert capsys.readouterr().out.count("kuota harian terdeteksi") == 1


def test_unknown_429_escalates_but_per_minute_stays_short(jam):
    budget.kena_429("unknown", {}, "opaque response")
    assert budget._istirahat_sampai["unknown"] - jam.t == 60
    jam.t += 61
    budget.kena_429("unknown", {}, "opaque response")
    assert budget._istirahat_sampai["unknown"] - jam.t == 600
    budget.kena_429("rpm", {}, "RequestsPerMinute quota")
    budget.kena_429("rpm", {}, "RequestsPerMinute quota")
    assert budget._istirahat_sampai["rpm"] - jam.t == 60


def test_gemini_daily_429_skips_vision_and_scouter_chains(jam):
    cfg = {
        "scouter_provider_chain": ["google_gemini", "zai"],
        "vision_provider_chain": ["google_gemini_lite", "google_gemma", "zai"],
        "gemini_api_key": "kunci-tes",
        "zai_api_key": "kunci-tes",
    }
    budget.kena_429("gemini-3.1-flash-lite", cfg, "exceeded your current quota PerDay")
    assert "google_gemini" not in arti_scouter_client._resolve_chain(cfg)
    assert "google_gemini_lite" not in arti_vision_client._resolve_chain(cfg)


def test_kuota_per_model_terpisah(jam):
    budget.kena_429("gemini-3.1-flash-lite", {})
    assert budget.boleh("gemma-4-26b-a4b-it", 1, {})[0]


def test_log_hanya_saat_ganti_keadaan(jam, capsys):
    """Anti-spam: satu baris waktu mulai menahan, hening selama menahan,
    satu baris waktu pulih."""
    budget.kena_429("m", {"gemini_429_cooldown_sec": 60.0})
    budget.boleh("m", 1, {})
    budget.boleh("m", 1, {})
    budget.boleh("m", 1, {})
    keluaran = capsys.readouterr().out
    assert keluaran.count("[GeminiBudget]") == 1

    jam.t += 61.0
    budget.boleh("m", 1, {})
    assert "pulih" in capsys.readouterr().out


def test_none_di_config_tidak_mematikan_rem(jam):
    """Aturan 7 CLAUDE.md: kunci yang ADA bernilai None harus jatuh ke
    default, bukan meledak / mematikan rem."""
    cfg = {"gemini_rpm_budget": None, "gemini_tpm_budget": None,
           "gemini_429_cooldown_sec": None}
    for _ in range(budget.DEFAULT_RPM_BUDGET):
        budget.catat_panggilan("m")
    assert not budget.boleh("m", 1, cfg)[0]


def test_config_produksi_punya_kunci_budget():
    assert int(bridge.CONFIG["gemini_rpm_budget"]) == 12
    assert int(bridge.CONFIG["gemini_tpm_budget"]) == 200_000
    assert float(bridge.CONFIG["gemini_429_cooldown_sec"]) == 60.0


# --------------------------------------------------------------------------
# Crosscheck EKSEKUSI pintu tunggal (aturan 11): text_generate asli,
# HTTP dipalsukan — plus kontrol negatif membuktikan harness bisa lolos.
# --------------------------------------------------------------------------

class _ResponsPalsu:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


_BODY_OK = {
    "candidates": [{"content": {"parts": [{"text": "halo"}]}}],
    "usageMetadata": {"totalTokenCount": 321},
}


def test_choke_point_tidak_menembak_saat_rem_menahan(jam, monkeypatch):
    cfg = {"gemini_api_key": "kunci-tes"}
    tembakan = []

    def _post(url, json=None, timeout=None):
        tembakan.append(url)
        return _ResponsPalsu(200, _BODY_OK)

    monkeypatch.setattr(arti_gemini_vision.requests, "post", _post)

    # Kontrol negatif dulu: jatah segar -> harness MEMANG menembak & sukses.
    text, _ms = arti_gemini_vision.text_generate("halo", config=cfg)
    assert text == "halo"
    assert len(tembakan) == 1

    # Token balasan tercatat ke jendela TPM (321 dari usageMetadata).
    ok, _ = budget.boleh("gemini-3.1-flash-lite", 0, {"gemini_tpm_budget": 300})
    assert not ok

    # Rem menahan -> TIDAK ada request HTTP sama sekali.
    budget.kena_429("gemini-3.1-flash-lite", cfg)
    with pytest.raises(budget.JatahPenuh):
        arti_gemini_vision.text_generate("halo", config=cfg)
    assert len(tembakan) == 1, "rem lolos — HTTP tetap ditembak"


def test_429_dari_google_membungkam_panggilan_berikutnya(jam, monkeypatch):
    cfg = {"gemini_api_key": "kunci-tes"}
    tembakan = []

    def _post(url, json=None, timeout=None):
        tembakan.append(url)
        return _ResponsPalsu(429, {"error": "quota"})

    monkeypatch.setattr(arti_gemini_vision.requests, "post", _post)

    with pytest.raises(RuntimeError, match="HTTP 429"):
        arti_gemini_vision.text_generate("halo", config=cfg)
    assert len(tembakan) == 1

    # Panggilan kedua ditahan rem SEBELUM HTTP — inilah anti-spam-nya.
    with pytest.raises(budget.JatahPenuh):
        arti_gemini_vision.text_generate("halo", config=cfg)
    assert len(tembakan) == 1


def test_vision_generate_juga_lewat_rem(jam, monkeypatch):
    cfg = {"gemini_api_key": "kunci-tes"}
    monkeypatch.setattr(
        arti_gemini_vision.requests, "post",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP ditembak")),
    )
    budget.kena_429("gemma-4-26b-a4b-it", cfg)
    with pytest.raises(budget.JatahPenuh):
        arti_gemini_vision.vision_generate("halo", "aGFsbw==", config=cfg)


def test_vision_gemma_mematikan_thinking_untuk_budget_json(jam, monkeypatch):
    payload = {}

    def _post(_url, json=None, timeout=None):
        del timeout
        payload.update(json)
        return _ResponsPalsu(200, _BODY_OK)

    monkeypatch.setattr(arti_gemini_vision.requests, "post", _post)

    arti_gemini_vision.vision_generate(
        "JSON saja",
        "aGFsbw==",
        config={"gemini_api_key": "kunci-tes"},
        model="gemma-4-26b-a4b-it",
    )

    assert payload["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "minimal"
    }


def test_vision_gemini_lite_tidak_diberi_config_khusus_gemma(jam, monkeypatch):
    payload = {}

    def _post(_url, json=None, timeout=None):
        del timeout
        payload.update(json)
        return _ResponsPalsu(200, _BODY_OK)

    monkeypatch.setattr(arti_gemini_vision.requests, "post", _post)

    arti_gemini_vision.vision_generate(
        "JSON saja",
        "aGFsbw==",
        config={"gemini_api_key": "kunci-tes"},
        model="gemini-3.1-flash-lite",
    )

    assert "thinkingConfig" not in payload["generationConfig"]


# --------------------------------------------------------------------------
# Rantai melewati Gemini TANPA baris error selagi rem menahan
# --------------------------------------------------------------------------

def test_rantai_scouter_melewati_gemini_saat_istirahat(jam):
    cfg = {
        "scouter_provider_chain": ["google_gemini", "zai"],
        "gemini_api_key": "kunci-tes",
        "zai_api_key": "kunci-tes",
    }
    assert "google_gemini" in arti_scouter_client._resolve_chain(cfg)
    budget.kena_429("gemini-3.1-flash-lite", cfg)
    rantai = arti_scouter_client._resolve_chain(cfg)
    assert "google_gemini" not in rantai
    assert "zai" in rantai  # fallback tetap hidup


def test_rantai_vision_melewati_gemini_lite_saat_istirahat(jam):
    cfg = {
        "vision_provider_chain": ["google_gemini_lite", "google_gemma", "zai"],
        "gemini_api_key": "kunci-tes",
        "zai_api_key": "kunci-tes",
    }
    budget.kena_429("gemini-3.1-flash-lite", cfg)
    rantai = arti_vision_client._resolve_chain(cfg)
    assert "google_gemini_lite" not in rantai
    # gemma model lain — kuotanya terpisah, tetap boleh.
    assert "google_gemma" in rantai


def test_rantai_vision_gemma_bertahan_selama_cadangan_segar(jam):
    """google_gemma baru dilewati kalau model utama DAN cadangan sama-sama
    tertahan — kuota Google per model, cadangan bisa masih segar."""
    cfg = {
        "vision_provider_chain": ["google_gemma", "zai"],
        "gemini_api_key": "kunci-tes",
        "zai_api_key": "kunci-tes",
    }
    budget.kena_429("gemma-4-26b-a4b-it", cfg)
    assert "google_gemma" in arti_vision_client._resolve_chain(cfg)
    budget.kena_429("gemma-4-31b-it", cfg)
    assert "google_gemma" not in arti_vision_client._resolve_chain(cfg)

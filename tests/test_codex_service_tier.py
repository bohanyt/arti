"""Luna 6 fast sebagai otak suara utama (owner 23 Okt 2026).

Kontrak:
- `codex_service_tier` kosong/None = kwarg TIDAK dikirim (perilaku lama).
- `codex_service_tier: "priority"` dikirim per giliran ke SDK
  `Thread.run(service_tier=...)`, sejajar model/effort.
- `codex_model` dari config dipakai di jalur suara (bukan default modul).
- `agy_agent_enabled: false` -> jalur suara langsung Codex; agy tidak
  pernah init/spawn dan tidak ada log "[Agy] ...".
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

import arti_agy_agent
import arti_codex_agent as ca
import arti_bridge as bridge


class _ThreadRekam:
    def __init__(self, jawab="halo dari luna"):
        self.jawab = jawab
        self.panggilan: list[tuple[str, dict]] = []

    def run(self, teks, **kwargs):
        self.panggilan.append((teks, dict(kwargs)))

        class _R:
            final_response = self.jawab
            usage = None

        return _R()


def _pasang(monkeypatch, th):
    monkeypatch.setattr(ca, "_client", object())
    monkeypatch.setattr(ca, "_thread", th)
    monkeypatch.setattr(ca, "_turns", 0)


@pytest.mark.parametrize("nilai", [None, "", "   "])
def test_service_tier_kosong_berarti_tidak_dikirim(nilai):
    assert ca.service_tier({"codex_service_tier": nilai}) is None
    assert ca.service_tier({}) is None


def test_service_tier_priority_dibaca_apa_adanya():
    assert ca.service_tier({"codex_service_tier": " priority "}) == "priority"


def test_source_default_tier_none_di_bridge():
    assert bridge.CONFIG.get("codex_service_tier", None) in (None, "", "priority")
    src = Path(bridge.__file__).read_text(encoding="utf-8")
    assert '"codex_service_tier": None,' in src


def test_tanpa_tier_kwargs_persis_perilaku_lama(monkeypatch):
    th = _ThreadRekam()
    _pasang(monkeypatch, th)
    r = ca.send_turn("sys", "halo", {"codex_agent_enabled": True})
    assert r == "halo dari luna"
    teks, kwargs = th.panggilan[0]
    assert teks == "sys\n\nhalo"
    assert kwargs == {"model": "gpt-5.6-luna", "effort": "low"}


def test_luna6_priority_low_dikirim_ke_run(monkeypatch, capsys):
    th = _ThreadRekam()
    _pasang(monkeypatch, th)
    cfg = {
        "codex_agent_enabled": True,
        "codex_model": "gpt-6-luna",
        "codex_effort": "low",
        "codex_service_tier": "priority",
    }
    assert ca.send_turn("sys", "halo", cfg) == "halo dari luna"
    assert th.panggilan[0][1] == {
        "model": "gpt-6-luna", "effort": "low", "service_tier": "priority",
    }
    assert "[Codex] gpt-6-luna/low/priority " in capsys.readouterr().out


def test_sdk_terpasang_menerima_service_tier_per_giliran():
    oc_api = pytest.importorskip("openai_codex.api")
    params = inspect.signature(oc_api.Thread.run).parameters
    assert "service_tier" in params and "model" in params and "effort" in params


def test_transport_adapter_meneruskan_tier(monkeypatch):
    import arti_brain_adapters as ba

    seen = {}

    def _send(sys_p, user, cfg):
        seen.update(cfg)
        return "ok"

    monkeypatch.setattr(ca, "lease_identity", lambda cfg=None: ("m", "low", "s", 1))
    t = ba.CodexSendTransport(
        config={"codex_service_tier": "priority", "codex_model": "gpt-6-luna"},
        send_fn=_send,
    )
    rep = t.send("s", "u", timeout_s=1.0, lease=None)
    assert rep.ok and seen["codex_service_tier"] == "priority"
    assert seen["codex_model"] == "gpt-6-luna"


def _cfg_live_usulan(**over):
    """Bentuk config_local usulan: agy mati, Luna 6 fast utama."""
    cfg = {
        **bridge.CONFIG,
        "agy_agent_enabled": False,
        "agy_primary_voice": True,  # sisa config lama; harus tak berpengaruh
        "agy_model": "gemini-3.6-flash-low",
        "codex_agent_enabled": True,
        "codex_primary_voice": True,
        "codex_model": "gpt-6-luna",
        "codex_effort": "low",
        "codex_service_tier": "priority",
        "codex_effort_kelas_berat": [],
        "cursor_agent_enabled": True,
        "cursor_trigger_types": [
            "yt_chat", "curious", "donation", "video", "game", "mic", "ptt",
        ],
        "_codex_primary_tes": True,
        "_agy_primary_tes": True,
    }
    cfg.update(over)
    return cfg


@pytest.mark.parametrize("trigger", ["mic", "ptt", "yt_chat", "curious", "donation", "video"])
def test_agy_mati_routing_tetap_masuk_jalur_luna(trigger):
    assert bridge._should_route_to_cursor(trigger, _cfg_live_usulan()) is True


@pytest.mark.parametrize("trigger", ["mic", "yt_chat", "curious"])
def test_agy_mati_jalur_suara_luna6_priority_tanpa_agy(monkeypatch, capsys, trigger):
    import arti_cursor_agent

    th = _ThreadRekam(jawab="Jawaban Luna 6.")
    _pasang(monkeypatch, th)

    spawn = []

    def _factory(*a, **k):  # bukan factory asli -> pagar pytest agy tak menolong
        spawn.append(a)
        raise AssertionError("agy di-spawn padahal mati")

    monkeypatch.setattr(arti_agy_agent, "_process_factory", _factory)
    monkeypatch.setattr(
        arti_agy_agent, "send_turn",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("agy send_turn terpanggil")),
    )
    monkeypatch.setattr(arti_cursor_agent, "prewarm", lambda _cfg: True)
    monkeypatch.setattr(
        bridge, "groq_chat_completion",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Groq terpanggil")),
    )

    reply, _kal, sumber = asyncio.run(
        bridge._cursor_reply_with_fallback(
            "sys", "prompt", "ucapan", _cfg_live_usulan(), trigger
        )
    )
    assert reply == "Jawaban Luna 6." and sumber == "codex"
    assert th.panggilan[0][1] == {
        "model": "gpt-6-luna", "effort": "low", "service_tier": "priority",
    }
    assert spawn == []
    out = capsys.readouterr().out
    assert "[Agy]" not in out
    assert "[Codex] gpt-6-luna/low/priority " in out


def test_lapor_kuota_skips_agy_exe_when_agy_disabled(monkeypatch, capsys):
    import arti_agy_agent

    def boom(*a, **kw):
        raise AssertionError("agy.exe must not run when Agy is disabled")

    monkeypatch.setattr(arti_agy_agent, "baca_kuota", boom)
    assert arti_agy_agent.lapor_kuota("awal sesi", {"agy_agent_enabled": False}) == ""
    assert "[Agy]" not in capsys.readouterr().out


def test_lapor_kuota_still_reads_when_agy_enabled(monkeypatch, capsys):
    import arti_agy_agent

    monkeypatch.setattr(arti_agy_agent, "baca_kuota", lambda config, alasan=None: "sisa 90%")
    assert arti_agy_agent.lapor_kuota("awal sesi", {"agy_agent_enabled": True}) == "sisa 90%"
    assert "[Agy] kuota awal sesi: sisa 90%" in capsys.readouterr().out

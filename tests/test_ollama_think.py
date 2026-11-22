"""Ollama Cloud: penalaran tersembunyi tidak boleh memakan balasan Arti.

Ketujuh model gratis Ollama Cloud punya kemampuan `thinking`, dan penalarannya
dikembalikan di field TERPISAH (`message.thinking`) sambil tetap memakan jatah
`num_predict`. Terukur 13 Agu 2026: tanpa `think: False`, `nemotron-3-nano:30b`
mengembalikan `content` KOSONG TOTAL (603 karakter tertahan di `thinking`).

Ini kelas bug yang sama dengan qwen3.6 (`reasoning_effort: "none"`, fix 3baa8ef)
dan poolside (CoT menghabiskan budget). Sudah tiga kali menggigit — makanya
dikunci tes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_ollama_vision as O


def _tangkap_payload(monkeypatch):
    """Rekam payload yang DIKIRIM, tanpa menyentuh jaringan."""
    direkam = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"message": {"content": "halo dari Arti"}}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        direkam.update(json or {})
        return Resp()

    monkeypatch.setattr(O.requests, "post", fake_post)
    return direkam


def test_text_chat_mematikan_thinking(monkeypatch):
    direkam = _tangkap_payload(monkeypatch)
    teks, _ = O.text_chat(
        [{"role": "user", "content": "halo"}],
        config={"ollama_api_key": "kunci-tes"},
    )
    assert direkam.get("think") is False, "payload teks tidak mematikan thinking"
    assert teks == "halo dari Arti"


def test_vision_chat_mematikan_thinking(monkeypatch):
    direkam = _tangkap_payload(monkeypatch)
    O.vision_chat("apa ini", "ZmFrZQ==", config={"ollama_api_key": "kunci-tes"})
    assert direkam.get("think") is False, "payload vision tidak mematikan thinking"


def test_balasan_kosong_karena_thinking_bersuara(capsys):
    """Kalau model MENGABAIKAN flag, gejalanya balasan kosong — dan itu SENYAP.

    Kegagalan senyap adalah yang paling mahal di Arti: penonton cuma melihat
    dia diam. Penjaga ini memaksa kejadiannya muncul di log.
    """
    body = {"message": {"content": "", "thinking": "Okay so the user asks..." * 9}}
    hasil = O._ambil_balasan(body, "model-bandel")
    keluar = capsys.readouterr().out
    assert hasil == ""
    assert "MENGABAIKAN think=False" in keluar
    assert "model-bandel" in keluar


def test_balasan_normal_lolos_utuh(capsys):
    hasil = O._ambil_balasan({"message": {"content": "  Halo, aku Arti!  "}}, "x")
    assert hasil == "Halo, aku Arti!"
    assert capsys.readouterr().out == "", "balasan sehat tidak boleh bikin log"

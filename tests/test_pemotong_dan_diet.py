"""Paket 17 Agu malam — jawaban atas log tes 21.18 ("rusak", curious bisu):

1. Diet bahan (paket kohesi tahap 1-2, disetujui 17 Agu dini hari):
   RAG top-5 -> top-2, benang 2 kutipan -> 1, aturan SATU PIKIRAN di prompt
   curious. Obat composer yang p50-nya menabrak kapak 12 dtk (2 sukses
   semalam, 3 timeout, breaker 300 dtk).
2. Pemotong pintar: chunk TTS dipotong di batas pikiran (elipsis dilarang
   dipotong, kalimat pendek digabung) — keluhan "[recorded remark omitted]" (kata pendek dapat jeda 4 dtk sendiri).
3. Tambalan Groq: HTTP 200 content kosong = GAGAL (lanjut ke provider lain,
   bukan bisu), gpt-oss dapat reasoning_effort low (Groq MENGHORMATI low —
   beda dari provider free OpenRouter).
"""

from __future__ import annotations

import json

import arti_benang as ab
import arti_curious as ac
import arti_groq_stream as gs
import arti_bridge as bridge

_PANJANG1 = "Aku tuh dari tadi mikirin gerobak itu kenapa bisa kegedean sampai segitunya loh."
_PANJANG2 = "Terus makin dipikir makin aneh juga sih posisinya nempel di pinggir jurang gitu."


# ---------------------------------------------------------------------------
# Pemotong pintar
# ---------------------------------------------------------------------------

def test_elipsis_tidak_pernah_jadi_batas_potong():
    teks = (
        "Aku tuh mikir... jangan-jangan gerobaknya emang kegedean dari sononya. "
        + _PANJANG2
    )
    parts = gs.split_indonesian_sentences(teks)
    assert len(parts) == 2
    assert "mikir... jangan-jangan" in parts[0], "elipsis dipotong — jeda jatuh di pikiran menggantung"


def test_kalimat_pendek_menempel_ke_kalimat_berikutnya():
    parts = gs.split_indonesian_sentences(f"Wkwk. {_PANJANG1}")
    assert len(parts) == 1
    assert parts[0].startswith("Wkwk. ")


def test_ekor_pendek_menempel_ke_belakang():
    parts = gs.split_indonesian_sentences(f"{_PANJANG1} {_PANJANG2} Wkwk.")
    assert len(parts) == 2
    assert parts[-1].endswith("Wkwk.")


def test_dua_pikiran_panjang_tetap_dua_chunk():
    parts = gs.split_indonesian_sentences(f"{_PANJANG1} {_PANJANG2}")
    assert parts == [_PANJANG1, _PANJANG2]


def test_min_chars_1_kembali_ke_potong_murni():
    parts = gs.split_indonesian_sentences("Halo Kak! Aku Arti. Senang ketemu ya?", min_chars=1)
    assert parts == ["Halo Kak!", "Aku Arti.", "Senang ketemu ya?"]


def test_stream_collector_memakai_pemotong_pintar():
    """Loop separator internal stream masih polos — hasil AKHIR wajib
    dibentuk ulang lewat pemotong pintar."""
    lines = [
        b'data: {"choices":[{"delta":{"content":"Wkwk. "}}]}\n',
        ('data: {"choices":[{"delta":{"content":' + json.dumps(_PANJANG1) + '}}]}\n').encode(),
        b"data: [DONE]\n",
    ]
    full, sentences = gs.collect_streaming_reply(iter(lines))
    assert _PANJANG1 in full
    assert len(sentences) == 1, "chunk 'Wkwk.' sendirian = jeda napas nyasar"


# ---------------------------------------------------------------------------
# Diet bahan
# ---------------------------------------------------------------------------

def test_rag_top_k_produksi_sudah_diet():
    assert int(bridge.CONFIG["vault_rag_top_k"]) == 2


def test_benang_hanya_satu_kutipan():
    ab.reset_session()
    ab.catat("Arti (VTuber)", "Kutipan pertama yang lama banget.")
    ab.catat("Arti (VTuber)", "Kutipan kedua di tengah.")
    ab.catat("Arti (VTuber)", "Kutipan ketiga paling baru.")
    blok = ab.blok_prompt()
    ab.reset_session()
    assert blok.count("Barusan kamu bilang") == 1
    assert "Kutipan ketiga paling baru" in blok


def test_prompt_curious_membawa_satu_pikiran(monkeypatch):
    p1 = ac.build_initiative_prompt(
        dict(bridge.CONFIG, renungan_enabled=False), streamer_baru_bicara=True,
        ada_penonton=True, memory_bullets=["synthetic specific memory"], rng=lambda: 0.0
    )
    assert "SATU PIKIRAN" in p1
    assert "MELANJUTKAN kalimat sebelumnya" in p1

    class _Snap:
        scene = "Kucing tidur di kursi"
        playback_mmss = None

    monkeypatch.setattr(ac.screen_privacy, "allows_screen", lambda epoch=None: True)
    monkeypatch.setattr(ac.sc.screen_ring, "latest", lambda: _Snap())
    p2 = ac.build_prompt(dict(bridge.CONFIG, scouter_last_result={}))
    assert "SATU PIKIRAN" in p2


# ---------------------------------------------------------------------------
# Tambalan Groq — EKSEKUSI groq_chat_completion asli dengan HTTP palsu
# ---------------------------------------------------------------------------

class _Res:
    def __init__(self, content):
        self.status_code = 200
        self.text = ""
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _cfg():
    cfg = dict(bridge.CONFIG)
    cfg["groq_api_key"] = "kunci-tes"
    cfg["groq_roll_all_models_on_limit"] = False
    return cfg


def test_reasoning_effort_per_model(monkeypatch):
    payloads = []

    def _post(url, headers=None, json=None, timeout=None):
        payloads.append(json)
        return _Res("jawaban")

    monkeypatch.setattr(bridge.requests, "post", _post)
    for model, harapan in (
        ("openai/gpt-oss-120b", "low"),
        ("qwen/qwen3.6-27b", "none"),
        ("llama-3.1-8b-instant", None),
    ):
        bridge.groq_chat_completion(model, "sistem", "halo", _cfg())
        assert payloads[-1].get("reasoning_effort") == harapan, model


def test_content_kosong_jatuh_ke_openrouter(monkeypatch):
    """Bug curious-bisu 17 Agu: 200+kosong dulu di-return sebagai sukses,
    OpenRouter tak pernah dicoba. Sekarang kosong = lanjut rantai."""
    monkeypatch.setattr(bridge.requests, "post",
                        lambda *a, **k: _Res(""))
    monkeypatch.setattr(bridge, "_openrouter_after_groq",
                        lambda *a, **k: ("dari openrouter", "openrouter/tes"))
    reply, model = bridge.groq_chat_completion(
        "openai/gpt-oss-120b", "sistem", "halo", _cfg())
    assert reply == "dari openrouter"

    # Kontrol negatif: content terisi -> Groq sendiri yang menjawab.
    monkeypatch.setattr(bridge.requests, "post", lambda *a, **k: _Res("halo balik"))
    reply, model = bridge.groq_chat_completion(
        "openai/gpt-oss-120b", "sistem", "halo", _cfg())
    assert reply == "halo balik" and model == "openai/gpt-oss-120b"

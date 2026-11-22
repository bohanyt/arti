"""Catch-up observer (Bohan 2026-08-10: "[recorded remark omitted]") — sesi yang mati tanpa shutdown bersih dirangkum di startup
berikutnya, INKREMENTAL: segmen yang sudah punya beat tidak diringkas ulang
(hemat LLM + learnings tidak dobel).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curator as curator
import arti_observer_catchup as catchup
import arti_observer_pipeline as pipeline

SRC = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")


def _tulis_transcript(d: Path, sid: str, rows) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _rows_dua_bucket():
    """Bucket 0 (menit 0-9) + bucket 1 (menit 10-19)."""
    return [
        {"ts": "00:01:00", "kind": "streamer", "text": "halo sesi mulai"},
        {"ts": "00:05:00", "kind": "arti", "text": "halo bohan"},
        {"ts": "00:12:00", "kind": "streamer", "text": "bagian yang hilang"},
    ]


def _sandbox(monkeypatch, tmp_path: Path) -> dict:
    """Semua tulisan (beats, session md) masuk tmp — jangan sentuh vault asli."""
    monkeypatch.setattr(pipeline, "_ROOT", tmp_path)
    monkeypatch.setattr(curator, "_ROOT", tmp_path)
    monkeypatch.setattr(catchup, "_ROOT", tmp_path)
    return {"transcript_dir": str(tmp_path / "transcripts"),
            "observer_segment_minutes": 10}


# --- find_pending ---------------------------------------------------------

def test_transcript_tanpa_beats_terdeteksi_pending(monkeypatch, tmp_path):
    cfg = _sandbox(monkeypatch, tmp_path)
    _tulis_transcript(tmp_path / "transcripts", "2026-08-09-default", _rows_dua_bucket())
    assert catchup.find_pending(cfg) == ["2026-08-09-default"]


def test_transcript_lebih_baru_dari_beats_terdeteksi(monkeypatch, tmp_path):
    """Kasus nyata 2026-08-09: beats terakhir 23:21, transcript berakhir
    ~23:39 — sesi 23.35 tidak pernah masuk ingatan."""
    cfg = _sandbox(monkeypatch, tmp_path)
    tx = _tulis_transcript(tmp_path / "transcripts", "s1", _rows_dua_bucket())
    jsonl_path, _ = pipeline.beats_paths("s1", cfg)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text("{}", encoding="utf-8")
    lama = time.time() - 3600
    os.utime(jsonl_path, (lama, lama))
    assert catchup.find_pending(cfg) == ["s1"]
    # beats lebih baru dari transcript = sudah dirangkum -> bukan pending
    baru = time.time() + 300
    os.utime(jsonl_path, (baru, baru))
    assert catchup.find_pending(cfg) == []


def test_sesi_berjalan_dan_sesi_tua_tidak_disentuh(monkeypatch, tmp_path):
    cfg = _sandbox(monkeypatch, tmp_path)
    _tulis_transcript(tmp_path / "transcripts", "hari-ini", _rows_dua_bucket())
    tua = _tulis_transcript(tmp_path / "transcripts", "tua", _rows_dua_bucket())
    kadaluwarsa = time.time() - 30 * 86400
    os.utime(tua, (kadaluwarsa, kadaluwarsa))
    assert catchup.find_pending(cfg, current_session_id="hari-ini") == []


# --- run_catchup_session: inkremental ------------------------------------

def _fake_summarize(monkeypatch, hitung: list):
    import arti_observer_client as client

    def fake(block, config):
        hitung.append(block)
        return {"summary": f"ringkasan #{len(hitung)}", "topics": [],
                "facts": [], "worth_embed": False, "worth_learning": False,
                "noise_level": "low", "provider": "fake"}

    monkeypatch.setattr(client, "summarize_segment", fake)


def test_hanya_segmen_baru_yang_diringkas(monkeypatch, tmp_path):
    cfg = _sandbox(monkeypatch, tmp_path)
    _tulis_transcript(tmp_path / "transcripts", "s1", _rows_dua_bucket())
    jsonl_path, _ = pipeline.beats_paths("s1", cfg)
    # Bucket 0 sudah pernah dirangkum di shutdown bersih sebelumnya.
    beat_lama = pipeline.BeatDraft(
        session_id="s1", segment_index=0, t_start="00:00:00", t_end="00:10:00",
        event_count=2, summary="sudah ada", curator_status="approved")
    pipeline.write_beats_jsonl([beat_lama], jsonl_path)
    hitung: list = []
    _fake_summarize(monkeypatch, hitung)
    monkeypatch.setattr(
        "arti_observer_rag.reindex_beats_session", lambda sid, cfg2: None)
    r = catchup.run_catchup_session("s1", cfg)
    assert len(hitung) == 1, "bucket 0 TIDAK boleh diringkas ulang"
    assert r["baru"] == 1
    isi = catchup._load_existing_beats(jsonl_path)
    assert {b.segment_index for b in isi} == {0, 1}
    assert [b.summary for b in isi if b.segment_index == 0] == ["sudah ada"]


def test_tanpa_segmen_baru_mtime_dibump(monkeypatch, tmp_path):
    """Sesi yang sebenarnya sudah lengkap harus berhenti terdeteksi pending."""
    cfg = _sandbox(monkeypatch, tmp_path)
    tx = _tulis_transcript(tmp_path / "transcripts", "s1", _rows_dua_bucket()[:2])
    jsonl_path, _ = pipeline.beats_paths("s1", cfg)
    beat = pipeline.BeatDraft(
        session_id="s1", segment_index=0, t_start="00:00:00", t_end="00:10:00",
        event_count=2, summary="lengkap", curator_status="approved")
    pipeline.write_beats_jsonl([beat], jsonl_path)
    lama = time.time() - 3600
    os.utime(jsonl_path, (lama, lama))
    assert catchup.find_pending(cfg) == ["s1"]
    hitung: list = []
    _fake_summarize(monkeypatch, hitung)
    r = catchup.run_catchup_session("s1", cfg)
    assert r["baru"] == 0 and not hitung
    assert catchup.find_pending(cfg) == [], "mtime beats harus dibump"


def test_reindex_gagal_tidak_menghentikan(monkeypatch, tmp_path):
    cfg = _sandbox(monkeypatch, tmp_path)
    _tulis_transcript(tmp_path / "transcripts", "s1", _rows_dua_bucket())
    hitung: list = []
    _fake_summarize(monkeypatch, hitung)

    def meledak(sid, cfg2):
        raise RuntimeError("db terkunci")

    monkeypatch.setattr("arti_observer_rag.reindex_beats_session", meledak)
    r = catchup.run_catchup_session("s1", cfg)   # tidak boleh melempar
    assert r["baru"] == 2
    jsonl_path, _ = pipeline.beats_paths("s1", cfg)
    assert jsonl_path.is_file(), "beats tetap aman di disk walau indeks gagal"


def test_kill_switch_menghentikan_semuanya(monkeypatch, tmp_path):
    cfg = _sandbox(monkeypatch, tmp_path)
    cfg["observer_catchup_on_startup"] = False
    _tulis_transcript(tmp_path / "transcripts", "s1", _rows_dua_bucket())
    assert catchup.run_catchup(cfg) == []


# --- wiring bridge --------------------------------------------------------

def test_bridge_menjalankan_catchup_di_startup():
    i = SRC.index("def _observer_catchup_worker")
    blok = SRC[i - 300:i + 1600]
    assert "arti_observer_catchup" in blok
    assert "threading.Thread" in blok
    assert "observer_catchup_delay_sec" in blok
    assert 'CONFIG.get("observer_catchup_on_startup"' in blok, (
        "kill switch harus dicek sebelum thread dinyalakan")


def test_config_catchup_ada():
    import arti_bridge as b
    assert b.CONFIG["observer_catchup_on_startup"] is True
    assert float(b.CONFIG["observer_catchup_delay_sec"]) > 0
    assert float(b.CONFIG["observer_catchup_max_days"]) >= 1


# --- model rangkuman: composer via role "catchup" (Bohan 2026-08-10) -----


def test_role_catchup_composer_dan_terdaftar():
    """"[recorded remark omitted]"
    — role SENDIRI (bukan numpang observer): sesi di-keyed per role, numpang
    berarti sesi composer bisa terpakai kurasi shutdown (turun kelas diam)."""
    import arti_cursor_agent as ca
    assert "catchup" in ca.KNOWN_ROLES
    assert ca.resolve_role_model("catchup", {}) == ("composer-2.5", None)
    assert ca.role_timeout_sec("catchup", {}) == 45.0


def test_observer_shutdown_juga_composer():
    """Revisi Bohan 2026-08-10 ("[recorded remark omitted]"): kurasi shutdown ikut composer — hemat menang."""
    import arti_cursor_agent as ca
    assert ca.resolve_role_model("observer", {}) == ("composer-2.5", None)


def test_observer_client_menghormati_role_operan(monkeypatch):
    import arti_observer_client as oc
    import arti_scouter_client as scouter
    tangkap = {}

    def fake_provider(prompt, cfg):
        tangkap["role"] = cfg.get("cursor_role")
        return '{"summary": "ok", "noise_level": "low"}', 5

    monkeypatch.setitem(scouter._PROVIDERS, "fake", fake_provider)
    cfg = {"observer_provider_chain": ["fake"]}
    oc.summarize_segment("teks segmen", cfg)
    assert tangkap["role"] == "observer", "default harus tetap observer"
    cfg["observer_cursor_role"] = "catchup"
    oc.summarize_segment("teks segmen", cfg)
    assert tangkap["role"] == "catchup"


def test_catchup_mengoper_role_catchup(monkeypatch, tmp_path):
    import arti_observer_client as client
    cfg = _sandbox(monkeypatch, tmp_path)
    _tulis_transcript(tmp_path / "transcripts", "s1", _rows_dua_bucket())
    tangkap = {}

    def fake(block, config):
        tangkap["role"] = config.get("observer_cursor_role")
        return {"summary": "ok", "topics": [], "facts": [],
                "worth_embed": False, "worth_learning": False,
                "noise_level": "low", "provider": "fake"}

    monkeypatch.setattr(client, "summarize_segment", fake)
    monkeypatch.setattr(
        "arti_observer_rag.reindex_beats_session", lambda sid, cfg2: None)
    catchup.run_catchup_session("s1", cfg)
    assert tangkap["role"] == "catchup"


def test_config_role_catchup_ada():
    import arti_bridge as b
    assert b.CONFIG["observer_catchup_cursor_role"] == "catchup"
    assert float(b.CONFIG["observer_catchup_pause_sec"]) > 0


def test_semua_role_composer_dengan_config_ASLI():
    """Crosscheck 2026-08-10 nyaris kecolongan: default resolve_role_model
    sudah composer, tapi key CONFIG bridge (cursor_observer_model /
    cursor_lookup_model) masih menimpanya ke grok DIAM-DIAM — tes lama lolos
    karena memakai config kosong. Yang diuji di sini: CONFIG SESUNGGUHNYA."""
    import arti_cursor_agent as ca
    import arti_bridge as b
    for role in ca.KNOWN_ROLES:
        model, effort = ca.resolve_role_model(role, b.CONFIG)
        assert model == "composer-2.5", f"{role} masih {model} di CONFIG asli"
        assert effort is None, f"{role}: composer tidak punya param effort"


# --- REGRESI live 21.49 (11 Agu): catch-up membuat Arti "bego" -------------


def test_catchup_live_pakai_chain_gratis_bukan_cursor():
    """Akar "[recorded remark omitted]": catch-up 17 segmen composer berebut
    dengan giliran suara -> voice timeout 3x -> breaker Cursor tutup ->
    SELURUH sesi dijawab llama-3.1-8b (composer sukses NOL kali). Kerja latar
    tidak boleh memakai jalur yang dipakai penonton."""
    import arti_bridge as b
    rantai = b.CONFIG["observer_catchup_provider_chain"]
    assert rantai, "chain catch-up live wajib disetel eksplisit"
    assert "cursor" not in rantai, (
        "catch-up saat live TIDAK boleh menyentuh Cursor — composer dipakai "
        "penonton; pakai scripts/rangkum_susulan.py saat bridge mati")
    # Dulu di sini ada `assert rantai[0] == "nvidia"`. Itu memaku NAMA provider
    # pertama, bukan invariant-nya — dan langsung merah 14 Agu 2026 ketika
    # urutan disusun ulang dari bukti log live (nvidia 0/4, ReadTimeout 45 dtk
    # penuh tiap kali; google_gemini 4/4). Urutan memang HARUS bisa berubah
    # mengikuti provider mana yang sedang sehat.
    # Yang benar-benar dijaga: SEMUA anggota chain harus jalur gratis — dicek
    # menyeluruh, bukan cuma elemen pertama.
    GRATIS = {"nvidia", "cloudflare", "openrouter", "google_gemini", "zai", "ollama"}
    asing = [p for p in rantai if p not in GRATIS]
    assert not asing, (
        f"provider non-gratis menyusup ke chain catch-up: {asing} — "
        "kerja latar hanya boleh pakai jalur yang tidak dipakai penonton")


def test_catchup_menghormati_chain_operan(monkeypatch, tmp_path):
    import arti_observer_client as oc
    import arti_scouter_client as scouter
    cfg = _sandbox(monkeypatch, tmp_path)
    cfg["observer_catchup_provider_chain"] = ["fake"]
    _tulis_transcript(tmp_path / "transcripts", "s1", _rows_dua_bucket())
    dipakai = []

    def fake_provider(prompt, c):
        dipakai.append(c.get("observer_provider_chain"))
        return '{"summary": "ok", "noise_level": "low"}', 5

    monkeypatch.setitem(scouter._PROVIDERS, "fake", fake_provider)
    monkeypatch.setattr(
        "arti_observer_rag.reindex_beats_session", lambda sid, c: None)
    catchup.run_catchup_session("s1", cfg)
    assert dipakai and dipakai[0] == ["fake"], dipakai


def test_catchup_dicicil_dan_melaporkan_yang_ditunda(monkeypatch, tmp_path,
                                                    capsys):
    """Backlog besar dicicil per startup — dan yang ditunda WAJIB dilaporkan
    (diam soal separuh pekerjaan = bohong yang mahal)."""
    cfg = _sandbox(monkeypatch, tmp_path)
    cfg["observer_catchup_max_segmen"] = 1
    _tulis_transcript(tmp_path / "transcripts", "s1", _rows_dua_bucket())
    hitung: list = []
    _fake_summarize(monkeypatch, hitung)
    monkeypatch.setattr(
        "arti_observer_rag.reindex_beats_session", lambda sid, c: None)
    r = catchup.run_catchup_session("s1", cfg)
    assert len(hitung) == 1, "batas segmen tidak dihormati"
    assert r["ditunda"] == 1
    assert "DITUNDA" in capsys.readouterr().out


def test_worker_catchup_mengalah_ke_giliran_suara():
    """Wiring: worker menunggu selama _brain_busy/tts_is_playing."""
    i = SRC.index("def _pelan(stage: str")
    blok = SRC[i:i + 900]
    assert "_brain_busy or tts_is_playing" in blok
    assert "time.sleep(1.0)" in blok


def test_breaker_cursor_tidak_menutup_seperempat_jam():
    import arti_bridge as b
    assert float(b.CONFIG["cursor_breaker_cooldown_sec"]) <= 300, (
        "900 dtk = 15 menit siaran dijawab llama-8b")


def test_skrip_rangkum_susulan_ada_dan_pakai_composer():
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "scripts" /
           "rangkum_susulan.py").read_text(encoding="utf-8")
    assert "observer_provider_chain" in src and '"cursor"' in src
    assert "observer_catchup_max_segmen" in src, "standalone = tanpa batas"


def test_kerja_latar_tidak_pakai_cursor():
    """ATURAN yang dibayar dua kali malam 11 Agu: kerja latar (catch-up,
    scouter, vision) tidak boleh memakai jalur composer yang dipakai
    penonton. Scouter tiap 90 dtk x 15-27 dtk composer = voice (12 dtk)
    selalu timeout -> breaker -> llama-8b sepanjang siaran."""
    import arti_bridge as b
    scout = list(b.CONFIG.get("scouter_provider_chain") or [])
    assert scout and scout[0] != "cursor", (
        "scouter cursor-first merebut composer dari giliran suara")
    vis = list(b.CONFIG.get("vision_provider_chain") or [])
    assert vis and vis[0] != "cursor"
    cat = list(b.CONFIG.get("observer_catchup_provider_chain") or [])
    assert "cursor" not in cat

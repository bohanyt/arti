"""arti_provider_health: probe awal, dead-skip, breaker bersama, Codex dilindungi.

Permintaan Bohan 2026-10-23: "[recorded remark omitted]". Semua jaringan dipalsukan.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import requests

import arti_agy_agent as A
import arti_provider_health as ph
import arti_scouter_client as sc


class _Jam:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _bersih(monkeypatch):
    ph.reset()
    log: list[str] = []
    jam = _Jam()
    monkeypatch.setattr(ph, "_log", log.append)
    monkeypatch.setattr(ph, "_clock", jam)
    # Re-probe latar dijalankan SINKRON di tes supaya deterministik.
    monkeypatch.setattr(ph, "_spawn", lambda fn, _name: fn())
    import arti_executors

    monkeypatch.setattr(arti_executors, "_last_user_cancel", float("-inf"))
    yield log, jam
    ph.reset()


def _buka(name, cls="unavailable"):
    """Buka breaker lewat jalur live (3 gagal beruntun)."""
    for _ in range(3):
        ph.record_failure(name, cls)
    assert ph.state_of(name) == "open"


def _aktif(**kw):
    cfg = {"provider_probe_enabled": True, "provider_breaker_failures": 3,
           "provider_breaker_open_sec": 600}
    cfg.update(kw)
    assert ph.configure(cfg) is True


# --------------------------------------------------------------------------- #
# Klasifikasi
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("err, kelas", [
    (RuntimeError('agy menolak: invalid model selection (--model "gemini-3.6-flash-low" '
                  '--effort "low"): --effort is not supported for model "gemini-3.6-flash-low"'),
     "model_rejected"),
    ("HTTP 404: model_not_found", "model_rejected"),
    ("HTTP 400: model_decommissioned", "model_rejected"),
    ("HTTP 401: Unauthorized", "auth"),
    ("HTTP 403: forbidden", "auth"),
    (RuntimeError("API key not valid. Please pass a valid API key."), "auth"),
    (RuntimeError("GROQ_API_KEY kosong"), "auth"),
    (RuntimeError('HTTP 429: {"error":{"message":"Provider returned error","code":429}}'),
     "rate_limit"),
    (RuntimeError("RESOURCE_EXHAUSTED: quota exceeded"), "rate_limit"),
    (RuntimeError('HTTP 503: {\n  "error": {\n    "code": 503, "status": "UNAVAILABLE"}}'),
     "unavailable"),
    ("HTTP 502 bad gateway", "unavailable"),
    (requests.exceptions.ReadTimeout("read timed out"), "timeout"),
    (TimeoutError("init timeout"), "timeout"),
    (requests.exceptions.ConnectionError("Max retries exceeded"), "network"),
    (ConnectionRefusedError("refused"), "network"),
    (RuntimeError("process exit None"), "init_error"),
    (RuntimeError("cursor-sdk tidak terpasang"), "init_error"),
    (RuntimeError("jawaban kosong"), "unknown"),
    (ValueError("respons bukan objek JSON"), "unknown"),
])
def test_klasifikasi_tiap_kelas(err, kelas):
    assert ph.classify(err) == kelas


# String ASLI dari SDK / log smoke 2026-10-23_013147_bridge.log (grep -a).
_GEMINI_503_LOG = (
    'RuntimeError: HTTP 503: {\n  "error": {\n    "code": 503,\n    "message": "This model '
    'is currently experiencing high demand. Spikes in demand are usually temporary. '
    'Please try again later.",\n    "status": "UNAVAILABLE"\n  }\n}\n')
_OPENROUTER_429_LOG = (
    'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":'
    '"google/gemma-4-26b-a4b-it:free is temporarily rate-limited upstream. Please retry '
    'shortly"}}}')
_OPENROUTER_NO_ENDPOINTS = (
    'HTTP 404: {"error":{"message":"No endpoints found for '
    'nvidia/nemotron-3-super-120b-a12b:free.","code":404}}')
_AGY_REJECT_LOG = (
    'RuntimeError: agy menolak: invalid model selection (--model "gemini-3.6-flash-low" '
    '--effort "low"): --effort is not supported for model "gemini-3.6-flash-low"')
_AGY_RACE_LOG = "RuntimeError: process exit None"
_GROQ_RETIRED = ('HTTP 400: {"error":{"message":"The model `qwen/qwen3.6-27b` has been '
                 'decommissioned and is no longer supported.","type":"invalid_request_error",'
                 '"code":"model_decommissioned"}}')
_GROQ_429 = ('HTTP 429: {"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` '
             'in organization `org_x` service tier `on_demand` on tokens per minute (TPM)",'
             '"type":"tokens","code":"rate_limit_exceeded"}}')
_CODEX_WARM_FAIL = ("FileNotFoundError: [WinError 2] The system cannot find the file "
                    "specified: 'codex.exe'")


@pytest.mark.parametrize("teks, kelas", [
    (_GEMINI_503_LOG, "unavailable"),
    (_OPENROUTER_429_LOG, "rate_limit"),
    (_OPENROUTER_NO_ENDPOINTS, "unavailable"),  # sementara, BUKAN model_rejected
    (_AGY_REJECT_LOG, "model_rejected"),
    (_AGY_RACE_LOG, "init_error"),
    (_GROQ_RETIRED, "model_rejected"),
    (_GROQ_429, "rate_limit"),
    (_CODEX_WARM_FAIL, "init_error"),
])
def test_klasifikasi_string_sdk_asli(teks, kelas):
    assert ph.classify(teks) == kelas


def test_klasifikasi_kode_status():
    assert ph.classify_status(429) == "rate_limit"
    assert ph.classify_status(503) == "unavailable"
    assert ph.classify_status(401) == "auth"
    assert ph.classify_status(None) == "unknown"
    assert ph.classify(None, default="init_error") == "init_error"


def test_probe_mengklasifikasi_exception_dan_unknown_jadi_warn():
    def _503():
        raise RuntimeError("HTTP 503: UNAVAILABLE")

    def _aneh():
        raise ValueError("respons bukan objek JSON")

    # Galat SEMENTARA di probe = WARN (tidak menggerbang), hanya keras yang FAIL.
    assert ph._safe_probe(_503) == ph.ProbeResult("WARN", "unavailable",
                                                  "RuntimeError: HTTP 503: UNAVAILABLE")
    assert ph._safe_probe(lambda: ph.ProbeResult("FAIL", "rate_limit")).status == "WARN"
    assert ph._safe_probe(lambda: ph.ProbeResult("FAIL", "auth")).status == "FAIL"
    r = ph._safe_probe(_aneh)
    assert r.status == "WARN" and r.err_class == "unknown"
    assert ph._safe_probe(lambda: "ok").status == "OK"


# --------------------------------------------------------------------------- #
# Budget probe
# --------------------------------------------------------------------------- #


def test_budget_dihormati_probe_lambat_jadi_warn():
    _aktif()

    def lambat():
        time.sleep(3.0)
        return "telat"

    def gagal_keras():
        raise RuntimeError("HTTP 401: unauthorized")

    t0 = time.monotonic()
    res = ph.run_startup_probes(
        {"lambat": lambat, "cepat": lambda: "ok", "mati": gagal_keras}, budget=0.4)
    lama = time.monotonic() - t0
    assert lama < 1.5, lama
    assert res["lambat"].status == "WARN" and res["lambat"].err_class == "unknown"
    assert res["cepat"].status == "OK"
    assert res["mati"].status == "FAIL" and res["mati"].err_class == "auth"
    # WARN tidak menggerbang, FAIL keras = mati.
    assert ph.is_available("lambat") is True
    assert ph.is_available("mati") is False


def test_probe_paralel_bukan_berurutan():
    _aktif()
    probes = {f"p{i}": (lambda: time.sleep(0.3) or "ok") for i in range(6)}
    t0 = time.monotonic()
    res = ph.run_startup_probes(probes, budget=5.0)
    assert time.monotonic() - t0 < 1.2
    assert all(r.status == "OK" for r in res.values())


def test_transient_di_probe_hanya_warn_tidak_membuka_breaker(_bersih):
    log, jam = _bersih
    _aktif()

    def _429():
        raise RuntimeError(_OPENROUTER_429_LOG)

    res = ph.run_startup_probes({"openrouter": _429}, budget=2.0)
    assert res["openrouter"].status == "WARN" and res["openrouter"].err_class == "rate_limit"
    assert ph.state_of("openrouter") == "ok"
    assert ph.is_available("openrouter") is True
    assert not any("OPEN" in b for b in log)


def test_semua_probe_gagal_jaringan_tidak_ada_yang_dilewati(monkeypatch, _bersih):
    """Jaringan belum siap saat start: semua WARN, tidak ada yang dilewati 600 dtk."""
    log, jam = _bersih
    _aktif()
    urutan = ["google_gemini", "openrouter", "cloudflare", "nvidia"]

    def _net():
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='x', port=443): Max retries exceeded "
            "(Caused by NameResolutionError: getaddrinfo failed)")

    res = ph.run_startup_probes({n: _net for n in urutan + ["groq", "openrouter:voice"]},
                                budget=2.0)
    assert {r.status for r in res.values()} == {"WARN"}
    assert {r.err_class for r in res.values()} == {"network"}
    assert all(ph.is_available(n) for n in urutan + ["groq", "openrouter:voice"])
    dipanggil = _rantai_palsu(monkeypatch, urutan)
    r = sc.run_chain("streamer ngobrol", {})
    assert r.provider == "google_gemini" and dipanggil == ["google_gemini"]
    assert not any("OPEN" in b or "DEAD" in b for b in log)


# --------------------------------------------------------------------------- #
# Agy mati di awal: 80 giliran -> tepat 1 percobaan
# --------------------------------------------------------------------------- #


class _ProsesDitolak:
    """Proses agy palsu: kirim satu event result ERROR lalu keluar (bentuk asli)."""

    def __init__(self):
        import json
        import queue

        self._q = queue.Queue()
        self._q.put(json.dumps({"event": "result", "result": {
            "status": "ERROR", "response": "",
            "error": 'invalid model selection (--model "gemini-3.6-flash-low" '
                     '--effort "low"): --effort is not supported for model '
                     '"gemini-3.6-flash-low"'}}) + "\n")
        self.returncode = None

        class _Out:
            def __init__(s, q):
                s.q = q

            def readline(s):
                try:
                    v = s.q.get(timeout=5.0)
                except queue.Empty:
                    return ""
                return "" if v is None else v

        self.stdout = _Out(self._q)
        self.stderr = _Out(queue.Queue())
        self.stderr.q.put(None)
        self.stdin = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self._q.put(None)

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def test_agy_mati_di_probe_80_giliran_tepat_satu_percobaan(monkeypatch, capsys, _bersih):
    log, _jam = _bersih
    A.shutdown_session()
    mulai = []

    def factory(*_a, **_k):
        mulai.append(1)
        return _ProsesDitolak()

    monkeypatch.setattr(A, "_process_factory", factory)
    cfg = {"agy_agent_enabled": True, "agy_bin": "C:/fake/agy.exe",
           "agy_model": "gemini-3.6-flash-low", "agy_effort": "low",
           "agy_init_timeout_sec": 2.0, "agy_timeout_sec": 0.2,
           "provider_probe_enabled": True, "provider_probe_budget_sec": 3}
    ph.configure(cfg)
    probes = {k: v for k, v in ph.build_probes(cfg).items() if k == "agy"}
    res = ph.run_startup_probes(probes, budget=3.0)
    assert res["agy"].status == "FAIL" and res["agy"].err_class == "model_rejected"
    assert ph.state_of("agy") == "dead"

    for _ in range(80):
        A.prewarm(cfg)
        r = A.send_turn("sys", "isi", cfg)
        assert r.ok is False
    time.sleep(0.1)
    A.shutdown_session()

    assert len(mulai) == 1, "agy dicoba ulang setelah mati"
    keluaran = capsys.readouterr().out
    assert keluaran.count("[Agy] init gagal") == 1
    assert sum("agy DEAD" in b for b in log) == 1


def test_bridge_tidak_memanggil_agy_yang_mati(monkeypatch):
    import arti_bridge as bridge
    import arti_cursor_agent

    _aktif()
    ph.apply_probe("agy", ph.ProbeResult("FAIL", "model_rejected", "ditolak"))
    dipanggil = []
    monkeypatch.setattr(arti_cursor_agent, "prewarm", lambda _cfg: False)
    monkeypatch.setattr(bridge.arti_codex_agent, "prewarm", lambda _cfg: True)
    monkeypatch.setattr(bridge, "pick_groq_model", lambda *_a, **_k: "groq-test")
    monkeypatch.setattr(bridge, "groq_chat_completion", lambda *_a, **_k: ("Groq", "g"))
    monkeypatch.setattr(A, "prewarm", lambda *_a, **_k: dipanggil.append("prewarm") or False)
    monkeypatch.setattr(A, "send_turn", lambda *_a, **_k: dipanggil.append("turn"))
    monkeypatch.setattr(bridge.arti_codex_agent, "send_turn", lambda *_a, **_k: "jawaban Luna")
    cfg = {**bridge.CONFIG, "agy_agent_enabled": True, "agy_primary_voice": True,
           "codex_agent_enabled": True, "codex_primary_voice": True,
           "cursor_agent_enabled": False, "_agy_primary_tes": True,
           "_codex_primary_tes": True}
    for _ in range(5):
        reply, _c, src = asyncio.run(
            bridge._cursor_reply_with_fallback("sys", "p", "u", cfg, "mic"))
        assert (reply, src) == ("jawaban Luna", "codex")
    assert "turn" not in dipanggil


# --------------------------------------------------------------------------- #
# Breaker runtime
# --------------------------------------------------------------------------- #


def test_breaker_buka_setelah_n_lalu_tutup_lewat_probe_latar(_bersih):
    log, jam = _bersih
    _aktif()
    probe_dipanggil = []
    ph.register_probe("google_gemini", lambda: probe_dipanggil.append(1) or "ok")

    ph.record_failure("google_gemini", "unavailable")
    ph.record_failure("google_gemini", "unavailable")
    assert ph.is_available("google_gemini") is True
    ph.record_failure("google_gemini", "unavailable")
    assert ph.is_available("google_gemini") is False
    assert [b for b in log if "google_gemini OPEN" in b] == [
        "[Health] google_gemini OPEN (unavailable) — dilewati 10m, lalu probe ulang di latar"]

    jam.t += 599
    assert ph.is_available("google_gemini") is False
    assert probe_dipanggil == []
    jam.t += 2
    # Jendela habis: probe ulang SEKALI di latar; giliran ini tetap dilewati.
    assert ph.is_available("google_gemini") is False
    assert probe_dipanggil == [1]
    assert ph.is_available("google_gemini") is True
    assert sum("CLOSED" in b for b in log) == 1


def test_breaker_buka_lagi_kalau_probe_ulang_gagal_sementara(_bersih):
    log, jam = _bersih
    _aktif()

    def _masih_503():
        raise RuntimeError("HTTP 503")

    ph.register_probe("nvidia", _masih_503)
    for _ in range(3):
        ph.record_failure("nvidia", "unavailable")
    jam.t += 601
    assert ph.is_available("nvidia") is False
    assert ph.state_of("nvidia") == "open"
    jam.t += 300
    assert ph.is_available("nvidia") is False  # jendela baru dimulai saat re-probe


def test_galat_keras_tetap_tertutup_sesi_ini(_bersih):
    log, jam = _bersih
    _aktif()

    def _401():
        raise RuntimeError("HTTP 401 unauthorized")

    ph.register_probe("zai", _401)
    for _ in range(3):
        ph.record_failure("zai", "rate_limit")
    jam.t += 601
    assert ph.is_available("zai") is False
    assert ph.state_of("zai") == "dead"
    jam.t += 100000
    assert ph.is_available("zai") is False
    # galat keras saat LIVE butuh N kali beruntun (vonis instan hanya milik probe)
    ph.record_failure("ollama", "auth")
    ph.record_failure("ollama", "auth")
    assert ph.state_of("ollama") == "dead" and not ph.is_available("ollama")
    ph.record_failure("ollama", "auth")
    assert ph.state_of("ollama") == "dead"
    ph.record_failure("ollama", "auth")
    assert sum("ollama DEAD" in b for b in log) == 1


def test_galat_keras_live_diselingi_sementara_tidak_mati():
    _aktif()
    for cls in ("model_rejected", "unavailable", "model_rejected"):
        ph.record_failure("openrouter", cls)
    assert ph.state_of("openrouter") == "open"  # breaker biasa, bukan vonis mati


def test_vonis_mati_menang_atas_probe_ulang_telat(_bersih):
    log, jam = _bersih
    _aktif()

    def probe_telat():
        # Selagi re-probe berjalan, jalur lain memvonis mati (3 auth beruntun)...
        for _ in range(3):
            ph.record_failure("zai", "auth")
        return "ok"  # ...lalu probe ulang pulang OK — terlambat.

    ph.register_probe("zai", probe_telat)
    _buka("zai")
    jam.t += 601
    assert ph.is_available("zai") is False
    assert ph.state_of("zai") == "dead"
    ph.record_success("zai")
    ph.apply_probe("zai", ph.ProbeResult("OK"))
    assert ph.state_of("zai") == "dead" and ph.is_available("zai") is False
    assert not any("zai CLOSED" in b for b in log)


def test_dibatalkan_bukan_gagal_bukan_sukses():
    _aktif()
    assert ph.classify(asyncio.CancelledError()) == "cancelled"
    assert ph.classify("cancelled") == "cancelled"
    ph.record_failure("google_gemini", "unavailable")
    ph.record_failure("google_gemini", "unavailable")
    for _ in range(10):
        ph.record_failure("google_gemini", ph.classify(asyncio.CancelledError()))
        ph.record_failure("google_gemini", "cancelled")
    assert ph.state_of("google_gemini") == "ok"
    # bukan sukses juga: hitungan 2 tidak di-reset, gagal ke-3 membuka breaker
    ph.record_failure("google_gemini", "unavailable")
    assert ph.state_of("google_gemini") == "open"


def test_baris_telemetri_outcome_cancelled_dibuang_dari_dua_hitungan():
    _aktif()
    # Bentuk persis dari bridge-passive-voice 57ebb939: ok=True + outcome cancelled.
    ph.record_failure("groq", "unavailable")
    ph.record_failure("groq", "unavailable")
    for _ in range(5):
        ph.record_outcome("groq", ok=True, extra={"outcome": "cancelled"})
        ph.record_outcome("groq", ok=False, err=asyncio.CancelledError())
    assert ph._states["groq"].failures == 2  # tidak di-reset (bukan sukses), tidak naik
    ph.record_outcome("groq", ok=False, err=RuntimeError("HTTP 503"))
    assert ph.state_of("groq") == "open"
    for _ in range(3):
        ph.record_outcome("nvidia", ok=False, extra={"outcome": "error"}, err="HTTP 401")
    assert ph.state_of("nvidia") == "dead"


def test_groq_dibatalkan_di_tengah_jalan_tidak_dicatat(monkeypatch):
    """Giliran mic_pasif dibatalkan PTT: thread Groq tetap selesai di latar,
    hasilnya (sukses ATAU gagal) tidak boleh masuk hitungan."""
    import threading

    import arti_bridge as bridge
    import arti_executors

    _aktif()
    lepas = threading.Event()
    selesai = threading.Event()

    def inti(model, sys_p, user, config=None):
        lepas.wait(5)
        try:
            return bridge._openrouter_after_groq(sys_p, user, {}, 503)
        finally:
            selesai.set()

    monkeypatch.setattr(bridge, "_groq_chat_completion_inti", inti)
    monkeypatch.setattr(bridge.arti_openrouter, "openrouter_live_completion",
                        lambda *_a, **_k: (None, None))

    async def giliran():
        t = asyncio.ensure_future(arti_executors.run(
            "llm", bridge.groq_chat_completion, "m", "s", "u", {}))
        await asyncio.sleep(0.05)
        arti_executors.note_user_cancel()  # handler toggle PTT menyala
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    for _ in range(4):
        lepas.clear()
        selesai.clear()
        asyncio.run(giliran())
        lepas.set()
        assert selesai.wait(5)
        time.sleep(0.05)
    assert "groq" not in ph._states and "openrouter:voice" not in ph._states
    # Kontrol: tanpa pembatalan, kegagalan yang sama TERCATAT.
    lepas.set()
    asyncio.run(arti_executors.run("llm", bridge.groq_chat_completion, "m", "s", "u", {}))
    assert ph._states["groq"].failures == 1


def test_wait_for_habis_dihitung_timeout_barge_in_tidak(monkeypatch):
    """asyncio.wait_for yang habis = provider lambat -> gagal 'timeout' (breaker
    bisa terbuka). Pembatalan PTT/barge-in = bukan hasil."""
    import threading

    import arti_executors

    _aktif()
    selesai = threading.Event()

    def lambat(nama, jawab_ok):
        time.sleep(0.3)
        if jawab_ok:
            ph.record_success(nama)
        else:
            ph.record_failure(nama, "unknown")
        selesai.set()

    async def dengan_deadline(nama, jawab_ok):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                arti_executors.run("llm", lambat, nama, jawab_ok), timeout=0.05)

    for jawab_ok in (True, False, True):  # sukses telat pun = timeout
        selesai.clear()
        asyncio.run(dengan_deadline("vision:google_gemini_lite", jawab_ok))
        assert selesai.wait(5)
    st = ph._states["vision:google_gemini_lite"]
    assert ph.state_of("vision:google_gemini_lite") == "open"
    assert st.err_class in ("timeout", "unknown")

    async def barge_in(nama):
        t = asyncio.ensure_future(arti_executors.run("llm", lambat, nama, False))
        await asyncio.sleep(0.05)
        arti_executors.note_user_cancel()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    for _ in range(4):
        selesai.clear()
        asyncio.run(barge_in("groq"))
        assert selesai.wait(5)
    assert "groq" not in ph._states

    async def barge_in_pesan(nama):
        monkeypatch.setattr(arti_executors, "_last_user_cancel", float("-inf"))
        t = asyncio.ensure_future(arti_executors.run("llm", lambat, nama, False))
        await asyncio.sleep(0.05)
        t.cancel(arti_executors.BARGE_IN)
        with pytest.raises(asyncio.CancelledError):
            await t

    selesai.clear()
    asyncio.run(barge_in_pesan("openrouter:voice"))
    assert selesai.wait(5)
    assert "openrouter:voice" not in ph._states


def test_groq_404_model_terakhir_bukan_mati_hanya_breaker(monkeypatch, _bersih):
    """Rantai Groq 429,429,404: status terakhir 404 tidak boleh membunuh Groq."""
    import arti_bridge as bridge

    log, _jam = _bersih
    _aktif()
    tembakan: list[str] = []

    def inti(model, sys_p, user, config=None):
        tembakan.append(model)
        return bridge._openrouter_after_groq(sys_p, user, config or {}, 404)

    monkeypatch.setattr(bridge, "_groq_chat_completion_inti", inti)
    monkeypatch.setattr(bridge.arti_openrouter, "openrouter_live_completion",
                        lambda *_a, **_k: ("OR", "or-m"))
    cfg = {"openrouter_live_fallback_enabled": True}
    for _ in range(2):
        bridge.groq_chat_completion("m", "s", "u", cfg)
    assert ph.state_of("groq") == "ok"
    bridge.groq_chat_completion("m", "s", "u", cfg)
    assert ph.state_of("groq") == "open"  # breaker biasa (10 menit), BUKAN dead
    assert ph._states["groq"].err_class == "unknown"
    for _ in range(10):
        bridge.groq_chat_completion("m", "s", "u", cfg)
    assert ph.state_of("groq") == "open"
    assert not any("groq DEAD" in b for b in log)


def test_agy_dibatalkan_di_bridge_tidak_dicatat(monkeypatch):
    import arti_bridge as bridge
    import arti_cursor_agent

    _aktif()
    monkeypatch.setattr(arti_cursor_agent, "prewarm", lambda _cfg: False)
    monkeypatch.setattr(bridge.arti_codex_agent, "prewarm", lambda _cfg: True)
    monkeypatch.setattr(A, "prewarm", lambda *_a, **_k: True)
    monkeypatch.setattr(bridge.arti_codex_agent, "send_turn", lambda *_a, **_k: "Luna")
    monkeypatch.setattr(A, "send_turn",
                        lambda *_a, **_k: A.AgyResult(ok=False, reason="cancelled"))
    cfg = {**bridge.CONFIG, "agy_agent_enabled": True, "agy_primary_voice": True,
           "codex_agent_enabled": True, "cursor_agent_enabled": False,
           "_agy_primary_tes": True, "_codex_primary_tes": True}
    for _ in range(5):
        asyncio.run(bridge._cursor_reply_with_fallback("s", "p", "u", cfg, "mic"))
    assert ph.state_of("agy") == "ok" and "agy" not in ph._states

    def batal(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(A, "send_turn", batal)
    monkeypatch.setattr(A, "abort_turn", lambda *_a, **_k: None)
    for _ in range(5):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bridge._cursor_reply_with_fallback("s", "p", "u", cfg, "mic"))
    assert "agy" not in ph._states


def test_codex_thread_dibuang_saat_run_tidak_dicatat(monkeypatch):
    import arti_codex_agent as cx

    _aktif()
    jalan = []

    class _Th:
        def run(self, *_a, **_k):
            jalan.append(1)
            cx._tutup("barge-in")  # orang lain membuang thread di tengah run
            raise RuntimeError("HTTP 503 client closed")

    class _Cl:
        def close(self):
            pass

    monkeypatch.setattr(cx, "_privacy_epoch", cx.screen_privacy.epoch)
    monkeypatch.setattr(cx, "_thread", _Th())
    monkeypatch.setattr(cx, "_client", _Cl())
    for _ in range(5):
        monkeypatch.setattr(cx, "_thread", _Th())
        monkeypatch.setattr(cx, "_client", _Cl())
        assert cx.send_turn("s", "u", {"codex_agent_enabled": True,
                                       "codex_timeout_sec": 2}) is None
    assert len(jalan) == 5
    assert "codex" not in ph._states
    # Kontrol: kegagalan SUNGGUHAN (thread tidak dibuang orang lain) tercatat.

    class _ThGagal:
        def run(self, *_a, **_k):
            raise RuntimeError("HTTP 503 unavailable")

    monkeypatch.setattr(cx, "_thread", _ThGagal())
    monkeypatch.setattr(cx, "_client", _Cl())
    cx.send_turn("s", "u", {"codex_agent_enabled": True, "codex_timeout_sec": 2})
    assert ph._states["codex"].failures == 1


def test_init_error_live_dihitung_breaker_dulu():
    _aktif()
    ph.record_failure("agy", "init_error")
    assert ph.is_available("agy") is True
    ph.record_failure("agy", "init_error")
    ph.record_failure("agy", "init_error")
    assert ph.state_of("agy") == "dead"


def test_sukses_mereset_hitungan():
    _aktif()
    for _ in range(2):
        ph.record_failure("cloudflare", "timeout")
    ph.record_success("cloudflare")
    for _ in range(2):
        ph.record_failure("cloudflare", "timeout")
    assert ph.is_available("cloudflare") is True


# --------------------------------------------------------------------------- #
# Urutan rantai + Codex dilindungi
# --------------------------------------------------------------------------- #


def _rantai_palsu(monkeypatch, urutan, gagal=()):
    dipanggil = []

    def buat(nama):
        def fn(_prompt, _cfg):
            dipanggil.append(nama)
            if nama in gagal:
                raise RuntimeError("HTTP 503")
            return '{"summary": "ok", "emotion": "neutral"}', 5
        return fn

    monkeypatch.setattr(sc, "_PROVIDERS", {n: buat(n) for n in urutan})
    monkeypatch.setattr(sc, "_resolve_chain", lambda _c: list(urutan))
    return dipanggil


def test_urutan_fallback_tetap_hanya_yang_mati_dilewati(monkeypatch):
    _aktif()
    urutan = ["google_gemini", "openrouter", "cloudflare", "ollama", "zai", "nvidia"]
    dipanggil = _rantai_palsu(monkeypatch, urutan,
                              gagal={"google_gemini", "cloudflare", "ollama"})
    _buka("openrouter", "rate_limit")
    ph.apply_probe("zai", ph.ProbeResult("FAIL", "auth"))
    r = sc.run_chain("streamer ngobrol", {})
    assert r is not None and r.provider == "nvidia"
    assert dipanggil == ["google_gemini", "cloudflare", "ollama", "nvidia"]


def test_codex_tidak_pernah_dilewati_walau_breaker_atau_probe_fail(_bersih):
    log, jam = _bersih
    _aktif()
    ph.apply_probe("codex", ph.ProbeResult("FAIL", "auth", "not logged in"))
    assert ph.is_available("codex") is True
    for _ in range(10):
        ph.record_failure("codex", "unavailable")
    assert ph.is_available("codex") is True
    ph2 = ph.chain_key("codex", {}, lane="vision")
    assert ph2 == "codex" and ph.is_protected("codex")
    keras = [b for b in log if "!!! Codex (last resort) DOWN" in b]
    assert len(keras) == 1 and "auth" in keras[0]


def test_codex_breaker_terbuka_log_keras_sekali_lalu_tetap_dicoba(_bersih):
    log, jam = _bersih
    _aktif()
    for _ in range(9):
        ph.record_failure("codex", "timeout")
    assert ph.is_available("codex") is True
    assert sum("!!! Codex (last resort) DOWN: timeout" in b for b in log) == 1
    ph.record_success("codex")
    assert sum("Codex (last resort) CLOSED" in b for b in log) == 1


def test_rantai_semua_mati_berakhir_di_codex(monkeypatch):
    _aktif()
    urutan = ["google_gemini", "openrouter", "cloudflare", "codex"]
    dipanggil = _rantai_palsu(monkeypatch, urutan)
    for n in urutan[:-1]:
        ph.apply_probe(n, ph.ProbeResult("FAIL", "model_rejected"))
    ph.apply_probe("codex", ph.ProbeResult("FAIL", "unavailable"))  # down tapi dilindungi
    r = sc.run_chain("streamer ngobrol", {})
    assert r is not None and r.provider == "codex"
    assert dipanggil == ["codex"]  # tepat sekali per giliran


def test_codex_pertama_di_rantai_tetap_dilindungi_berdasar_nama(monkeypatch):
    _aktif()
    urutan = ["codex", "google_gemini", "nvidia"]
    dipanggil = _rantai_palsu(monkeypatch, urutan, gagal={"codex"})
    for _ in range(5):
        ph.record_failure("codex", "unavailable")
    r = sc.run_chain("streamer ngobrol", {})
    assert r.provider == "google_gemini"
    assert dipanggil == ["codex", "google_gemini"]


def test_observer_melewati_yang_mati_urutan_tetap(monkeypatch):
    import arti_observer_client as oc

    _aktif()
    dipanggil = _rantai_palsu(monkeypatch, ["cursor", "google_gemini", "nvidia"])
    ph.apply_probe("cursor:observer", ph.ProbeResult("FAIL", "init_error"))
    out = oc.summarize_segment("isi segmen", {"observer_provider_chain":
                                              ["cursor", "google_gemini", "nvidia"]})
    assert out["provider"] == "google_gemini"
    assert dipanggil == ["google_gemini"]


def test_web_lookup_melewati_yang_mati(monkeypatch):
    import arti_web_lookup as wl

    _aktif()
    dipanggil = []
    monkeypatch.setattr(wl, "_PROVIDERS", {
        "groq_compound": lambda q, c: dipanggil.append("groq_compound") or "x",
        "cursor": lambda q, c: dipanggil.append("cursor") or "hasil cursor",
    })
    ph.apply_probe("web:groq_compound", ph.ProbeResult("FAIL", "auth"))
    text, src = wl.lookup("cuaca", {"web_lookup_provider_chain": ["groq_compound", "cursor"]})
    assert (text, src) == ("hasil cursor", "cursor")
    assert dipanggil == ["cursor"]


# --------------------------------------------------------------------------- #
# Groq suara (pembungkus) + kill switch
# --------------------------------------------------------------------------- #


def _groq_gagal_503(monkeypatch, bridge, tembakan):
    def inti(model, sys_p, user, config=None):
        tembakan.append(model)
        return bridge._openrouter_after_groq(sys_p, user, config or {}, 503)

    monkeypatch.setattr(bridge, "_groq_chat_completion_inti", inti)
    monkeypatch.setattr(bridge.arti_openrouter, "openrouter_live_completion",
                        lambda *_a, **_k: ("jawaban OR", "or-model"))


def test_groq_breaker_lewati_groq_langsung_openrouter(monkeypatch):
    import arti_bridge as bridge

    _aktif()
    tembakan: list[str] = []
    _groq_gagal_503(monkeypatch, bridge, tembakan)
    cfg = {"openrouter_live_fallback_enabled": True}
    for _ in range(3):
        assert bridge.groq_chat_completion("m", "s", "u", cfg) == ("jawaban OR", "or-model")
    assert ph.state_of("groq") == "open"
    for _ in range(5):
        assert bridge.groq_chat_completion("m", "s", "u", cfg) == ("jawaban OR", "or-model")
    assert len(tembakan) == 3


def test_kill_switch_perilaku_lama(monkeypatch, _bersih):
    import arti_bridge as bridge

    log, _jam = _bersih
    assert ph.configure({"provider_probe_enabled": False}) is False
    for _ in range(10):
        ph.record_failure("google_gemini", "auth")
    assert ph.is_available("google_gemini") is True
    assert ph.state_of("google_gemini") == "ok"
    tembakan: list[str] = []
    _groq_gagal_503(monkeypatch, bridge, tembakan)
    for _ in range(6):
        bridge.groq_chat_completion("m", "s", "u", {})
    assert len(tembakan) == 6
    dipanggil = []
    assert ph.start_startup_probe({"provider_probe_enabled": False},
                                  probes={"x": lambda: dipanggil.append(1)}) is None
    assert dipanggil == [] and log == []


def test_modul_diam_tanpa_configure():
    ph.record_failure("x", "auth")
    assert ph.is_available("x") is True and ph.enabled() is False


# --------------------------------------------------------------------------- #
# Visibilitas
# --------------------------------------------------------------------------- #


def test_ringkasan_satu_baris(_bersih):
    log, jam = _bersih
    _aktif()
    ph.register_probe("codex", lambda: "ok")
    ph.register_probe("groq", lambda: "ok")
    _buka("google_gemini")
    jam.t += 180
    ph.apply_probe("agy", ph.ProbeResult("FAIL", "model_rejected"))
    assert ph.summary_line() == (
        "providers: codex OK, groq OK, google_gemini OPEN(unavailable) 7m, "
        "agy DEAD(model_rejected)")


def test_start_startup_probe_mencetak_blok_dan_ringkasan(_bersih):
    log, _jam = _bersih
    out: list[str] = []
    ph.start_startup_probe(
        {"provider_probe_enabled": True, "provider_probe_budget_sec": 2},
        log=out.append, background=False,
        probes={"groq": lambda: "ok",
                "agy": lambda: (_ for _ in ()).throw(RuntimeError("invalid model selection"))})
    teks = "\n".join(out)
    assert "[OK  ] groq" in teks
    assert "[FAIL] agy" in teks and "model_rejected" in teks and "DEAD" in teks
    assert out[-1] == "[Health] providers: groq OK, agy DEAD(model_rejected)"


def test_health_monitor_menulis_baris_provider(_bersih):
    import arti_health

    _aktif()
    ph.apply_probe("agy", ph.ProbeResult("FAIL", "init_error"))
    baris: list[str] = []
    rekam: list[dict] = []
    arti_health.emit_health({}, log=baris.append, record=lambda s, _c: rekam.append(s))
    assert baris[-1] == "[Health] providers: agy DEAD(init_error)"
    assert rekam[0]["providers"] == "providers: agy DEAD(init_error)"


def test_build_probes_hanya_yang_dinyalakan():
    cfg = {
        "codex_agent_enabled": True, "agy_agent_enabled": True,
        "groq_api_key": "gsk_x", "openrouter_api_key": "or-x",
        "cursor_agent_enabled": True,
        "scouter_provider_chain": ["google_gemini", "openrouter", "groq"],
        "observer_provider_chain": ["cursor", "google_gemini", "nvidia"],
        "vision_enabled": False, "vision_provider_chain": ["google_gemma"],
        "web_lookup_enabled": True, "web_lookup_provider_chain": ["groq_compound", "cursor"],
    }
    assert list(ph.build_probes(cfg)) == [
        "codex", "agy", "groq", "openrouter:voice", "cursor:voice",
        "google_gemini", "openrouter", "cursor:observer",
        "web:groq_compound", "cursor:lookup",
    ]
    mati = {**cfg, "codex_agent_enabled": False, "agy_agent_enabled": False,
            "cursor_agent_enabled": False, "web_lookup_enabled": False}
    assert "agy" not in ph.build_probes(mati) and "codex" not in ph.build_probes(mati)


# --------------------------------------------------------------------------- #
# Review 21cb06ff: rantai tidak pernah kosong, config deploy, biaya probe
# --------------------------------------------------------------------------- #

# Config DEPLOY Sabtu (dari koordinator): codex terakhir di rantai teks,
# suara Codex gpt-6-luna priority, agy mati. Vision sengaja TANPA codex (privasi).
DEPLOY = {
    "provider_probe_enabled": True, "provider_probe_budget_sec": 12,
    "provider_breaker_failures": 3, "provider_breaker_open_sec": 600,
    "codex_agent_enabled": True, "codex_primary_voice": True,
    "codex_model": "gpt-6-luna", "codex_service_tier": "priority",
    "agy_agent_enabled": False, "cursor_agent_enabled": True,
    "groq_api_key": "gsk_x", "openrouter_api_key": "or-x",
    "scouter_provider_chain": ["google_gemini", "openrouter", "cloudflare", "ollama",
                               "zai", "nvidia", "codex"],
    "observer_provider_chain": ["google_gemini", "cloudflare", "openrouter", "zai",
                                "ollama", "nvidia", "codex"],
    "vision_enabled": False,
    "vision_provider_chain": ["google_gemini_lite", "openrouter", "ollama", "google_gemma",
                              "cloudflare", "zai", "nvidia"],
    "web_lookup_enabled": True,
}


def test_config_deploy_codex_dilindungi_di_dua_rantai_agy_tidak_diprobe(monkeypatch, _bersih):
    log, _jam = _bersih
    assert ph.configure(DEPLOY)
    probes = ph.build_probes(DEPLOY)
    assert "agy" not in probes and "codex" in probes
    # Hanya 2 entri terdepan per rantai, codex di rantai tidak di-probe dobel.
    assert [k for k in probes if k in DEPLOY["scouter_provider_chain"]
            or k in DEPLOY["observer_provider_chain"]] == [
        "codex", "google_gemini", "openrouter", "cloudflare"]
    ph.apply_probe("codex", ph.ProbeResult("FAIL", "auth", "not logged in"))
    for rantai in ("scouter_provider_chain", "observer_provider_chain"):
        lain = [n for n in DEPLOY[rantai] if n != "codex"]
        for n in lain:
            ph.apply_probe(n, ph.ProbeResult("FAIL", "model_rejected"))
        assert ph.usable(DEPLOY[rantai], lambda n: ph.chain_key(n, DEPLOY)) == {"codex"}
    dipanggil = _rantai_palsu(monkeypatch, DEPLOY["scouter_provider_chain"])
    assert sc.run_chain("streamer ngobrol", DEPLOY).provider == "codex"
    assert dipanggil == ["codex"]
    assert sum("!!! Codex (last resort) DOWN" in b for b in log) == 1


def test_rantai_nyata_semua_dilewati_entri_pertama_yang_belum_mati_dicoba(monkeypatch):
    _aktif()
    rantai = DEPLOY["vision_provider_chain"]  # rantai nyata tanpa codex
    ph.apply_probe("google_gemini_lite", ph.ProbeResult("FAIL", "auth"))  # mati
    for n in rantai[1:]:
        _buka(n)
    assert ph.usable(rantai) == set()  # open/dead must both be skipped
    dipanggil = _rantai_palsu(monkeypatch, rantai)
    r = sc.run_chain("streamer ngobrol", {})
    assert r is None and dipanggil == []
    # Semua mati: rantai kosong (mati tetap tidak dicoba).
    for n in rantai[1:]:
        ph.apply_probe(n, ph.ProbeResult("FAIL", "model_rejected"))
    assert ph.usable(rantai) == set()


def test_probe_murah_endpoint_gratis_tanpa_gambar(monkeypatch):
    """Probe = GET metadata (/models, /key); tidak ada generate, tidak ada gambar."""
    import arti_vision_client as vc

    panggilan = []

    class _R:
        status_code = 200
        text = "{}"

    def fake_request(method, url, headers=None, json=None, timeout=None):
        panggilan.append((method, url.split("?")[0], json))
        return _R()

    monkeypatch.setattr(requests, "request", fake_request)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(sc, "_PROVIDERS", {
        n: (lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("generate dipanggil")))
        for n in sc._PROVIDERS})
    monkeypatch.setattr(vc, "_PROVIDERS", {
        n: (lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("vision dipanggil")))
        for n in vc._PROVIDERS})
    cfg = {**DEPLOY, "codex_agent_enabled": False, "cursor_agent_enabled": False,
           "vision_enabled": True, "cloudflare_api_token": "t", "cloudflare_account_id": "a",
           "web_lookup_enabled": False}
    _aktif()
    res = ph.run_startup_probes(ph.build_probes(cfg), budget=3.0)
    assert all(r.status == "OK" for r in res.values()), res
    assert all(m == "GET" and body is None for m, _u, body in panggilan)
    assert "vision:google_gemini_lite" in res and "vision:openrouter" in res
    assert "vision:ollama" not in res  # tanpa endpoint gratis = tidak di-probe
    assert len(panggilan) == len(res) <= 8

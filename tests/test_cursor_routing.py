"""Wiring Cursor di bridge: routing per-trigger, rantai fallback, invarian animasi.

Semua di-monkeypatch — tanpa API key, tanpa jaringan.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_vtuber_bridge as b


# --- default CONFIG harus AMAN --------------------------------------------


def test_cursor_defaults_shipped_are_off():
    """Default yang DIKIRIM di source harus aman untuk repo publik.

    Sengaja membaca SOURCE, bukan `b.CONFIG`: CONFIG sudah ditimpa overlay
    config_local.json milik mesin ini (di mana Cursor memang dinyalakan), jadi
    memeriksanya membuat test bergantung mesin — persis yang bikin 4 test ini gagal
    saat crosscheck pra-live.
    """
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"cursor_agent_enabled": False,' in src, "kill switch harus default OFF"
    assert '"cursor_scratch_dir": "",' in src, "scratch dir harus diisi per mesin"
    assert '"cursor_fast_param": False,' in src, (
        "fast=True berharga 6x untuk selisih ~0,5 detik"
    )
    assert '"cursor_reject_on_tool_call": True,' in src
    assert '"cursor_trigger_types": ["yt_chat"],' in src


def test_cursor_config_keys_exist_at_runtime():
    for key in (
        "cursor_agent_enabled", "cursor_trigger_types", "cursor_model",
        "cursor_fast_param", "cursor_scratch_dir", "cursor_timeout_sec",
        "cursor_reject_on_tool_call", "cursor_max_consecutive_failures",
    ):
        assert key in b.CONFIG, f"CONFIG kehilangan {key}"


# --- routing per-trigger ---------------------------------------------------


def _cfg_on(tmp_path):
    return {
        **b.CONFIG,
        "cursor_agent_enabled": True,
        "cursor_api_key": "x",
        "cursor_scratch_dir": str(tmp_path),
        # PIN eksplisit — jangan mewarisi dari b.CONFIG. config_local.json mesin ini
        # menambah "curious" ke trigger types untuk tes live seharian, dan test yang
        # mewarisinya jadi bergantung mesin (persis kelas bug yang sama dengan
        # test_cursor_defaults dulu).
        "cursor_trigger_types": ["yt_chat"],
    }


def test_routing_off_when_flag_off():
    """Config eksplisit, bukan b.CONFIG — supaya tidak bergantung config_local mesin."""
    cfg = {**b.CONFIG, "cursor_agent_enabled": False}
    for t in ("yt_chat", "mic", "curious"):
        assert b._should_route_to_cursor(t, cfg) is False


def test_routing_only_yt_chat_when_on(tmp_path):
    cfg = _cfg_on(tmp_path)
    assert b._should_route_to_cursor("yt_chat", cfg) is True
    assert b._should_route_to_cursor("mic", cfg) is False, "mic butuh instan — tetap Groq"
    assert b._should_route_to_cursor("curious", cfg) is False


def test_routing_respects_custom_trigger_types(tmp_path):
    cfg = {**_cfg_on(tmp_path), "cursor_trigger_types": ["mic"]}
    assert b._should_route_to_cursor("mic", cfg) is True
    assert b._should_route_to_cursor("yt_chat", cfg) is False


def test_routing_false_when_scratch_dir_invalid():
    cfg = {**b.CONFIG, "cursor_agent_enabled": True, "cursor_api_key": "x",
           "cursor_scratch_dir": ""}
    assert b._should_route_to_cursor("yt_chat", cfg) is False


# --- rantai fallback -------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _patch_chain(monkeypatch, cursor_result, groq_reply="dari groq", warm=True):
    import arti_cursor_agent as ca

    # Sesi dianggap SUDAH hangat: jalur dingin punya test sendiri di
    # test_cursor_agent.py. Tanpa ini seluruh matriks jatuh ke cabang pemanasan.
    monkeypatch.setattr(ca, "prewarm", lambda cfg: warm)
    monkeypatch.setattr(ca, "send_turn", lambda s, u, c: cursor_result)
    monkeypatch.setattr(b, "pick_groq_model", lambda *a, **k: "model-x")
    monkeypatch.setattr(
        b, "groq_chat_completion", lambda *a, **k: (groq_reply, "model-x")
    )
    monkeypatch.setattr(b, "incharacter_fallback_reply", lambda s: "Hmm, bingung aku...")


@pytest.mark.parametrize(
    "reason,ok,text,tool_calls,expected_source",
    [
        ("ok", True, "dari cursor", 0, "cursor"),
        ("timeout", False, None, 0, "groq"),
        ("empty", False, None, 0, "groq"),
        ("error", False, None, 0, "groq"),
        ("ratelimit", False, None, 0, "groq"),
        ("disabled", False, None, 0, "groq"),
        # tool_call ditolak WALAU ada teks — sinyal agen melenceng dari tugasnya
        ("tool_call", False, "teks tapi manggil tool", 1, "groq"),
    ],
)
def test_fallback_matrix(monkeypatch, tmp_path, reason, ok, text, tool_calls, expected_source):
    import arti_cursor_agent as ca

    res = ca.CursorResult(text=text, ok=ok, reason=reason, tool_calls=tool_calls)
    _patch_chain(monkeypatch, res)
    reply, sents, source = _run(
        b._cursor_reply_with_fallback("sys", "prompt", "user", _cfg_on(tmp_path))
    )
    assert source.split(":")[0] == expected_source
    assert reply, "Arti tidak boleh pernah bisu"


def test_falls_through_to_incharacter_when_groq_also_dies(monkeypatch, tmp_path):
    import arti_cursor_agent as ca

    _patch_chain(monkeypatch, ca.CursorResult(reason="error"), groq_reply=None)
    reply, _sents, source = _run(
        b._cursor_reply_with_fallback("sys", "prompt", "user", _cfg_on(tmp_path))
    )
    assert source == "incharacter"
    assert reply, "lapis terakhir harus tetap menghasilkan teks"


def test_cancelled_marks_dirty_and_reraises(monkeypatch, tmp_path):
    """Barge-in PTT: sesi ditandai rusak, TAPI CancelledError harus naik terus.

    Kalau ditelan, handler pembatalan yang sudah ada di bridge tidak akan berjalan.
    """
    import arti_cursor_agent as ca

    marked = []
    monkeypatch.setattr(ca, "mark_dirty_global", lambda r: marked.append(r))
    monkeypatch.setattr(ca, "prewarm", lambda cfg: True)

    def _boom(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ca, "send_turn", _boom)
    with pytest.raises(asyncio.CancelledError):
        _run(b._cursor_reply_with_fallback("sys", "prompt", "user", _cfg_on(tmp_path)))
    assert marked, "sesi harus ditandai rusak sebelum re-raise"


def test_sentences_contract_matches_groq_path():
    """Kontrak sama dengan jalur Groq: list hanya diisi kalau >1 kalimat."""
    assert b._sentences_or_empty("Cuma satu kalimat saja") == []
    multi = b._sentences_or_empty("Kalimat satu. Kalimat dua. Kalimat tiga.")
    assert len(multi) > 1


# --- invarian: branch provider TIDAK BOLEH menyentuh animasi ---------------


_ANIMATION_NAMES = {
    "apply_speaking",
    "apply_turn_end",
    "run_nod_while_tts",
    "start_idle_animation",
    "should_nod_for_emotion",
    "voice_queue_enabled",
}


def test_do_api_call_never_touches_animation():
    """Mengunci janji "animasi tidak tersentuh" secara otomatis.

    Bohan tidak bisa menguji VTube Studio saat fitur ini dibangun, jadi jaminannya
    harus mekanis: branch provider hanya boleh menulis ai_reply dan
    tts_sentence_chunks. Seluruh kode animasi berjalan SETELAH `await current_api_task`.
    """
    tree = ast.parse((ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "do_api_call":
            target = node
            break
    assert target is not None, "do_api_call tidak ditemukan"

    found = {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(target)
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    leaked = found & _ANIMATION_NAMES
    assert not leaked, f"branch provider menyentuh jalur animasi: {sorted(leaked)}"


def test_cursor_branch_is_first_and_others_became_elif():
    """Branch Cursor disisipkan di depan; rantai lama tidak dirombak."""
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "if _cursor_route:" in src
    assert 'elif provider == "gemini_live"' in src, (
        "rantai provider lama harus jadi elif, bukan dirombak"
    )


def test_cold_session_goes_to_groq_and_kicks_off_prewarm(monkeypatch, tmp_path):
    """Sesi dingin TIDAK boleh menahan turn — harus langsung ke Groq.

    Ini cacat yang ketahuan saat crosscheck pra-live: sesi dingin butuh ~18-21 detik,
    jauh di atas cursor_timeout_sec. Kalau turn menunggu, ia timeout, sesi ditandai
    rusak, didaur ulang, lalu dingin lagi — Cursor tidak akan PERNAH terpakai.
    """
    import arti_cursor_agent as ca

    calls = {"prewarm": 0, "send_turn": 0}

    def _prewarm(cfg):
        calls["prewarm"] += 1
        return False  # belum hangat

    def _send(*a, **k):
        calls["send_turn"] += 1
        return ca.CursorResult(ok=True, reason="ok", text="JANGAN dipakai")

    monkeypatch.setattr(ca, "prewarm", _prewarm)
    monkeypatch.setattr(ca, "send_turn", _send)
    monkeypatch.setattr(b, "pick_groq_model", lambda *a, **k: "m")
    monkeypatch.setattr(b, "groq_chat_completion", lambda *a, **k: ("dari groq", "m"))

    reply, _s, source = _run(
        b._cursor_reply_with_fallback("sys", "prompt", "user", _cfg_on(tmp_path))
    )
    assert source.startswith("groq"), "turn dingin harus dilayani Groq"
    assert reply == "dari groq"
    assert calls["prewarm"] == 1, "pemanasan harus dipicu"
    assert calls["send_turn"] == 0, "JANGAN menunggu sesi dingin"


def test_cursor_timeout_has_margin_over_measured_p95():
    """cursor_timeout_sec dipakai sebagai deadline internal DAN dasar wait_for.

    Distribusi composer bergeser naik TIAP sesi live: spike max 5,12; sore
    2026-08-02 n=56 p50 5,16 / max 6,905 (axe lama 7,0 memancung 11%);
    seharian 2026-08-03 n=26 p50 7,38 / p90 9,31 / max 9,907 — max sukses
    NEMPEL di kapak 10,0, dan gagal (20 timeout + 7 outer) > sukses. Tiap
    timeout juga mark_dirty -> sesi daur ulang -> turn berikut dingin
    (curious/inisiatif = BISU). 12,0 = ruang ukur ekor jujur.
    Jangan diturunkan tanpa distribusi latensi baru dari log live.
    """
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"cursor_timeout_sec": 12.0,' in src, (
        "timeout harus punya margin di atas p95 terukur terbaru (9,9 dtk 3/8)"
    )


# --- invarian: TTS harus pakai teks FINAL, bukan chunk mentah ---------------


def test_tts_chunks_rebuilt_from_cleaned_reply():
    """Tag [EMOTION:...] pernah BOCOR ke suara (Arti mengucapkan "emotion senang").

    Akar masalah: do_api_call membuat tts_sentence_chunks dari jawaban MENTAH,
    lalu main loop membersihkan ai_reply tiga kali (strip [MEMORY_SAVE], filter
    panjang, strip [EMOTION:]) — tapi chunk lama tetap dipakai untuk tts.speak.
    Jawaban multi-kalimat mengucapkan tag di chunk terakhir; jawaban terpotong
    filter tetap diucapkan versi panjangnya.

    Invarian: setelah parse_reply_emotion, chunks WAJIB di-rebuild dari ai_reply
    final SEBELUM loop tts.speak.
    """
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    parse_at = src.index("parse_reply_emotion(ai_reply)")
    speak_at = src.index("for chunk in tts_sentence_chunks:")
    rebuild = "tts_sentence_chunks = _sentences_or_empty(ai_reply)"
    assert rebuild in src, "chunk TTS tidak pernah di-rebuild dari teks final"
    rebuild_at = src.index(rebuild)
    assert parse_at < rebuild_at < speak_at, (
        "rebuild chunk harus terjadi SETELAH strip [EMOTION:] dan SEBELUM tts.speak"
    )


def test_cold_session_precious_trigger_waits_for_warmup(monkeypatch, tmp_path):
    """Live sore2 2026-08-02: reaksi video Rp 2.000 ("BEST OF ZACH 2") kena sesi
    dingin -> dijawab Groq 8B -> Bohan: "kayaknya gak liat deh dia". Trigger
    video/donation TIDAK diburu waktu dan kontennya tak tergantikan (digest
    video, terima kasih donatur) — wajib TUNGGU pemanasan, bukan lempar ke 8B."""
    import arti_cursor_agent as ca

    state = {"warm_checks": 0}
    monkeypatch.setattr(ca, "prewarm", lambda cfg: False)

    def _is_warm():
        state["warm_checks"] += 1
        return state["warm_checks"] >= 2  # hangat pada cek kedua (~2 dtk)

    monkeypatch.setattr(ca, "is_warm", _is_warm)
    monkeypatch.setattr(
        ca, "send_turn",
        lambda *a, **k: ca.CursorResult(ok=True, reason="ok", text="lihat videonya"),
    )
    monkeypatch.setattr(b, "pick_groq_model", lambda *a, **k: "m")
    monkeypatch.setattr(b, "groq_chat_completion", lambda *a, **k: ("dari groq", "m"))

    cfg = {**_cfg_on(tmp_path), "cursor_warmup_wait_precious_sec": 5.0}
    reply, _s, source = _run(
        b._cursor_reply_with_fallback("sys", "p", "u", cfg, trigger_type="video")
    )
    assert source == "cursor", "reaksi video wajib menunggu pemanasan"
    assert reply == "lihat videonya"
    assert state["warm_checks"] >= 2


def test_cold_session_precious_trigger_gives_up_after_budget(monkeypatch, tmp_path):
    """Pemanasan tak kunjung selesai -> tetap jatuh ke Groq setelah budget habis
    (donatur bayar tidak boleh bisu selamanya)."""
    import arti_cursor_agent as ca

    monkeypatch.setattr(ca, "prewarm", lambda cfg: False)
    monkeypatch.setattr(ca, "is_warm", lambda: False)
    monkeypatch.setattr(b, "pick_groq_model", lambda *a, **k: "m")
    monkeypatch.setattr(b, "groq_chat_completion", lambda *a, **k: ("dari groq", "m"))

    cfg = {**_cfg_on(tmp_path), "cursor_warmup_wait_precious_sec": 1.5}
    reply, _s, source = _run(
        b._cursor_reply_with_fallback("sys", "p", "u", cfg, trigger_type="donation")
    )
    assert source.startswith("groq") and reply == "dari groq"


def test_startup_prewarms_voice_session_too():
    """Sore3 2026-08-02: pemanas startup cuma scout+vision — sesi voice nunggu
    trigger pertama, jadi inisiatif awal sesi selalu hangus kena "sesi belum
    hangat". Startup wajib ikut memanaskan sesi voice (non-blocking)."""
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    i = src.index("def _prewarm_cursor_roles")
    seg = src[i:i + 1600]
    assert "prewarm(CONFIG)" in seg, "voice prewarm harus ikut startup"
    assert "cursor_trigger_types" in seg, "hanya bila routing voice ke cursor aktif"

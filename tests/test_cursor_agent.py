"""Unit test arti_cursor_agent — helper murni saja.

Tidak butuh cursor-sdk terpasang, tidak butuh API key, tidak menyentuh jaringan.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_cursor_agent as ca


# --- stub message: meniru bentuk objek dari run.messages() -------------------


class _Block:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _Inner:
    def __init__(self, content) -> None:
        self.content = content


class _Msg:
    def __init__(self, type_: str, content=None) -> None:
        self.type = type_
        if content is not None:
            self.message = _Inner(content)


def _assistant(*blocks) -> _Msg:
    return _Msg("assistant", list(blocks))


# --- extract_text_blocks -----------------------------------------------------


def test_extract_text_blocks_takes_only_text_type():
    msg = _assistant(
        _Block("thinking", "JANGAN ikut"),
        _Block("text", "halo "),
        _Block("tool_call", "JANGAN ikut"),
        _Block("text", "Bohan"),
    )
    assert ca.extract_text_blocks(msg) == "halo Bohan"


def test_extract_text_blocks_handles_dict_shape():
    msg = {"type": "assistant", "message": {"content": [{"type": "text", "text": "oke"}]}}
    assert ca.extract_text_blocks(msg) == "oke"


def test_extract_text_blocks_tolerates_missing_fields():
    assert ca.extract_text_blocks(_Msg("assistant")) == ""
    assert ca.extract_text_blocks({"type": "assistant"}) == ""


# --- collect_run_messages ----------------------------------------------------


def test_collect_ignores_non_assistant_types_but_counts_them():
    msgs = [
        _Msg("system"),
        _assistant(_Block("text", "Nyala ")),
        _Msg("thinking"),
        _Msg("tool_call"),
        _assistant(_Block("text", "dong!")),
        _Msg("usage"),
    ]
    out = ca.collect_run_messages(msgs, timeout_s=30)
    assert out.text == "Nyala dong!"
    assert out.tool_calls == 1
    assert out.thinking_blocks == 1
    assert out.timed_out is False


def test_collect_deadline_uses_injected_clock():
    """Deterministik, tanpa sleep."""
    ticks = iter([0.0, 1.0, 2.0, 99.0, 99.0, 99.0])
    msgs = [_assistant(_Block("text", "a")), _assistant(_Block("text", "b")),
            _assistant(_Block("text", "c"))]
    out = ca.collect_run_messages(msgs, timeout_s=5, clock=lambda: next(ticks))
    assert out.timed_out is True


def test_collect_records_first_text_latency():
    ticks = iter([0.0, 0.0, 1.5, 1.5, 2.0])
    msgs = [_Msg("thinking"), _assistant(_Block("text", "x"))]
    out = ca.collect_run_messages(msgs, timeout_s=30, clock=lambda: next(ticks))
    assert out.first_text_ms is not None


# --- validate_scratch_dir ----------------------------------------------------


def test_scratch_dir_rejects_empty_and_relative():
    assert ca.validate_scratch_dir("")[0] is False
    assert ca.validate_scratch_dir("relatif/path")[0] is False


def test_scratch_dir_rejects_repo_and_git(tmp_path):
    assert ca.validate_scratch_dir(str(ROOT))[0] is False, "repo sendiri harus ditolak"
    d = tmp_path / "punya_git"
    (d / ".git").mkdir(parents=True)
    assert ca.validate_scratch_dir(str(d))[0] is False


def test_scratch_dir_rejects_dir_inside_repo(tmp_path):
    inside = ROOT / "data"
    if inside.is_dir():
        assert ca.validate_scratch_dir(str(inside))[0] is False


def test_scratch_dir_accepts_clean_outside_dir(tmp_path):
    ok, resolved = ca.validate_scratch_dir(str(tmp_path))
    assert ok is True
    assert Path(resolved).is_dir()


# --- should_recycle ----------------------------------------------------------


def test_should_recycle_matrix():
    cfg = {"cursor_session_max_turns": 20, "cursor_session_max_age_sec": 1800}
    assert ca.should_recycle(0, 0, False, cfg)[0] is False
    assert ca.should_recycle(20, 0, False, cfg)[0] is True
    assert ca.should_recycle(5, 2000, False, cfg)[0] is True
    # dirty menang atas apa pun — sesi mati di turn 15 saat uji nyata
    assert ca.should_recycle(1, 1, True, cfg)[0] is True


def test_should_recycle_zero_disables_limit():
    cfg = {"cursor_session_max_turns": 0, "cursor_session_max_age_sec": 0}
    assert ca.should_recycle(9999, 999999, False, cfg)[0] is False


# --- is_available: menyaring SEBELUM menyentuh jaringan ----------------------


def test_is_available_false_when_flag_off():
    ok, why = ca.is_available({})
    assert ok is False and "enabled" in why


def test_is_available_false_without_key(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    ok, why = ca.is_available({"cursor_agent_enabled": True})
    assert ok is False and "KEY" in why.upper()


def test_is_available_false_when_sdk_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "sdk_module_name", lambda: None)
    ok, why = ca.is_available({
        "cursor_agent_enabled": True,
        "cursor_api_key": "x",
        "cursor_scratch_dir": str(tmp_path),
    })
    assert ok is False and "sdk" in why.lower()


# --- circuit breaker ---------------------------------------------------------


def test_breaker_opens_after_consecutive_failures(monkeypatch, tmp_path):
    """Setelah N gagal, send_turn berhenti menyentuh SDK sama sekali.

    Ini yang mencegah kuota habis di tengah live berubah jadi dead air tiap pesan:
    saat uji nyata, sesi mati lalu lima turn berikutnya gagal instan.
    """
    ca.reset_breaker()
    cfg = {
        "cursor_agent_enabled": True,
        "cursor_api_key": "x",
        "cursor_scratch_dir": str(tmp_path),
        "cursor_max_consecutive_failures": 3,
    }
    monkeypatch.setattr(ca, "sdk_module_name", lambda: "cursor_sdk")

    class _FailSession:
        def __init__(self, *a, **k):
            self.turn_count = 0
            self.config = {}

        def send_collect(self, *a, **k):
            return ca.CursorResult(reason=ca.REASON_ERROR)

    monkeypatch.setattr(ca, "_session", None)
    monkeypatch.setattr(ca, "CursorSession", _FailSession)

    for _ in range(3):
        ca.send_turn("s", "u", cfg)

    called = {"n": 0}

    class _Spy(_FailSession):
        def send_collect(self, *a, **k):
            called["n"] += 1
            return ca.CursorResult(reason=ca.REASON_ERROR)

    monkeypatch.setattr(ca, "CursorSession", _Spy)
    res = ca.send_turn("s", "u", cfg)
    assert res.reason == ca.REASON_DISABLED
    assert called["n"] == 0, "breaker terbuka tapi SDK masih disentuh"
    ca.reset_breaker()


def test_send_turn_unavailable_short_circuits(monkeypatch):
    ca.reset_breaker()
    res = ca.send_turn("s", "u", {})
    assert res.reason == ca.REASON_UNAVAILABLE


# --- pemanas sesi (ditambahkan setelah crosscheck pra-live) ------------------
#
# Tanpa ini jalur Cursor TIDAK AKAN PERNAH terpakai: sesi dingin butuh ~18-21 detik,
# jauh di atas cursor_timeout_sec. Chat pertama timeout -> sesi ditandai rusak ->
# didaur ulang -> chat berikutnya dingin lagi -> timeout lagi, selamanya.


def test_prewarm_never_blocks_and_reports_not_warm(monkeypatch, tmp_path):
    ca.reset_breaker()
    monkeypatch.setattr(ca, "_session", None)
    monkeypatch.setattr(ca, "sdk_module_name", lambda: "cursor_sdk")
    started = []

    class _Thread:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            started.append(1)  # sengaja TIDAK dijalankan: pastikan non-blocking

    monkeypatch.setattr(ca.threading, "Thread", _Thread)
    cfg = {
        "cursor_agent_enabled": True,
        "cursor_api_key": "x",
        "cursor_scratch_dir": str(tmp_path),
    }
    assert ca.prewarm(cfg) is False, "sesi dingin harus lapor belum hangat"
    assert started, "pemanasan harus jalan di latar belakang"
    ca._warming = False


def test_is_warm_requires_completed_first_turn(monkeypatch, tmp_path):
    """`warmed` BEDA dari "agen sudah dibuat".

    Regresi nyata: is_warm() sempat True setelah ~6 detik (agen ada) padahal pemanasan
    baru selesai di ~20 detik. Chat yang datang di sela itu berebut dengan pemanasan
    dan tetap lambat — terukur 9,6 detik.
    """
    sess = ca.CursorSession({})
    sess._agent = object()
    sess.dirty = False
    sess.warmed = False
    monkeypatch.setattr(ca, "_session", sess)
    assert ca.is_warm() is False, "agen ada tapi giliran pertama belum selesai"
    sess.warmed = True
    assert ca.is_warm() is True
    sess.dirty = True
    assert ca.is_warm() is False, "sesi rusak tidak boleh dianggap hangat"


def test_prewarm_short_circuits_when_unavailable():
    ca.reset_breaker()
    assert ca.prewarm({}) is False


# --- kunci model: HANYA composer-2.5 non-fast --------------------------------


def test_ensure_raises_instead_of_falling_back_to_plain_string(monkeypatch, tmp_path):
    """Kalau ModelSelection gagal dibangun, sesi HARUS gagal — bukan pakai id polos.

    Dokumentasi Cursor: id polos "composer-2.5" tanpa param sering di-resolve ke
    varian FAST (6x lebih mahal). Keputusan eksplisit Bohan: hanya composer-2.5
    non-fast. Fallback senyap = membakar kuota Fast tanpa ketahuan; lebih baik turn
    jatuh ke Groq.
    """
    import sys
    import types

    import pytest

    fake = types.ModuleType("fake_cursor_sdk_mod")

    class _BrokenModelSelection:
        def __init__(self, *a, **k):
            raise TypeError("bentuk API berubah")

    fake.ModelSelection = _BrokenModelSelection
    fake.ModelParameterValue = lambda **k: None
    fake.LocalAgentOptions = lambda **k: None

    class _Agent:
        @staticmethod
        def create(**k):
            raise AssertionError("Agent.create tidak boleh tercapai dengan id polos")

    fake.Agent = _Agent
    sys.modules["fake_cursor_sdk_mod"] = fake
    try:
        monkeypatch.setattr(ca, "sdk_module_name", lambda: "fake_cursor_sdk_mod")
        monkeypatch.setattr(
            ca, "launch_sdk_bridge", lambda sdk, ws: (object(), None, None)
        )
        sess = ca.CursorSession({"cursor_scratch_dir": str(tmp_path)})
        with pytest.raises(TypeError):
            sess.ensure()
    finally:
        del sys.modules["fake_cursor_sdk_mod"]


def test_send_never_overrides_model_per_turn():
    """Model dipilih SEKALI saat sesi dibangun; tidak ada override per-send.

    SendOptions(model=...) itu sticky untuk send berikutnya — kalau sampai terpakai,
    satu typo bisa memindahkan semua turn ke model lain (termasuk pool API $20 yang
    sudah habis). Kunci di level source.
    """
    src = (ROOT / "arti_cursor_agent.py").read_text(encoding="utf-8")
    assert "SendOptions(mode=" in src
    assert "SendOptions(model=" not in src, "tidak boleh ada override model per-send"


# --- breaker half-open (untuk live yang ditinggal seharian) ------------------


def test_breaker_reopens_after_cooldown(monkeypatch, tmp_path):
    """Setelah cooldown, Cursor boleh dicoba lagi — jangan mati sisa hari."""
    ca.reset_breaker()
    monkeypatch.setattr(ca, "_breaker_open", True)
    monkeypatch.setattr(ca, "_breaker_opened_at", 0.0)  # sudah lama sekali
    monkeypatch.setattr(ca.time, "monotonic", lambda: 10_000.0)
    assert ca._breaker_allows_attempt({"cursor_breaker_cooldown_sec": 900}) is True
    assert ca._breaker_open is False, "breaker harus ter-reset penuh"
    ca.reset_breaker()


def test_breaker_stays_closed_before_cooldown(monkeypatch):
    ca.reset_breaker()
    monkeypatch.setattr(ca, "_breaker_open", True)
    monkeypatch.setattr(ca, "_breaker_opened_at", 9_500.0)
    monkeypatch.setattr(ca.time, "monotonic", lambda: 10_000.0)  # baru 500s
    assert ca._breaker_allows_attempt({"cursor_breaker_cooldown_sec": 900}) is False
    ca.reset_breaker()


def test_breaker_cooldown_zero_is_permanent(monkeypatch):
    """Perilaku lama tetap bisa dipilih: 0 = mati sampai restart."""
    ca.reset_breaker()
    monkeypatch.setattr(ca, "_breaker_open", True)
    monkeypatch.setattr(ca, "_breaker_opened_at", 0.0)
    monkeypatch.setattr(ca.time, "monotonic", lambda: 999_999.0)
    assert ca._breaker_allows_attempt({"cursor_breaker_cooldown_sec": 0}) is False
    ca.reset_breaker()


def test_prewarm_respects_cooldown_not_raw_flag(monkeypatch, tmp_path):
    """prewarm dipanggil SEBELUM send_turn di bridge. Kalau ia cek flag mentah,
    turn selalu belok ke Groq sebelum cooldown sempat dievaluasi — breaker tidak
    pernah menutup kembali. Terdeteksi saat wiring."""
    ca.reset_breaker()
    monkeypatch.setattr(ca, "_breaker_open", True)
    monkeypatch.setattr(ca, "_breaker_opened_at", 0.0)
    monkeypatch.setattr(ca.time, "monotonic", lambda: 10_000.0)
    monkeypatch.setattr(ca, "sdk_module_name", lambda: "cursor_sdk")
    monkeypatch.setattr(ca, "is_warm", lambda: True)
    ok = ca.prewarm({
        "cursor_agent_enabled": True,
        "cursor_api_key": "x",
        "cursor_scratch_dir": str(tmp_path),
        "cursor_breaker_cooldown_sec": 900,
    })
    assert ok is True, "cooldown lewat -> prewarm harus membuka jalan lagi"
    ca.reset_breaker()

"""Tests for arti_desktop_audio ring buffer and guards."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_desktop_audio as da


def test_dialogue_ring_maxlen():
    ring = da.DialogueRing(max_lines=3)
    for i in range(5):
        ring.append(f"line {i}")
    snap = ring.snapshot()
    assert len(snap) == 3
    assert snap[0].text == "line 2"


def test_should_reject_while_tts_playing():
    assert not da.should_accept_desktop_transcript(
        "hello",
        tts_is_playing=True,
        last_tts_end=None,
        is_echo_of_arti=lambda _: False,
    )


def test_should_reject_echo():
    assert not da.should_accept_desktop_transcript(
        "sama persis",
        tts_is_playing=False,
        last_tts_end=None,
        is_echo_of_arti=lambda t: t == "sama persis",
    )


def test_ingest_appends_when_ok():
    ring = da.DialogueRing(max_lines=5)
    ok = da.ingest_desktop_transcript(
        "dialog video",
        tts_is_playing=False,
        last_tts_end=None,
        is_echo_of_arti=lambda _: False,
        ring=ring,
    )
    assert ok
    assert ring.snapshot()[0].text == "dialog video"


# --- Telinga selalu-nyala (2026-08-02): RMS gate, worker fungsional, TTL --------


import time

import numpy as np


def test_chunk_rms():
    assert da.chunk_rms(np.zeros(1600, dtype="float32")) == 0.0
    assert da.chunk_rms(np.ones(1600, dtype="float32") * 0.5) > 0.4
    assert da.chunk_rms([]) == 0.0


def _run_worker(cfg, chunks, *, tts_seq=None, transcribe=None, filt=None,
                listening=None):
    """Jalankan worker dengan fake: chunks habis -> config OFF -> loop exit."""
    da.dialogue_ring.clear()
    calls = {"rec": 0, "tr": 0}
    pool = list(chunks)

    def record():
        calls["rec"] += 1
        if not pool:
            cfg["desktop_audio_enabled"] = False
            return None
        item = pool.pop(0)
        if not pool:
            cfg["desktop_audio_enabled"] = False
        return item

    tts_state = list(tts_seq or [])

    def tts_playing():
        return tts_state.pop(0) if tts_state else False

    def transcribe_default(audio):
        calls["tr"] += 1
        return "halo dari video"

    da.desktop_audio_worker(
        cfg,
        get_tts_is_playing=tts_playing,
        get_last_tts_end=lambda: None,
        is_echo_of_arti=lambda t: False,
        record_chunk=record,
        transcribe_chunk=transcribe or transcribe_default,
        filter_text=filt,
        is_listening=listening,
        sleep_sec=0.01,
    )
    return calls


def _base_cfg(**over):
    cfg = {
        "desktop_audio_enabled": True,
        "desktop_audio_min_rms": 0.01,
        "desktop_audio_post_tts_cooldown_sec": 0.0,
        "desktop_audio_chunk_sec": 0.1,
    }
    cfg.update(over)
    return cfg


def test_worker_silent_chunk_skips_transcribe():
    loud = np.ones(800, dtype="float32") * 0.3
    silent = np.zeros(800, dtype="float32")
    calls = _run_worker(_base_cfg(), [silent, loud])
    assert calls["tr"] == 1, "chunk sunyi tidak boleh bayar transkripsi"
    assert [e.text for e in da.dialogue_ring.snapshot()] == ["halo dari video"]


def test_worker_drops_chunk_when_tts_started_midway():
    """TTS nyala DI TENGAH chunk (cek sesudah-rekam) -> buang — anti Arti
    mendengar suaranya sendiri lewat routing listen CABLE->headset."""
    loud = np.ones(800, dtype="float32") * 0.3
    # urutan get_tts_is_playing: loop-top False (rekam jalan), ingest True (buang)
    calls = _run_worker(_base_cfg(), [loud], tts_seq=[False, True])
    assert calls["tr"] == 1
    assert da.dialogue_ring.snapshot() == [], "chunk overlap TTS wajib dibuang"


def test_worker_cooldown_from_config_respected():
    da.dialogue_ring.clear()
    ok = da.ingest_desktop_transcript(
        "ekor gema",
        tts_is_playing=False,
        last_tts_end=time.time() - 5.0,
        is_echo_of_arti=lambda t: False,
        post_tts_cooldown_sec=10.0,
    )
    assert ok is False, "cooldown dari CONFIG harus dihormati (dulu hardcoded 3.0)"


def test_worker_hallucination_filter_applied():
    loud = np.ones(800, dtype="float32") * 0.3
    calls = _run_worker(
        _base_cfg(), [loud],
        filt=lambda t: "",  # saringan bilang: halusinasi
    )
    assert calls["tr"] == 1
    assert da.dialogue_ring.snapshot() == []


def test_worker_listening_off_records_nothing():
    loud = np.ones(800, dtype="float32") * 0.3
    cfg = _base_cfg()
    state = {"n": 0}

    def listening():
        state["n"] += 1
        if state["n"] >= 3:
            cfg["desktop_audio_enabled"] = False
        return False

    calls = _run_worker(cfg, [loud], listening=listening)
    assert calls["rec"] == 0, "'dengar off' = tidak merekam sama sekali"


def test_format_context_fresh_ttl():
    ring = da.DialogueRing(max_lines=10)
    now = 10_000.0
    ring.append("lirik lagu lama", wall_ts=now - 900)
    ring.append("dialog barusan", wall_ts=now - 30)
    out = da.format_context_fresh(ttl_sec=180, now=now, ring=ring)
    assert out == "dialog barusan", "baris kedaluwarsa tidak boleh bocor ke turn"
    assert da.format_context_fresh(ttl_sec=180, now=now + 500, ring=ring) == ""


def test_bridge_wiring_source_checks():
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "hermes_vtuber_bridge.py").read_text(
        encoding="utf-8"
    )
    assert "[AUDIO TERDENGAR" in src, "injeksi turn normal harus terpasang"
    assert "BUKAN teks yang terlihat di layar" in src, (
        "label lama 'TERDENGAR DI LAYAR' bikin Arti ngira halusinasi audio "
        "adalah teks yang KELIHATAN (live seharian 2026-08-03)"
    )
    assert '"dengar on"' in src and '"dengar off"' in src, "console toggle telinga"
    assert "GROQ_API_KEY_" in src, "pool kunci telinga dari env GROQ_API_KEY_*"
    assert "make_loopback_record_chunk" in src, "capture harus terpasang ke worker"
    assert 'language="id")' in src or 'language="id"' in src, (
        "default transcribe_audio jalur mic tetap paksa id"
    )
    i = src.index("def _desktop_transcribe")
    assert "language=None" in src[i:i + 700], "jalur desktop wajib auto-detect bahasa"


def test_desktop_groq_key_pool_rotation(monkeypatch):
    """Pool telinga = semua GROQ_API_KEY_* KECUALI kunci utama (jatah mic);
    rotasi round-robin menyebar kuota; duplikat & nilai non-gsk dibuang.
    (2026-08-03: Bohan nambah 3 akun — _bo, _g, _g2.)"""
    import os as _os

    import hermes_vtuber_bridge as b

    for name in list(_os.environ):
        if name.startswith("GROQ_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_utama_khusus_mic")
    monkeypatch.setenv("GROQ_API_KEY_bo", "gsk_bo")
    monkeypatch.setenv("GROQ_API_KEY_g", "gsk_g")
    monkeypatch.setenv("GROQ_API_KEY_g2", "gsk_g")  # duplikat nilai -> sekali
    monkeypatch.setenv("GROQ_API_KEY_x", "bukan-gsk")  # format salah -> buang

    pool = b._desktop_groq_keys()
    assert pool == ["gsk_bo", "gsk_g"], pool
    assert "gsk_utama_khusus_mic" not in pool, "kunci utama = jatah mic, jangan disentuh"

    picked = []
    monkeypatch.setattr(
        b, "transcribe_audio",
        lambda a, sr, use_groq, api_key=None, language="id", quiet=False:
            picked.append(api_key) or "t",
    )
    b._desktop_transcribe._kidx = 0
    for _ in range(4):
        b._desktop_transcribe([0.0])
    assert picked == ["gsk_bo", "gsk_g", "gsk_bo", "gsk_g"], "rotasi round-robin"


def test_desktop_transcribe_no_pool_falls_back_local(monkeypatch):
    import os as _os

    import hermes_vtuber_bridge as b

    for name in list(_os.environ):
        if name.startswith("GROQ_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    seen = {}
    monkeypatch.setattr(
        b, "transcribe_audio",
        lambda a, sr, use_groq, api_key=None, language="id", quiet=False: seen.update(
            use_groq=use_groq, language=language, quiet=quiet) or "t",
    )
    b._desktop_transcribe([0.0])
    assert seen == {"use_groq": False, "language": None, "quiet": True}, (
        "tanpa pool: whisper lokal + bahasa auto + tanpa spam print"
    )


# --- capture factory: bug COM pre-init (live pagi 2026-08-03) + deadman ---------


def test_no_manual_com_preinit_in_module():
    """CoInitializeEx manual bikin _COMLibrary soundcard dapat S_FALSE ->
    import modul GAGAL berulang ("Error 0x100000001" + AttributeError __del__
    tiap 5 dtk sepanjang sesi pagi 2026-08-03). soundcard init COM sendiri."""
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "arti_desktop_audio.py").read_text(
        encoding="utf-8"
    )
    assert "CoInitializeEx" not in src.replace(
        "CoInitializeEx manual bikin", ""
    ), "pre-init COM manual dilarang — biar soundcard yang urus"


def test_record_chunk_success_via_injected_opener():
    class FakeRec:
        def record(self, numframes):
            return np.ones((numframes, 1), dtype="float32") * 0.2

    cfg = {"desktop_audio_chunk_sec": 0.01, "desktop_audio_device": "FakeSpk"}
    rec = da.make_loopback_record_chunk(cfg, open_recorder=lambda w, sr: (FakeRec(), lambda: None))
    out = rec()
    assert out is not None and out.ndim == 1
    assert len(out) == int(0.01 * da.DEFAULT_SAMPLERATE)


def test_record_chunk_deadman_stops_retrying(monkeypatch):
    """Gagal beruntun MAX_CAPTURE_FAILS kali = menyerah diam — bukan spam
    error tiap 5 detik selamanya seperti sesi pagi."""
    monkeypatch.setattr(da.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def bad_opener(want, sr):
        calls["n"] += 1
        raise RuntimeError("Error 0x100000001")

    cfg = {"desktop_audio_chunk_sec": 0.01, "desktop_audio_device": "X"}
    rec = da.make_loopback_record_chunk(cfg, open_recorder=bad_opener)
    for _ in range(da.MAX_CAPTURE_FAILS):
        assert rec() is None
    assert calls["n"] == da.MAX_CAPTURE_FAILS
    assert rec() is None  # sudah mati: opener tidak boleh disentuh lagi
    assert calls["n"] == da.MAX_CAPTURE_FAILS


def test_worker_prints_what_arti_hears(capsys):
    loud = np.ones(800, dtype="float32") * 0.3
    _run_worker(_base_cfg(), [loud])
    out = capsys.readouterr().out
    assert "[Dengar] halo dari video" in out, (
        "tiap baris yang masuk ring harus kelihatan di terminal"
    )


def test_discontinuity_warning_silenced_in_source():
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "arti_desktop_audio.py").read_text(
        encoding="utf-8"
    )
    assert 'message="data discontinuity in recording"' in src, (
        "warning jinak soundcard wajib dibungkam — spam pagi 2026-08-03"
    )

# --- bedah live seharian 2026-08-03: junk flood + echo bocor ---------------------


def test_junk_filter_kills_daylong_offenders():
    """516/679 baris [Dengar] junk: "." x446, "¶¶" x51, kredit subtitle
    multibahasa. "Продолжение следует..." sampai dibahas Arti on-stream."""
    for junk in (
        ".", "¶¶", "...", "🎵", "ん", "you", "me", "Bye.", "Huh?",
        "Продолжение следует...",
        "Субтитры сделал DimaTorzok",
        "Субтитры создавал DimaTorzok",
        "ご視聴ありがとうございました",
        "ご視聴ありがとうございました。",
        "Terima kasih telah menonton!",
        "Thank you for watching!",
        "ПОДПИШИСЬ",
        "チャンネル登録をお願いいたします。",
        "구독과 좋아요를 눌러주세요.",
    ):
        assert da.looks_like_whisper_junk(junk), junk


def test_junk_filter_keeps_real_dialogue():
    for real in (
        "Masih banyak info tentang multo.",
        "Kira-kira apa ini nih yang sedang terjadi dengan Bohan?",
        "She's smiling like smoke, and eyes is the moon",
        "Поехали.",  # kata nyata RU — bukan kredit subtitle
    ):
        assert not da.looks_like_whisper_junk(real), real


def test_worker_drops_junk_transcript():
    loud = np.ones(800, dtype="float32") * 0.3
    n = {"tr": 0}

    def tr(audio):
        n["tr"] += 1
        return "Продолжение следует..."

    _run_worker(_base_cfg(), [loud, loud], transcribe=tr)
    assert n["tr"] == 2, "junk dibuang SETELAH transkrip, bukan sebelum"
    assert da.dialogue_ring.snapshot() == [], "junk tidak boleh menyentuh ring"


def test_worker_drops_consecutive_duplicate():
    """Musik/loop bikin Whisper mengulang kalimat sama persis tiap chunk."""
    loud = np.ones(800, dtype="float32") * 0.3
    texts = ["I'm going to put it in the middle of the bag."] * 3
    n = {"tr": 0}

    def tr(audio):
        n["tr"] += 1
        return texts.pop(0)

    _run_worker(_base_cfg(), [loud, loud, loud], transcribe=tr)
    assert n["tr"] == 3
    assert len(da.dialogue_ring.snapshot()) == 1, "dup beruntun = satu salinan"


def test_ingest_rejects_tts_overlap_via_chunk_start():
    """Echo bocor live seharian: TTS selesai DI TENGAH jendela chunk, tapi
    cek "sekarang" (pasca transkripsi 1-2 dtk) sudah lolos cooldown. Kini:
    last_tts_end >= awal chunk = overlap = buang."""
    ok = da.should_accept_desktop_transcript(
        "Wah, masa semangat cari uang bohan gitu sih?",
        tts_is_playing=False,
        last_tts_end=100.5,       # TTS selesai SETELAH chunk mulai
        is_echo_of_arti=lambda t: False,
        post_tts_cooldown_sec=0.0,
        chunk_start_ts=100.0,
    )
    assert ok is False, "chunk yang tumpang tindih TTS wajib dibuang"


def test_ingest_cooldown_measured_from_chunk_start():
    common = dict(
        tts_is_playing=False,
        last_tts_end=100.5,
        is_echo_of_arti=lambda t: False,
        post_tts_cooldown_sec=3.0,
    )
    assert da.should_accept_desktop_transcript(
        "dialog", chunk_start_ts=103.0, **common
    ) is False, "chunk mulai 2,5 dtk pasca-TTS masih di dalam cooldown 3,0"
    assert da.should_accept_desktop_transcript(
        "dialog", chunk_start_ts=104.0, **common
    ) is True


def test_worker_passes_chunk_start_to_ingest():
    """Fungsional: get_last_tts_end = "barusan banget" (>= awal chunk) harus
    ditolak MESKI cooldown 0 — membuktikan chunk_start_ts mengalir dari loop
    worker ke ingest, bukan cuma ada di signature."""
    loud = np.ones(800, dtype="float32") * 0.3
    da.dialogue_ring.clear()
    cfg = _base_cfg()
    pool = [loud]

    def record():
        if not pool:
            cfg["desktop_audio_enabled"] = False
            return None
        item = pool.pop(0)
        if not pool:
            cfg["desktop_audio_enabled"] = False
        return item

    da.desktop_audio_worker(
        cfg,
        get_tts_is_playing=lambda: False,
        get_last_tts_end=lambda: time.time(),  # TTS "selesai" setelah chunk mulai
        is_echo_of_arti=lambda t: False,
        record_chunk=record,
        transcribe_chunk=lambda a: "kalimat arti sendiri",
        sleep_sec=0.01,
    )
    assert da.dialogue_ring.snapshot() == []


def test_echo_fragment_prefix_detected_in_bridge():
    """[Dengar] "Wah, masa semangat cari uang bohan gitu sih?" = awal persis
    jawaban panjang Arti — ratio 0.7 whole-string meloloskannya. Cek fragmen
    hanya berlaku SESAAT pasca-TTS (audit 3/8: streamer yang mengutip Arti
    10 menit kemudian jangan ikut dimakan)."""
    import time as _t

    import hermes_vtuber_bridge as b

    old = b.last_arti_reply_text
    old_tts = getattr(b.voice_listener_worker, "_last_tts_end", None)
    try:
        b.last_arti_reply_text = (
            "Wah, masa semangat cari uang Bohan gitu sih? Sempet aku pikir, "
            "apa aku sudah cukup membantu atau nggak? Lalu, Bohan, kok "
            "sekarang kamu masih belum memiliki cukup uang untuk aku?"
        )
        frag = "Wah, masa semangat cari uang bohan gitu sih?"
        b.voice_listener_worker._last_tts_end = _t.time() - 3.0
        assert b.is_asr_echo_of_arti(frag), "ekor listen 3 dtk pasca-TTS = echo"
        assert not b.is_asr_echo_of_arti("Masih banyak info tentang multo.")
        assert not b.is_asr_echo_of_arti("uang untuk"), "fragmen pendek jangan overmatch"
        b.voice_listener_worker._last_tts_end = _t.time() - 600.0
        assert not b.is_asr_echo_of_arti(frag), (
            "streamer MENGUTIP Arti 10 menit kemudian bukan echo"
        )
    finally:
        b.last_arti_reply_text = old
        if old_tts is None:
            if hasattr(b.voice_listener_worker, "_last_tts_end"):
                del b.voice_listener_worker._last_tts_end
        else:
            b.voice_listener_worker._last_tts_end = old_tts

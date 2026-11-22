"""Tests refleks instan — bunyi pendek SEBELUM LLM sempat berpikir.

Spek Bohan 2026-08-05: "[recorded remark omitted]". Terukur: dari dipukul sampai ada bunyi butuh MINIMAL ~6 dtk.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402
import arti_reflex as rf  # noqa: E402
import arti_bridge as bridge  # noqa: E402


# --- pustaka kalimat ------------------------------------------------------

def test_library_shape():
    assert len(rf.ALL_LINES) == 50
    assert len(set(rf.ALL_LINES)) == 50, "ada kalimat kembar"
    for line in rf.ALL_LINES:
        assert 1 <= len(line.split()) <= 5, f"terlalu panjang untuk refleks: {line!r}"
        assert line.strip() == line and line


def test_every_category_has_lines():
    for cat, lines in rf.REFLEX_LINES.items():
        assert lines, cat


def test_reflex_starts_from_aware_state():
    """Momen kagetnya WAJIB 'aware' (ArtiAware, kelopak mata melebar) — bukan
    langsung lampu bicara, yang akan melewatkan momen itu."""
    import arti_expression_runtime as expr

    assert rf.REFLEX_VTS_STATE == "aware"
    assert "kaget" not in expr.EMOTION_MAP, (
        "kalau 'kaget' jadi emosi resmi, tinjau ulang pemetaan mood refleks"
    )
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    # state 'aware' memang dikenal VTSController (bukan nama karangan)
    j = src.index("async def trigger_expression_state")
    assert 'state == "aware"' in src[j:j + 900]


def test_cache_name_stable_unique_and_safe():
    a = rf.cache_name("Aduh!")
    assert a == rf.cache_name("Aduh!")            # stabil antar-proses
    assert a != rf.cache_name("Aduh?")            # beda tanda baca != tabrakan
    for line in rf.ALL_LINES:
        n = rf.cache_name(line)
        assert n.endswith(".wav")
        assert all(c.isalnum() or c in "-." for c in n), n
    assert len({rf.cache_name(x) for x in rf.ALL_LINES}) == 50


# --- pemetaan event -------------------------------------------------------

def test_event_mapping():
    assert rf.reflex_for_event({"ev": "hurt", "source": "creeper"}) == "kaget"
    # luka tanpa sumber (jatuh) -> nada nyeri, bukan kaget dituduh diserang
    assert rf.reflex_for_event({"ev": "hurt", "source": "unknown"}) == "sakit"
    assert rf.reflex_for_event({"ev": "death"}) == "mati"
    assert rf.reflex_for_event({"ev": "low_health", "health": 4}) == "panik"
    assert rf.reflex_for_event({"ev": "hostile_near"}) == "bahaya"


def test_quiet_events_never_trigger_sound():
    for ev in ({"ev": "status"}, {"ev": "chat"}, {"ev": "roam_leg"},
               {"ev": "spawned"}, {"ev": "task_done"},
               {"ev": "low_health", "health": 0}):  # 0 = jalur mati yang bicara
        assert rf.reflex_for_event(ev) is None, ev


# --- rate limit & anti-ulang ---------------------------------------------

def test_min_gap_between_reflexes():
    lim = rf.ReflexLimiter()
    cfg = {"reflex_min_gap_sec": 3.0}
    assert rf.should_react(lim, 100.0, cfg) is True
    rf.mark_reacted(lim, 100.0)
    assert rf.should_react(lim, 102.0, cfg) is False   # dipukul beruntun
    assert rf.should_react(lim, 103.0, cfg) is True


def test_fresh_limiter_may_react_immediately():
    # 0.0 = BELUM PERNAH, bukan "barusan" (pelajaran bug _cooled).
    assert rf.should_react(rf.ReflexLimiter(), 5.0, {}) is True


def test_lines_do_not_repeat_until_pool_exhausted():
    lim = rf.ReflexLimiter()
    seq = [rf.pick_line("mati", lim, rng=lambda: 0.0) for _ in range(6)]
    assert len(set(seq)) == 6, f"mengulang terlalu cepat: {seq}"


def test_pick_line_unknown_category():
    assert rf.pick_line("tidak_ada", rf.ReflexLimiter()) is None


def test_note_tells_llm_not_to_repeat():
    note = rf.note_for_llm("Aduh!")
    assert "Aduh!" in note and "JANGAN ulangi" in note


# --- integrasi bridge -----------------------------------------------------

def test_bridge_skips_reflex_while_speaking(monkeypatch):
    """sd.play satu jalur global: bunyi saat bicara MEMOTONG kalimatnya.

    Sejak 2026-08-07 ada pengecualian yang disengaja — ancaman BARU boleh
    menyela. Jadi yang diuji di sini penyerang yang SUDAH dikenal; kalau tidak,
    tes ini cuma menegakkan aturan yang sudah diganti.
    """
    import time
    played = []
    lim = rf.ReflexLimiter()
    lim.sumber_ts["creeper"] = time.time()
    monkeypatch.setitem(bridge.CONFIG, "reflex_enabled", True)
    monkeypatch.setattr(bridge, "tts_is_playing", True)
    monkeypatch.setattr(bridge, "_reflex_limiter", lim)
    monkeypatch.setattr(bridge, "_reflex_worker", lambda *a: played.append(a))
    bridge._play_reflex({"ev": "hurt", "source": "creeper"})
    assert played == []


def test_bridge_kill_switch(monkeypatch):
    played = []
    monkeypatch.setitem(bridge.CONFIG, "reflex_enabled", False)
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    monkeypatch.setattr(bridge, "_reflex_worker", lambda *a: played.append(a))
    bridge._play_reflex({"ev": "death"})
    assert played == []


def test_bridge_note_expires(monkeypatch):
    import time as _t
    monkeypatch.setattr(bridge, "_reflex_note", '(Kamu sudah refleks teriak "Ah!")')
    monkeypatch.setattr(bridge, "_reflex_note_ts", _t.time())
    assert "Ah!" in bridge._reflex_context_note()
    monkeypatch.setattr(bridge, "_reflex_note_ts", _t.time() - 60)
    assert bridge._reflex_context_note() == ""


def test_runner_calls_reflex_before_reaction():
    """Refleks WAJIB dipanggil untuk tiap event, bukan cuma yang bereaksi."""
    seen = []
    runner = am.MinecraftRunner(
        {"minecraft_streamer_name": "x"},
        {"reflex": lambda ev: seen.append(ev.get("ev")),
         "queue_reaction": lambda t: None, "add_history": lambda s, m: None},
        open_proc=lambda: None,
    )
    for line in ('{"ev": "status", "health": 20}',
                 '{"ev": "hurt", "source": "creeper", "health": 12}',
                 'bukan json'):
        runner._handle_line(line)
    assert seen == ["status", "hurt"], seen


def test_reflex_hook_failure_never_kills_reader(capsys):
    runner = am.MinecraftRunner(
        {}, {"reflex": lambda ev: 1 / 0, "queue_reaction": lambda t: None},
        open_proc=lambda: None,
    )
    runner._handle_line('{"ev": "death"}')   # tidak boleh melempar
    assert "hook gagal" in capsys.readouterr().out


# --- cache suara ----------------------------------------------------------

def test_voice_cache_complete_and_trimmed():
    """Cache harus lengkap DAN tanpa hening panjang di depan.

    Supertonic menempel ~0,4 dtk hening di awal tiap hasil; untuk refleks itu
    fatal — setengah detik yang membedakan "kaget" dari "lapor".
    """
    import soundfile as sf
    import numpy as np

    cache = ROOT / "data" / "reflex"
    if not cache.exists():
        import pytest
        pytest.skip("cache belum dibuat (scripts/build_reflex_cache.py)")
    hilang = [ln for ln in rf.ALL_LINES if not (cache / rf.cache_name(ln)).exists()]
    assert not hilang, f"{len(hilang)} kalimat belum disintesis: {hilang[:3]}"
    for line in rf.ALL_LINES[:8]:
        data, sr = sf.read(cache / rf.cache_name(line))
        amp = np.abs(data if data.ndim == 1 else data.mean(axis=1))
        lead_ms = np.where(amp > amp.max() * 0.02)[0][0] / sr * 1000
        assert lead_ms <= 80, f"{line!r} hening awal {lead_ms:.0f}ms — terlalu telat"
        assert len(data) / sr <= 2.5, f"{line!r} kepanjangan untuk refleks"


# --- refleks sebagai giliran bicara mini (spek Bohan: "diselipin") --------

def test_every_reflex_mood_really_exists():
    """Regresi paling penting: nama emosi di luar EMOTION_MAP no-op TANPA
    peringatan — itu yang bikin ArtiSenyum hilang berbulan-bulan tanpa
    ketahuan, dan yang hampir terjadi lagi dengan 'kaget'."""
    import arti_expression_runtime as expr

    for cat in rf.REFLEX_LINES:
        mood = rf.mood_for(cat)
        assert mood in expr.EMOTION_MAP, f"{cat} -> {mood!r} tidak ada di EMOTION_MAP"
    assert rf.REFLEX_MOOD_PLAYER_HIT in expr.EMOTION_MAP


def test_player_punch_reads_as_annoyed_not_confused():
    """Dipukul Bohan itu kesal, dipukul mob itu kaget — datanya sudah ada
    sejak penyerang diambil dari paket damage_event."""
    assert rf.mood_for("kaget", {"source_kind": "player"}) == "marah"
    assert rf.mood_for("kaget", {"source_kind": "creeper"}) == "bingung"
    assert rf.mood_for("kaget", None) == "bingung"
    # kelompok lain tidak ikut berubah walau penyerangnya pemain
    # Dibunuh Bohan = kesal, bukan sedih (killer_kind dulu tak pernah dibaca)
    assert rf.mood_for("mati", {"killer_kind": "player"}) == "marah"
    assert rf.mood_for("mati", {"killer_kind": "hostile"}) == "sedih"


def test_reflex_worker_full_sequence_and_no_stuck_state():
    """aware -> lampu+mood -> default, dan JANGAN bereskan kalau turn
    sungguhan sudah mengambil alih (lampunya milik turn itu)."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    i = src.index("def _reflex_worker")
    blok = src[i:src.index("\ndef ", i + 10)]
    # Cari POLA PEMANGGILAN, bukan kata lepas — komentar di kode ikut memuat
    # nama fungsinya dan dulu membuat urutan terbaca terbalik.
    urut = [
        blok.index("trigger_expression_state(arti_reflex.REFLEX_VTS_STATE)"),
        blok.index("apply_speaking(vts, mood"),
        blok.index("sd.play("),
        # Pembersih wajah kini fungsi bersarang (keputusannya harus DI DALAM
        # coroutine), jadi yang diukur pemanggilannya di finally — bukan
        # letak definisinya, yang memang ada di atas.
        blok.index("_jadwalkan(_bereskan_wajah())"),
    ]
    assert urut == sorted(urut), "urutan aware -> bicara+mood -> bunyi -> default"
    # Pagar anti-rebut: HANYA "sedang bicara sekarang". Cek generasi TTS sempat
    # dipakai dan justru salah arah — turn yang mulai DAN selesai selagi
    # refleks berjalan membuat refleks menolak membereskan wajahnya sendiri.
    assert "tts_play_generation" not in blok
    # Pagarnya kini DI DALAM `_bereskan_wajah` (dievaluasi saat coroutine
    # benar-benar jalan, bukan saat dijadwalkan) — lihat
    # test_face_cleanup_decided_inside_coroutine di test_minecraft_audit_fixes.
    assert "if tts_is_playing or _brain_busy:" in blok
    # Ekspresi TIDAK BOLEH ditunggu hasilnya (dulu .result(timeout=3) x2 bikin
    # refleks telat sampai 6 dtk saat loop utama sibuk).
    # Dibaca lewat AST, bukan teks: komentar/docstring di fungsi ini justru
    # MENYEBUT .result() untuk menjelaskan kenapa dilarang, dan pemeriksaan
    # berbasis string tiga kali tertipu oleh itu.
    import ast

    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_reflex_worker"
    )
    menunggu = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "result"
    ]
    assert not menunggu, "menunggu coroutine = refleks tidak instan lagi"
    # Bunyi wajib dicek ulang persis sebelum diputar (stream audio global).
    # Diiris dari awal jalur putar supaya tidak tertipu pagar `_bereskan_wajah`
    # di atasnya — yang MEMANG masih menyebut _brain_busy, dengan tujuan lain.
    jalur = blok[blok.index("data, samplerate = sf.read"):]
    assert jalur.index("if tts_is_playing:") < jalur.index("sd.play(")
    # ...dan pagar itu TIDAK boleh ikut melihat _brain_busy: selagi Arti mikir
    # refleks memang harus bunyi (itu inti fiturnya); tabrakan audio dicegah
    # _audio_lock. Lihat test_one_reflex_per_turn_not_total_silence.
    # (baris komentar dibuang dulu — komentar di situ JUSTRU menyebut
    # _brain_busy untuk menjelaskan kenapa tidak dipakai; pemeriksaan berbasis
    # string sudah beberapa kali tertipu pola ini.)
    kode = [b for b in jalur[:jalur.index("sd.play(")].splitlines()
            if not b.strip().startswith("#")]
    assert not any("_brain_busy" in b for b in kode)


def test_death_and_panic_can_pierce_the_gap():
    """Regresi audit 2026-08-05: bot mengirim hurt -> low_health -> death dalam
    milidetik yang SAMA; jeda tunggal 3 dtk bikin 'Ish!' menang dan 14 dari 50
    kalimat (panik+mati) jadi kode mati — momen paling dramatis dapat celetukan
    receh."""
    lim = rf.ReflexLimiter()
    cfg = {"reflex_min_gap_sec": 3.0}
    rf.mark_reacted(lim, 100.0)                     # baru saja "Aduh!"
    assert rf.should_react(lim, 100.001, cfg, "kaget") is False
    assert rf.should_react(lim, 100.001, cfg, "mati") is True
    assert rf.should_react(lim, 100.001, cfg, "panik") is True
    assert rf.should_react(lim, 100.001, cfg, "bahaya") is False


def test_reflex_silences_mic_and_ear_without_dropping_reactions():
    """Refleks harus membungkam mic & telinga, TAPI tidak boleh memakai
    tts_is_playing — itu akan membuang trigger reaksi untuk event yang sama."""
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert "not tts_is_playing and not _reflex_playing" in src, "gerbang mic"
    assert "tts_is_playing or _reflex_playing" in src, "gerbang telinga desktop"
    # queue_voice_trigger TIDAK boleh ikut melihat _reflex_playing
    i = src.index("def queue_voice_trigger")
    assert "_reflex_playing" not in src[i:i + 3000]


def test_note_written_only_after_sound_and_consumed_once(monkeypatch):
    import time as _t
    # ditulis sesudah bunyi -> _play_reflex sendiri tidak boleh menulisnya
    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    i, j = src.index("def _play_reflex"), src.index("def _reflex_worker")
    assert "_reflex_note = arti_reflex.note_for_llm" not in src[i:j]
    akhir = src.index("\ndef ", j + 10)
    assert "_reflex_note = arti_reflex.note_for_llm" in src[j:akhir]
    # sekali pakai
    monkeypatch.setattr(bridge, "_reflex_note", '(teriak "Ah!")')
    monkeypatch.setattr(bridge, "_reflex_note_ts", _t.time())
    assert bridge._reflex_context_note() != ""
    assert bridge._reflex_context_note() == "", "catatan menempel di turn berikutnya"


# --- sumber lingkungan & prioritas (Bohan 2026-08-07) --------------------

def test_environment_damage_counts_as_a_source():
    """"[recorded remark omitted]" —
    jatuh dan lava BUKAN "tidak diketahui", itu kejadian yang berbeda."""
    lim, t = rf.ReflexLimiter(), 100.0
    assert rf.sumber_serangan(
        {"ev": "hurt", "source": "unknown", "damage_type": "fall"}) == "fall"
    assert rf.sumber_baru(
        {"ev": "hurt", "source": "unknown", "damage_type": "fall"}, lim, t) is True
    assert rf.sumber_baru(
        {"ev": "hurt", "source": "unknown", "damage_type": "fall"}, lim, t + 1) is False
    assert rf.sumber_baru(
        {"ev": "hurt", "source": "unknown", "damage_type": "lava"}, lim, t + 2) is True
    assert rf.sumber_baru(
        {"ev": "hurt", "source": "unknown", "damage_type": "magic"}, lim, t + 3) is True


def test_attacker_name_wins_over_damage_type():
    """Panah skeleton = "skeleton", bukan "arrow" — yang penting siapa, bukan
    lewat apa, kalau pelakunya diketahui."""
    assert rf.sumber_serangan(
        {"ev": "hurt", "source": "skeleton", "damage_type": "arrow"}) == "skeleton"


def test_truly_unknown_damage_has_no_source():
    for ev in ({"ev": "hurt", "source": "unknown", "damage_type": ""},
               {"ev": "hurt", "source": "unknown"}, {"ev": "hurt"}):
        assert rf.sumber_serangan(ev) == "", ev


def test_priority_order_is_explicit():
    assert rf.prioritas("mati") < rf.prioritas("panik") < rf.prioritas("bahaya")
    assert rf.prioritas("bahaya") < rf.prioritas("sakit") < rf.prioritas("kaget")
    assert rf.prioritas("ngawur") > rf.prioritas("kaget")


def test_more_important_reflex_may_cut_the_queue():
    """Tanpa ini "Ish!" (kaget) menelan "Mundur mundur!" (bahaya) hanya karena
    datang 1 detik lebih dulu."""
    cfg = {"reflex_min_gap_sec": 3.0}
    lim = rf.ReflexLimiter()
    rf.mark_reacted(lim, 100.0, "kaget")
    assert rf.should_react(lim, 101.5, cfg, "kaget") is False
    assert rf.should_react(lim, 101.5, cfg, "bahaya") is True
    # tapi tidak boleh menempel — dua bunyi 0,3 dtk terdengar seperti satu
    # suara yang pecah
    assert rf.should_react(lim, 100.3, cfg, "bahaya") is False
    # dan yang KURANG penting tetap menunggu gilirannya
    lim2 = rf.ReflexLimiter()
    rf.mark_reacted(lim2, 100.0, "bahaya")
    assert rf.should_react(lim2, 101.5, cfg, "kaget") is False


def test_bot_reads_damage_types_from_the_server():
    """Angkanya milik server/datapack, bukan konstan — jangan dihardcode.
    Diverifikasi 2026-08-07: 49 entri, fall=10, arrow=0, magic=27."""
    src = (ROOT / "mc-bot" / "bot.js").read_text(encoding="utf-8")
    kode = [b for b in src.splitlines() if not b.strip().startswith("//")]
    gabung = "\n".join(kode)
    assert "registry_data" in gabung and "damage_type" in gabung
    assert "damage_type: tipe" in gabung, "tipe luka tidak ikut dikirim di event hurt"
    assert "damageTypes[packet.sourceTypeId]" in gabung
    # tidak ada angka id yang dipatok di kode
    assert "sourceTypeId === 10" not in gabung

"""Fitur A (v0.7): inisiatif — Arti buka topik sendiri saat hening.

SPEK FINAL (klarifikasi Bohan 2026-08-02, dua putaran):
1. Hening = 30 dtk sejak ARTI terakhir bicara (bales chat/streamer/monolog)
   DAN 5 dtk sejak streamer bersuara APAPUN di mic (termasuk omongan pasif —
   pagar anti-motong: "kalau aku lagi banyak ngomong takutnya dia motong").
2. Chat penonton yang ngobrol sendiri TIDAK ngeblok — ruangan kosong justru
   Arti yang harus banyak ngomong.
3. Cadence FLAT tiap 30 dtk (backoff_base 0 di config_local); eskalasi
   eksponensial tersedia via config untuk yang mau Arti kalem.
Semua pakai fake clock — tanpa sleep, tanpa jaringan.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_curious as ac

CFG = {"initiative_enabled": True, "initiative_backoff_base_sec": 0}


@pytest.fixture(autouse=True)
def _fresh():
    ac.reset_session()
    yield
    ac.reset_session()


def _fire(now, arti_ts=0.0, streamer_ts=0.0, **over):
    return ac.should_fire_initiative(
        {**CFG, **over},
        now=now,
        last_arti_ts=arti_ts,
        last_streamer_ts=streamer_ts,
        tts_playing=False,
        brain_busy=False,
        ptt_active=False,
    )


# --- gerbang dasar --------------------------------------------------------------


def test_disabled_by_default_in_shipped_config():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"initiative_enabled": False,' in src, "kill switch wajib default OFF"
    for key in ("initiative_quiet_sec", "initiative_streamer_gap_sec",
                "initiative_backoff_base_sec", "initiative_backoff_max_sec"):
        assert f'"{key}"' in src


def test_off_flag_never_fires():
    assert _fire(1000.0, initiative_enabled=False) is False


def test_busy_gates_block():
    for kw in ({"tts_playing": True}, {"brain_busy": True}, {"ptt_active": True}):
        base = dict(tts_playing=False, brain_busy=False, ptt_active=False)
        base.update(kw)
        assert ac.should_fire_initiative(
            CFG, now=1000.0, last_arti_ts=0.0, last_streamer_ts=0.0, **base
        ) is False, kw


# --- dua jam hening (spek final) --------------------------------------------------


def test_needs_30s_since_arti_last_spoke():
    assert _fire(now=1029.0, arti_ts=1000.0) is False, "Arti baru diam 29 dtk"
    assert _fire(now=1030.0, arti_ts=1000.0) is True


def test_streamer_talking_blocks_even_if_arti_long_quiet():
    """Bohan lagi cerita panjang -> Arti JANGAN motong, walau dia sendiri
    sudah diam 10 menit. Cukup 5 dtk jeda napas streamer."""
    assert _fire(now=1600.0, arti_ts=1000.0, streamer_ts=1597.0) is False, (
        "streamer baru bersuara 3 dtk lalu"
    )
    assert _fire(now=1600.0, arti_ts=1000.0, streamer_ts=1594.0) is True


def test_viewer_chat_does_not_block():
    """Gate tidak menerima jam chat penonton sama sekali — ngobrol antar
    penonton bukan alasan Arti bungkam."""
    import inspect

    params = inspect.signature(ac.should_fire_initiative).parameters
    assert "last_streamer_ts" in params and "last_arti_ts" in params
    assert "last_activity_ts" not in params, (
        "jam aktivitas-umum sudah dicabut dari spek final"
    )


def test_flat_cadence_every_30s_when_room_dead():
    """'kalau lagi kosong banget, harusnya si arti yang banyak ngomong'."""
    assert _fire(now=1030.0, arti_ts=1000.0) is True
    ac.mark_initiative_fired(1030.0)
    # dia selesai monolog jam 1040 -> 30 dtk kemudian boleh lagi, terus-menerus
    assert _fire(now=1070.0, arti_ts=1040.0) is True
    ac.mark_initiative_fired(1070.0)
    assert _fire(now=1095.0, arti_ts=1080.0) is False, "belum 30 dtk sejak dia bicara"
    assert _fire(now=1110.0, arti_ts=1080.0) is True


def test_backoff_available_when_configured():
    cfg_over = {"initiative_backoff_base_sec": 180.0}
    ac.mark_initiative_fired(1030.0)
    assert _fire(now=1100.0, arti_ts=1040.0, **cfg_over) is False, (
        "streak 1 + backoff 180 -> belum boleh walau sudah 30 dtk hening"
    )
    assert _fire(now=1030.0 + 181.0, arti_ts=1040.0, **cfg_over) is True


# --- build_initiative_prompt ---------------------------------------------------


def test_prompt_uses_memory_material_deterministically():
    rolls = iter([0.0, 0.0])
    p = ac.build_initiative_prompt(
        {}, memory_bullets=["- [2026-07-21] Streamer suka nasi goreng"],
        rng=lambda: next(rolls),
    )
    assert "nasi goreng" in p
    # Persona 2026-08-03: pertanyaan penutup OPSIONAL, bukan kewajiban SATU.
    assert "OPSIONAL" in p and "pendirian" in p
    assert "[Inisiatif" in p


def test_prompt_can_pick_present_viewer():
    rolls = iter([0.0, 0.0, 0.9])
    p = ac.build_initiative_prompt(
        {},
        memory_bullets=["- [2026-07-21] fakta"],
        present_viewers=["@penontonsetia241"],
        rng=lambda: next(rolls),
    )
    assert "@penontonsetia241" in p


def test_prompt_fallback_when_no_material():
    p = ac.build_initiative_prompt({}, rng=lambda: 0.5)
    assert "penasaran" in p.lower()
    assert "[Inisiatif" in p


def test_prompt_never_mentions_system_terms():
    p = ac.build_initiative_prompt(
        {}, memory_bullets=["- [2026-07-21] fakta"], rng=lambda: 0.0
    )
    assert "Jangan menyebut sistem" in p


# --- wiring bridge --------------------------------------------------------------


def test_bridge_wires_initiative_block():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    idx_block = src.index("arti_curious.should_fire_initiative(")
    idx_queue = src.index('trigger_type="curious"', idx_block)
    assert idx_queue - idx_block < 1500
    assert "is_vision_active()" not in src[idx_block - 400:idx_block], (
        "blok inisiatif TIDAK boleh di dalam gate vision"
    )
    # dua jam spek final tersambung
    assert "last_arti_ts=_last_arti_reply_ts" in src
    assert "last_streamer_ts=_last_streamer_speech_ts" in src
    # jam streamer di-update untuk SEMUA ucapan streamer (termasuk pasif)
    assert "_last_streamer_speech_ts = time.time()" in src
    # masa tenang 30 dtk setelah SIAP (Arti "dianggap baru bicara")
    siap = src.index("SISTEM SIAP!")  # baris print di main_loop (komentar tanpa '!')
    assert "_last_arti_reply_ts = time.time()" in src[siap - 800:siap]


def test_viewer_presence_recorded_outside_queue_mode(monkeypatch):
    """Bug crosscheck v0.7: dict presence lama hanya terisi di mode voice_queue
    (OFF) -> bahan 'sapa penonton hadir' selamanya kosong."""
    import hermes_vtuber_bridge as b

    monkeypatch.setattr(b.session_transcript, "append_from_history",
                        lambda *a, **k: None)
    b._yt_viewers_seen.clear()

    b.add_to_history("Viewer @tester123 (YouTube)", "halo arti")
    assert "@tester123" in b._yt_viewers_seen
    assert "@tester123" in b._initiative_materials()["present_viewers"]

    b.add_to_history("Viewer @Streamlabs (YouTube)", "Top Total Jam nonton")
    assert "@Streamlabs" not in b._yt_viewers_seen

    b._yt_viewers_seen.clear()


def test_failed_proactive_turn_goes_silent_not_canned():
    """Inisiatif gagal total pernah jatuh ke kalimat kaleng 'ulang pertanyaannya
    dong?' padahal tidak ada yang bertanya."""
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert src.count('return None, [], "skip-curious"') == 2
    assert "trigger_type=trigger.trigger_type" in src


def test_kapan_alone_is_not_temporal():
    """'kapan arti debut' = pertanyaan SEJARAH — boost recency sempat
    menenggelamkan kanon (WARN health check 2026-08-02)."""
    import arti_vault_rag as rag

    assert rag.is_temporal_query("kapan arti debut?") is False
    assert rag.is_temporal_query("kapan kamu dibuat") is False
    assert rag.is_temporal_query("kapan terakhir kita ngobrol?") is True


def test_dead_turn_does_not_flood():
    """Regresi live 2026-08-02 sore: turn inisiatif MATI (NameError jendela Q&A)
    -> Arti tidak pernah bicara -> arti_ts beku -> gate quiet lolos terus ->
    nembak tiap tick main loop (180x dalam 5 menit). Cadence flat wajib
    berjarak quiet_sec dari TEMBAKAN terakhir, bukan cuma dari omongan Arti."""
    assert _fire(now=1030.0, arti_ts=1000.0) is True
    ac.mark_initiative_fired(1030.0)
    # turn crash — arti_ts tidak pernah maju dari 1000
    assert _fire(now=1032.0, arti_ts=1000.0) is False, "2 dtk setelah nembak: rem!"
    assert _fire(now=1055.0, arti_ts=1000.0) is False
    assert _fire(now=1061.0, arti_ts=1000.0) is True, "30 dtk berlalu — boleh lagi"


# --- bahan memori: saring meta + wajib konteks sesi lama (live sore2 2/8) -------


def test_meta_learning_bullets_filtered_from_material():
    """Junk kurator ("Sistem mendorong inisiatif...") tidak boleh jadi topik —
    live sore2: Arti tiba-tiba bahas memori lama tanpa konteks, viewer bingung."""
    assert ac.is_meta_learning_bullet(
        "- [2026-08-02] Stream fact: Sistem mendorong inisiatif mengangkat "
        "fakta throttle per mesin pesawat dari catatan 2026-08-01."
    )
    assert ac.is_meta_learning_bullet(
        "- [2026-08-02] Stream fact: Arti menyebut lampu neon saat inisiatif."
    )
    assert not ac.is_meta_learning_bullet(
        "- [2026-08-01] Bohan kena copyright claim audio The Great Sea."
    )
    # 2026-08-03: kurator nyimpen dapur bridge sebagai "stream fact"
    assert ac.is_meta_learning_bullet(
        "- [2026-08-03] Stream fact: Arti mengutip kebiasaan mengabaikan kata "
        "'you'/'thank you' sendirian di speaker sebagai noise Whisper ASR."
    )
    assert ac.is_meta_learning_bullet(
        "- [2026-08-03] Stream fact: Arti menyinggung bridge session sempat "
        "terbuka dua kali di stream yang sama."
    )
    # semua bullet meta -> bahan memori kosong -> jatuh ke bahan bebas
    p = ac.build_initiative_prompt(
        {},
        memory_bullets=["- [2026-08-02] Sistem mendorong inisiatif angkat fakta X"],
        rng=lambda: 0.0,
    )
    assert "Sistem mendorong" not in p
    assert "Kenangan" not in p


def test_memory_material_demands_old_session_context():
    """Bahan sesi lama wajib disebut asalnya ('pas live kemarin...') TANPA
    tanggal — Bohan: "kemarin kamu ngomongin apa... aku nggak tahu deh"."""
    p = ac.build_initiative_prompt(
        {},
        memory_bullets=['- [2026-08-01] "The Holiday" (2006), lagu Celine Dion'],
        rng=lambda: 0.0,
    )
    assert "SESI LAMA" in p
    assert "TANPA tanggal" in p
    assert "The Holiday" in p


def test_material_not_repeated_within_session():
    """Anti "arti looping" (live sore3 2/8: topik aviasi Rafi diangkat 2x dalam
    8 menit): bahan yang sudah dipakai sesi ini tidak dipilih lagi."""
    ac.reset_session()
    bullet = "- [2026-08-01] Dewi-radio108 ngomongin mekanika pesawat"
    p1 = ac.build_initiative_prompt({}, memory_bullets=[bullet], rng=lambda: 0.0)
    assert "mekanika pesawat" in p1
    # bullet sama ditawarkan lagi -> harus DITOLAK, jatuh ke bahan bebas
    p2 = ac.build_initiative_prompt({}, memory_bullets=[bullet], rng=lambda: 0.0)
    assert "mekanika pesawat" not in p2
    # viewer juga dilacak
    p3 = ac.build_initiative_prompt({}, present_viewers=["@rafi"], rng=lambda: 0.0)
    assert "@rafi" in p3
    p4 = ac.build_initiative_prompt({}, present_viewers=["@rafi"], rng=lambda: 0.0)
    assert "@rafi" not in p4
    # reset sesi membuka lagi
    ac.reset_session()
    p5 = ac.build_initiative_prompt({}, memory_bullets=[bullet], rng=lambda: 0.0)
    assert "mekanika pesawat" in p5


def test_echo_mode_legacy_meta_sentence_removed():
    """Live pagi 2026-08-03 — misteri "halo aku membaca 50 catatan livestream
    kamu" TERPECAHKAN: bukan model bocor, tapi kalimat HARDCODED echo-mode
    zaman awal bridge yang bunyi tiap semua provider gagal (termasuk turn
    proaktif yang dijanjikan diam). Wajib: kalimat itu HILANG, proaktif diam
    beneran, panggilan langsung dapat fallback in-character."""
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "catatan sejarah stream kamu, dan mendengar" not in src, (
        "kalimat echo legacy harus dihapus permanen"
    )
    i = src.index("[Echo Mode + History Context]")
    seg = src[i:i + 1400]
    assert '"[Curious", "[Inisiatif"' in seg, "guard proaktif di branch echo"
    assert "incharacter_fallback_reply" in seg, "panggilan langsung != bisu"


def test_material_same_topic_not_repeated():
    """Live pagi 2026-08-03: inisiatif #2 & #3 dua-duanya soal nasi — bullet
    beda teks, topik sama (kurator bikin varian duplikat). Cek level topik."""
    ac.reset_session()
    b1 = "- [2026-08-02] Stream fact: ada nasi virtual buat nge-track poin, cek pake !nasi"
    b2 = "- [2026-08-02] Stream fact: gimmick ngumpulin nasi lewat !nasi kayak mini game"
    p1 = ac.build_initiative_prompt({}, memory_bullets=[b1], rng=lambda: 0.0)
    assert "nasi" in p1
    p2 = ac.build_initiative_prompt({}, memory_bullets=[b2], rng=lambda: 0.0)
    assert "nasi" not in p2, "topik sama tidak boleh diangkat dua kali sesesi"
    # topik lain tetap boleh
    p3 = ac.build_initiative_prompt(
        {}, memory_bullets=["- [2026-08-01] Bohan suka pixel art gaya retro"],
        rng=lambda: 0.0,
    )
    assert "pixel art" in p3

# --- backoff semua-provider-gagal (live seharian 2026-08-03: 80x skip) ----------


def test_provider_fail_backoff_blocks_initiative():
    """Groq 429 + Cursor tutup -> inisiatif jangan nembak lagi tiap 30 dtk."""
    assert _fire(now=1030.0, arti_ts=1000.0, streamer_ts=0.0) is True
    assert ac.should_fire_initiative(
        CFG, now=1030.0, last_arti_ts=1000.0, last_streamer_ts=0.0,
        tts_playing=False, brain_busy=False, ptt_active=False,
        provider_fail_until=1330.0,
    ) is False, "masih di masa rehat pasca provider gagal total"
    assert ac.should_fire_initiative(
        CFG, now=1331.0, last_arti_ts=1000.0, last_streamer_ts=0.0,
        tts_playing=False, brain_busy=False, ptt_active=False,
        provider_fail_until=1330.0,
    ) is True, "rehat lewat -> boleh nyoba lagi"


def test_bridge_notes_provider_fail_and_wires_backoff(monkeypatch):
    import time as _time

    import hermes_vtuber_bridge as b

    monkeypatch.setattr(b, "_init_provider_fail_until", 0.0)
    b._note_curious_provider_fail({"initiative_provider_fail_backoff_sec": 300.0})
    assert b._init_provider_fail_until > _time.time() + 250

    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert src.count("_note_curious_provider_fail(cfg)") == 2, (
        "kedua situs skip-curious (sesi dingin & fallback gagal) wajib mencatat"
    )
    i = src.index("arti_curious.should_fire_initiative(")
    assert "provider_fail_until=_init_provider_fail_until" in src[i:i + 600], (
        "main loop harus meneruskan masa rehat ke gate"
    )

# --- detektor kehidupan + persona mandiri (permintaan Bohan 2026-08-03) ---------


def test_is_dormant_after_long_human_silence():
    """~1 jam nol viewer -> Arti monolog terus. Kini: X dtk tanpa chat/mic
    manusia = proaktif tidur; timestamp maju (chat masuk) = bangun."""
    cfg = {"initiative_dormant_after_idle_sec": 600.0}
    assert ac.is_dormant(cfg, now=2000.0, last_human_ts=1000.0) is True
    assert ac.is_dormant(cfg, now=1500.0, last_human_ts=1000.0) is False
    assert ac.is_dormant(cfg, now=2000.0, last_human_ts=0.0) is False, (
        "belum ada data (startup) jangan blokir"
    )
    assert ac.is_dormant(
        {"initiative_dormant_after_idle_sec": 0}, now=9e9, last_human_ts=1.0
    ) is False, "<=0 = fitur mati (perilaku lama)"


def test_dormant_blocks_should_fire_initiative():
    assert ac.should_fire_initiative(
        CFG, now=2000.0, last_arti_ts=1000.0, last_streamer_ts=0.0,
        tts_playing=False, brain_busy=False, ptt_active=False,
        last_human_ts=1000.0,
    ) is False, "ruangan mati total = tidur"
    assert ac.should_fire_initiative(
        CFG, now=2000.0, last_arti_ts=1000.0, last_streamer_ts=0.0,
        tts_playing=False, brain_busy=False, ptt_active=False,
        last_human_ts=1900.0,
    ) is True, "ada tanda kehidupan 100 dtk lalu = normal"


def test_boring_screen_hook_detected():
    """Background hitam + mic mute: layar gelapnya sendiri jangan jadi topik."""
    for boring in (
        "",
        "Layar hanya menampilkan latar belakang hitam dengan garis misterius",
        "layar gelap tanpa aktivitas",
        "Layar kosong, tidak menampilkan apa-apa",
    ):
        assert ac.is_boring_screen_hook(boring), boring
    for ok in (
        "Sketsa Cakrawala SMP setengah jadi di kanvas",
        "Minecraft: rumah Kuartee kebakar",
    ):
        assert not ac.is_boring_screen_hook(ok), ok


def test_initiative_materials_drop_boring_hook():
    import hermes_vtuber_bridge as b

    old = b.CONFIG.get("scouter_last_result")
    try:
        b.CONFIG["scouter_last_result"] = {
            "summary": "ringkasan", "curious_hook": "layar gelap membosankan"
        }
        assert b._initiative_materials()["screen_hook"] == ""
        b.CONFIG["scouter_last_result"] = {
            "summary": "s", "curious_hook": "Minecraft rumah kebakar"
        }
        assert b._initiative_materials()["screen_hook"] == "Minecraft rumah kebakar"
    finally:
        b.CONFIG["scouter_last_result"] = old


def test_bridge_wires_dormancy_to_both_proactive_paths():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "last_human_ts=_last_human_activity_ts" in src
    i = src.index("arti_curious.is_dormant(")
    seg = src[i:i + 2500]
    j = seg.index('CONFIG.get("curious_enabled")')
    curious_gate = seg[j:j + 300]
    assert "not _dormant_now" in curious_gate, "jalur curious layar wajib ikut tidur"
    assert "_init_provider_fail_until" in curious_gate, (
        "audit 3/8: backoff provider wajib memagari curious layar juga"
    )
    assert "and not _dormant_now" in src[src.index('CONFIG.get("initiative_enabled"'):][:300], (
        "jalur inisiatif wajib ikut tidur"
    )


def test_persona_no_mandatory_question_to_streamer():
    """'nanya bohan mulu, kayak gapunya pendirian' — kewajiban 1-pertanyaan
    dicabut dari SEMUA prompt proaktif; opini jadi instruksi utama."""
    src_c = (ROOT / "arti_curious.py").read_text(encoding="utf-8")
    src_v = (ROOT / "arti_voice_pipeline.py").read_text(encoding="utf-8")
    for src, name in ((src_c, "arti_curious"), (src_v, "arti_voice_pipeline")):
        assert "tepat SATU pertanyaan" not in src, name
        assert "tepat satu pertanyaan" not in src, name
    assert "1 pertanyaan di akhir" not in src_v
    p = ac.build_initiative_prompt({}, memory_bullets=["- [2026-07-21] fakta"], rng=lambda: 0.0)
    assert "OPSIONAL" in p and "pendirian" in p
    addon = ac.build_curious_system_addon({})
    assert "TIDAK wajib" in addon

# --- bahan sapaan penonton baru (telemetri jumlah penonton, 2026-08-03) ----------


def test_join_note_is_top_weight_material():
    note = "Barusan ada yang masuk nonton (penonton naik ke 4). Sapa santai."
    p = ac.build_initiative_prompt(
        {}, viewer_join_note=note, rng=lambda: 0.0
    )
    assert note in p


def test_join_note_not_repeated_for_same_event():
    note = "Barusan ada yang masuk nonton (penonton naik ke 5). Sapa santai."
    p1 = ac.build_initiative_prompt({}, viewer_join_note=note, rng=lambda: 0.0)
    assert note in p1
    p2 = ac.build_initiative_prompt(
        {}, viewer_join_note=note,
        memory_bullets=["- [2026-07-21] fakta lain"], rng=lambda: 0.0,
    )
    assert note not in p2, "event kenaikan yang sama jangan disapa dua kali"


# --- audit lanjutan 2026-08-03: wake_word & dedup hook ---------------------------


def test_wake_word_counts_as_life_sign(monkeypatch):
    """Mode wake tidak pernah lewat add_to_history("Streamer") — tanpa bump
    di queue_voice_trigger, Bohan ngobrol via wake word tetap kena dormansi."""
    import time as _t

    import hermes_vtuber_bridge as b

    for ttype in ("wake_word", "ptt", "mic", "yt_chat", "donation", "video"):
        monkeypatch.setattr(b, "_last_human_activity_ts", 0.0)
        monkeypatch.setattr(b, "_last_streamer_speech_ts", 0.0)
        monkeypatch.setattr(b, "_brain_busy", True)  # trigger di-DROP
        b.queue_voice_trigger("halo arti", trigger_type=ttype)
        assert b._last_human_activity_ts > _t.time() - 5, (
            f"{ttype} wajib menghidupkan jam kehidupan walau trigger di-drop"
        )
        if ttype in ("wake_word", "ptt", "mic"):
            assert b._last_streamer_speech_ts > _t.time() - 5, (
                f"{ttype} = streamer bersuara (pagar anti-motong)"
            )

    # ucapan Arti sendiri / turn proaktif BUKAN tanda kehidupan
    monkeypatch.setattr(b, "_last_human_activity_ts", 0.0)
    b.queue_voice_trigger("[Inisiatif]", trigger_type="curious")
    assert b._last_human_activity_ts == 0.0


def test_curious_hook_dedup_actually_records(monkeypatch):
    """mark_fired() dipanggil TANPA CONFIG = _recent_hooks selamanya kosong =
    dedup 'hook terlalu mirip' mati (audit 3/8)."""
    import hermes_vtuber_bridge as b

    ac.reset_session()
    monkeypatch.setitem(
        b.CONFIG, "scouter_last_result", {"curious_hook": "sketsa Cakrawala di kanvas"}
    )
    ac.mark_fired(b.CONFIG)
    assert ac._recent_hooks, "hook wajib tercatat untuk dedup"

    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "arti_curious.mark_fired(CONFIG)" in src
    assert "arti_curious.mark_fired()" not in src

"""Tests for arti_memory_quality."""
import arti_memory_quality as mq


def test_sanitize_strips_thinking_blocks():
    tag = "think"
    raw = f"<{tag}>chain of thought here</{tag}>\nRingkasan bersih dalam bahasa Indonesia."
    out = mq.sanitize_model_text(raw)
    assert "chain of thought" not in out
    assert "Ringkasan bersih" in out


def test_should_save_learning_rejects_noise():
    assert not mq.should_save_learning("tidak ditemukan")
    assert not mq.should_save_learning("short")
    assert mq.should_save_learning("Bohan suka nasi goreng pedas level 3")


def test_append_not_duplicate():
    assert mq.is_duplicate_learning(
        "Reflection: Arti suka membantu stream",
        ["- [2026-06-01] Reflection: Arti suka membantu stream"],
    )


def test_filter_memories_for_startup_today_only():
    mems = [
        "- [2026-06-03] lama",
        "- [2026-06-04] hari ini satu",
        "- [2026-06-04] hari ini dua",
    ]
    out = mq.filter_memories_for_startup(mems, today="2026-06-04")
    assert len(out) == 2
    assert all("[2026-06-04]" in m for m in out)


# --- Dedup parafrase (ditambahkan 2026-07-31) --------------------------------
#
# Sebelum ini gerbang duplikat cuma exact-match + substring, jadi buta terhadap
# parafrase. Terbukti: 6 varian "Arti debut co-host 27 Mei 2026" lolos SEMUA karena
# sisipan kata di tengah ("pada", "tanggal", "adalah") memutus uji substring. Itulah
# yang membuat vault menumpuk jadi 60 entri dengan 28 di antaranya duplikat.

_DEBUT_VARIAN = [
    "Arti debut co-host pada 27 Mei 2026",
    "Arti debut co-host 27 Mei 2026",
    "Arti adalah co-host yang debut pada 27 Mei 2026",
    "debut co-host jatuh pada 27 Mei 2026",
    "Debut co-host pada tanggal 27 Mei 2026",
    "Arti debut co-host adalah 27 Mei 2026",
]

# Fakta yang benar-benar berbeda. Kemiripan token TERTINGGI di antara mereka terukur
# 0,154 pada data nyata — ambang 0,35 memberi jarak aman 2,3x.
_FAKTA_BERBEDA = [
    "Streamer punya sisa utang in-game yang belum lunas di episode 1",
    "Streamer menulis skrip Minecraft episode 2 di Google Docs",
    "Scene pembuka (cold open) EP2 berlatar di gubuk saat malam hujan deras",
    "Map Minecraft sudah menampilkan area lapangan sekolah",
    "Teaser proyek Minecraft sudah dirilis setahun sebelum EP2 digarap",
    "Streamer memotivasi diri bekerja supaya bisa membeli game Fable 5",
    "Gemini Flash memiliki kecepatan API yang sangat tinggi untuk tugas coding",
    "Streamer memprediksi masa depan industri AI akan berfokus pada energi terbarukan",
]


def _simulate_accumulate(facts):
    """Tiru cara runtime menyimpan: tiap fakta diuji terhadap yang sudah tersimpan."""
    import arti_memory_quality as mq

    kept = []
    for f in facts:
        if mq.should_save_learning(f, kept):
            kept.append(f"- [2026-07-22] {f}")
    return kept


def test_dedup_catches_paraphrases():
    kept = _simulate_accumulate(_DEBUT_VARIAN)
    assert len(kept) == 1, f"6 varian fakta identik harus jadi 1, dapat {len(kept)}"


def test_dedup_does_not_merge_distinct_facts():
    """Yang paling berbahaya: fakta sah ditolak diam-diam dan hilang selamanya."""
    kept = _simulate_accumulate(_FAKTA_BERBEDA)
    assert len(kept) == len(_FAKTA_BERBEDA), (
        f"fakta berbeda ikut tertolak: {len(_FAKTA_BERBEDA)} -> {len(kept)}"
    )


def test_fact_similarity_ignores_stopwords_and_order():
    import arti_memory_quality as mq

    a = "Arti debut co-host pada 27 Mei 2026"
    b = "debut co-host jatuh pada 27 Mei 2026"
    assert mq.fact_similarity(a, b) >= 0.35
    assert mq.fact_similarity("kucing lucu sekali", "mobil cepat sekali") < 0.35
    assert mq.fact_similarity("", "apa pun") == 0.0


def test_dedup_threshold_is_tunable():
    import arti_memory_quality as mq

    a, b = _DEBUT_VARIAN[0], _DEBUT_VARIAN[3]
    line = [f"- [2026-07-22] {a}"]
    assert mq.is_duplicate_learning(b, line, threshold=0.35) is True
    # threshold=0 mematikan lapis kemiripan -> kembali ke perilaku lama
    assert mq.is_duplicate_learning(b, line, threshold=0) is False


# --- Fungsi yang sebelumnya NOL test -----------------------------------------


def test_list_learning_bullets_reads_only_memory_section(tmp_path):
    import arti_memory_quality as mq

    text = (
        "# Judul\n\nBasa-basi.\n\n"
        "## Memori Jangka Panjang\n\n"
        "- [2026-07-22] fakta satu\n"
        "- [2026-07-22] fakta dua\n\n"
        "## Seksi Lain\n\n"
        "- [2026-07-22] JANGAN ikut terbaca\n"
    )
    bullets = mq.list_learning_bullets(text)
    assert len(bullets) == 2
    assert all("JANGAN" not in b for b in bullets)


def test_append_learning_writes_and_keeps_newest(tmp_path):
    """append_learning adalah PENULIS BERKAS dan sebelumnya nol test.

    Yang dikunci di sini: cap menyimpan entri TERBARU (`bullets[-max:]`), bukan yang
    terlama. Arah ini pernah terbalik di scripts/prune_vault_memory.py baris 62
    (`kept[:60]`), yang artinya memori terbaru justru dibuang saat cap tercapai.
    """
    import arti_memory_quality as mq

    p = tmp_path / "learn.md"
    p.write_text("# H\n\n## Memori Jangka Panjang\n\n", encoding="utf-8")
    # Fakta yang benar-benar berbeda satu sama lain — kalau pakai "fakta nomor 0..4"
    # gerbang dedup justru menolaknya (dan memang benar: angka pendek dulu dibuang,
    # sisanya identik). Itu yang ketahuan waktu test ini pertama ditulis.
    facts = [
        "Streamer suka kopi hitam tanpa gula",
        "Penonton penontonpertama sering meragukan keaslian akun",
        "Map Minecraft menampilkan area lapangan sekolah",
        "Streamer memprediksi AI akan berfokus pada energi terbarukan",
        "Teaser proyek dirilis setahun sebelum episode digarap",
    ]
    for f in facts:
        mq.append_learning(p, f, max_bullets=3)
    bullets = mq.list_learning_bullets(p.read_text(encoding="utf-8"))
    assert len(bullets) == 3, f"cap tidak dihormati: {len(bullets)}"
    joined = " ".join(bullets)
    assert "energi terbarukan" in joined, "entri terbaru harus disimpan"
    assert "kopi hitam" not in joined, "entri terlama harus dibuang, bukan sebaliknya"


# --- Veto angka --------------------------------------------------------------


def test_different_numbers_are_never_duplicates():
    """Angka membedakan makna — EP1 vs EP2 itu lore nyata di stream ini.

    Sebelum diperbaiki, filter token `len > 2` membuang angka pendek sehingga
    "episode 1" vs "episode 2" berskor 1.000, dan "debut 27 Mei" vs "debut 28 Mei"
    juga 1.000. Veto ini mengalahkan uji kemiripan, bukan sekadar menurunkan skornya.
    """
    import arti_memory_quality as mq

    pasangan = [
        ("Streamer menggarap Minecraft episode 1", "Streamer menggarap Minecraft episode 2"),
        ("Scene cold open EP1 di hutan", "Scene cold open EP2 di gubuk"),
        ("Streamer target selesai 1 jam", "Streamer target selesai 3 jam"),
        ("Arti debut 27 Mei 2026", "Arti debut 28 Mei 2026"),
    ]
    for a, b in pasangan:
        assert mq.is_duplicate_learning(b, [f"- [2026-07-22] {a}"]) is False, (
            f"angka berbeda tapi dianggap duplikat: {a!r} vs {b!r}"
        )


def test_same_numbers_still_deduped():
    """Veto angka tidak boleh melumpuhkan dedup saat angkanya memang sama."""
    import arti_memory_quality as mq

    a = "Arti debut co-host pada 27 Mei 2026"
    b = "debut co-host jatuh pada 27 Mei 2026"
    assert mq.is_duplicate_learning(b, [f"- [2026-07-22] {a}"]) is True


def test_every_skip_substring_is_actually_rejected():
    """Sebelumnya cuma 1 dari 20 frasa saring yang teruji."""
    import arti_memory_quality as mq

    for phrase in mq._SKIP_SUBSTRINGS:
        fact = f"Stream fact: {phrase} dan tambahan kata biar panjangnya cukup"
        assert mq.should_save_learning(fact) is False, f"lolos padahal harus disaring: {phrase!r}"

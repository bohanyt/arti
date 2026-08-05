"""Vault RAG unit tests (no LM Studio required)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_vault_rag as rag


def test_embed_query_cached_reuses_vector(monkeypatch):
    calls: list[int] = []

    def fake_embed(texts, config, **kwargs):
        calls.append(1)
        return [[1.0, 0.0]]

    monkeypatch.setattr(rag, "embed_texts", fake_embed)
    rag.clear_query_embed_cache()
    cfg = dict(rag.DEFAULT_CONFIG)
    assert rag.embed_query_cached("lampu", cfg) == [1.0, 0.0]
    assert rag.embed_query_cached("lampu", cfg) == [1.0, 0.0]
    assert len(calls) == 1


def test_chunk_text_skips_junk_overlap_fragments():
    log = "[16:56:22] [Streamer] Terima kasih. " * 30
    chunks = rag.chunk_text(log, chunk_chars=200, overlap=60)
    for _, body in chunks:
        assert not rag._is_junk_chunk(body, 48)
        assert "asih." not in body or len(body) > 80


def test_chunk_text_splits_long_paragraph():
    text = "A" * 900
    chunks = rag.chunk_text(text, chunk_chars=200, overlap=20)
    assert len(chunks) >= 3
    assert all(len(c[1]) <= 220 for c in chunks)


def test_search_with_mock_embeddings(tmp_path, monkeypatch):
    db = tmp_path / "rag.db"
    cfg = {
        **rag.DEFAULT_CONFIG,
        "vault_rag_db_path": str(db),
        "vault_rag_min_score": 0.1,
    }
    rag.init_db(cfg)
    conn = rag._connect(cfg)
    conn.execute(
        """
        INSERT INTO chunks (
            source_path, source_type, folder, chunk_index, heading,
            content, content_hash, mtime, char_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "vault/concepts/test.md",
            "vault_concept",
            "vault/concepts",
            0,
            "Lampu",
            "Param178 mengatur lampu kepala Arti di VTS.",
            "hash1",
            1.0,
            40,
            1.0,
        ),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    conn.execute(
        "INSERT INTO embeddings (chunk_id, model, dim, vector) VALUES (?, ?, ?, ?)",
        (cid, "test", 3, rag._pack_vector(vec.tolist())),
    )
    rag._sync_fts(conn, cid)
    conn.commit()
    conn.close()

    def fake_embed(texts, config, **kwargs):
        if "lampu" in texts[0].lower():
            return [[1.0, 0.0, 0.0]]
        return [[0.0, 1.0, 0.0]]

    monkeypatch.setattr(rag, "embed_texts", fake_embed)
    hits = rag.search("lampu kepala", cfg, top_k=3)
    assert hits
    assert "Param178" in hits[0]["content"]


def test_format_hits_for_prompt():
    hits = [
        {
            "score": 0.9,
            "source_path": "vault/x.md",
            "source_type": "vault_concept",
            "heading": "H",
            "content": "isi penting",
        }
    ]
    out = rag.format_hits_for_prompt(hits, 500)
    assert "[VAULT RAG" in out
    assert "isi penting" in out


def test_enrich_rag_query_history():
    # Debut label sekarang config-driven (genericized) — bukan hardcoded di kode
    cfg = {"arti_debut_label": "27 Mei 2026", "arti_archive_from": "2026-05-27"}
    q = rag.enrich_rag_query("arti mulai ada sejak kapan", cfg)
    assert "27 Mei 2026" in q
    assert "vault sessions" in q


def test_enrich_rag_query_plain_unchanged():
    assert rag.enrich_rag_query("halo arti") == "halo arti"


def test_append_rag_history_fallback_injects_canon(monkeypatch):
    cfg = dict(rag.DEFAULT_CONFIG)
    cfg["vault_rag_enabled"] = True
    cfg["vault_rag_live_enabled"] = True
    cfg["vault_rag_history_min_score"] = 0.99

    monkeypatch.setattr(rag, "search", lambda q, c, top_k=None: [{"score": 0.1, "source_path": "vault/other.md", "source_type": "vault", "heading": "", "content": "x"}])
    monkeypatch.setattr(rag, "get_canon_origin_block", lambda c: "[VAULT RAG — ASAL USUL (kanon)]\nDebut 27 Mei 2026")

    out = rag.append_rag_to_system("BASE", "arti mulai sejak kapan", cfg)
    assert "ASAL USUL (kanon)" in out
    assert "27 Mei 2026" in out


def test_select_diverse_caps_per_source():
    """Satu berkas tidak boleh memonopoli slot retrieval.

    Regresi untuk temuan 2026-07-31: pemilihan murni top-k membuat 7 dari 12 query uji
    punya >=3 dari 5 slot dari satu berkas, dan query "apa yang bohan kerjain kemarin"
    mengembalikan 3 potongan arti_self_knowledge.md — nol yang menjawab pertanyaannya.
    """
    import arti_vault_rag as rag

    # a.md mendominasi skor teratas, tapi ada cukup berkas lain untuk mengisi 5 slot
    # tanpa melewati kuota — inilah kondisi nyata (573 chunk di 51 berkas, prefilter 72).
    combined = [
        (0.90, {"source_path": "a.md", "content": "a1"}),
        (0.89, {"source_path": "a.md", "content": "a2"}),
        (0.88, {"source_path": "a.md", "content": "a3"}),
        (0.87, {"source_path": "a.md", "content": "a4"}),
        (0.50, {"source_path": "b.md", "content": "b1"}),
        (0.49, {"source_path": "b.md", "content": "b2"}),
        (0.40, {"source_path": "c.md", "content": "c1"}),
        (0.39, {"source_path": "c.md", "content": "c2"}),
    ]
    hits = rag._select_diverse(combined, 5, {"vault_rag_max_per_source": 2})
    per: dict[str, int] = {}
    for h in hits:
        per[h["source_path"]] = per.get(h["source_path"], 0) + 1
    assert len(hits) == 5
    assert max(per.values()) <= 2, f"berkas masih memonopoli: {per}"
    assert len(per) == 3, f"harus menjangkau 3 berkas, dapat {per}"
    # yang tertinggal harus tetap yang skor tertinggi dari tiap berkas
    assert [h["content"] for h in hits] == ["a1", "a2", "b1", "b2", "c1"]


def test_select_diverse_fills_over_cap_rather_than_returning_fewer():
    """Kalau berkas beragamnya kurang, kuota BOLEH dilewati demi mengisi slot.

    Ini keputusan sadar: 5 potongan dengan satu berkas dapat 3 lebih berguna daripada
    4 potongan. Terpantau di uji nyata bahwa jalur ini jarang terpakai — pada 12 query
    uji, kuota selalu terpenuhi tanpa perlu melewati batas.
    """
    import arti_vault_rag as rag

    combined = [
        (0.9, {"source_path": "a.md", "content": "a1"}),
        (0.8, {"source_path": "a.md", "content": "a2"}),
        (0.7, {"source_path": "a.md", "content": "a3"}),
        (0.6, {"source_path": "b.md", "content": "b1"}),
    ]
    hits = rag._select_diverse(combined, 4, {"vault_rag_max_per_source": 2})
    assert len(hits) == 4, "lebih baik lewat kuota daripada mengembalikan lebih sedikit"
    assert [h["content"] for h in hits] == ["a1", "a2", "b1", "a3"]


def test_select_diverse_never_returns_fewer():
    """Kalau berkas beragamnya kurang, slot tetap diisi dari yang tadi dilewati."""
    import arti_vault_rag as rag

    combined = [(1.0 - i / 10, {"source_path": "solo.md", "content": str(i)}) for i in range(6)]
    hits = rag._select_diverse(combined, 5, {"vault_rag_max_per_source": 2})
    assert len(hits) == 5, f"jumlah hit berkurang jadi {len(hits)}"


def test_select_diverse_disabled_is_plain_topk():
    """max_per_source=0 harus mengembalikan perilaku lama persis."""
    import arti_vault_rag as rag

    combined = [(1.0 - i / 10, {"source_path": "a.md", "content": str(i)}) for i in range(6)]
    hits = rag._select_diverse(combined, 3, {"vault_rag_max_per_source": 0})
    assert [h["content"] for h in hits] == ["0", "1", "2"]


def test_search_by_timecode_matches_stored_paths(tmp_path, monkeypatch):
    """Regresi: pola lama `folder LIKE 'watch-parties/...'` tidak pernah cocok.

    Kolom `folder` disimpan sebagai str(Path(rel).parent) = "vault/watch-parties",
    dengan prefix "vault/". Akibatnya pencarian timecode watch-party selalu
    mengembalikan 0 hit walaupun datanya ada di index.
    """
    import sqlite3

    import arti_vault_rag as rag

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, source_path TEXT, source_type TEXT,"
        " folder TEXT, heading TEXT, content TEXT, chunk_index INTEGER)"
    )
    con.execute(
        "INSERT INTO chunks VALUES (1, 'vault/watch-parties/tadc-ep1.md', 'vault_session',"
        " 'vault/watch-parties', '[03:45] adegan', 'Pomni masuk ruangan', 0)"
    )
    con.commit()
    con.close()

    cfg = {"vault_rag_db_path": str(db)}
    monkeypatch.setattr(rag, "_db_path", lambda c: db)
    hits = rag.search_by_timecode("tadc-ep1", "03:45", config=cfg)
    assert hits, "pola lama mengembalikan 0 hit walaupun datanya ada"
    assert "Pomni" in hits[0]["content"]


# --- Penanda waktu di konteks (ditambahkan 2026-07-31) -----------------------
#
# Masalah: format_hits_for_prompt cuma menampilkan path dan SKOR. Potongan 27 Mei dan
# 27 Juli duduk berdampingan tanpa penanda, dan yang ditonjolkan justru skor — yang
# menyiratkan "makin tinggi makin benar". Arti tidak punya dasar memilih saat dua
# potongan bertentangan.
#
# Tidak diselesaikan lewat peringkat karena _recency_score_boost berbentuk tebing
# (+0,15 hari ini / +0,08 <=7 hari / 0 selebihnya) dan terukur 95% dari 573 chunk
# mendapat NOL. Menajamkannya jadi peluruhan agresif akan mengubur memori lama yang
# justru kadang ditanya ("cerita waktu debut dong").


def test_chunk_date_label_dated_vs_standing():
    import arti_vault_rag as rag

    assert rag.chunk_date_label("vault/sessions/2026-05-27-default.md") == "2026-05-27"
    assert rag.chunk_date_label("transcripts/2026-07-31-default.jsonl") == "2026-07-31"
    # Berkas kurasi tidak bertanggal — dijaga tetap mutakhir, jadi "berlaku sekarang"
    assert rag.chunk_date_label("vault/concepts/arti_live_learnings.md") == "catatan tetap"
    assert rag.chunk_date_label("ARTI_VIEWERS.md") == "catatan tetap"
    assert rag.chunk_date_label("") == "catatan tetap"


def test_format_hits_shows_dates_and_conflict_rule():
    import arti_vault_rag as rag

    hits = [
        {
            "score": 0.9,
            "source_path": "vault/sessions/2026-05-27-default.md",
            "source_type": "vault_session",
            "heading": "Lama",
            "content": "Arti belum bisa baca chat",
        },
        {
            "score": 0.7,
            "source_path": "vault/concepts/arti_self_knowledge.md",
            "source_type": "vault_concept",
            "heading": "Baru",
            "content": "Arti baca chat YouTube",
        },
    ]
    out = rag.format_hits_for_prompt(hits, 2400)
    assert "[2026-05-27]" in out, "tanggal sesi harus tampil di konteks"
    assert "[catatan tetap]" in out, "berkas kurasi harus ditandai berlaku sekarang"
    assert "tanggal terbaru" in out, "aturan konflik harus ada di header"
    assert "bukan kebenaran" in out, "skor harus dinyatakan bukan ukuran kebenaran"


def test_rag_header_fits_budget_without_displacing_hits():
    """Header adalah overhead TETAP; ia tidak boleh menggusur cuplikan.

    Versi 298 char terukur menggusur hit ke-5 di 2 dari 6 query uji. Header dipadatkan
    ke ~172 char DAN vault_rag_max_context_chars dinaikkan 2200 -> 2400 untuk menyerapnya.
    """
    import arti_vault_rag as rag

    header = rag.format_hits_for_prompt(
        [{"score": 1, "source_path": "x", "source_type": "t", "heading": "", "content": "y"}],
        99999,
    ).split("\n")[0]
    assert len(header) <= 200, f"header membengkak jadi {len(header)} char"

    cap = rag.DEFAULT_CONFIG["vault_rag_max_context_chars"]
    assert cap >= 2400

    # Invarian yang benar: kenaikan anggaran harus MENUTUPI pertumbuhan header, sehingga
    # jumlah cuplikan yang muat tidak pernah berkurang dibanding sebelum perubahan.
    # (Menguji angka absolut "5 harus muat" salah: dengan cuplikan panjang maksimum,
    # 5 buah tidak muat di 2200 dengan header lama pun.)
    _OLD_HEADER_LEN = 76
    _OLD_CAP = 2200
    assert cap - len(header) >= _OLD_CAP - _OLD_HEADER_LEN, (
        f"anggaran cuplikan menyusut: {cap - len(header)} < {_OLD_CAP - _OLD_HEADER_LEN}"
    )

    # Dan pada ukuran cuplikan yang realistis, 5 hit tetap utuh.
    hits = [
        {
            "score": 0.9 - i / 100,
            "source_path": f"vault/sessions/2026-07-2{i}-default.md",
            "source_type": "vault_session",
            "heading": "H",
            "content": "x" * 280,
        }
        for i in range(5)
    ]
    out = rag.format_hits_for_prompt(hits, cap)
    assert out.count("\n(") == 5, f"cuplikan tergusur: cuma {out.count(chr(10) + '(')} dari 5"


# --- Upgrade B (v0.7): recency decay halus + query temporal + [HARI INI] -------


def _boost(days_ago: int, **cfg):
    from datetime import date, timedelta

    d = (date.today() - timedelta(days=days_ago)).isoformat()
    return rag._recency_score_boost(f"vault/sessions/{d}-default.md", cfg)


def test_decay_smooth_same_magnitude_and_monotonic():
    assert _boost(0) == pytest.approx(0.15), "maks harus tetap 0.15 (hormati desain lama)"
    assert _boost(14) == pytest.approx(0.075, abs=0.001), "half-life 14 hari"
    seq = [_boost(d) for d in (0, 3, 7, 14, 21, 30, 45)]
    assert seq == sorted(seq, reverse=True), "boost harus menurun monoton"
    assert _boost(8) > 0.0, "umur 8 hari tidak boleh jatuh ke nol (cacat cliff lama)"
    assert _boost(70) == 0.0, "praktis nol di ~8 minggu"


def test_decay_cliff_mode_preserved_via_config():
    cliff = {"vault_rag_recency_half_life_days": 0}
    assert _boost(0, **cliff) == pytest.approx(0.15)
    assert _boost(3, **cliff) == pytest.approx(0.08)
    assert _boost(8, **cliff) == 0.0


def test_undated_path_gets_no_boost():
    assert rag._recency_score_boost("vault/concepts/arti_origin.md", {}) == 0.0


def test_temporal_query_detection():
    for q in ("kapan terakhir aku bahas rust?", "kemarin bohan ngapain",
              "yang barusan kamu bilang", "topik minggu lalu apa"):
        assert rag.is_temporal_query(q) is True, q
    for q in ("apa itu vault", "siapa bohan", "arti suka anime apa"):
        assert rag.is_temporal_query(q) is False, q


def test_temporal_multiplier_only_for_temporal_queries():
    assert rag.recency_multiplier("kapan terakhir live?", {}) == pytest.approx(2.0)
    assert rag.recency_multiplier("siapa bohan", {}) == pytest.approx(1.0)
    assert rag.recency_multiplier("kapan terakhir live?",
                                 {"vault_rag_temporal_multiplier": 3.0}) == pytest.approx(3.0)


def test_bridge_injects_today_block_per_turn():
    """Temuan rekon: tanggal sesi TIDAK PERNAH ada di prompt Arti — dia tak bisa
    menghitung label [YYYY-MM-DD] jadi 'tiga hari lalu'. Dirakit per-turn karena
    sesi bisa nyebrang tengah malam."""
    import time as _t

    import hermes_vtuber_bridge as b

    blk = b.build_today_block({"arti_debut_date": "2026-05-27"})
    assert _t.strftime("%Y-%m-%d") in blk
    assert "[HARI INI]" in blk
    assert "hari jadi co-host" in blk
    assert "[ASAL USUL ARTI]" in blk, "harus mengingatkan aturan tanggal debut"
    # Concern Bohan 2026-08-02: jangan sampai Arti 'thinking out loud' —
    # "berdasarkan tanggal sekian..." atau nyebut tanggal tiap akhir jawaban.
    assert "JANGAN menyebut" in blk, "wajib ada larangan mengumumkan tanggal"
    assert "DITANYA" in blk, "tanggal hanya disebut kalau memang ditanya"

    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "dynamic_system_prompt + build_today_block() + viewer_block_for" in src, (
        "blok tanggal harus dirakit PER-TURN (sesi bisa lewat tengah malam)"
    )
    assert any("[HARI INI]" in m for m in b._SYSTEM_PROMPT_BLOCK_MARKERS), (
        "harus terdaftar di marker trim"
    )

"""Embedding mati TIDAK boleh membuat Arti pikun total.

Probe 13 Agu 2026: dengan LM Studio tak terjangkau, `search()` mengembalikan 0
hit untuk SEMUA query — padahal skor FTS-nya sudah dihitung beberapa baris di
atas dan DB punya indeks full-text 1083 baris yang tidak tersentuh. Efeknya di
siaran: Arti tetap ngobrol, cuma tiba-tiba kehilangan seluruh ingatan jangka
panjangnya, dan satu-satunya jejak cuma sebaris "[Vault RAG] Query embed gagal".
LM Studio itu aplikasi desktop yang dinyalakan manual — lupa membukanya bukan
skenario aneh.

Terukur sesudah perbaikan pada DB vault sungguhan: "minecraft tadi malam
ngapain" 0 hit -> 3 hit; "cerita soal nether portal" 0 -> 3; "kucing" tetap 0
(memang tidak ada di ingatannya).
"""

from pathlib import Path

import arti_vault_rag as rag

_SRC = (Path(__file__).resolve().parent.parent / "arti_vault_rag.py").read_text(
    encoding="utf-8"
)


def _baris(cid, sumber, isi, folder="sessions"):
    return {
        "id": cid,
        "source_path": sumber,
        "source_type": "note",
        "folder": folder,
        "heading": "",
        "content": isi,
    }


def test_gagal_embed_tidak_lagi_pulang_tangan_kosong():
    """Penjaga sumber: cabang except WAJIB memakai jalur lexical, bukan `return []`."""
    # Anchor lewat pemanggilan embed query — `except Exception as e:` sendirian
    # muncul belasan kali di modul ini (aturan proyek nomor 10: jendela
    # source-slice gampang menangkap potongan yang salah).
    assert _SRC.count("qvec = np.array(embed_query_cached(query, query_cfg)") == 1
    cabang = _SRC.split("qvec = np.array(embed_query_cached(query, query_cfg)")[1]
    cabang = cabang.split("\n        ids = ")[0]
    assert "_hits_lexical_saja" in cabang
    assert "return []" not in cabang


def test_peringkat_ikut_skor_kata_kunci():
    rows = [_baris(1, "a.md", "isi a"), _baris(2, "b.md", "isi b"),
            _baris(3, "c.md", "isi c")]
    fts = {1: 0.3, 2: 0.9, 3: 0.6}

    hits = rag._hits_lexical_saja(rows, fts, 3, {}, "apa saja")

    assert [h["source_path"] for h in hits] == ["b.md", "c.md", "a.md"]
    assert hits[0]["keyword"] == 0.9


def test_potongan_tanpa_kecocokan_kata_kunci_dibuang():
    rows = [_baris(1, "a.md", "isi a"), _baris(2, "b.md", "isi b")]

    hits = rag._hits_lexical_saja(rows, {1: 0.5}, 5, {}, "apa saja")

    assert [h["source_path"] for h in hits] == ["a.md"]


def test_tidak_ada_kecocokan_sama_sekali_balik_kosong():
    """Lebih baik diam daripada menyuntik potongan acak ke prompt."""
    rows = [_baris(1, "a.md", "isi a")]

    assert rag._hits_lexical_saja(rows, {}, 5, {}, "kucing") == []


def test_ambang_skor_gabungan_TIDAK_dipakai_di_jalur_lexical():
    """`vault_rag_min_score` (0,28) dikalibrasi untuk skor campuran 0,72 semantik
    + 0,28 kata kunci. Kalau dipakai pada skor kata kunci telanjang, hampir semua
    hasil terbuang dan kita kembali ke kepikunan yang sama."""
    rows = [_baris(1, "a.md", "isi a")]
    cfg = {"vault_rag_min_score": 0.9}

    hits = rag._hits_lexical_saja(rows, {1: 0.2}, 5, cfg, "apa saja")

    assert len(hits) == 1
    # Ambangnya tetap bisa dipasang, tapi lewat kunci yang terpisah.
    assert rag._hits_lexical_saja(
        rows, {1: 0.2}, 5, {"vault_rag_min_score_lexical": 0.5}, "apa saja"
    ) == []


def test_hasil_jujur_bahwa_sisi_semantik_tidak_ada():
    rows = [_baris(1, "a.md", "isi a")]

    h = rag._hits_lexical_saja(rows, {1: 0.7}, 5, {}, "apa saja")[0]

    assert h["semantic"] is None
    assert h["lexical_fallback"] is True


def test_satu_berkas_tidak_boleh_memonopoli_konteks():
    """Pembatas keberagaman yang sama dengan jalur normal tetap berlaku."""
    rows = [_baris(i, "sama.md", f"isi {i}") for i in range(1, 6)]
    fts = {i: 1.0 - i / 100 for i in range(1, 6)}

    hits = rag._hits_lexical_saja(rows, fts, 5, {"vault_rag_max_per_source": 2}, "x")

    dari_sama = [h for h in hits if h["source_path"] == "sama.md"]
    assert len(dari_sama) <= 2 or len(hits) == len(rows)

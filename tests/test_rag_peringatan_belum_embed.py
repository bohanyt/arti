"""Potongan terindeks-tapi-belum-ter-embed harus BERSUARA saat startup.

Kalau batch embedding satu berkas gagal total lalu tidak pernah dicoba lagi,
observer RAG punya potongan tanpa embedding. Efeknya: seluruh ingatan berkas itu
tak terlihat oleh pencarian, sementara cek naif "sudah terindeks?" tetap menjawab SUDAH.

Angkanya sudah lama ada di `index_stats` (chunks vs embedded); yang kurang cuma
ada yang menyuarakan kalau timpang.
"""

from pathlib import Path

_SRC = (
    Path(__file__).resolve().parent.parent / "arti_bridge.py"
).read_text(encoding="utf-8")


def _blok_startup() -> str:
    anchor = 'rag_st = arti_vault_rag.index_stats(CONFIG)'
    assert _SRC.count(anchor) == 1
    return _SRC.split(anchor)[1].split("except Exception as e:")[0]


def test_selisih_embedded_diperiksa():
    blok = _blok_startup()
    assert 'rag_st["embedded"] < rag_st["chunks"]' in blok


def test_peringatannya_menyebut_akibat_dan_obatnya():
    blok = _blok_startup()
    assert "PERINGATAN" in blok
    assert "reindex-all" in blok


def test_db_kosong_tetap_punya_pesan_sendiri():
    """Dua keadaan yang beda: DB kosong vs sebagian tidak ter-embed."""
    blok = _blok_startup()
    assert 'rag_st["chunks"] == 0' in blok
    assert blok.index('rag_st["chunks"] == 0') < blok.index(
        'rag_st["embedded"] < rag_st["chunks"]'
    )

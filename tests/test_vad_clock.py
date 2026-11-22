"""Jam VAD harus jam SUNGGUHAN, bukan hitungan potongan audio.

Baris lama `silence_duration += 0.1` mengasumsikan tiap
potongan audio 100 ms, padahal `sd.InputStream` dibuat tanpa `blocksize` →
ukuran potongan ditentukan driver mic (~20 ms di mesin uji). Akibatnya
`asr_ptt_silence_tail_sec: 10.0` cuma ~2 detik nyata, DAN panjang jeda itu
berubah diam-diam kalau perangkat/driver audio berganti.
"""

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent / "arti_bridge.py").read_text(
    encoding="utf-8"
)


def test_hitungan_potongan_tidak_dipakai_lagi():
    assert "silence_duration += 0.1" not in _SRC


def test_ekor_sunyi_diukur_dengan_perf_counter():
    assert _SRC.count("silence_duration = time.perf_counter() - last_voice_at") == 1
    # Titik nolnya harus ditulis ulang tiap potongan BERSUARA, kalau tidak
    # jamnya menghitung dari awal stream dan ucapan pertama langsung terpotong.
    assert _SRC.count("last_voice_at = time.perf_counter()") >= 2


def test_ekor_ptt_lima_detik_di_config_produksi():
    """Rule: uji terhadap CONFIG produksi, bukan dict kosong."""
    import arti_bridge as hb

    assert hb.CONFIG["asr_ptt_silence_tail_sec"] == 5.0
    assert hb.CONFIG["asr_silence_tail_sec"] == 2.0

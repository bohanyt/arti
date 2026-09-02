"""Renungan — busur mikir keras-keras multi-giliran (permintaan Bohan 20 Agu).

"ada 'wondering' ga sih? dia ngomong sendiri thinking out loud biar kerekam
trus otomatis build buat jadi reasoningnya sendiri... kayak storytime gitu,
yang mengajak diri dia mikir sama viewer mikir, tapi ga bergantung banget
sama viewer."

Lubang yang diisi: tiap giliran proaktif selama ini adalah PULAU — benang
(arti_benang) cuma melarang MENGULANG, tidak pernah menyuruh MELANJUTKAN
pikiran. Renungan menyimpan busur: satu pertanyaan miliknya sendiri yang
dikembangkan beberapa giliran sampai ketemu kesimpulan, lalu kesimpulannya
disimpan ke vault (lewat corong append_learning yang bergerbang
fakta_sudah_ada) — jadi pendirian yang bisa dia kutip di sesi berikutnya.

Bentuk busur (langkah = ucapan Arti yang SUDAH terjadi):
  1. BUKA   — lempar keheranan/pertanyaan yang dia sendiri pengen tahu
  2. TEORI  — coba jawab sendiri dengan satu dugaan
  3. UJI    — cari lubang di teorinya sendiri / contoh tandingan
  4. SIMPUL — pendirian final + punchline; busur tutup, kesimpulan ke vault

Prinsip desain (tiap-tiapnya dari luka lama):
- BUKAN jalur proaktif baru: numpang slot inisiatif yang sudah ada — cadence,
  pagar anti-motong, detektor tidur, rem provider semua tak tersentuh, dan
  NOL panggilan composer tambahan.
- Momen selalu menang (pola gerakan dialog): penonton baru / streamer baru
  bicara / main game -> renungan ngalah, disambung nanti. Busur yang
  kelamaan ngambang (umur > renungan_umur_max_sec) di-drop diam-diam.
- Penonton diajak mikir tapi TIDAK ditunggu; kalau ada chat yang nyambung
  topik, pendapatnya jadi bahan langkah berikutnya (bibit uptake kohesi
  tahap 3).
- Modul murni tanpa LLM/IO, dipanggil dari jalur panas add_to_history —
  murah dan catat() dilarang melempar (kontrak sama dengan arti_benang).
- Pagar pytest: test_dialog_inisiatif memanggil build_initiative_prompt
  dengan CONFIG PRODUKSI (config_local bisa menyalakan renungan), jadi
  giliran_renungan no-op di bawah pytest kecuali force=True — pola yang
  sama dengan arti_debug_log.pasang & prewarm Codex (suite pernah
  memanggil Luna sungguhan).
"""

from __future__ import annotations

import os
import re
import threading
import time

_kunci = threading.Lock()

# --- state busur (satu busur aktif per sesi pada satu waktu) ---------------
_topik: str = ""            # pertanyaan pembukanya (diambil dari ucapan Arti)
_posisi: str = ""           # ucapan Arti terakhir di busur ini
_langkah: int = 0           # 0 = tidak ada busur
_menunggu: bool = False     # prompt sudah dibangun, jawaban Arti belum tercatat
_dibuka_ts: float = 0.0
_tutup_ts: float = 0.0      # untuk cooldown antar-busur
_kesimpulan_pending: str | None = None
_pendapat: list[tuple[str, str]] = []   # (nama, teks) chat yang nyambung topik
_MAX_PENDAPAT = 2
_hitung_eligible: int = 0   # giliran inisiatif yang BISA membuka (cadence)
_lanjut_flip: bool = False  # selang-seling mode duet
_seed_idx: int = 0

_MAKS_KUTIP = 250
_MAKS_TOPIK = 150

# Bahan pembuka darurat kalau tidak ada seed dari memori/layar — dirotasi
# bergilir (bukan acak: acak murni pernah mengulang, dan tarikan rng ekstra
# meledakkan tes lama beriterasi pas — pelajaran gerakan_dialog).
DEFAULT_SEEDS = (
    "kebiasaan aneh manusia yang kamu perhatiin selama nemenin stream",
    "kenapa orang betah nonton orang lain ngelakuin sesuatu",
    "hal yang menurutmu semua orang salah paham",
    "sesuatu yang dulu kamu yakini tapi sekarang kamu ragukan",
    "kenapa hal receh bisa bikin seneng banget",
    "apa yang bikin sebuah obrolan enak didengerin",
)


def reset_session() -> None:
    global _topik, _posisi, _langkah, _menunggu, _dibuka_ts, _tutup_ts
    global _kesimpulan_pending, _hitung_eligible, _lanjut_flip, _seed_idx
    global _buka_idx
    with _kunci:
        _buka_idx = 0
        _topik = ""
        _posisi = ""
        _langkah = 0
        _menunggu = False
        _dibuka_ts = 0.0
        _tutup_ts = 0.0
        _kesimpulan_pending = None
        _pendapat.clear()
        _hitung_eligible = 0
        _lanjut_flip = False
        _seed_idx = 0


def ada_busur() -> bool:
    with _kunci:
        return _langkah > 0 or _menunggu


# Kata umum yang bukan penanda topik (duplikat kecil dari arti_curious —
# renungan TIDAK boleh import arti_curious: curious yang mengimport renungan).
_STOPWORDS = frozenset({
    "bohan", "arti", "stream", "live", "chat", "viewer", "penonton",
    "streamer", "yang", "untuk", "dengan", "karena", "adalah", "sedang",
    "sudah", "belum", "masih", "juga", "atau", "pada", "kayak", "banget",
    "gimana", "kenapa", "menurut", "kalian", "orang",
    # Kata pengisi ajakan-mikir: bukan penanda topik, dan kalau ikut masuk
    # label vault dia jadi cetakan gaya bicara lagi (log [time removed]).
    "coba", "pikirin", "mikirin", "mikir", "kepikiran", "sebenarnya",
    "jangan", "kalau", "bikin", "lebih", "yaelah",
})


def _kata_topik(teks: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]{4,}", (teks or "").lower())
        if w not in _STOPWORDS
    }


def _kalimat_tanya_terakhir(teks: str) -> str:
    kalimat = [k.strip() for k in re.split(r"(?<=[.!?…])\s+", teks) if k.strip()]
    tanya = [k for k in kalimat if k.endswith("?")]
    return tanya[-1] if tanya else ""


def _drop_busur_terkunci() -> None:
    """Buang busur tanpa kesimpulan (kelamaan ngambang). Panggil DALAM _kunci."""
    global _topik, _posisi, _langkah, _menunggu, _dibuka_ts
    _topik = ""
    _posisi = ""
    _langkah = 0
    _menunggu = False
    _dibuka_ts = 0.0
    _pendapat.clear()


def catat(source: str, teks: str, config: dict | None = None) -> None:
    """Dipanggil untuk TIAP entri history (sebelah arti_benang.catat).

    Jawaban Arti saat busur menunggu -> posisi maju satu langkah; langkah
    terakhir -> busur tutup + kesimpulan pending (bridge yang menulisnya ke
    vault lewat save_long_term_memory, BUKAN modul ini — jalur panas dilarang
    IO). Chat penonton/streamer yang nyambung topik -> bahan langkah depan.
    Tidak pernah melempar.
    """
    global _topik, _posisi, _langkah, _menunggu, _tutup_ts, _kesimpulan_pending
    try:
        teks = (teks or "").strip()
        src = (source or "").lower()
        if not teks or "system" in src:
            return
        cfg = config or {}
        with _kunci:
            if "arti" in src:
                if not _menunggu:
                    return
                _menunggu = False
                _posisi = teks[:_MAKS_KUTIP]
                _langkah += 1
                if _langkah == 1:
                    _topik = (_kalimat_tanya_terakhir(teks) or teks)[:_MAKS_TOPIK]
                maks = int(cfg.get("renungan_max_langkah", 4))
                if _langkah >= maks:
                    # Topik disimpan sebagai KATA KUNCI, bukan kutipan mentah.
                    # Log [date removed] [time removed]: format lama menyimpan kalimat pembuka
                    # apa adanya, jadi frasa template ikut masuk vault — lalu
                    # RAG menariknya kembali dan Arti meniru dirinya sendiri
                    # (3 jawaban memakai frasa itu SESUDAH prompt-nya dicabut).
                    # Ingatan tidak boleh menjadi cetakan gaya bicara.
                    kunci = [w for w in _kata_topik(_topik)][:5]
                    label = ", ".join(sorted(kunci)) or "obrolan barusan"
                    _kesimpulan_pending = (
                        f"Renungan Arti soal {label} — kesimpulannya: "
                        f"{_posisi[:200]}"
                    )
                    _tutup_ts = time.time()
                    _drop_busur_terkunci()
                return
            # Manusia (penonton/streamer) menimpali sesuatu yang nyambung
            # dengan renungan yang lagi jalan -> tampung buat langkah depan.
            if _langkah > 0 and len(_pendapat) < _MAX_PENDAPAT:
                topik_kata = _kata_topik(_topik) | _kata_topik(_posisi)
                if topik_kata & _kata_topik(teks):
                    nama = (
                        source.replace("Viewer", "").replace("(YouTube)", "")
                        .replace("(ketik)", "").strip(" @:") or "penonton"
                    )
                    if "streamer" in src:
                        nama = "streamer"
                    _pendapat.append((nama[:24], teks[:150]))
    except Exception:  # noqa: BLE001 — jalur panas, dilarang meledak
        pass


def pop_kesimpulan() -> str | None:
    """Kesimpulan busur yang baru tutup — sekali ambil, lalu hangus."""
    global _kesimpulan_pending
    with _kunci:
        k = _kesimpulan_pending
        _kesimpulan_pending = None
        return k


# --------------------------------------------------------------------------- #
# Pembangun prompt
# --------------------------------------------------------------------------- #

_INSTRUKSI_LANGKAH = {
    2: (
        "Sekarang coba jawab sendiri: lempar SATU teori/dugaanmu soal itu dan "
        "bela alasannya. Jangan buru-buru yakin — ini baru dugaan."
    ),
    3: (
        "Sekarang UJI teorimu sendiri: cari lubangnya, contoh tandingan, atau "
        "sisi yang tadi kamu lewatkan. Boleh berubah pikiran — itu justru seru."
    ),
    4: (
        "Waktunya nutup renungan: kasih kesimpulan/pendirian FINAL-mu dengan "
        "yakin — satu kalimat inti yang bakal kamu pegang, boleh plus "
        "punchline khas kamu."
    ),
}


# Sudut pembuka, DIROTASI bergilir. Live [date removed] (sesi renungan perdana):
# 11 dari 211 jawaban membuka dengan frasa contoh yang dulu ditulis harfiah
# di prompt ini — pelajaran catchphrase terulang (contoh dalam tanda kutip
# = template yang ditiru mentah). Sejak itu: NOL frasa contoh, dan bentuk
# pembukanya digilir supaya tiap busur terdengar beda.
_SUDUT_BUKA = (
    "Mulai dari satu hal kecil yang bikin kamu ganjil — kenapa bisa begitu?",
    "Mulai dari pengakuan: ada hal yang kamu kira udah kamu ngerti, "
    "ternyata pas dipikir lagi kamu nggak yakin.",
    "Mulai dari perbandingan aneh: dua hal yang kelihatannya nggak nyambung "
    "tapi menurutmu mirip.",
    "Mulai dari pertanyaan yang agak memalukan buat ditanya — hal dasar yang "
    "kayaknya semua orang udah tahu.",
    "Mulai dari tebakan liar yang kamu sendiri belum yakin benar apa nggak.",
    "Mulai dari yang mengganjal dari obrolan atau kejadian barusan.",
)
_buka_idx = 0


def _baris_penonton(ada_penonton: bool) -> str:
    if ada_penonton:
        return (
            "Penonton boleh diajak ikut mikir, tapi JANGAN nunggu jawaban "
            "mereka — pikiranmu tetap jalan sendiri. Ajakannya pakai "
            "kalimatmu sendiri dan ganti-ganti; jangan mengulang cara "
            "mengajak yang sama seperti giliran sebelumnya. "
        )
    return (
        "TIDAK ADA penonton yang menyimak — jangan menyapa siapa-siapa, "
        "bergumam mikir sendiri saja. "
    )


def _build_buka(seed: str, ada_penonton: bool) -> str:
    global _buka_idx
    sudut = _SUDUT_BUKA[_buka_idx % len(_SUDUT_BUKA)]
    _buka_idx += 1
    return (
        "[Renungan — mulai mikir keras-keras]\n"
        "Kamu lagi kepikiran sesuatu dan pengen mikirinnya pelan-pelan, "
        "bukan sekali lempar selesai.\n"
        f"Bahan pancingan: {seed}\n"
        f"Sudut pembuka giliran ini: {sudut}\n"
        "Buka renunganmu: lempar SATU pertanyaan/keheranan yang KAMU SENDIRI "
        "pengen tahu jawabannya — akhiri dengan tanda tanya. Rumuskan dengan "
        "kalimatmu sendiri, JANGAN pakai pola pembuka yang sama dengan "
        "renungan sebelumnya. JANGAN dijawab "
        "sekarang; gantung dulu, kamu bakal mikirinnya beberapa giliran ke "
        "depan. "
        + _baris_penonton(ada_penonton)
        + "Maksimal 3 kalimat, Bahasa Indonesia, penuh karakter kamu. "
        "Jangan menyebut sistem, memori, log, atau hal teknis."
    )


def _build_lanjut(langkah_berikut: int, ada_penonton: bool) -> str:
    instruksi = _INSTRUKSI_LANGKAH.get(
        langkah_berikut, _INSTRUKSI_LANGKAH[4]
    )
    baris = [
        "[Renungan — lanjutkan mikirmu]",
        f"Renungan yang lagi kamu kunyah: «{_topik}»",
        f"Posisi terakhirmu: «{_posisi}» — LANJUTKAN dari situ, jangan mulai "
        "dari nol dan jangan mengulang kalimatmu.",
    ]
    if _pendapat:
        for nama, teks in _pendapat:
            baris.append(
                f"{nama} sempat nimbrung: «{teks}» — timbang pendapatnya "
                "(setuju atau bantah), tapi tetap pikiranmu yang jalan."
            )
        _pendapat.clear()
    baris.append(
        instruksi + " "
        + _baris_penonton(ada_penonton)
        + "Maksimal 3 kalimat, Bahasa Indonesia. SATU PIKIRAN saja. "
        "Jangan menyebut sistem, memori, log, atau hal teknis."
    )
    return "\n".join(baris)


def giliran_renungan(
    config: dict,
    *,
    mode: str = "duet",
    ada_penonton: bool = True,
    seed: str = "",
    now: float | None = None,
    force: bool = False,
) -> str | None:
    """Prompt renungan untuk slot inisiatif ini, atau None = jalur normal.

    Dipanggil arti_curious.build_initiative_prompt SETELAH momen-menang
    (game / penonton baru / streamer baru bicara sudah di-skip pemanggil).
    `force` = lewati pagar pytest (untuk tes renungan sendiri).
    """
    global _menunggu, _dibuka_ts, _hitung_eligible, _lanjut_flip, _seed_idx
    if not (config or {}).get("renungan_enabled", False):
        return None
    if "PYTEST_CURRENT_TEST" in os.environ and not force:
        return None  # lihat docstring modul: CONFIG produksi bocor ke tes lama
    t = time.time() if now is None else float(now)
    with _kunci:
        if _langkah > 0 or _menunggu:
            # Busur kelamaan ngambang (operator lagi rame ngobrol / momen menang
            # terus) -> drop diam-diam, jangan nyambung renungan basi.
            umur_max = float(config.get("renungan_umur_max_sec", 900.0))
            if _dibuka_ts and t - _dibuka_ts > umur_max:
                _drop_busur_terkunci()
                return None
            if _menunggu:
                # Giliran sebelumnya mati sebelum Arti menjawab — ulangi
                # langkah yang sama, jangan maju.
                if _langkah == 0:
                    return _build_buka(
                        seed or DEFAULT_SEEDS[_seed_idx % len(DEFAULT_SEEDS)],
                        ada_penonton,
                    )
                return _build_lanjut(_langkah + 1, ada_penonton)
            # Selang-seling di mode duet: renungan bergantian dengan celetukan
            # biasa biar gak kerasa kuliah. Mode host (Arti pegang siaran) =
            # storytime memang acaranya -> lanjut tiap slot.
            if mode == "duet" and bool(config.get("renungan_selang_seling", True)):
                _lanjut_flip = not _lanjut_flip
                if not _lanjut_flip:
                    return None
            _menunggu = True
            return _build_lanjut(_langkah + 1, ada_penonton)

        # --- tidak ada busur: pertimbangkan buka baru ---
        if _tutup_ts and t - _tutup_ts < float(
            config.get("renungan_cooldown_sec", 600.0)
        ):
            return None
        n = int(config.get("renungan_buka_tiap_n", 3))
        if mode == "host_chat":
            n = max(1, n - 1)  # Arti pegang siaran: renungan lebih rajin
        _hitung_eligible += 1
        if n > 1 and _hitung_eligible % n != 0:
            return None
        if not seed:
            seed = DEFAULT_SEEDS[_seed_idx % len(DEFAULT_SEEDS)]
            _seed_idx += 1
        _menunggu = True
        _dibuka_ts = t
        _lanjut_flip = False
        return _build_buka(seed, ada_penonton)

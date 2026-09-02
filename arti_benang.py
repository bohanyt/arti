"""Benang obrolan — ingatan dialog lintas giliran proaktif (lapis B).

Lahir dari tes 16 Agu 14.28 (dump streamer): dalam 9 giliran curious, "sorokan
kegedean" diungkit 5 balasan BERUNTUN, "teori liar" dinagih 3x, tema "ujian"
4x. Akarnya: tiap giliran curious itu pulau — Arti tidak ingat dia barusan
bilang apa dan sudah menagih apa, jadi tiap giliran jatuh ke jangkar terakhir
yang sama. Keluhan produk: "kamu kayak ngerespon doang bukannya berdialog".

Modul ini menyimpan benang tipis per sesi:
1. Ucapan Arti terakhir (ring kecil) -> larangan mengulang topik/frasa.
2. SATU pertanyaan terbuka miliknya -> boleh disinggung sekali; kalau sudah
   ditagih dan tetap tak dijawab, RELAKAN (jangan jadi penagih hutang).
3. Streamer merespons apa pun -> pertanyaan dianggap tuntas (obrolan santai,
   bukan interogasi — jawaban "gak tau deh" pun jawaban).

Sengaja heuristik ringan tanpa LLM: dipanggil dari add_to_history (jalur
panas), harus murah dan tidak boleh melempar.
"""

from __future__ import annotations

import re
import threading
from collections import deque

_kunci = threading.Lock()
_ucapan_arti: deque[str] = deque(maxlen=3)
_pertanyaan: str | None = None
_ditagih: int = 0

_MAKS_KUTIP = 150


def reset_session() -> None:
    global _pertanyaan, _ditagih
    with _kunci:
        _ucapan_arti.clear()
        _pertanyaan = None
        _ditagih = 0


def _kalimat_tanya_terakhir(teks: str) -> str | None:
    if "?" not in teks:
        return None
    kalimat = [k.strip() for k in re.split(r"(?<=[.!?])\s+", teks) if k.strip()]
    tanya = [k for k in kalimat if k.endswith("?")]
    return tanya[-1][:_MAKS_KUTIP] if tanya else None


def _mirip(a: str, b: str) -> bool:
    aw = set(re.findall(r"\w+", a.lower()))
    bw = set(re.findall(r"\w+", b.lower()))
    if not aw or not bw:
        return False
    return len(aw & bw) / len(aw | bw) >= 0.4


def catat(source: str, teks: str) -> None:
    """Dipanggil untuk TIAP entri history. Tidak pernah melempar."""
    global _pertanyaan, _ditagih
    try:
        teks = (teks or "").strip()
        if not teks:
            return
        with _kunci:
            if "arti" in (source or "").lower():
                _ucapan_arti.append(teks[:_MAKS_KUTIP])
                tanya = _kalimat_tanya_terakhir(teks)
                if tanya:
                    if _pertanyaan and _mirip(tanya, _pertanyaan):
                        # Dia menagih pertanyaan yang sama lagi.
                        _ditagih += 1
                    else:
                        _pertanyaan = tanya
                        _ditagih = 0
            elif "system" not in (source or "").lower():
                # Streamer ATAU penonton merespons apa pun = pertanyaannya
                # dianggap tuntas. Obrolan santai, bukan interogasi.
                _pertanyaan = None
                _ditagih = 0
    except Exception:  # noqa: BLE001 — jalur panas, dilarang meledak
        pass


def blok_prompt() -> str:
    """Blok injeksi untuk prompt curious. Kosong kalau belum ada benang."""
    with _kunci:
        if not _ucapan_arti:
            return ""
        baris = ["[Benang obrolan — supaya kamu nyambung, bukan mengulang]"]
        # Diet bahan (paket kohesi [date removed]): SATU kutipan terakhir cukup untuk
        # anti-ulang; dua kutipan ikut menggemukkan prompt composer yang
        # malam [date removed] p50-nya sudah menabrak kapak 12 dtk.
        for u in list(_ucapan_arti)[-1:]:
            baris.append(f"Barusan kamu bilang: «{u}»")
        baris.append(
            "JANGAN mengulang topik/frasa dari ucapanmu barusan — bawa arah "
            "baru atau kembangkan, jangan muter di tempat."
        )
        if _pertanyaan:
            if _ditagih == 0:
                baris.append(
                    f"Pertanyaanmu yang belum dijawab: «{_pertanyaan}» "
                    "— boleh kamu singgung SEKALI kalau pas."
                )
            else:
                baris.append(
                    f"Pertanyaanmu «{_pertanyaan}» sudah kamu tagih "
                    "dan tetap tidak dijawab — RELAKAN. Jangan sebut lagi, "
                    "ganti arah."
                )
        return "\n".join(baris)

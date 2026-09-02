"""Per-turn pipeline stage timing for Arti latency diagnostics."""

from __future__ import annotations

import time
from typing import Any

_STAGE_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("vts_mikir_ms", "start", "after_mikir"),
    ("rag_ms", "after_mikir", "after_rag"),
    ("llm_ms", "after_rag", "after_llm"),
    ("tts_ms", "after_llm", "after_tts"),
)

_tts_synth_ms: int | None = None
_tts_play_ms: int | None = None
_tts_jeda_ms: int = 0
_tts_chunks: int = 0
_pending_asr_stages: dict[str, int] | None = None


def note_tts_jeda_ms(ms: int) -> None:
    """Jeda napas antar kalimat (17 Agu) — dicatat sendiri, JANGAN menyamar
    jadi tts_play/lain: itu hening yang disengaja, bukan biaya pipeline."""
    global _tts_jeda_ms
    _tts_jeda_ms += max(0, int(ms))


def pop_asr_stages() -> dict[str, int]:
    """ASR timing captured before queue_voice_trigger (PTT / wake word)."""
    global _pending_asr_stages
    stages = _pending_asr_stages or {}
    _pending_asr_stages = None
    return dict(stages)


def set_pending_asr_stages(stages: dict[str, int] | None) -> None:
    global _pending_asr_stages
    _pending_asr_stages = dict(stages) if stages else None


def note_tts_synth_ms(ms: int) -> None:
    """AKUMULASI, bukan timpa.

    Pelajaran 12 Agu 2026: jawaban Arti dipecah per kalimat lalu diucapkan
    dalam loop (`for chunk in tts_sentence_chunks: await tts.speak(chunk)`),
    jadi fungsi ini dipanggil sekali PER KALIMAT. Versi lama menimpa nilai
    sebelumnya, sehingga log hanya melaporkan kalimat TERAKHIR — median 7,7
    detik per giliran raib dari `[Latency]` (57 giliran, sesi 11 Agu).
    """
    global _tts_synth_ms, _tts_chunks
    _tts_synth_ms = (_tts_synth_ms or 0) + max(0, int(ms))
    _tts_chunks += 1


def note_tts_play_ms(ms: int) -> None:
    """AKUMULASI — lihat alasan di `note_tts_synth_ms`."""
    global _tts_play_ms
    _tts_play_ms = (_tts_play_ms or 0) + max(0, int(ms))


def consume_tts_stages() -> dict[str, int]:
    global _tts_synth_ms, _tts_play_ms, _tts_chunks, _tts_jeda_ms
    out: dict[str, int] = {}
    if _tts_synth_ms is not None:
        out["tts_synth_ms"] = _tts_synth_ms
    if _tts_play_ms is not None:
        out["tts_play_ms"] = _tts_play_ms
    if _tts_jeda_ms > 0:
        out["tts_jeda_ms"] = _tts_jeda_ms
    if _tts_chunks > 1:
        out["tts_chunks"] = _tts_chunks
    _tts_synth_ms = None
    _tts_play_ms = None
    _tts_jeda_ms = 0
    _tts_chunks = 0
    return out


class PipelineTimer:
    """Mark named checkpoints; compute stage deltas in milliseconds."""

    __slots__ = ("_t0", "_marks", "_extra")

    def __init__(self, extra: dict[str, Any] | None = None):
        # Giliran baru = papan tulis bersih. Sejak akumulasi TTS ([date removed]),
        # giliran yang mati sebelum sempat memanggil stages_ms() akan
        # meninggalkan sisa di global — dan sisa itu dulu ikut terhitung di
        # giliran BERIKUTNYA, bikin angkanya menggelembung tanpa sebab.
        consume_tts_stages()
        self._t0 = time.perf_counter()
        self._marks: dict[str, float] = {"start": self._t0}
        self._extra: dict[str, Any] = dict(extra) if extra else {}

    def mark(self, name: str) -> None:
        self._marks[name] = time.perf_counter()

    def _delta_ms(self, start: str, end: str) -> int | None:
        if start not in self._marks or end not in self._marks:
            return None
        return int((self._marks[end] - self._marks[start]) * 1000)

    def stages_ms(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, start, end in _STAGE_PAIRS:
            value = self._delta_ms(start, end)
            if value is not None:
                out[key] = value

        for key, value in self._extra.items():
            if isinstance(value, (int, float)):
                out[key] = int(value)

        tts = consume_tts_stages()
        out.update(tts)
        if "tts_synth_ms" in out or "tts_play_ms" in out:
            out["tts_ms"] = out.get("tts_synth_ms", 0) + out.get("tts_play_ms", 0)

        end_t = self._marks.get("after_tts", self._marks.get("end"))
        if end_t is not None:
            out["total_ms"] = int((end_t - self._t0) * 1000)

            # SISA yang tidak diakui tahap mana pun (koreografi VTS, nod,
            # antre gerbang sounddevice, jeda antar-kalimat). Sebelum ini
            # angkanya cuma menghilang diam-diam dan bikin `total` tampak
            # tidak masuk akal. Lebih baik punya nama daripada jadi misteri.
            komponen = sum(
                out.get(k, 0)
                for k in ("vts_mikir_ms", "rag_ms", "llm_ms", "tts_ms",
                          "tts_jeda_ms")
            )
            # SELALU dicetak, termasuk kalau NEGATIF. Sisa negatif berarti
            # tahapan mengaku lebih lama dari totalnya sendiri — mustahil, jadi
            # ada yang bocor (mis. angka TTS milik giliran sebelumnya). Versi
            # pertama fungsi ini menyembunyikannya dengan `if sisa > 0`, dan itu
            # mengulangi persis penyakit yang sedang kita berantas: alat ukur
            # yang bungkam waktu ada yang aneh. Ketahuan saat crosscheck [date removed].
            out["lain_ms"] = out["total_ms"] - komponen

            # Yang BENAR-BENAR dirasakan operator: `total` mulai dihitung sesudah
            # ASR selesai, padahal dia sudah menunggu vad_tail (10 dtk sunyi,
            # `asr_ptt_silence_tail_sec`) + transkripsi sebelum itu.
            if "vad_tail_ms" in out:
                out["dirasa_ms"] = (
                    out["vad_tail_ms"] + out.get("asr_ms", 0) + out["total_ms"]
                )
        return out


def format_latency_line(stages: dict[str, int]) -> str:
    """One terminal line: [Latency] asr=… rag=… llm=… tts=… total=…"""
    keys = (
        "vad_tail_ms",
        "asr_ms",
        "vts_mikir_ms",
        "rag_ms",
        "llm_ms",
        "tts_synth_ms",
        "tts_play_ms",
        "tts_jeda_ms",
        "tts_ms",
        "tts_chunks",
        "lain_ms",
        "total_ms",
        "dirasa_ms",
    )
    parts = []
    for k in keys:
        if k in stages:
            # Kunci non-`_ms` adalah hitungan, bukan durasi — jangan diberi
            # akhiran "ms" (dulu `tts_chunks=3ms` yang tidak ada artinya).
            if k.endswith("_ms"):
                parts.append(f"{k[:-3]}={stages[k]}ms")
            else:
                parts.append(f"{k}={stages[k]}")
    return "[Latency] " + " ".join(parts)

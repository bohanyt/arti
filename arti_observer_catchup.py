"""Catch-up observer: sesi yang mati tanpa shutdown bersih tetap dirangkum.

Keluhan Bohan 2026-08-10: "[recorded remark omitted]".
Pipeline observer cuma jalan di SHUTDOWN BERSIH — bridge yang di-force-close,
crash, atau OOM meninggalkan transcript utuh di disk tapi beats-nya tidak
pernah ditulis (terbukti: transcript 2026-08-09 berakhir ~23:39, beats
terakhir ditulis 23:21 — sesi 23.35 tidak pernah masuk ingatan).

Prinsip kerja:
- Deteksi: transcript yang TIDAK punya beats, atau yang mtime-nya lebih baru
  dari beats-nya (margin), = ada ekor yang belum dirangkum.
- INKREMENTAL: segmen yang sudah punya beat TIDAK diringkas ulang — hemat
  LLM, dan learnings tidak diduplikasi (curate_beats menulis learning saat
  kurasi; menjalankan ulang satu hari penuh = varian duplikat, pelajaran
  kurator 2026-08-01). Index segmen = bucket jam-dinding (sec // 600), jadi
  stabil walau transcript bertambah.
- Kalau catch-up sendiri terpotong (thread daemon mati bersama proses),
  startup berikutnya melanjutkan dari titik yang sama — sifat yang sama
  dengan reindex self-healing vault RAG.

Batas jujur: segmen TERAKHIR yang sempat dirangkum sebelum crash bisa
kehilangan ekornya (baris yang datang sesudah shutdown di bucket 10-menit
yang sama tidak diringkas ulang) — kerugian maksimal <10 menit per crash.
"""

from __future__ import annotations

from arti_runtime import ipc
import dataclasses
import json
import time
from pathlib import Path
from typing import Any, Callable

import arti_curator as curator
import arti_observer_pipeline as pipeline

_ROOT = Path(__file__).resolve().parent


def _transcript_dir(config: dict | None = None) -> Path:
    sub = str((config or {}).get("transcript_dir") or "transcripts")
    p = Path(sub)
    return p if p.is_absolute() else _ROOT / sub


def find_pending(config: dict, current_session_id: str = "",
                 now: float | None = None) -> list[str]:
    """Session id yang transcript-nya belum (sepenuhnya) dirangkum.

    Sesi yang sedang berjalan TIDAK pernah ikut — dia masih menulis, dan
    jatahnya tetap shutdown bersih.
    """
    now = time.time() if now is None else now
    max_days = float(config.get("observer_catchup_max_days", 7))
    margin = float(config.get("observer_catchup_margin_sec", 120.0))
    d = _transcript_dir(config)
    if not d.is_dir():
        return []
    hasil: list[str] = []
    for tx in sorted(d.glob("*.jsonl")):
        sid = tx.stem
        if sid == current_session_id:
            continue
        try:
            umur = now - tx.stat().st_mtime
        except OSError:
            continue
        if umur > max_days * 86400:
            continue
        jsonl_path, _ = pipeline.beats_paths(sid, config)
        if not jsonl_path.is_file() or _partial_marker(jsonl_path).is_file():
            hasil.append(sid)
        elif tx.stat().st_mtime > jsonl_path.stat().st_mtime + margin:
            hasil.append(sid)
    return hasil


def _partial_marker(jsonl_path: Path) -> Path:
    """Exists while beats are written per segment and not all are done yet.

    Incremental writes bump the beats mtime past the transcript's, so without
    this marker an interrupted (or deferred) run would look complete.
    """
    return jsonl_path.with_name(jsonl_path.name.replace("_beats.jsonl", "_beats.partial"))


@dataclasses.dataclass
class IncrementalResult:
    beats: list[Any]          # existing + new, sorted by segment index
    baru: int = 0             # newly summarized segments
    approved: int = 0
    rejected: int = 0
    learnings: int = 0
    ditunda: int = 0          # pending segments deferred by max_segments


def observe_pending_segments(
    sid: str,
    tx: Path | str,
    config: dict,
    *,
    summarize_config: dict | None = None,
    on_progress: Callable[[str, int, int, str], None] | None = None,
    max_segments: int = 0,
    resummarize_grown: bool = False,
) -> IncrementalResult:
    """Summarize + curate only segments without a beat; persist after EACH one.

    Shared by catch-up and the shutdown pipeline (audit L02): shutdown costs
    O(this session), and closing the terminal early keeps finished segments.
    resummarize_grown: also redo a segment whose row count grew since its beat
    (a same-date restart inside the same 10-minute bucket).
    """
    rows = pipeline.load_transcript_rows(tx)
    minutes = int(config.get("observer_segment_minutes", 10))
    segments = pipeline.segment_by_minutes(rows, minutes=minutes)
    jsonl_path, md_path = pipeline.beats_paths(sid, config)
    by_index = {b.segment_index: b for b in _load_existing_beats(jsonl_path)}

    def _pending(seg) -> bool:
        lama = by_index.get(seg.index)
        if lama is None:
            return True
        return resummarize_grown and int(lama.event_count or 0) < seg.event_count

    baru_seg = [s for s in segments if _pending(s)]
    res = IncrementalResult(beats=[])
    if max_segments > 0 and len(baru_seg) > max_segments:
        res.ditunda = len(baru_seg) - max_segments
        baru_seg = baru_seg[:max_segments]
    marker = _partial_marker(jsonl_path)
    if baru_seg:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
    total = len(baru_seg)
    for i, seg in enumerate(baru_seg):
        def _cb(stage: str, j: int, n: int, label: str, _i: int = i) -> None:
            # Map per-segment progress onto the whole run; one final "done".
            if on_progress and not (stage == "summarize" and j >= n):
                on_progress(stage, _i + j, total, label)

        drafts = pipeline.observe_segments(sid, [seg], summarize_config or config,
                                           on_progress=_cb)
        hasil = curator.curate_beats(drafts, config)
        res.baru += len(hasil.beats)
        res.approved += hasil.approved_count
        res.rejected += hasil.rejected_count
        res.learnings += hasil.learnings_written
        for b in hasil.beats:
            by_index[b.segment_index] = b
        gabung = [by_index[k] for k in sorted(by_index)]
        pipeline.write_beats_jsonl(gabung, jsonl_path)
        pipeline.write_beats_md(gabung, md_path, sid)
    if on_progress and total:
        on_progress("summarize", total, total, "done")
    res.beats = [by_index[k] for k in sorted(by_index)]
    if not res.ditunda:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
    return res


_BEAT_FIELDS = {f.name for f in dataclasses.fields(pipeline.BeatDraft)}


def _load_existing_beats(jsonl_path: Path) -> list[pipeline.BeatDraft]:
    if not jsonl_path.is_file():
        return []
    out: list[pipeline.BeatDraft] = []
    for line in ipc.read_text(jsonl_path, encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        try:
            out.append(pipeline.BeatDraft(
                **{k: v for k, v in d.items() if k in _BEAT_FIELDS}))
        except TypeError:
            continue
    return out


def run_catchup_session(
    sid: str,
    config: dict,
    on_progress: Callable[[str, int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Rangkum HANYA segmen yang belum punya beat, lalu tulis gabungannya."""
    tx = _transcript_dir(config) / f"{sid}.jsonl"
    jsonl_path, _ = pipeline.beats_paths(sid, config)
    # Cicil: batasi segmen per pemanggilan supaya sesi live tidak menggiling
    # backlog puluhan segmen sekaligus. 0/None = tanpa batas (standalone).
    batas = config.get("observer_catchup_max_segmen")
    try:
        batas = int(batas or 0)
    except (TypeError, ValueError):
        batas = 0

    # Chain rangkuman catch-up. Saat dipanggil bridge LIVE, config memberi
    # chain GRATIS tanpa cursor — kerja latar tidak boleh merebut jalur yang
    # dipakai penonton (regresi live 21.49: breaker Cursor tutup, seluruh
    # sesi jatuh ke llama-8b). Pemanggil standalone (bridge mati) boleh
    # menyetel ["cursor"] untuk memakai composer + cache-nya.
    cfg_rangkum = {**config, "observer_cursor_role": str(
        config.get("observer_catchup_cursor_role", "catchup"))}
    rantai = config.get("observer_catchup_provider_chain")
    if rantai:
        cfg_rangkum["observer_provider_chain"] = list(rantai)
    # learnings: yang baru saja (kurasi hanya untuk segmen baru)
    hasil = observe_pending_segments(sid, tx, config, summarize_config=cfg_rangkum,
                                     on_progress=on_progress, max_segments=batas)
    dipangkas = hasil.ditunda
    gabung = hasil.beats
    if not hasil.baru:
        # Tidak ada ekor baru — tulis ulang beats (bump mtime) supaya sesi ini
        # tidak terdeteksi pending lagi di startup berikutnya.
        pipeline.write_beats_jsonl(gabung, jsonl_path)
        return {"sid": sid, "baru": 0, "approved": 0}
    try:
        import arti_observer_rag as obs_rag
        obs_rag.reindex_beats_session(sid, config)
    except Exception as e:  # noqa: BLE001 — indeks bisa menyusul; beats sudah aman di disk
        print(f"[Observer] Catch-up {sid}: reindex gagal ({type(e).__name__}: {e})")
    curator.append_timeline_to_session_md(sid, gabung)
    if dipangkas:
        # JANGAN diam soal yang ditunda — "sudah dirangkum" yang sebenarnya
        # separuh itu bohong yang mahal.
        print(f"[Observer] Catch-up {sid}: {dipangkas} segmen DITUNDA ke "
              "startup berikutnya (batas per sesi)")
    return {"sid": sid, "baru": hasil.baru,
            "approved": hasil.approved, "ditunda": dipangkas}


def run_catchup(
    config: dict,
    current_session_id: str = "",
    on_progress: Callable[[str, int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Jalankan catch-up untuk semua sesi pending. Dipanggil dari thread daemon."""
    if not config.get("observer_catchup_on_startup", True):
        return []
    pending = find_pending(config, current_session_id)
    if not pending:
        return []
    print(f"[Observer] Catch-up: {len(pending)} sesi belum (sepenuhnya) "
          f"dirangkum: {', '.join(pending)}")
    hasil: list[dict[str, Any]] = []
    for sid in pending:
        try:
            r = run_catchup_session(sid, config, on_progress=on_progress)
            if r["baru"]:
                print(f"[Observer] Catch-up {sid}: {r['baru']} beat baru "
                      f"({r['approved']} approved) — ingatannya terselamatkan")
            else:
                print(f"[Observer] Catch-up {sid}: tidak ada segmen baru")
            hasil.append(r)
        except Exception as e:  # noqa: BLE001 — satu sesi gagal jangan menghentikan sisanya
            print(f"[Observer] Catch-up {sid} GAGAL: {type(e).__name__}: {e}")
    return hasil

"""Observer segment pipeline — load JSONL, 10min beats, write artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent

_TS_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")


@dataclass
class Segment:
    index: int
    t_start: str
    t_end: str
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def event_count(self) -> int:
        return len(self.rows)

    def text_block(self) -> str:
        lines: list[str] = []
        for row in self.rows:
            kind = row.get("kind", "")
            name = row.get("name") or row.get("viewer") or ""
            text = (row.get("text") or "").strip()
            if not text:
                continue
            prefix = f"[{row.get('ts', '')}] {kind}"
            if name:
                prefix += f" {name}"
            lines.append(f"{prefix}: {text[:800]}")
        return "\n".join(lines)


@dataclass
class BeatDraft:
    session_id: str
    segment_index: int
    t_start: str
    t_end: str
    event_count: int
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    worth_embed: bool = False
    worth_learning: bool = False
    noise_level: str = "low"
    canon_conflicts: list[str] = field(default_factory=list)
    provider: str = ""
    curator_status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ts_to_seconds(ts: str) -> int | None:
    m = _TS_RE.match((ts or "").strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 3600 + mi * 60 + s


def seconds_to_ts(sec: int) -> str:
    sec = max(0, sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def load_transcript_rows(path: Path | str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def segment_by_minutes(rows: list[dict[str, Any]], minutes: int = 10) -> list[Segment]:
    if not rows:
        return []
    bucket_sec = max(1, minutes) * 60
    segments: dict[int, Segment] = {}
    fallback_idx = 0
    for row in rows:
        ts = row.get("ts", "")
        sec = ts_to_seconds(str(ts))
        if sec is None:
            idx = fallback_idx // max(1, len(rows) // max(1, (len(rows) // 50) or 1))
            sec = idx * bucket_sec
        idx = sec // bucket_sec
        if idx not in segments:
            start = idx * bucket_sec
            end = start + bucket_sec
            segments[idx] = Segment(
                index=idx,
                t_start=seconds_to_ts(start),
                t_end=seconds_to_ts(end),
                rows=[],
            )
        segments[idx].rows.append(row)
        fallback_idx += 1
    return [segments[k] for k in sorted(segments.keys())]


def write_beats_jsonl(beats: list[BeatDraft], path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for b in beats:
            f.write(json.dumps(b.to_dict(), ensure_ascii=False) + "\n")
    return p


def write_beats_md(beats: list[BeatDraft], path: Path | str, session_id: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Timeline beats — {session_id}", ""]
    for b in beats:
        status = b.curator_status
        lines.append(f"## {b.t_start} – {b.t_end} (seg {b.segment_index}, {status})")
        lines.append("")
        lines.append(b.summary or "(no summary)")
        if b.topics:
            lines.append("")
            lines.append(f"**Topics:** {', '.join(b.topics)}")
        if b.facts:
            lines.append("")
            lines.append("**Facts:**")
            for fact in b.facts:
                if isinstance(fact, dict):
                    lines.append(f"- {fact.get('text', fact)}")
                else:
                    lines.append(f"- {fact}")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def run_observe(
    session_id: str,
    transcript_path: Path | str,
    config: dict,
    on_progress: Callable[[str, int, int, str], None] | None = None,
) -> list[BeatDraft]:
    """Segment transcript and summarize each segment via observer client."""
    import arti_observer_client as client

    rows = load_transcript_rows(transcript_path)
    minutes = int(config.get("observer_segment_minutes", 10))
    segments = segment_by_minutes(rows, minutes=minutes)
    total = len(segments)
    beats: list[BeatDraft] = []

    for i, seg in enumerate(segments):
        if on_progress:
            on_progress("summarize", i, total, f"seg {seg.index} {seg.t_start}")
        block = seg.text_block()
        if not block.strip():
            beats.append(
                BeatDraft(
                    session_id=session_id,
                    segment_index=seg.index,
                    t_start=seg.t_start,
                    t_end=seg.t_end,
                    event_count=0,
                    summary="(kosong)",
                    noise_level="high",
                    curator_status="rejected",
                )
            )
            continue
        draft = client.summarize_segment(block, config)
        beats.append(
            BeatDraft(
                session_id=session_id,
                segment_index=seg.index,
                t_start=seg.t_start,
                t_end=seg.t_end,
                event_count=seg.event_count,
                summary=draft.get("summary", ""),
                topics=list(draft.get("topics") or []),
                facts=list(draft.get("facts") or []),
                worth_embed=bool(draft.get("worth_embed", False)),
                worth_learning=bool(draft.get("worth_learning", False)),
                noise_level=str(draft.get("noise_level") or "low"),
                provider=str(draft.get("provider") or ""),
                curator_status="pending",
            )
        )

    if on_progress:
        on_progress("summarize", total, total, "done")
    return beats


def beats_paths(session_id: str, config: dict | None = None) -> tuple[Path, Path]:
    cfg = config or {}
    vault = _ROOT / "vault" / "sessions"
    jsonl = vault / f"{session_id}_beats.jsonl"
    md = vault / f"{session_id}_beats.md"
    return jsonl, md

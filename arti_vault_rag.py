"""Vault RAG — chunk, embed (LM Studio), SQLite vectors, hybrid retrieve (Fase 4)."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import requests

from arti_runtime import vault_farm

_ROOT = Path(__file__).resolve().parent
_rag_search_lock = threading.Lock()
_embedding_session = requests.Session()
_embedding_breaker_lock = threading.Lock()
_embedding_breaker_until = 0.0
_embedding_probe_active = False
_embedding_active_base: str | None = None
_embedding_fail_count = 0
_embedding_route_logged = False
LOCAL_LMSTUDIO_EMBED_URL = "http://127.0.0.1:1234/v1"
_rerank_session = requests.Session()
_rerank_breaker_lock = threading.Lock()
_rerank_breaker_until = 0.0
_rerank_probe_active = False
_log = logging.getLogger(__name__)
_QUERY_EMBED_CACHE: dict[str, list[float]] = {}
_QUERY_EMBED_CACHE_MAX = 32

_ARTI_SUBJECT = r"(?:arti|kamu|kau|dirimu|lu|lo|elu)"
_ORIGIN_EVENT = r"(?:debut|lahir|diciptakan|dibuat|ada|eksis|jadi co host|pertama kali (?:siaran|stream))"
_ORIGIN_RAG_RE = re.compile(
    rf"\b(?:"
    rf"{_ARTI_SUBJECT}\s+(?:(?:itu|kamu)\s+)?(?:"
    r"debut|lahir|diciptakan|dibuat|asal usul|sejarah|"
    r"pertama kali (?:siaran|stream|jadi co host)|"
    r"mulai (?:ada|eksis|jadi co host|(?:siaran|stream) sejak kapan|"
    r"(?:sejak )?kapan(?: (?:debut|sih|ya|dong|tuh))*$)|"
    r"(?:sudah|udah) berapa lama (?:ada|eksis|jadi co host)|ada sejak kapan)|"
    rf"(?:debut|asal usul|sejarah|kelahiran)\s+{_ARTI_SUBJECT}|"
    rf"pertama kali\s+{_ARTI_SUBJECT}\s+(?:siaran|stream|jadi co host)|"
    rf"(?:sejak kapan|dari kapan)\s+{_ARTI_SUBJECT}\s+(?:itu\s+)?{_ORIGIN_EVENT}|"
    rf"{_ARTI_SUBJECT}\s+(?:sejak kapan|kapan)\s+(?:mulai\s+)?{_ORIGIN_EVENT}"
    r")\b",
    re.IGNORECASE,
)


def enrich_rag_query(query: str, config: dict | None = None) -> str:
    """Boost only questions about ARTI's own origin, not general memories."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    q = (query or "").strip()
    if not q:
        return q
    tokens = " ".join(re.findall(r"\w+", q.lower()))
    if not _ORIGIN_RAG_RE.search(tokens):
        return q
    debut = cfg.get("arti_debut_label", "debut date")
    archive = cfg.get("arti_archive_from", "YYYY-MM-DD")
    return (
        f"{q} Arti fakta contoh {debut} arsip vault sessions {archive} "
        f"arti_origin sejarah stream"
    )


DEFAULT_CONFIG: dict[str, Any] = {
    "vault_rag_enabled": True,
    "vault_rag_live_enabled": True,
    "vault_rag_lite_enabled": True,
    "vault_rag_db_path": "data/vault_rag.db",
    # `docs/handoff/**/*.md` DIKELUARKAN 2026-07-31. Dokumen itu handoff untuk developer,
    # bukan memori Arti, dan tiga masalah nyata:
    #   1. BASI — FASE-2.md masih menyebut laguna-xs.2, owl-alpha, scout, qwen3-32b;
    #      semuanya sudah 404 atau diganti. Arti akan menjelaskan sistem yang tidak ada lagi.
    #   2. ALTITUDE SALAH — judul seksinya "Model Live2D — parameter kritis (<model>.cdi3.json)",
    #      "Scribble lock (SEMUA .exp3.json di folder model)". Kalau dibacakan, Arti terdengar
    #      seperti membacakan dokumentasi, bukan ngobrol.
    #   3. MENDESAK MEMORI ASLI — dokumen teknis bisa mengambil banyak chunk dan
    #      mendesak memori kurasi. Padahal ARTI_SOUL.md membatasi jawaban 3 kalimat,
    #      jadi detail sebanyak itu tidak mungkin terpakai.
    # Penggantinya: `vault/concepts/arti_self_knowledge.md` — jawaban soal cara kerjanya
    # sendiri, ditulis pakai suara Arti di altitude yang penonton peduli.
    # transcripts/**/*.jsonl DIKELUARKAN: transkrip mentah (chat spam, noise ASR,
    # leaderboard bot) ikut ter-embed dan menenggelamkan memori kurasi. Persis
    # dialog mentah diwakili ringkasan + beats, transkrip tetap arsip di disk.
    # Ingat: menghapus glob TIDAK membersihkan chunk lama — hapus DB lalu rebuild.
    "vault_rag_index_globs": [
        "vault/**/*.md",
        "vault/sessions/*_beats.md",
        "ARTI_*.md",
    ],
    "vault_rag_chunk_chars": 420,
    "vault_rag_chunk_overlap": 60,
    "vault_rag_min_chunk_chars": 48,
    "vault_rag_top_k": 2,  # 5 -> 2: diet bahan 17 Agu — selaras CONFIG bridge
    # Maksimum potongan dari SATU berkas per query. Tanpa ini, satu berkas bisa memakan
    # 3-4 dari 5 slot dan menenggelamkan memori lain — lihat _select_diverse.
    # 0 = matikan pembatasan (perilaku sebelum 2026-07-31).
    "vault_rag_max_per_source": 2,
    # 2400, naik dari 2200 pada 2026-07-31. Header instruksi RAG tumbuh dari 76 jadi 172
    # char (menambahkan aturan konflik tanggal), dan itu overhead TETAP yang tidak boleh
    # mengambil porsi cuplikan: terukur menggusur hit ke-5 di 2 dari 6 query uji. Selisih
    # ~200 char ≈ 50 token per turn — murah dibanding kehilangan satu cuplikan.
    "vault_rag_max_context_chars": 2400,
    "vault_rag_semantic_weight": 0.72,
    "vault_rag_min_score": 0.28,
    "vault_rag_rerank_enabled": False,
    "vault_rag_rerank_base_url": "",
    "vault_rag_rerank_top_n": 20,
    "vault_rag_rerank_connect_timeout_sec": 2.0,
    "vault_rag_rerank_timeout_sec": 1.5,
    "vault_rag_rerank_breaker_sec": 60.0,
    "vault_rag_rerank_min_score": None,
    "vault_rag_recency_boost_today": 0.15,
    "vault_rag_recency_boost_week": 0.08,     # dipakai hanya di mode cliff (half_life <= 0)
    # Decay halus recency (upgrade B v0.7): maks tetap boost_today, separuh tiap
    # 14 hari, praktis nol ~8 minggu. <= 0 = balik ke cliff lama.
    "vault_rag_recency_half_life_days": 14.0,
    # Query temporal ("kapan terakhir...") -> boost recency digandakan.
    "vault_rag_temporal_multiplier": 2.0,
    "lmstudio_embedding_base_url": "http://127.0.0.1:1234/v1",
    # Tried after the configured primary fails. Default = local LM Studio (same as the primary default).
    "lmstudio_embedding_fallback_base_url": "http://127.0.0.1:1234/v1",
    "lmstudio_embedding_model": "text-embedding-mxbai-embed-large-v1",
    "lmstudio_embedding_query_prefix": "",
    "lmstudio_embedding_doc_prefix": "",
    "lmstudio_embedding_timeout_sec": 120,
    "lmstudio_embedding_connect_timeout_sec": 2.0,
    "lmstudio_embedding_breaker_sec": 60.0,
    "lmstudio_embedding_batch_size": 16,
    # Observer RAG at reply time (brain batch B, 2026-10-21). The observer DB
    # (stream-moment beats) is searched with the SAME query embedding and its
    # top-k joins the vault candidates BEFORE the single rerank call. Missing or
    # failing DB = exactly the old vault-only path. Kill switch below.
    "observer_rag_live_enabled": True,
    "observer_rag_live_top_k": 3,
    "observer_rag_live_breaker_sec": 60.0,
    "observer_db_path": "data/observer_rag.db",
}


def source_root(config: dict | None = None) -> Path:
    """Where the notes live (vault/**, ARTI_*.md). Default: this checkout. Mabar points it at the live stream-folder
    folder and keeps the db run-local: notes are only READ from here (index + canon origin), never written."""
    root = (config or {}).get("vault_rag_root")
    return Path(root).resolve() if root else _ROOT


def _db_path(config: dict) -> Path:
    rel = config.get("vault_rag_db_path", "data/vault_rag.db")
    p = Path(rel)
    if not p.is_absolute():
        p = _ROOT / p
    root = config.get("vault_rag_root")
    if root and Path(p).resolve().is_relative_to(Path(root).resolve()):
        # A foreign notes root is read-only: its own db (and anything else under it) is never opened for writing.
        raise PermissionError("vault_rag_db_path must be outside vault_rag_root")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(config: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(config), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(config: dict | None = None) -> None:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    conn = _connect(cfg)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            source_type TEXT NOT NULL,
            folder TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            heading TEXT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            mtime REAL NOT NULL,
            char_count INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS embeddings (
            chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS folder_summaries (
            folder TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_path);
        CREATE INDEX IF NOT EXISTS idx_chunks_folder ON chunks(folder);
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content,
            heading,
            source_path,
            tokenize='unicode61'
        )
        """
    )
    conn.commit()
    conn.close()


def _content_hash(source_path: str, chunk_index: int, content: str) -> str:
    raw = f"{source_path}\0{chunk_index}\0{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_DATE_IN_PATH = re.compile(r"(\d{4}-\d{2}-\d{2})")


# Query yang bertanya soal WAKTU ("kapan terakhir...", "kemarin ngapain") —
# recency boost-nya digandakan supaya memori terbaru menang dari yang lama.
# "kapan" POLOS sengaja BUKAN marker: "kapan arti debut" itu pertanyaan
# SEJARAH — boost recency justru menenggelamkan fakta kanon (ketahuan di
# health check 2026-08-02: probe origin kalah dari chunk sesi score 0.9995).
# "[recorded remark omitted]" tetap tertangkap lewat kata "terakhir".
_TEMPORAL_QUERY_RE = re.compile(
    r"\b(terakhir|kemarin|tadi|barusan|baru saja|baru-baru|terbaru|paling baru|"
    r"minggu lalu|bulan lalu|akhir-akhir ini|belakangan)\b",
    re.IGNORECASE,
)


def is_temporal_query(query: str) -> bool:
    return bool(_TEMPORAL_QUERY_RE.search(query or ""))


def recency_multiplier(query: str, config: dict) -> float:
    """Pengganda boost recency: >1 hanya untuk query temporal."""
    if is_temporal_query(query):
        return float(config.get("vault_rag_temporal_multiplier", 2.0))
    return 1.0


def _recency_score_boost(source_path: str, config: dict) -> float:
    """Additive boost — decay halus, bukan cliff (upgrade B, v0.7).

    Cliff lama (+0.15 hari ini / +0.08 <=7 hari / 0 sisanya) membuat memori umur
    8 hari dan 8 minggu bernilai sama. Decay eksponensial dengan MAGNITUDO sama
    (maks tetap boost_today) menghormati desain awal — konflik fakta tetap
    diselesaikan lewat LABEL tanggal di prompt, boost hanya penentu urutan halus.
    Lantai 0.01 = praktis nol di ~8 minggu.

    `vault_rag_recency_half_life_days <= 0` mengembalikan perilaku cliff lama.
    """
    m = _DATE_IN_PATH.search(source_path or "")
    if not m:
        return 0.0
    try:
        from datetime import datetime

        chunk_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        age = (datetime.now().date() - chunk_date).days
        if age < 0:
            return 0.0
        today_boost = float(config.get("vault_rag_recency_boost_today", 0.15))
        half_life = float(config.get("vault_rag_recency_half_life_days", 14.0))
        if half_life <= 0:  # mode cliff lama
            if age == 0:
                return today_boost
            if age <= 7:
                return float(config.get("vault_rag_recency_boost_week", 0.08))
            return 0.0
        boost = today_boost * (0.5 ** (age / half_life))
        return boost if boost >= 0.01 else 0.0
    except ValueError:
        pass
    return 0.0


_TECHNICAL_HEADING = re.compile(
    r"^(?:api\s+usage|metrik|metrics|latency\s+metrics|cuplikan\s+error\s*\(debug log\))(?=\s|$)", re.I)
_BEAT_METADATA = re.compile(r"\s*\(seg\s+\d+\s*,\s*approved\)", re.I)
_TRANSCRIPT_PATH = re.compile(r"(?:transcripts[/\\]|arti-live-session[^\s`]*\.md)", re.I)


def _memory_heading(heading: str) -> str:
    heading = _BEAT_METADATA.sub("", heading or "").strip()
    if _TRANSCRIPT_PATH.search(heading) or re.match(r"^Restart\s+\d", heading, re.I):
        return ""
    return heading


def _filter_technical_sections(text: str, heading: str = "") -> str:
    """Strip bridge telemetry, preserving actual stream conversation.

    The heading argument fences old DB chunks whose body starts halfway through
    a table. The same filter runs before chunking and before prompt truncation;
    deployment therefore protects existing indexes without rebuilding them.
    """
    from arti_live_safety import is_backstage
    if is_backstage(_memory_heading(heading)) or _TECHNICAL_HEADING.match(heading.strip().lstrip("#").strip()):
        return ""
    text = re.sub(r"<!--.*?(?:-->|\Z)", "", text, flags=re.S)
    out = []
    skip_level = None
    for line in text.replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if match:
            level, title = len(match[1]), match[2]
            if skip_level is not None and level > skip_level:
                continue
            skip_level = level if (_TECHNICAL_HEADING.match(title) or is_backstage(_memory_heading(title))) else None
            if skip_level is not None:
                continue
            title = _memory_heading(title)
            if title:
                out.append(match[1] + " " + title)
            continue
        if skip_level is not None or is_backstage(stripped):
            continue
        if (re.match(r"^>\s*Vault slim\b", stripped, re.I)
                or _TRANSCRIPT_PATH.search(stripped)):
            continue
        # Legacy heading-less telemetry fragments (including overlap chunks).
        if "|" in stripped and re.search(
                r"\b(?:provider|model|tokens?|latency|subsystem|cost|trigger arti|jawaban arti|"
                r"gpt-\d|claude-|gemini-|groq|openrouter|lmstudio|cursor_chain|whisper|embeddinggemma|codex|supertone)\b",
                stripped, re.I):
            continue
        if stripped.startswith("|") and re.fullmatch(r"[|:\s-]+", stripped):
            continue
        out.append(_BEAT_METADATA.sub("", line))
    return "\n".join(out).strip()


def _preprocess_source_text(text: str) -> str:
    """Bersihkan fence code & baris duplikat sebelum chunk."""
    text = _filter_technical_sections(text)
    # Buang blok ```...``` utuh (log mentah) — ringkasan di luar fence tetap ke-index
    text = re.sub(r"```[\w]*\n.*?```", "\n", text, flags=re.DOTALL)
    text = re.sub(r"```+", " ", text)
    lines: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        key = re.sub(r"\s+", " ", ln.lower())[:120]
        if key in seen and len(key) < 80:
            continue
        seen.add(key)
        lines.append(ln)
    return "\n\n".join(lines)


def _is_junk_chunk(content: str, min_chars: int = 48) -> bool:
    c = content.strip()
    if len(c) < min_chars:
        return True
    letters = sum(1 for ch in c if ch.isalpha())
    if letters < 22:
        return True
    if c.count("`") >= 3 and letters < 40:
        return True
    words = re.findall(r"[\w']+", c, re.UNICODE)
    if len(words) >= 4 and len(set(w.lower() for w in words)) < 5:
        return True
    # Fragmen overlap: hampir semua potongan substring pendek yang sama
    if len(c) < 90 and re.fullmatch(r"[\s\W\d\[\]:]+", c.replace("Streamer", "").replace("Arti", "")):
        return True
    return False


def _split_dense_log_paragraph(para: str, chunk_chars: int) -> list[str]:
    """Pecah log `[HH:MM:SS] [Streamer]` per baris, bukan geser 1 kata."""
    if not re.search(r"\[\d{1,2}:\d{2}:\d{2}\]", para):
        return []
    parts = re.split(r"(?=\[\d{1,2}:\d{2}:\d{2}\])", para)
    out: list[str] = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(buf) + len(p) + 1 <= chunk_chars:
            buf = f"{buf} {p}".strip() if buf else p
        else:
            if buf and len(buf) >= 48:
                out.append(buf)
            buf = p if len(p) <= chunk_chars else p[:chunk_chars]
    if buf and len(buf) >= 48:
        out.append(buf)
    return out


def chunk_text(text: str, chunk_chars: int = 420, overlap: int = 60) -> list[tuple[str, str]]:
    """Return list of (heading, chunk_body)."""
    text = _preprocess_source_text(text.strip())
    if not text:
        return []

    sections: list[tuple[str, str]] = []
    current_heading = ""
    buf: list[str] = []

    def flush_section() -> None:
        nonlocal buf, current_heading
        body = "\n".join(buf).strip()
        if body:
            sections.append((current_heading, body))
        buf = []

    for line in text.splitlines():
        if re.match(r"^#{1,3}\s+", line):
            flush_section()
            current_heading = line.strip().lstrip("#").strip()
        else:
            buf.append(line)
    flush_section()

    if not sections:
        sections = [("", text)]

    out: list[tuple[str, str]] = []
    for heading, body in sections:
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        if not paras:
            continue
        current = ""
        for para in paras:
            candidate = f"{current}\n\n{para}".strip() if current else para
            if len(candidate) <= chunk_chars:
                current = candidate
                continue
            if current:
                out.append((heading, current))
            if len(para) <= chunk_chars:
                current = para
                continue
            log_chunks = _split_dense_log_paragraph(para, chunk_chars)
            if log_chunks:
                for piece in log_chunks:
                    out.append((heading, piece))
                current = ""
                continue
            step = max(chunk_chars - overlap, 80)
            start = 0
            while start < len(para):
                end = min(len(para), start + chunk_chars)
                piece = para[start:end].strip()
                if len(piece) >= 48:
                    out.append((heading, piece))
                if end >= len(para):
                    break
                start += step
            current = ""
        if current:
            out.append((heading, current))

    min_c = 48
    return [(h, b) for h, b in out if not _is_junk_chunk(b, min_c)]


def _read_source(path: Path) -> str | None:
    if not path.is_file():
        return None
    if path.suffix.lower() == ".jsonl":
        lines = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                kind = row.get("kind", "")
                name = row.get("name") or row.get("viewer") or ""
                text = (row.get("text") or "").strip()
                if text:
                    prefix = f"[{row.get('ts', '')}] {kind}"
                    if name:
                        prefix += f" {name}"
                    lines.append(f"{prefix}: {text[:500]}")
            except json.JSONDecodeError:
                continue
        return "\n".join(lines) if lines else None
    return path.read_text(encoding="utf-8", errors="replace")


def _source_type(rel: str) -> str:
    if rel.startswith("vault/sessions") and "_beats.md" in rel:
        return "session_beats"
    if rel.startswith("vault/sessions"):
        return "vault_session"
    if rel.startswith("vault/concepts"):
        return "vault_concept"
    if rel.startswith("transcripts/"):
        return "transcript"
    if rel.startswith("docs/handoff"):
        return "handoff"
    return "vault_other"


def iter_index_files(config: dict) -> list[Path]:
    cfg = {**DEFAULT_CONFIG, **config}
    globs = cfg.get("vault_rag_index_globs") or DEFAULT_CONFIG["vault_rag_index_globs"]
    seen: set[Path] = set()
    files: list[Path] = []
    root = source_root(cfg)
    for pattern in globs:
        for p in sorted(root.glob(pattern)):
            if not p.is_file():
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            if p.name.lower() == "index.md" and "sessions" in str(p).replace("\\", "/"):
                continue
            if _is_excluded_from_index(p, cfg):
                continue
            files.append(p)
    return files


# Pola berkas yang cocok dengan glob index tapi TIDAK boleh jadi memori Arti.
_INDEX_EXCLUDE_SUFFIXES = (".example.md",)
_INDEX_EXCLUDE_NAMES = ("tadc-ep1-sample.md",)


def _soul_in_system_prompt(source_path: str | Path, config: dict) -> bool:
    """Exclude raw soul from new indexing and legacy retrieval, without DB deletion."""
    name = str(source_path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return config.get("soul_prompt_enabled", True) and name == "arti_soul.md"


def _is_excluded_from_index(p: Path, config: dict | None = None) -> bool:
    """True kalau berkas ini tertangkap glob tapi isinya bukan memori sungguhan.

    - `*.example.md` — `ARTI_SOUL.example.md` dan `ARTI_VIEWERS.example.md` tertangkap
      glob `ARTI_*.md`. Isinya placeholder: `ExampleViewer`, `(nama co-host AI kamu)`,
      `YYYY-MM-DD`. Kalau ter-embed, RAG bisa mengembalikan `ExampleViewer` sebagai
      penonton sungguhan atau template persona sebagai kepribadian Arti.
    - `tadc-ep1-sample.md` — berkas contoh format watch-party (bukan watch party asli).
      `watch_party_enabled` default False, tapi berkas ini tetap ter-embed lewat
      `vault/**/*.md`, sehingga konten The Amazing Digital Circus bisa muncul di jawaban
      yang tidak ada hubungannya.
    """
    if _soul_in_system_prompt(p, config or {}):
        return True
    name = p.name.lower()
    if name in _INDEX_EXCLUDE_NAMES:
        return True
    return any(name.endswith(sfx) for sfx in _INDEX_EXCLUDE_SUFFIXES)


def list_lmstudio_embedding_models(base_url: str, timeout: int = 10) -> list[str]:
    try:
        res = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if res.status_code != 200:
            return []
        data = res.json()
        ids = []
        for item in data.get("data", []):
            mid = item.get("id") or item.get("name")
            if mid:
                ids.append(str(mid))
        return ids
    except Exception:
        return []


def _budgeted_timeouts(
    connect: float, read: float, deadline: float | None = None,
) -> tuple[float, float]:
    """Reserve connect + read within the remaining live budget, including retries."""
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining < 0.1:
            raise requests.Timeout("RAG live budget exhausted")
        if connect + read + 0.01 > remaining:
            connect = min(connect, remaining * 0.25)
            read = min(read, remaining - connect - 0.01)
    return connect, read


def embedding_route(config: dict | None = None) -> tuple[str, str]:
    """Configured primary and a distinct fallback (empty if same/unset)."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    primary = (cfg.get("lmstudio_embedding_base_url") or LOCAL_LMSTUDIO_EMBED_URL).rstrip("/")
    fallback = (cfg.get("lmstudio_embedding_fallback_base_url") or "").strip().rstrip("/")
    if fallback == primary:
        fallback = ""
    return primary, fallback


def embedding_status() -> dict[str, Any]:
    with _embedding_breaker_lock:
        return {
            "base": _embedding_active_base,
            "fails": _embedding_fail_count,
            "available": time.monotonic() >= _embedding_breaker_until,
        }


def reset_embedding_client() -> None:
    """Tests: forget the remembered host, fail counter, and breaker."""
    global _embedding_active_base, _embedding_fail_count, _embedding_route_logged
    global _embedding_breaker_until, _embedding_probe_active
    with _embedding_breaker_lock:
        _embedding_active_base = None
        _embedding_fail_count = 0
        _embedding_route_logged = False
        _embedding_breaker_until = 0.0
        _embedding_probe_active = False


def probe_embedding_endpoints(config: dict | None = None, *, get=None, timeout: float = 1.5) -> dict[str, Any]:
    """Which embedding host answers. Never raises. `get(url, timeout=)` is injectable (tests: no network)."""
    get = get or (lambda url, **kw: requests.get(url, timeout=kw.get("timeout", timeout)))
    primary, fallback = embedding_route(config)
    tried: list[tuple[str, str, str]] = []
    for kind, base in (("primary", primary), ("fallback", fallback)):
        if not base:
            continue
        try:
            res = get(f"{base}/models", timeout=timeout)
            code = int(getattr(res, "status_code", 0) or 0)
        except Exception as exc:  # noqa: BLE001 — probe must not raise
            tried.append((kind, base, type(exc).__name__))
            continue
        if code == 200:
            return dict(ok=True, using=kind, base=base, fallback=fallback, tried=tried)
        tried.append((kind, base, "HTTP %s" % code))
    return dict(ok=False, using=None, base=None, fallback=fallback, tried=tried)


def _embed_bases(cfg: dict) -> list[str]:
    primary, fallback = embedding_route(cfg)
    active = _embedding_active_base
    if active and active in (primary, fallback):
        ordered = [active]
        other = fallback if active == primary else primary
        if other and other not in ordered:
            ordered.append(other)
        return ordered
    ordered = [primary]
    if fallback:
        ordered.append(fallback)
    return ordered


def _log_embed_route_once(using: str, base: str | None, why: str = "") -> None:
    global _embedding_route_logged
    if _embedding_route_logged:
        return
    _embedding_route_logged = True
    extra = ("; " + why) if why else ""
    print("[Vault RAG] embed endpoint: using %s%s fails=%d" % (using + ((" " + base) if base else ""), extra, _embedding_fail_count))


def _embed_one(
    texts: list[str],
    cfg: dict,
    base: str,
    *,
    telemetry_subsystem: str,
    telemetry_purpose: str,
    probing: bool,
    trip_breaker: bool,
) -> list[list[float]]:
    global _embedding_breaker_until, _embedding_probe_active
    model = cfg.get("lmstudio_embedding_model") or "text-embedding-mxbai-embed-large-v1"
    timeout = (float(cfg.get("lmstudio_embedding_connect_timeout_sec", 2.0)),
               float(cfg.get("lmstudio_embedding_timeout_sec", 120)))
    batch_size = int(cfg.get("lmstudio_embedding_batch_size", 16))
    url = f"{base}/embeddings"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer lm-studio"}
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = [t.replace("\n", " ").strip()[:8000] for t in texts[i : i + batch_size]]
        payload = {"model": model, "input": batch}
        t0 = time.perf_counter()
        try:
            try:
                res = _embedding_session.post(
                    url, headers=headers, json=payload,
                    timeout=_budgeted_timeouts(*timeout, cfg.get("_rag_deadline")),
                )
            except requests.ConnectionError as e:
                # Keep-alive that the server/NAT already dropped fails once even when
                # the host is healthy — retry once on a new connection. Connect-timeout is not retried.
                if isinstance(e, requests.Timeout):
                    raise
                res = _embedding_session.post(
                    url, headers=headers, json=payload,
                    timeout=_budgeted_timeouts(*timeout, cfg.get("_rag_deadline")),
                )
        except (requests.ConnectionError, requests.Timeout):
            if trip_breaker:
                with _embedding_breaker_lock:
                    _embedding_breaker_until = time.monotonic() + float(cfg.get("lmstudio_embedding_breaker_sec", 60.0))
                    _embedding_probe_active = False
            raise
        ms = int((time.perf_counter() - t0) * 1000)
        ok = res.status_code == 200
        try:
            import arti_api_telemetry as tel

            tel.record_call(
                subsystem=telemetry_subsystem,
                provider="lmstudio",
                model=model,
                latency_ms=ms,
                ok=ok,
                usage=tel.UsageInfo(total_tokens=sum(len(t) for t in batch)),
                extra={"batch_size": len(batch), "purpose": telemetry_purpose, "base": base},
                config=cfg,
            )
        except Exception:
            pass
        if res.status_code != 200:
            if trip_breaker:
                with _embedding_breaker_lock:
                    if res.status_code >= 500:
                        _embedding_breaker_until = time.monotonic() + float(cfg.get("lmstudio_embedding_breaker_sec", 60.0))
                    elif probing:
                        _embedding_breaker_until = 0.0
                    _embedding_probe_active = False
            raise RuntimeError(f"LM Studio embeddings HTTP {res.status_code}: {res.text[:300]}")
        try:
            body = res.json()
            items = sorted(body.get("data", []), key=lambda x: x.get("index", 0))
            for item in items:
                emb = item.get("embedding")
                if not emb:
                    raise RuntimeError("Embedding kosong dari LM Studio")
                vectors.append([float(x) for x in emb])
        except Exception:
            if probing and trip_breaker:
                with _embedding_breaker_lock:
                    _embedding_breaker_until = 0.0
                    _embedding_probe_active = False
            raise
    if probing and trip_breaker:
        with _embedding_breaker_lock:
            _embedding_breaker_until = 0.0
            _embedding_probe_active = False
    return vectors


def embed_texts(
    texts: list[str],
    config: dict,
    *,
    telemetry_subsystem: str = "embed",
    telemetry_purpose: str = "reindex",
) -> list[list[float]]:
    """Embed via LM Studio OpenAI-compatible /v1/embeddings.

    Try the configured primary, then `lmstudio_embedding_fallback_base_url` (local LM Studio
    by default). Remember which host works. On total failure trip the breaker (backoff) and
    count the miss; the next call after backoff re-probes. One log line at the first outcome.
    """
    if not texts:
        return []
    cfg = {**DEFAULT_CONFIG, **config}
    bases = _embed_bases(cfg)

    global _embedding_breaker_until, _embedding_probe_active, _embedding_active_base, _embedding_fail_count
    now = time.monotonic()
    with _embedding_breaker_lock:
        if now < _embedding_breaker_until:
            raise requests.ConnectionError("Embedding circuit breaker open")
        if _embedding_breaker_until and _embedding_probe_active:
            raise requests.ConnectionError("Embedding circuit breaker probe in progress")
        probing = bool(_embedding_breaker_until)
        if probing:
            _embedding_probe_active = True

    last_exc: BaseException | None = None
    primary, fallback = embedding_route(cfg)
    for i, base in enumerate(bases):
        last = i == len(bases) - 1
        try:
            vectors = _embed_one(
                texts, cfg, base,
                telemetry_subsystem=telemetry_subsystem,
                telemetry_purpose=telemetry_purpose,
                probing=probing, trip_breaker=last,
            )
        except (requests.ConnectionError, requests.Timeout, RuntimeError) as exc:
            last_exc = exc
            continue
        kind = "fallback" if fallback and base == fallback else "primary"
        why = "primary unreachable" if kind == "fallback" else ""
        with _embedding_breaker_lock:
            _embedding_active_base = base
            if probing:
                _embedding_breaker_until = 0.0
                _embedding_probe_active = False
        _log_embed_route_once(kind, base, why)
        return vectors

    with _embedding_breaker_lock:
        _embedding_active_base = None
        _embedding_fail_count += 1
        _embedding_probe_active = False
        if last_exc is None or isinstance(last_exc, (requests.ConnectionError, requests.Timeout)):
            _embedding_breaker_until = time.monotonic() + float(cfg.get("lmstudio_embedding_breaker_sec", 60.0))
        elif isinstance(last_exc, RuntimeError) and "HTTP 5" in str(last_exc):
            _embedding_breaker_until = time.monotonic() + float(cfg.get("lmstudio_embedding_breaker_sec", 60.0))
    _log_embed_route_once("none", None, "primary and fallback unreachable")
    if last_exc:
        raise last_exc
    raise requests.ConnectionError("Embedding endpoints unreachable")


def embedding_breaker_open() -> bool:
    with _embedding_breaker_lock:
        return time.monotonic() < _embedding_breaker_until


def _rerank_candidates(
    combined: list[tuple[float, dict[str, Any]]],
    query: str,
    cfg: dict,
    *,
    deadline: float,
) -> list[tuple[float, dict[str, Any]]]:
    """Fail open to hybrid order; never mutate hits until a full response validates."""
    if not combined or not cfg.get("vault_rag_rerank_enabled", False):
        return combined
    base = (cfg.get("vault_rag_rerank_base_url") or "").rstrip("/")
    if not base:
        return combined
    try:
        n = min(len(combined), max(1, int(cfg.get("vault_rag_rerank_top_n", 20))))
        connect = float(cfg.get("vault_rag_rerank_connect_timeout_sec", 2.0))
        read = float(cfg.get("vault_rag_rerank_timeout_sec", 1.5))
        _budgeted_timeouts(connect, read, deadline)
    except (ValueError, TypeError, requests.Timeout):
        return combined

    global _rerank_breaker_until, _rerank_probe_active
    with _rerank_breaker_lock:
        if time.monotonic() < _rerank_breaker_until:
            return combined
        if _rerank_breaker_until and _rerank_probe_active:
            return combined
        probing = bool(_rerank_breaker_until)
        if probing:
            _rerank_probe_active = True

    try:
        payload = {"query": query, "documents": [h["content"] for _, h in combined[:n]], "top_n": n}
        for attempt in range(2):
            try:
                response = _rerank_session.post(
                    f"{base}/v1/rerank", json=payload,
                    timeout=_budgeted_timeouts(connect, read, deadline),
                )
                break
            except requests.ConnectionError as exc:
                if attempt or isinstance(exc, requests.Timeout):
                    raise
                # Same single fresh-connection retry as the embedding transport.
        if response.status_code != 200:
            raise RuntimeError(f"Rerank HTTP {response.status_code}")
        results = response.json()["results"]
        if not isinstance(results, list) or len(results) != n:
            raise ValueError("Incomplete rerank results")
        scores: dict[int, float] = {}
        for result in results:
            index = result["index"]
            score = float(result["relevance_score"])
            if (type(index) is not int or index < 0 or index >= n or index in scores
                    or not math.isfinite(score)):
                raise ValueError("Invalid rerank result")
            scores[index] = score
        ranked = [(hybrid, {**hit, "rerank_score": scores[i]})
                  for i, (hybrid, hit) in enumerate(combined[:n])]
        ranked.sort(key=lambda pair: pair[1]["rerank_score"], reverse=True)
        cutoff = cfg.get("vault_rag_rerank_min_score")
        if cutoff is None:
            ranked.extend(combined[n:])
        else:
            # Unscored tail cannot bypass an explicitly enabled relevance gate.
            ranked = [pair for pair in ranked if pair[1]["rerank_score"] >= float(cutoff)]
    except Exception as exc:
        with _rerank_breaker_lock:
            _rerank_breaker_until = time.monotonic() + float(cfg.get("vault_rag_rerank_breaker_sec", 60.0))
            _rerank_probe_active = False
        _log.debug("[Vault RAG] rerank skipped (%s)", type(exc).__name__)
        return combined

    with _rerank_breaker_lock:
        if probing:
            _rerank_breaker_until = 0.0
            _rerank_probe_active = False
    if time.monotonic() >= deadline:
        return combined
    _log.debug("[Vault RAG] rerank top_score=%.4f", max(scores.values()))
    return ranked


def load_cli_config(path: Path | None = None) -> dict[str, Any]:
    """Load shipped defaults plus the local machine overlay used by the bridge."""
    cfg = dict(DEFAULT_CONFIG)
    local_config_path = path or (_ROOT / "config_local.json")
    if local_config_path.is_file():
        try:
            local_overrides = json.loads(local_config_path.read_text(encoding="utf-8"))
            if isinstance(local_overrides, dict):
                cfg.update(local_overrides)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[CONFIG] config_local.json gagal dibaca, pakai default: {exc}")
    return cfg


def _prewarm_ping(cfg: dict) -> None:
    """Badan pemanasan — dipisah supaya bisa diuji tanpa thread."""
    t0 = time.perf_counter()
    try:
        embed_texts(["ping pemanasan"], cfg, telemetry_purpose="prewarm")
        print(f"[Vault RAG] Embedding hangat ({time.perf_counter() - t0:.1f}s)")
    except Exception as e:  # noqa: BLE001 — pemanasan gagal bukan alasan crash
        print(
            f"[Vault RAG] Pemanasan embedding gagal ({type(e).__name__}) — "
            "panggilan RAG pertama mungkin jatuh ke kata kunci"
        )


def prewarm_embedding(config: dict | None = None) -> None:
    """Bangunkan model embedding LM Studio di LATAR, jangan tunggu giliran.

    Log 18 Agu 22.31: LM Studio me-load Qwen3 dari nol saat request pertama
    datang (~10-16 dtk) sementara timeout query cuma 8 dtk — panggilan RAG
    pertama SELALU timeout dan jatuh ke pencarian kata kunci. Ping ini
    dipanggil di awal startup wizard: modelnya bangun selagi Bohan masih
    menjawab checklist. Timeout ping dilonggarkan sendiri (cold load memang
    lama; justru itu yang sedang dibayar di sini)."""
    cfg = {**DEFAULT_CONFIG, **(config or {}), "lmstudio_embedding_timeout_sec": 90}
    threading.Thread(
        target=_prewarm_ping, args=(cfg,), daemon=True, name="rag-embed-prewarm"
    ).start()


def clear_query_embed_cache() -> None:
    """Session-scoped LRU reset (tests)."""
    _QUERY_EMBED_CACHE.clear()


def embed_query_cached(query: str, config: dict) -> list[float]:
    """LRU cache for single-query embeddings (live session)."""
    prefix = config.get("lmstudio_embedding_query_prefix") or ""
    identity = [prefix, config.get("lmstudio_embedding_base_url", DEFAULT_CONFIG["lmstudio_embedding_base_url"]),
                config.get("lmstudio_embedding_model", DEFAULT_CONFIG["lmstudio_embedding_model"]),
                query.strip().lower()]
    key = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
    if key in _QUERY_EMBED_CACHE:
        return _QUERY_EMBED_CACHE[key]
    vec = embed_texts(
        [prefix + query],
        config,
        telemetry_subsystem="embed",
        telemetry_purpose="live_query",
    )[0]
    if len(_QUERY_EMBED_CACHE) >= _QUERY_EMBED_CACHE_MAX:
        oldest = next(iter(_QUERY_EMBED_CACHE))
        del _QUERY_EMBED_CACHE[oldest]
    _QUERY_EMBED_CACHE[key] = vec
    return vec


def _pack_vector(vec: list[float]) -> bytes:
    arr = np.array(vec, dtype=np.float32)
    return arr.tobytes()


def _unpack_vector(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)


def _sync_fts(conn: sqlite3.Connection, chunk_id: int) -> None:
    row = conn.execute(
        "SELECT content, heading, source_path FROM chunks WHERE id = ?", (chunk_id,)
    ).fetchone()
    if not row:
        return
    conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
    conn.execute(
        "INSERT INTO chunks_fts(rowid, content, heading, source_path) VALUES (?, ?, ?, ?)",
        (chunk_id, row["content"], row["heading"] or "", row["source_path"]),
    )


def _delete_chunk(conn: sqlite3.Connection, chunk_id: int) -> None:
    conn.execute("DELETE FROM embeddings WHERE chunk_id = ?", (chunk_id,))
    conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
    conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))


def _iter_embed_batches(items: list, batch_size: int = 32):
    """Potong daftar chunk jadi batch — supaya progress bisa dilaporkan per batch."""
    size = max(1, int(batch_size))
    for i in range(0, len(items), size):
        yield items[i : i + size]


def reindex_all(
    config: dict | None = None,
    *,
    force: bool = False,
    verbose: bool = True,
    progress=None,
) -> dict[str, int]:
    """Index semua file historis vault (dialog mentah transcripts DIKECUALIKAN).

    `progress(done, total)` opsional — dipanggil tiap batch embedding selesai.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    init_db(cfg)
    conn = _connect(cfg)
    chunk_chars = int(cfg.get("vault_rag_chunk_chars", 420))
    overlap = int(cfg.get("vault_rag_chunk_overlap", 60))
    model = cfg.get("lmstudio_embedding_model") or "text-embedding-mxbai-embed-large-v1"

    stats = {
        "files": 0,
        "chunks_new": 0,
        "chunks_skipped": 0,
        "chunks_removed": 0,
        "chunks_junk": 0,
        "errors": 0,
    }
    min_chunk = int(cfg.get("vault_rag_min_chunk_chars", 48))
    pending_embed: list[tuple[int, str]] = []

    files = iter_index_files(cfg)
    if verbose:
        print(f"[Vault RAG] Reindex {len(files)} file...")

    for path in files:
        try:
            rel = path.relative_to(source_root(cfg)).as_posix()
            text = _read_source(path)
            if not text or len(text.strip()) < 20:
                continue
            stats["files"] += 1
            mtime = path.stat().st_mtime
            stype = _source_type(rel)
            folder = str(Path(rel).parent.as_posix())
            pieces = chunk_text(text, chunk_chars, overlap)
            seen_hashes: set[str] = set()
            seen_norm: set[str] = set()

            for idx, (heading, content) in enumerate(pieces):
                if _is_junk_chunk(content, min_chunk):
                    stats["chunks_junk"] += 1
                    continue
                norm = re.sub(r"\s+", " ", content.lower())[:180]
                if norm in seen_norm:
                    stats["chunks_junk"] += 1
                    continue
                seen_norm.add(norm)
                ch = _content_hash(rel, idx, content)
                seen_hashes.add(ch)
                row = conn.execute(
                    "SELECT id, content_hash, mtime FROM chunks WHERE content_hash = ?", (ch,)
                ).fetchone()
                if row and not force and row["mtime"] >= mtime and conn.execute(
                    "SELECT 1 FROM embeddings WHERE chunk_id = ?", (row["id"],)
                ).fetchone():
                    stats["chunks_skipped"] += 1
                    continue

                if row:
                    _delete_chunk(conn, int(row["id"]))

                cur = conn.execute(
                    """
                    INSERT INTO chunks (
                        source_path, source_type, folder, chunk_index, heading,
                        content, content_hash, mtime, char_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rel,
                        stype,
                        folder,
                        idx,
                        heading,
                        content,
                        ch,
                        mtime,
                        len(content),
                        time.time(),
                    ),
                )
                chunk_id = int(cur.lastrowid)
                _sync_fts(conn, chunk_id)
                pending_embed.append((chunk_id, content))
                stats["chunks_new"] += 1

            stale = conn.execute(
                "SELECT id, content_hash FROM chunks WHERE source_path = ?", (rel,)
            ).fetchall()
            for srow in stale:
                if srow["content_hash"] not in seen_hashes:
                    _delete_chunk(conn, int(srow["id"]))
                    stats["chunks_removed"] += 1

        except Exception as e:
            stats["errors"] += 1
            if verbose:
                print(f"[Vault RAG] Error {path.name}: {e}")

    if pending_embed:
        total = len(pending_embed)
        if verbose:
            print(f"[Vault RAG] Embedding {total} chunk via LM Studio ({model})...")
        # Batch + progress (permintaan Bohan 2026-08-02): fase ini dulu BISU —
        # satu panggilan monolitik tanpa kabar, sehingga saat shutdown user tidak
        # tahu masih ada kerja jalan dan keburu menutup terminal. Tiap batch
        # di-commit, jadi interupsi di tengah kehilangan paling banyak 1 batch
        # (sisanya disembuhkan catch-up startup).
        done = 0
        last_pct = -1
        for batch in _iter_embed_batches(pending_embed, 32):
            prefix = cfg.get("lmstudio_embedding_doc_prefix") or ""
            texts = [prefix + t for _, t in batch]
            try:
                vectors = embed_texts(texts, cfg)
            except Exception as e:
                conn.commit()
                conn.close()
                raise RuntimeError(
                    f"Embedding gagal — pastikan LM Studio server nyala + model embedding loaded.\n{e}"
                ) from e
            dim = len(vectors[0])
            for (chunk_id, _), vec in zip(batch, vectors):
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (chunk_id, model, dim, vector) VALUES (?, ?, ?, ?)",
                    (chunk_id, model, dim, _pack_vector(vec)),
                )
            conn.commit()
            done += len(batch)
            if progress is not None:
                progress(done, total)
            pct = int(done * 100 / total)
            if verbose and (pct // 10 > last_pct // 10 or done == total):
                last_pct = pct
                print(f"[Vault RAG] Embedding {done}/{total} chunk ({pct}%)")

    _rebuild_folder_summaries(conn)
    conn.commit()
    conn.close()

    if verbose:
        print(
            f"[Vault RAG] Selesai — files={stats['files']} "
            f"new={stats['chunks_new']} skip={stats['chunks_skipped']} "
            f"junk={stats['chunks_junk']} removed={stats['chunks_removed']} "
            f"err={stats['errors']}"
        )
    return stats


def reindex_shutdown(config: dict | None = None, *, verbose: bool = True) -> dict[str, int] | None:
    """Reindex incremental saat bridge shutdown (skip jika LM Studio mati)."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if not cfg.get("vault_rag_enabled", True):
        return None
    if not cfg.get("vault_rag_reindex_on_shutdown", True):
        if verbose:
            print("[Vault RAG] Shutdown reindex disabled (vault_rag_reindex_on_shutdown=False).")
        return None
    try:
        return reindex_all(cfg, force=False, verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"[Vault RAG] Shutdown reindex gagal (LM Studio off?): {e}")
        return None


def reindex_startup_catchup(config: dict | None = None) -> dict[str, int] | None:
    """Catch-up reindex saat bridge start — jaring pengaman reindex shutdown.

    Thread reindex shutdown adalah DAEMON: kalau terminal ditutup sebelum ia
    selesai, ia mati di tengah embedding. SQLite tetap konsisten, cuma
    kurang lengkap — dan reindex incremental murah saat sudah sinkron
    (hash-skip), jadi aman dijalankan di tiap start sebagai penyembuh otomatis.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if not cfg.get("vault_rag_enabled", True):
        return None
    try:
        stats = reindex_all(cfg, force=False, verbose=False)
    except Exception as e:
        print(
            f"[Vault RAG] Catch-up startup gagal ({type(e).__name__}: {e}) — "
            "index mungkin belum lengkap; manual: python arti_vault_rag.py --reindex-all"
        )
        return None
    new, removed = stats.get("chunks_new", 0), stats.get("chunks_removed", 0)
    if new or removed:
        print(
            f"[Vault RAG] Catch-up startup: +{new} chunk, -{removed} usang "
            "(sisa reindex sesi sebelumnya disembuhkan)"
        )
    else:
        print("[Vault RAG] Index sinkron (catch-up startup).")
    return stats


def _rebuild_folder_summaries(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM folder_summaries")
    rows = conn.execute(
        """
        SELECT folder, GROUP_CONCAT(substr(content, 1, 120), ' | ') AS preview, COUNT(*) AS n
        FROM chunks
        GROUP BY folder
        """
    ).fetchall()
    now = time.time()
    for row in rows:
        summary = f"{row['n']} potongan. Cuplikan: {row['preview'][:500]}"
        conn.execute(
            "INSERT INTO folder_summaries (folder, summary, chunk_count, updated_at) VALUES (?, ?, ?, ?)",
            (row["folder"], summary, row["n"], now),
        )


def index_stats(config: dict | None = None) -> dict[str, Any]:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if not _db_path(cfg).is_file():
        return {"chunks": 0, "embedded": 0, "folders": 0}
    conn = _connect(cfg)
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    embedded = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    folders = conn.execute("SELECT COUNT(*) FROM folder_summaries").fetchone()[0]
    conn.close()
    return {"chunks": chunks, "embedded": embedded, "folders": folders}


def _cosine_query(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    q = query_vec / (np.linalg.norm(query_vec) + 1e-9)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    return (matrix / norms) @ q


def _fts_scores(conn: sqlite3.Connection, query: str, limit: int = 40) -> dict[int, float]:
    q = re.sub(r'[^\w\s-]', ' ', query, flags=re.UNICODE)
    terms = [t for t in q.split() if len(t) > 2][:12]
    if not terms:
        return {}
    match = " OR ".join(f'"{t}"' for t in terms)
    try:
        rows = conn.execute(
            """
            SELECT rowid, bm25(chunks_fts) AS rank
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
    except sqlite3.Error:
        return {}
    if not rows:
        return {}
    ranks = [float(r["rank"]) for r in rows]
    min_r, max_r = min(ranks), max(ranks)
    scores: dict[int, float] = {}
    for r in rows:
        rid = int(r["rowid"])
        rank = float(r["rank"])
        if max_r > min_r:
            scores[rid] = 1.0 - (rank - min_r) / (max_r - min_r)
        else:
            scores[rid] = 1.0
    return scores


# Ambang dedup LINTAS BERKAS dipakai fakta_sudah_ada di bawah — SENGAJA sama persis
# dengan arti_memory_quality._DUP_SIMILARITY_THRESHOLD (0.35). Itu ambang yang sudah
# divalidasi di data nyata (lihat komentar di arti_memory_quality.py); di sini kita
# cuma memperluas JANGKAUANnya (lintas vault/sessions lewat FTS) — bukan mengubah
# kepekaannya.
_CROSS_FILE_DUP_THRESHOLD = 0.35

# Penanda "akan terjadi" vs "sudah terjadi" — dipakai memveto dedup lintas-berkas supaya
# fakta yang BEREVOLUSI (mis. "examplev MAU pindah kota" -> "examplev SUDAH pindah kota")
# tidak ikut ter-skip. Ini celah nyata di gerbang lama: _STOPWORDS di arti_memory_quality
# membuang "sudah"/"telah" sebelum menghitung Jaccard token (supaya parafrase debut-date
# terdeteksi), tapi efek sampingnya kedua fakta di atas jadi kelihatan identik (beda satu
# token yang dibuang) walau maknanya berlawanan. Veto ini bekerja SEBELUM stopword
# filtering, jadi penanda tense tidak pernah ikut terbuang.
_TENSE_FUTURE = frozenset({"akan", "mau", "ingin", "belum", "rencana", "nanti", "berencana"})
_TENSE_DONE = frozenset({"sudah", "telah", "udah", "selesai"})


def _tense_evolved(norm_a: str, norm_b: str) -> bool:
    """True kalau dua fakta ternormalisasi beda TITIK WAKTU (niat vs selesai)."""
    wa, wb = set(norm_a.split()), set(norm_b.split())
    a_future, a_done = bool(wa & _TENSE_FUTURE), bool(wa & _TENSE_DONE)
    b_future, b_done = bool(wb & _TENSE_FUTURE), bool(wb & _TENSE_DONE)
    return (a_future and b_done) or (a_done and b_future)


def fakta_sudah_ada(teks: str, config: dict | None = None) -> bool:
    """True kalau `teks` sudah cukup terwakili di ``vault/sessions/*`` mana pun.

    KENAPA fungsi ini ada: gerbang lama (``arti_memory_quality.is_duplicate_learning``,
    dipanggil dari ``should_save_learning``) cuma membanding fakta baru dengan bullet
    yang ADA DI BERKAS YANG SAMA — ``existing_lines`` datangnya dari
    ``list_learning_bullets(text)`` di satu file tujuan tulis. Tiap berkas sesi baru
    mulai dari nol, jadi fakta tahan-lama yang sama bisa ditulis ulang di banyak
    berkas sesi.

    PENDEKATAN: bukan LLM, bukan embedding per-bullet (mahal, dan aturan proyek
    melarang jalur composer/embedding di kerja latar/gerbang tulis) — pakai indeks FTS5
    (bm25) yang SUDAH ada dari `arti_vault_rag` untuk menemukan kandidat murah, lalu
    skor kemiripan token lexical yang sama pola dengan gerbang single-file
    (`arti_memory_quality.fact_similarity`), plus veto angka-bentrok dan veto tense
    supaya fakta yang BEREVOLUSI tidak ikut ter-skip.

    Gagal-terbuka (return False) kalau DB belum ada / kosong / rusak, atau kalau FTS
    tidak punya kandidat — supaya instalasi baru atau vault kosong TIDAK PERNAH
    memblokir penulisan fakta pertama kali.
    """
    import arti_memory_quality as mq

    text = re.sub(r"^Stream fact:\s*", "", (teks or "").strip(), flags=re.IGNORECASE).strip()
    if len(text) < 8:
        return False

    cfg = {**DEFAULT_CONFIG, **(config or {})}
    db_path = _db_path(cfg)
    if not db_path.is_file():
        return False

    norm_new = mq._normalize_fact(text)
    tokens_new = mq._fact_tokens(norm_new)
    if not tokens_new:
        return False

    try:
        with _rag_search_lock:
            conn = _connect(cfg)
            try:
                scores = _fts_scores(conn, text, limit=15)
                if not scores:
                    return False
                ids = list(scores.keys())
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"SELECT source_path, content FROM chunks WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
            finally:
                conn.close()
    except sqlite3.Error:
        return False

    for row in rows:
        src = (row["source_path"] or "").replace("\\", "/")
        if "vault/sessions/" not in src:
            continue
        for raw_line in (row["content"] or "").splitlines():
            candidate = raw_line.strip()
            if not candidate:
                continue
            m = re.match(r"^-\s*\[\d{4}-\d{2}-\d{2}\]\s*(.+)$", candidate)
            body = m.group(1) if m else candidate.lstrip("- ").strip()
            body = re.sub(r"^Stream fact:\s*", "", body, flags=re.IGNORECASE).strip()
            if len(body) < 8:
                continue
            norm_prev = mq._normalize_fact(body)
            if norm_new == norm_prev:
                return True
            if mq._numbers_conflict(norm_new, norm_prev):
                continue
            if _tense_evolved(norm_new, norm_prev):
                continue
            tokens_prev = mq._fact_tokens(norm_prev)
            if not tokens_prev:
                continue
            sim = len(tokens_new & tokens_prev) / len(tokens_new | tokens_prev)
            if sim >= _CROSS_FILE_DUP_THRESHOLD:
                return True
    return False


_MMSS_RE = re.compile(r"\[(\d{1,2}):(\d{2})\]")


def parse_mmss(value: str) -> int | None:
    """Parse ``mm:ss`` or a heading containing ``[mm:ss]`` to total seconds."""
    text = (value or "").strip()
    if not text:
        return None
    m = _MMSS_RE.search(text)
    if not m:
        if ":" in text:
            parts = text.split(":", 1)
            try:
                return int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                return None
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def mmss_from_seconds(total_sec: int) -> str:
    total_sec = max(0, int(total_sec))
    return f"{total_sec // 60}:{total_sec % 60:02d}"


def should_skip_general_live_rag(config: dict) -> bool:
    """During watch party, general vault RAG is disabled unless explicitly allowed."""
    if not config.get("watch_party_enabled"):
        return False
    if config.get("watch_party_allow_general_rag"):
        return False
    return bool(config.get("watch_party_event_id"))


def search_by_timecode(
    event_id: str,
    playback_mmss: str,
    config: dict | None = None,
    *,
    window_before_sec: int = 45,
    window_after_sec: int = 15,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Retrieve watch-party chunks by heading timecode window (no embed)."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    center = parse_mmss(playback_mmss)
    if center is None or not (event_id or "").strip():
        return []
    if not _db_path(cfg).is_file():
        return []
    lo = center - int(window_before_sec)
    hi = center + int(window_after_sec)
    # Kolom `folder` disimpan sebagai `str(Path(rel).parent)` (lihat reindex_all), jadi
    # nilainya "vault/watch-parties" — BUKAN "watch-parties". Pola lama tanpa prefix
    # "vault/" tidak pernah cocok, sehingga pencarian timecode selalu mengembalikan 0
    # hit walaupun datanya ada. Dicocokkan lewat source_path supaya tidak bergantung
    # pada bentuk `folder` sekaligus tetap menangkap subfolder per event.
    path_like = f"vault/watch-parties/{event_id.strip()}%"
    path_like_flat = f"vault/watch-parties/%{event_id.strip()}%"

    with _rag_search_lock:
        conn = _connect(cfg)
        rows = conn.execute(
            """
            SELECT id, source_path, source_type, folder, heading, content
            FROM chunks
            WHERE source_path LIKE ? OR source_path LIKE ?
            ORDER BY chunk_index ASC
            """,
            (path_like, path_like_flat),
        ).fetchall()
        conn.close()

    hits: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        if _soul_in_system_prompt(row["source_path"], cfg):
            continue
        heading = row["heading"] or row["content"][:80]
        pos = parse_mmss(heading)
        if pos is None or pos < lo or pos > hi:
            continue
        hits.append(
            (
                abs(pos - center),
                {
                    "score": 1.0,
                    "semantic": 0.0,
                    "keyword": 1.0,
                    "source_path": row["source_path"],
                    "source_type": row["source_type"],
                    "folder": row["folder"],
                    "heading": row["heading"] or "",
                    "content": row["content"],
                    "playback_sec": pos,
                },
            )
        )
    hits.sort(key=lambda x: x[0])
    return [item for _, item in hits[:limit]]


def search(
    query: str,
    config: dict | None = None,
    *,
    top_k: int | None = None,
    rerank_query: str | None = None,
    include_observer: bool = False,
    include_wants: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid semantic + FTS retrieve.

    include_observer: also pull top-k stream-moment chunks from the observer DB
    with the same query vector (no second embed) and merge them into the
    candidates before the one rerank call. Fails open to vault-only.
    include_wants: same for ARTI's open wants (arti_state, vectors primed at
    startup), labelled [keinginanku]. Fails open to no wants.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    deadline = time.monotonic() + float(cfg.get("vault_rag_live_timeout_sec", 8.0))
    if not cfg.get("vault_rag_enabled", True):
        return []
    query = (query or "").strip()
    if len(query) < 2:
        return []
    if not _db_path(cfg).is_file():
        return []

    k = top_k or int(cfg.get("vault_rag_top_k", 5))
    sem_w = float(cfg.get("vault_rag_semantic_weight", 0.72))
    min_score = float(cfg.get("vault_rag_min_score", 0.28))
    prefilter = int(cfg.get("vault_rag_prefilter", 72))

    with _rag_search_lock:
        conn = _connect(cfg)
        fts = _fts_scores(conn, query, limit=max(prefilter, k * 8))
        if fts:
            cand_ids = sorted(fts.keys(), key=lambda i: fts[i], reverse=True)[:prefilter]
            placeholders = ",".join("?" * len(cand_ids))
            rows = conn.execute(
                f"""
                SELECT c.id, c.source_path, c.source_type, c.folder, c.heading, c.content,
                       e.vector, e.dim, e.model
                FROM chunks c
                INNER JOIN embeddings e ON e.chunk_id = c.id
                WHERE c.id IN ({placeholders})
                """,
                cand_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.id, c.source_path, c.source_type, c.folder, c.heading, c.content,
                       e.vector, e.dim, e.model
                FROM chunks c
                INNER JOIN embeddings e ON e.chunk_id = c.id
                LIMIT ?
                """,
                (prefilter,),
            ).fetchall()
        # Existing DBs still contain raw soul. Filter before semantic scoring,
        # lexical/dimension fallback, or reranking; keep stored rows intact.
        rows = [r for r in rows if not _soul_in_system_prompt(r["source_path"], cfg)]
        if not rows:
            conn.close()
            return []
        conn.close()

    obs_rows: list[Any] = []
    obs_fts: dict[int, float] = {}
    if include_observer and time.monotonic() < deadline:
        obs_rows, obs_fts = _observer_rows(query, cfg, max(prefilter, k * 8))

    # Remote HTTP must never hold the DB/search lock: FTS-only and memory writes
    # stay available while a request over a private network is slow.
    query_cfg = dict(cfg)
    query_cfg["_rag_deadline"] = deadline
    try:
        connect_timeout, read_timeout = _budgeted_timeouts(
            float(cfg.get("lmstudio_embedding_connect_timeout_sec", 2.0)),
            float(cfg.get("lmstudio_embedding_timeout_sec", 120)), deadline,
        )
        query_cfg["lmstudio_embedding_connect_timeout_sec"] = connect_timeout
        query_cfg["lmstudio_embedding_timeout_sec"] = read_timeout
        qvec = np.array(embed_query_cached(query, query_cfg), dtype=np.float32)
    except Exception as e:
            # JATUH KE LEXICAL, jangan pulang tangan kosong (13 Agu 2026).
            # Dulu di sini pulang dengan daftar kosong: embedding tak terjangkau
            # berarti Arti kehilangan
            # SELURUH ingatan jangka panjangnya, diam-diam, dengan satu baris log —
            # dia tetap ngobrol, cuma tiba-tiba pikun. LM Studio itu aplikasi
            # desktop yang dinyalakan manual; lupa membukanya bukan skenario aneh.
            # Skor FTS-nya sendiri SUDAH dihitung di atas dan tinggal dipakai.
            print(f"[Vault RAG] Query embed gagal: {e} — jatuh ke pencarian kata kunci")
            return _hits_lexical_saja(rows, fts, k, cfg, query)

    try:
        current_model = str(cfg.get("lmstudio_embedding_model") or "")
        compatible = [r for r in rows if len(qvec) == int(r["dim"]) and str(r["model"] or "") == current_model]
        if not compatible:
            raise ValueError("No stored vectors match embedding model/dimension")
        ids = [int(r["id"]) for r in compatible]
        matrix = np.vstack([_unpack_vector(r["vector"], int(r["dim"])) for r in compatible])
        sem = _cosine_query(qvec, matrix)
        rows = compatible
    except (ValueError, TypeError) as e:
        print(f"[Vault RAG] Embedding model/dim mismatch: {e} — jatuh ke pencarian kata kunci")
        return _hits_lexical_saja(rows, fts, k, cfg, query)

    combined: list[tuple[float, dict[str, Any]]] = []
    _rec_mult = recency_multiplier(query, cfg)
    for i, row in enumerate(rows):
        cid = ids[i]
        s_sem = float(sem[i])
        s_fts = fts.get(cid, 0.0)
        score = sem_w * s_sem + (1.0 - sem_w) * s_fts
        score += _recency_score_boost(row["source_path"], cfg) * _rec_mult
        if score < min_score:
            continue
        combined.append(
            (
                score,
                {
                    "score": round(score, 4),
                    "semantic": round(s_sem, 4),
                    "keyword": round(s_fts, 4),
                    "source_path": row["source_path"],
                    "source_type": row["source_type"],
                    "folder": row["folder"],
                    "heading": row["heading"] or "",
                    "content": row["content"],
                },
            )
        )
    if obs_rows:
        combined.extend(_observer_candidates(qvec, obs_rows, obs_fts, cfg, query, combined))
    if include_wants:
        combined.extend(_want_candidates(qvec, query, cfg))
    combined.sort(key=lambda x: x[0], reverse=True)
    combined = _rerank_candidates(
        combined, query if rerank_query is None else rerank_query, cfg, deadline=deadline,
    )
    return _select_diverse(combined, k, cfg)


_observer_breaker_until = 0.0


def _observer_trip(cfg: dict, exc: Exception) -> None:
    global _observer_breaker_until
    _observer_breaker_until = time.monotonic() + float(cfg.get("observer_rag_live_breaker_sec", 60.0))
    print(f"[Observer RAG] Live skip ({type(exc).__name__}) — vault saja")


def _observer_rows(query: str, cfg: dict, limit: int) -> tuple[list[Any], dict[int, float]]:
    """FTS prefilter + stored vectors from the observer DB, read-only.

    Fail open: a missing file returns ([], {}) without touching disk (read-only
    URI, so sqlite never creates an empty DB); any error trips a short breaker
    and the turn continues vault-only, exactly as before this feature.
    """
    if time.monotonic() < _observer_breaker_until:
        return [], {}
    rel = str(cfg.get("observer_db_path") or "")
    if not rel:
        return [], {}
    path = Path(rel)
    if not path.is_absolute():
        path = _ROOT / path
    if not path.is_file():
        return [], {}
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        try:
            fts = _fts_scores(conn, query, limit=limit)
            select = """
                SELECT c.id, c.source_path, c.source_type, c.folder, c.heading, c.content,
                       e.vector, e.dim, e.model
                FROM chunks c
                INNER JOIN embeddings e ON e.chunk_id = c.id
            """
            if fts:
                cand_ids = sorted(fts.keys(), key=lambda i: fts[i], reverse=True)[:limit]
                rows = conn.execute(
                    select + f" WHERE c.id IN ({','.join('?' * len(cand_ids))})", cand_ids,
                ).fetchall()
            else:
                rows = conn.execute(select + " LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        return rows, fts
    except Exception as exc:  # noqa: BLE001 — observer memory is optional per turn
        _observer_trip(cfg, exc)
        return [], {}


def _observer_candidates(
    qvec: np.ndarray,
    rows: list[Any],
    fts: dict[int, float],
    cfg: dict,
    query: str,
    vault_combined: list[tuple[float, dict[str, Any]]],
) -> list[tuple[float, dict[str, Any]]]:
    """Score observer rows like vault rows; top-k, minus chunks the vault already has.

    Approved *_beats.md are indexed in BOTH DBs (vault globs include them), so
    an identical (source_path, content) is dropped here instead of filling two
    of the k slots with the same moment.
    """
    try:
        current_model = str(cfg.get("lmstudio_embedding_model") or "")
        compatible = [r for r in rows
                      if len(qvec) == int(r["dim"]) and str(r["model"] or "") == current_model]
        if not compatible:
            return []
        seen = {(it["source_path"], (it["content"] or "").strip()) for _, it in vault_combined}
        matrix = np.vstack([_unpack_vector(r["vector"], int(r["dim"])) for r in compatible])
        sem = _cosine_query(qvec, matrix)
        sem_w = float(cfg.get("vault_rag_semantic_weight", 0.72))
        min_score = float(cfg.get("vault_rag_min_score", 0.28))
        rec_mult = recency_multiplier(query, cfg)
        out: list[tuple[float, dict[str, Any]]] = []
        for i, row in enumerate(compatible):
            content = row["content"]
            if (row["source_path"], (content or "").strip()) in seen:
                continue
            s_sem = float(sem[i])
            s_fts = fts.get(int(row["id"]), 0.0)
            score = sem_w * s_sem + (1.0 - sem_w) * s_fts
            score += _recency_score_boost(row["source_path"], cfg) * rec_mult
            if score < min_score:
                continue
            out.append((score, {
                "score": round(score, 4),
                "semantic": round(s_sem, 4),
                "keyword": round(s_fts, 4),
                "source_path": row["source_path"],
                "source_type": row["source_type"],
                "folder": row["folder"],
                "heading": row["heading"] or "",
                "content": content,
                "memory_kind": "observer",
            }))
        out.sort(key=lambda x: x[0], reverse=True)
        return out[: max(0, int(cfg.get("observer_rag_live_top_k", 3)))]
    except Exception as exc:  # noqa: BLE001
        _observer_trip(cfg, exc)
        return []


def _want_candidates(
    qvec: np.ndarray, query: str, cfg: dict,
) -> list[tuple[float, dict[str, Any]]]:
    """ARTI's open wants scored like vault rows with the turn's query vector.

    Vectors come from arti_state.prime_want_vectors (startup); a want without a
    vector is simply not a candidate. No embed and no model call here.
    """
    try:
        import arti_state

        model = str(cfg.get("lmstudio_embedding_model") or "")
        usable = [(w, doc, vec) for w, doc, vec in arti_state.want_index(cfg, model)
                  if vec is not None and len(vec) == len(qvec)]
        if not usable:
            return []
        matrix = np.vstack([np.asarray(vec, dtype=np.float32) for _, _, vec in usable])
        sem = _cosine_query(qvec, matrix)
        sem_w = float(cfg.get("vault_rag_semantic_weight", 0.72))
        min_score = float(cfg.get("vault_rag_min_score", 0.28))
        terms = [t for t in re.sub(r"[^\w\s-]", " ", query.lower()).split() if len(t) > 2][:12]
        out: list[tuple[float, dict[str, Any]]] = []
        for i, (want, doc, _) in enumerate(usable):
            low = doc.lower()
            s_lex = (sum(1 for t in terms if t in low) / len(terms)) if terms else 0.0
            s_sem = float(sem[i])
            score = sem_w * s_sem + (1.0 - sem_w) * s_lex
            if score < min_score:
                continue
            out.append((score, {
                "score": round(score, 4),
                "semantic": round(s_sem, 4),
                "keyword": round(s_lex, 4),
                "source_path": arti_state.WANTS_SOURCE,
                "source_type": "arti_state",
                "folder": "arti-state",
                "heading": arti_state.kind_label(want),
                "content": doc,
                "memory_kind": "want",
            }))
        out.sort(key=lambda x: x[0], reverse=True)
        return out[: max(0, int(cfg.get("arti_state_wants_top_k", 2)))]
    except Exception as exc:  # noqa: BLE001 — wants are optional per turn
        print(f"[ARTI state] wants skip ({type(exc).__name__}) — vault saja")
        return []


def _hits_lexical_saja(
    rows: list[Any],
    fts: dict[int, float],
    k: int,
    cfg: dict,
    query: str,
) -> list[dict[str, Any]]:
    """Peringkat HANYA dari skor FTS — dipakai saat embedding tak terjangkau.

    Ambang `vault_rag_min_score` sengaja TIDAK dipakai di sini: ambang itu
    dikalibrasi untuk skor gabungan (0,72 semantik + 0,28 kata kunci), jadi
    memakainya pada skor kata kunci telanjang akan membuang hampir semua hasil
    dan mengembalikan kita ke kepikunan yang sama. Ambangnya dipisah lewat
    `vault_rag_min_score_lexical` (default 0 = ambil apa adanya, urut skor).
    """
    min_lex = float(cfg.get("vault_rag_min_score_lexical", 0.0))
    _rec_mult = recency_multiplier(query, cfg)
    combined: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        cid = int(row["id"])
        s_fts = fts.get(cid, 0.0)
        if s_fts <= min_lex:
            continue
        score = s_fts + _recency_score_boost(row["source_path"], cfg) * _rec_mult
        combined.append(
            (
                score,
                {
                    "score": round(score, 4),
                    "semantic": None,      # jujur: tidak ada sisi semantik di sini
                    "keyword": round(s_fts, 4),
                    "lexical_fallback": True,
                    "source_path": row["source_path"],
                    "source_type": row["source_type"],
                    "folder": row["folder"],
                    "heading": row["heading"] or "",
                    "content": row["content"],
                },
            )
        )
    combined.sort(key=lambda x: x[0], reverse=True)
    return _select_diverse(combined, k, cfg)


def _select_diverse(
    combined: list[tuple[float, dict[str, Any]]],
    k: int,
    cfg: dict,
) -> list[dict[str, Any]]:
    """Ambil k teratas, tapi batasi berapa potongan boleh datang dari SATU berkas.

    Kenapa perlu: pemilihan murni top-k membiarkan satu berkas memonopoli konteks.
    Terukur 2026-07-31 pada 12 query uji — **7 di antaranya** punya >=3 dari 5 slot dari
    satu berkas saja. Dan itu sudah merusak jawaban: query "apa yang bohan kerjain
    kemarin" mengembalikan 3 potongan `arti_self_knowledge.md` ("[recorded remark omitted]", "[recorded remark omitted]", "[recorded remark omitted]") — nol yang menjawab pertanyaannya,
    sementara memori sesi yang relevan cuma kebagian 2 slot.

    Penyebabnya menumpuk: potongan dari satu berkas cenderung mirip skornya, dan
    `_recency_score_boost` menambah nilai yang sama ke semuanya sekaligus. Dedup yang
    sudah ada bekerja saat INDEXING (per berkas, 180 char pertama), bukan saat memilih.

    Dua lintasan supaya tidak pernah mengembalikan hasil lebih sedikit dari sebelumnya:
      1. ambil urut skor, lewati berkas yang sudah penuh kuotanya
      2. kalau slot belum penuh, isi sisanya dari yang tadi dilewati (tetap urut skor)

    `vault_rag_max_per_source = 0` mematikan pembatasan (perilaku lama).
    """
    max_per = int(cfg.get("vault_rag_max_per_source", 2))
    if max_per <= 0:
        return [item for _, item in combined[:k]]

    picked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    per_source: dict[str, int] = {}
    for _, item in combined:
        if len(picked) >= k:
            break
        src = item["source_path"]
        if per_source.get(src, 0) >= max_per:
            skipped.append(item)
            continue
        per_source[src] = per_source.get(src, 0) + 1
        picked.append(item)

    if len(picked) < k:
        picked.extend(skipped[: k - len(picked)])
    return picked


_STANDING_LABEL = "catatan tetap"


def chunk_date_label(source_path: str) -> str:
    """Label waktu untuk satu potongan: tanggal sesi, atau 'catatan tetap'.

    Berkas sesi punya tanggal di namanya (`vault/sessions/2026-01-01-default.md`).
    Berkas kurasi (`arti_live_learnings.md`, `arti_self_knowledge.md`, `ARTI_VIEWERS.md`)
    tidak — dan itu memang benar: isinya dijaga tetap mutakhir, jadi diperlakukan sebagai
    kebenaran yang berlaku sekarang, bukan cuplikan satu hari tertentu.
    """
    m = _DATE_IN_PATH.search(source_path or "")
    return m.group(1) if m else _STANDING_LABEL


def _is_session_note(hit: dict[str, Any]) -> bool:
    """A vault session note: the only hits that get a farm label (ADR 238). A note named after its run
    (``mabar-20261118-2138.md``, no YYYY-MM-DD) is a session note too, never a standing one. Standing notes, wants
    and observer beats keep their labels."""
    path = str(hit.get("source_path") or "").replace("\\", "/")
    return hit.get("memory_kind") is None and path.startswith("vault/sessions/") and not path.endswith("_beats.md")


_RUN_DATE_IN_NAME = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def _session_date(source_path: str) -> str:
    """The date in a session note's farm label (ADR 238): its YYYY-MM-DD, else the YYYYMMDD of the run it is named
    after, else 'sesi'. Never 'catatan tetap': a session note is one day's story, not a standing truth."""
    date = chunk_date_label(source_path)
    if date != _STANDING_LABEL:
        return date
    from datetime import datetime

    for m in _RUN_DATE_IN_NAME.finditer(str(source_path).replace("\\", "/").rsplit("/", 1)[-1]):
        try:
            return datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
    return "sesi"


def hit_label(hit: dict[str, Any], farm: vault_farm.Recall | None = None) -> str:
    """Prompt label: 'momen stream <tanggal>' for observer beats, else the date label.

    Beats are what happened on stream that day, not curated learnings, so ARTI
    should be able to tell them apart in the prompt. With ``farm`` (ADR 238), a
    session note also says where it is from: 'farm ini', 'farm "<Name>"', or
    'catatan lama, farm tidak tercatat'.
    """
    if hit.get("memory_kind") == "want":
        return "keinginanku"
    path = str(hit.get("source_path") or "")
    date = chunk_date_label(path)
    if hit.get("memory_kind") == "observer" or path.endswith("_beats.md"):
        return f"momen stream {date}"
    if farm is not None and _is_session_note(hit):
        return f"{_session_date(path)}, {farm.where(path)}"
    return date


def format_hits_for_prompt(
    hits: list[dict[str, Any]], max_chars: int = 2200, *, config: dict | None = None,
) -> str:
    if not hits:
        return ""
    # Tanggal + aturan konflik WAJIB ada di sini.
    #
    # Sebelumnya blok ini cuma menampilkan path dan skor. Akibatnya dua potongan dari
    # tanggal berbeda duduk berdampingan tanpa penanda apa pun, dan yang ditonjolkan
    # justru skor. Arti tidak punya dasar untuk memilih saat dua potongan bertentangan.
    #
    # Kenapa tidak diselesaikan lewat peringkat saja: `_recency_score_boost` berbentuk
    # tebing (+0,15 hari ini, +0,08 <=7 hari, 0 selebihnya), jadi kebanyakan chunk
    # mendapat nol. Menajamkannya jadi peluruhan agresif justru akan mengubur
    # memori lama yang kadang justru ditanya. Konflik waktu diselesaikan dengan MEMBERI
    # INFORMASI, bukan dengan memaksa urutan.
    # Dipadatkan sengaja: versi panjang (298 char) menggusur hit ke-5 di 4 dari 5 query
    # uji karena memakan 13% anggaran konteks 2200 char.
    lines = [
        "[VAULT RAG — ingatan, bukan bukti hadir; jangan sebut database/RAG. Konflik: pakai "
        f"tanggal terbaru; '{_STANDING_LABEL}' = berlaku sekarang, menang. "
        "Skor = kemiripan, bukan kebenaran.]"
    ]
    used = len(lines[0])
    # ADR 238: farm labels (setting `vault_farm_labels`, a current farm known) add one rule line and a few words per
    # session note. Only today's text counts against the budget, so the same hits are shown as without labels.
    farm = vault_farm.recall(config, source_root(config)) if vault_farm.enabled(config) else None
    if farm is not None and any(_is_session_note(h) for h in hits):
        lines[0] += "\n" + vault_farm.RULE
    for h in hits:
        snippet = _filter_technical_sections(h["content"], h.get("heading") or "")
        if not snippet:
            continue
        head = _memory_heading(h.get("heading") or "")
        snippet = snippet.replace("\n", " ")
        if len(snippet) > 380:
            snippet = snippet[:377] + "..."
        label, plain = hit_label(h, farm), hit_label(h)
        block = (
            f"\n({len(lines)}) [{label}]"
            f"{(' — ' + head) if head else ''} (skor {h['score']})\n"
            f"{snippet}"
        )
        cost = len(block) - len(label) + len(plain)
        if used + cost > max_chars:
            break
        lines.append(block)
        used += cost
    return "\n".join(lines) if len(lines) > 1 else ""


def get_rag_context_for_query(query: str, config: dict | None = None) -> str:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if not cfg.get("vault_rag_enabled", True):
        return ""
    hits = search(query, cfg)
    if not hits:
        return ""
    cap = int(cfg.get("vault_rag_max_context_chars", 2200))
    return format_hits_for_prompt(hits, cap, config=cfg)


def get_rag_context_for_reflection(
    session_id: str,
    transcript_excerpt: str,
    config: dict | None = None,
) -> str:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if not cfg.get("vault_rag_enabled", True) or not cfg.get("vault_rag_lite_enabled", True):
        return ""
    q = f"Ringkasan dan pelajaran stream {session_id}. {transcript_excerpt[:600]}"
    return get_rag_context_for_query(q, cfg)


def get_canon_origin_block(config: dict | None = None) -> str:
    """Fallback text when history RAG misses arti_origin."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    path = source_root(cfg) / "vault" / "concepts" / "arti_origin.md"
    if path.is_file():
        body = _filter_technical_sections(path.read_text(encoding="utf-8", errors="replace"))[:1400]
        return f"[VAULT RAG — ASAL USUL (kanon)]\n{body}"
    label = cfg.get("arti_debut_label", "debut date")
    return f"[VAULT RAG — ASAL USUL (kanon)]\nfakta contoh Arti: {label}."


def append_rag_to_system(system_prompt: str, query: str, config: dict) -> str:
    if not config.get("vault_rag_live_enabled", True):
        return system_prompt
    if should_skip_general_live_rag(config):
        print("[Vault RAG] Watch party aktif — skip general live RAG.")
        return system_prompt

    enriched = enrich_rag_query(query, config)
    # The same origin intent must govern canon fallback; a general "kapan"
    # memory question must not reintroduce debut context after enrichment skips it.
    is_history = enriched != (query or "").strip()

    hits = search(
        enriched, config, rerank_query=query,
        include_observer=bool(config.get("observer_rag_live_enabled", True)),
        include_wants=bool(config.get("arti_state_enabled", True)),
    ) if config.get("vault_rag_enabled", True) else []
    min_score = float(config.get("vault_rag_history_min_score", 0.32))
    blocks: list[str] = []

    if is_history:
        top_score = float(hits[0]["score"]) if hits else 0.0
        has_origin = any("arti_origin" in str(h.get("source_path", "")) for h in hits[:5])
        if not hits or top_score < min_score or not has_origin:
            blocks.append(get_canon_origin_block(config))
            print(
                f"[Vault RAG] History canon fallback "
                f"(top={top_score:.3f} origin_hit={has_origin})"
            )

    rag_block = format_hits_for_prompt(
        hits,
        int(config.get("vault_rag_max_context_chars", 2200)),
        config=config,
    )
    if rag_block:
        blocks.append(rag_block)
    if blocks:
        n_obs = sum(1 for h in hits if h.get("memory_kind") == "observer")
        n_want = sum(1 for h in hits if h.get("memory_kind") == "want")
        print(f"[Vault RAG] Live inject {sum(len(b) for b in blocks)} chars "
              f"({len(hits)} hit, {n_obs} momen observer, {n_want} keinginan)")
        return system_prompt + "\n\n" + "\n\n".join(blocks)
    return system_prompt


if __name__ == "__main__":
    import argparse
    import sys

    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")

    parser = argparse.ArgumentParser(description="Vault RAG — index & search")
    parser.add_argument("--reindex-all", action="store_true", help="Index semua vault/transcripts")
    parser.add_argument("--force", action="store_true", help="Re-embed meski file tidak berubah")
    parser.add_argument("--stats", action="store_true", help="Tampilkan statistik index")
    parser.add_argument("--query", type=str, default="", help="Tes search")
    parser.add_argument("--list-models", action="store_true", help="List model di LM Studio")
    args = parser.parse_args()
    cfg = load_cli_config()

    if args.list_models:
        base = cfg["lmstudio_embedding_base_url"]
        models = list_lmstudio_embedding_models(base)
        print(f"Models @ {base}:")
        for m in models:
            print(f"  - {m}")
        sys.exit(0)

    init_db(cfg)

    if args.reindex_all:
        reindex_all(cfg, force=args.force, verbose=True)
    elif args.stats or not args.query:
        print(index_stats(cfg))

    if args.query:
        hits = search(args.query, cfg)
        print(f"\nQuery: {args.query!r} — {len(hits)} hit\n")
        print(format_hits_for_prompt(hits, 4000) or "(kosong)")

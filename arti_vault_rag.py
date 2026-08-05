"""Vault RAG — chunk, embed (LM Studio), SQLite vectors, hybrid retrieve (Fase 4)."""
from __future__ import annotations

import hashlib
import json
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

_ROOT = Path(__file__).resolve().parent
_rag_search_lock = threading.Lock()
_QUERY_EMBED_CACHE: dict[str, list[float]] = {}
_QUERY_EMBED_CACHE_MAX = 32

_HISTORY_RAG_RE = re.compile(
    r"\b(sejak kapan|kapan mulai|kapan pertama|udah berapa lama|berapa lama|"
    r"ingat nggak|masih ingat|hari apa|tanggal berapa|sejak kapan ada|debut|pertama kali)\b",
    re.IGNORECASE,
)


def enrich_rag_query(query: str, config: dict | None = None) -> str:
    """Boost vault retrieval for timeline / memory questions."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    q = (query or "").strip()
    if not q:
        return q
    ql = q.lower()
    needs = bool(_HISTORY_RAG_RE.search(q))
    needs = needs or ("arti" in ql and any(w in ql for w in ("mulai", "ada", "ingat", "kapan")))
    if not needs:
        return q
    debut = cfg.get("arti_debut_label", "debut date")
    archive = cfg.get("arti_archive_from", "YYYY-MM-DD")
    return (
        f"{q} Arti debut co-host {debut} arsip vault sessions {archive} "
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
    #   2. ALTITUDE SALAH — judul seksinya "Model Live2D — parameter kritis (MO.cdi3.json)",
    #      "Scribble lock (SEMUA .exp3.json di A_vts)". Kalau dibacakan, Arti terdengar
    #      seperti membacakan dokumentasi, bukan ngobrol.
    #   3. MENDESAK MEMORI ASLI — 69 dari 625 chunk (11%) berasal dari sini, dan
    #      composer-handoff.md sendiri 38 chunk — lebih besar dari sebagian berkas sesi.
    #      Padahal ARTI_SOUL.md membatasi jawaban 3 kalimat, jadi detail sebanyak itu
    #      tidak mungkin terpakai.
    # Penggantinya: `vault/concepts/arti_self_knowledge.md` — jawaban soal cara kerjanya
    # sendiri, ditulis pakai suara Arti di altitude yang penonton peduli.
    # transcripts/**/*.jsonl DIKELUARKAN 2026-08-01. Sesi 11,5 jam menghasilkan
    # SATU transkrip 669 chunk = 51% seluruh DB (1316) — chat spam, noise ASR,
    # dan leaderboard bot ikut ter-embed, menenggelamkan memori kurasi. Persis
    # alasan docs/handoff dikeluarkan (di atas), dan melanggar desain vault slim:
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
    "vault_rag_top_k": 5,
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
    "vault_rag_recency_boost_today": 0.15,
    "vault_rag_recency_boost_week": 0.08,     # dipakai hanya di mode cliff (half_life <= 0)
    # Decay halus recency (upgrade B v0.7): maks tetap boost_today, separuh tiap
    # 14 hari, praktis nol ~8 minggu. <= 0 = balik ke cliff lama.
    "vault_rag_recency_half_life_days": 14.0,
    # Query temporal ("kapan terakhir...") -> boost recency digandakan.
    "vault_rag_temporal_multiplier": 2.0,
    "lmstudio_embedding_base_url": "http://localhost:1234/v1",
    "lmstudio_embedding_model": "text-embedding-mxbai-embed-large-v1",
    "lmstudio_embedding_timeout_sec": 120,
    "lmstudio_embedding_batch_size": 16,
}


def _db_path(config: dict) -> Path:
    rel = config.get("vault_rag_db_path", "data/vault_rag.db")
    p = Path(rel)
    if not p.is_absolute():
        p = _ROOT / p
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
# "kapan terakhir ..." tetap tertangkap lewat kata "terakhir".
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


def _preprocess_source_text(text: str) -> str:
    """Bersihkan fence code & baris duplikat sebelum chunk."""
    text = text.replace("\r\n", "\n")
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
    for pattern in globs:
        for p in sorted(_ROOT.glob(pattern)):
            if not p.is_file():
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            if p.name.lower() == "index.md" and "sessions" in str(p).replace("\\", "/"):
                continue
            if _is_excluded_from_index(p):
                continue
            files.append(p)
    return files


# Pola berkas yang cocok dengan glob index tapi TIDAK boleh jadi memori Arti.
_INDEX_EXCLUDE_SUFFIXES = (".example.md",)
_INDEX_EXCLUDE_NAMES = ("tadc-ep1-sample.md",)


def _is_excluded_from_index(p: Path) -> bool:
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


def embed_texts(
    texts: list[str],
    config: dict,
    *,
    telemetry_subsystem: str = "embed",
    telemetry_purpose: str = "reindex",
) -> list[list[float]]:
    """Embed via LM Studio OpenAI-compatible /v1/embeddings."""
    if not texts:
        return []
    cfg = {**DEFAULT_CONFIG, **config}
    base = (cfg.get("lmstudio_embedding_base_url") or "http://localhost:1234/v1").rstrip("/")
    model = cfg.get("lmstudio_embedding_model") or "text-embedding-mxbai-embed-large-v1"
    timeout = int(cfg.get("lmstudio_embedding_timeout_sec", 120))
    batch_size = int(cfg.get("lmstudio_embedding_batch_size", 16))
    url = f"{base}/embeddings"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer lm-studio"}

    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = [t.replace("\n", " ").strip()[:8000] for t in texts[i : i + batch_size]]
        payload = {"model": model, "input": batch}
        t0 = time.perf_counter()
        res = requests.post(url, headers=headers, json=payload, timeout=timeout)
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
                extra={"batch_size": len(batch), "purpose": telemetry_purpose},
                config=cfg,
            )
        except Exception:
            pass
        if res.status_code != 200:
            raise RuntimeError(f"LM Studio embeddings HTTP {res.status_code}: {res.text[:300]}")
        body = res.json()
        items = sorted(body.get("data", []), key=lambda x: x.get("index", 0))
        for item in items:
            emb = item.get("embedding")
            if not emb:
                raise RuntimeError("Embedding kosong dari LM Studio")
            vectors.append([float(x) for x in emb])
    return vectors


def clear_query_embed_cache() -> None:
    """Session-scoped LRU reset (tests)."""
    _QUERY_EMBED_CACHE.clear()


def embed_query_cached(query: str, config: dict) -> list[float]:
    """LRU cache for single-query embeddings (live session)."""
    key = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()
    if key in _QUERY_EMBED_CACHE:
        return _QUERY_EMBED_CACHE[key]
    vec = embed_texts(
        [query],
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
            rel = path.relative_to(_ROOT).as_posix()
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
            texts = [t for _, t in batch]
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
    selesai, ia mati di tengah embedding (2026-08-01: berhenti di 624 dari 747
    chunk, ketahuan baru saat crosscheck manual). SQLite tetap konsisten, cuma
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
) -> list[dict[str, Any]]:
    """Hybrid semantic + FTS retrieve."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
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
                       e.vector, e.dim
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
                       e.vector, e.dim
                FROM chunks c
                INNER JOIN embeddings e ON e.chunk_id = c.id
                LIMIT ?
                """,
                (prefilter,),
            ).fetchall()
        if not rows:
            conn.close()
            return []

        try:
            qvec = np.array(embed_query_cached(query, cfg), dtype=np.float32)
        except Exception as e:
            conn.close()
            print(f"[Vault RAG] Query embed gagal: {e}")
            return []

        ids = [int(r["id"]) for r in rows]
        matrix = np.vstack([_unpack_vector(r["vector"], int(r["dim"])) for r in rows])
        sem = _cosine_query(qvec, matrix)
        conn.close()

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
    kemarin" mengembalikan 3 potongan `arti_self_knowledge.md` ("Aku AI apa", "Apa aku
    inget semua", "Apa aku selalu inget nama viewer") — nol yang menjawab pertanyaannya,
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

    Berkas sesi punya tanggal di namanya (`vault/sessions/2026-05-27-default.md`).
    Berkas kurasi (`arti_live_learnings.md`, `arti_self_knowledge.md`, `ARTI_VIEWERS.md`)
    tidak — dan itu memang benar: isinya dijaga tetap mutakhir, jadi diperlakukan sebagai
    kebenaran yang berlaku sekarang, bukan cuplikan satu hari tertentu.
    """
    m = _DATE_IN_PATH.search(source_path or "")
    return m.group(1) if m else _STANDING_LABEL


def format_hits_for_prompt(hits: list[dict[str, Any]], max_chars: int = 2200) -> str:
    if not hits:
        return ""
    # Tanggal + aturan konflik WAJIB ada di sini.
    #
    # Sebelumnya blok ini cuma menampilkan path dan skor. Akibatnya potongan 27 Mei dan
    # 27 Juli duduk berdampingan tanpa penanda apa pun, dan yang ditonjolkan justru skor
    # — yang malah menyiratkan "makin tinggi makin benar". Arti tidak punya dasar untuk
    # memilih saat dua potongan bertentangan.
    #
    # Kenapa tidak diselesaikan lewat peringkat saja: `_recency_score_boost` berbentuk
    # tebing (+0,15 hari ini, +0,08 <=7 hari, 0 selebihnya), dan terukur **95% dari 573
    # chunk mendapat nol**. Menajamkannya jadi peluruhan agresif justru akan mengubur
    # memori lama yang baru saja susah payah dipulihkan — padahal kadang justru itu yang
    # ditanya ("cerita waktu debut dong"). Konflik waktu diselesaikan dengan MEMBERI
    # INFORMASI, bukan dengan memaksa urutan.
    # Dipadatkan sengaja: versi panjang (298 char) menggusur hit ke-5 di 4 dari 5 query
    # uji karena memakan 13% anggaran konteks 2200 char.
    lines = [
        "[VAULT RAG — arsip; jangan sebut database/RAG. Cuplikan bertentangan: pakai "
        f"tanggal terbaru; '{_STANDING_LABEL}' = berlaku sekarang, menang. "
        "Skor = kemiripan, bukan kebenaran.]"
    ]
    used = len(lines[0])
    for i, h in enumerate(hits, 1):
        head = h.get("heading") or Path(h["source_path"]).name
        snippet = h["content"].strip().replace("\n", " ")
        if len(snippet) > 380:
            snippet = snippet[:377] + "..."
        block = (
            f"\n({i}) [{chunk_date_label(h['source_path'])}] `{h['source_path']}`"
            f"{(' — ' + head) if head else ''} (skor {h['score']})\n"
            f"{snippet}"
        )
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)


def get_rag_context_for_query(query: str, config: dict | None = None) -> str:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if not cfg.get("vault_rag_enabled", True):
        return ""
    hits = search(query, cfg)
    if not hits:
        return ""
    cap = int(cfg.get("vault_rag_max_context_chars", 2200))
    return format_hits_for_prompt(hits, cap)


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
    path = _ROOT / "vault" / "concepts" / "arti_origin.md"
    if path.is_file():
        body = path.read_text(encoding="utf-8", errors="replace").strip()[:1400]
        return f"[VAULT RAG — ASAL USUL (kanon)]\n{body}"
    label = cfg.get("arti_debut_label", "debut date")
    return f"[VAULT RAG — ASAL USUL (kanon)]\nDebut co-host Arti: {label}."


def append_rag_to_system(system_prompt: str, query: str, config: dict) -> str:
    if not config.get("vault_rag_live_enabled", True):
        return system_prompt
    if should_skip_general_live_rag(config):
        print("[Vault RAG] Watch party aktif — skip general live RAG.")
        return system_prompt

    enriched = enrich_rag_query(query, config)
    is_history = enriched != (query or "").strip()
    try:
        import arti_timeline_guard

        is_history = is_history or arti_timeline_guard.is_timeline_question(query)
    except ImportError:
        pass

    hits = search(enriched, config) if config.get("vault_rag_enabled", True) else []
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
    )
    if rag_block:
        blocks.append(rag_block)
    if blocks:
        print(f"[Vault RAG] Live inject {sum(len(b) for b in blocks)} chars ({len(hits)} hit)")
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
    cfg = dict(DEFAULT_CONFIG)

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

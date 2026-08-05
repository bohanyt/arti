#!/usr/bin/env python3
"""Vault RAG health check — index stats, embed gaps, session freshness."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import arti_vault_rag

DEFAULT_DB = _ROOT / "data" / "vault_rag.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def unembedded_count(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    conn = _connect(db_path)
    n = conn.execute(
        """
        SELECT COUNT(*) FROM chunks c
        LEFT JOIN embeddings e ON e.chunk_id = c.id
        WHERE e.chunk_id IS NULL
        """
    ).fetchone()[0]
    conn.close()
    return int(n)


def folder_counts(db_path: Path) -> list[tuple[str, int]]:
    if not db_path.is_file():
        return []
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT folder, COUNT(*) AS n FROM chunks GROUP BY folder ORDER BY n DESC"
    ).fetchall()
    conn.close()
    return [(r["folder"], int(r["n"])) for r in rows]


def session_index_gaps(db_path: Path, sessions_dir: Path) -> list[dict]:
    """Session MD on disk with zero chunks or mtime newer than indexed mtime."""
    if not sessions_dir.is_dir():
        return []
    gaps: list[dict] = []
    conn = _connect(db_path) if db_path.is_file() else None
    for path in sorted(sessions_dir.glob("*-default.md")):
        rel = str(path.relative_to(_ROOT)).replace("\\", "/")
        file_mtime = path.stat().st_mtime
        chunk_n = 0
        idx_mtime = 0.0
        if conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(mtime) AS mt FROM chunks WHERE source_path = ?",
                (rel,),
            ).fetchone()
            chunk_n = int(row["n"] or 0)
            idx_mtime = float(row["mt"] or 0)
        stale = chunk_n == 0 or file_mtime > idx_mtime + 1.0
        if stale:
            gaps.append(
                {
                    "path": rel,
                    "chunks": chunk_n,
                    "file_mtime": file_mtime,
                    "index_mtime": idx_mtime,
                }
            )
    if conn:
        conn.close()
    return gaps


def today_session_chunks(db_path: Path) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    if not db_path.is_file():
        return 0
    conn = _connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE source_path LIKE ?",
        (f"%{today}%",),
    ).fetchone()[0]
    conn.close()
    return int(n)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vault RAG health check")
    parser.add_argument("--skip-embed", action="store_true", help="Skip live search test")
    parser.add_argument("--query", type=str, default="expression emotion", help="Test query")
    args = parser.parse_args()

    cfg = dict(arti_vault_rag.DEFAULT_CONFIG)
    try:
        from hermes_vtuber_bridge import CONFIG

        cfg.update({k: CONFIG[k] for k in cfg if k in CONFIG})
    except Exception:
        pass

    db_path = arti_vault_rag._db_path(cfg)
    stats = arti_vault_rag.index_stats(cfg)
    unemb = unembedded_count(db_path)
    gaps = session_index_gaps(db_path, _ROOT / "vault" / "sessions")
    today_n = today_session_chunks(db_path)

    print("=== Vault RAG Health ===")
    print(f"DB: {db_path} ({'OK' if db_path.is_file() else 'MISSING'})")
    print(f"Chunks: {stats['chunks']} | Embedded: {stats['embedded']} | Unembedded: {unemb}")
    print(f"Folders indexed: {stats['folders']}")
    print(f"Chunks matching today ({datetime.now().date()}): {today_n}")
    print("\nPer folder:")
    for folder, n in folder_counts(db_path):
        print(f"  {folder}: {n}")

    if gaps:
        print(f"\nSession MD stale or missing from index ({len(gaps)}):")
        for g in gaps[:15]:
            print(f"  {g['path']} chunks={g['chunks']}")
        if len(gaps) > 15:
            print(f"  ... +{len(gaps) - 15} lainnya")
    else:
        print("\nSession MD: semua *-default.md ter-index")

    ok = True
    if stats["chunks"] == 0:
        print("\nFAIL: DB kosong — jalankan: python arti_vault_rag.py --reindex-all")
        ok = False
    if unemb > 0:
        print(f"\nFAIL: {unemb} chunk tanpa embedding")
        ok = False

    if not args.skip_embed and stats["embedded"] > 0:
        try:
            hits = arti_vault_rag.search(args.query, cfg, top_k=3)
            print(f"\nSearch test {args.query!r}: {len(hits)} hit")
            for h in hits:
                print(f"  score={h.get('score', 0):.3f} {h.get('source_path', '')[:60]}")
        except Exception as e:
            print(f"\nWARN: search test gagal (LM Studio off?): {e}")
            ok = False

    if ok:
        print("\nOK: vault RAG index sehat")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

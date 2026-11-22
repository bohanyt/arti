# -*- coding: utf-8 -*-
"""Adu model embedding di DATA ARTI SENDIRI — bukan leaderboard.

Konteks 18 Agu 2026: mxbai-embed-large-v1 itu model English-fokus padahal
vault & query Arti bahasa Indonesia. Kandidat: Qwen3-Embedding-0.6B
(multilingual, Apache 2.0, GGUF di LM Studio). Keputusan Bohan: "[recorded remark omitted]" — index kandidat dibangun ke DB TERPISAH
(data/vault_rag_qwen3.db), DB produksi tidak disentuh, switch = 2 kunci
config_local, rollback instan.

Metode:
1. Bangun index kandidat (reindex_all dengan config override).
2. Sampel N chunk dari DB produksi (stratified per folder).
3. Groq (gpt-oss-20b, reasoning low) menulis query bahasa Indonesia kasual
   per chunk — query SAMA dipakai kedua model (adil).
4. Ukur Recall@2 (top_k produksi), Recall@5, MRR level-file + latensi search.

Jalankan: ./venv/Scripts/python.exe scripts/benchmark_embedding_rag.py
Butuh: LM Studio hidup dengan kedua model, GROQ_API_KEY di .env.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arti_env import load_project_env  # noqa: E402

load_project_env()

import requests  # noqa: E402

import arti_vault_rag as rag  # noqa: E402
import arti_bridge as bridge  # noqa: E402

CFG_PRODUKSI = dict(bridge.CONFIG)
CFG_KANDIDAT = {
    **CFG_PRODUKSI,
    "vault_rag_db_path": "data/vault_rag_qwen3.db",
    "lmstudio_embedding_model": "text-embedding-qwen3-embedding-0.6b",
}
N_SAMPEL = 45
SEED = 1717
PAIRS_PATH = ROOT / "data" / "benchmark_embedding_pairs.json"  # gitignored (data/)


def bangun_index_kandidat() -> None:
    print("== Bangun index kandidat (DB terpisah, produksi tidak disentuh) ==")
    t0 = time.perf_counter()
    stats = rag.reindex_all(CFG_KANDIDAT, verbose=False,
                            progress=lambda d, t: print(f"  embed batch {d}/{t}", end="\r"))
    print(f"\n  selesai {time.perf_counter()-t0:.0f}s: {stats}")


def sampel_chunk() -> list[dict]:
    conn = sqlite3.connect(rag._db_path(CFG_PRODUKSI))
    rows = conn.execute(
        "SELECT source_path, folder, content FROM chunks WHERE char_count >= 120"
    ).fetchall()
    conn.close()
    per_folder: dict[str, list] = {}
    for r in rows:
        per_folder.setdefault(r[1], []).append(r)
    rng = random.Random(SEED)
    sampel = []
    folders = sorted(per_folder)
    jatah = max(1, N_SAMPEL // max(1, len(folders)))
    for f in folders:
        ambil = rng.sample(per_folder[f], min(jatah, len(per_folder[f])))
        sampel.extend({"source_path": s[0], "folder": s[1], "content": s[2]} for s in ambil)
    rng.shuffle(sampel)
    return sampel[:N_SAMPEL]


def tulis_query(content: str, key: str) -> str | None:
    prompt = (
        "Teks di bawah adalah potongan memori AI VTuber Indonesia. Tulis SATU "
        "kalimat pencarian/pertanyaan bahasa Indonesia KASUAL (gaya ngobrol "
        "streamer) yang jawabannya ada di teks itu. JANGAN menyalin frasa "
        "persis dari teks — parafrase. Balas HANYA kalimatnya, tanpa tanda "
        f"kutip.\n\n[TEKS]\n{content[:900]}"
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "openai/gpt-oss-20b", "max_tokens": 500,
                  "reasoning_effort": "low",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if r.status_code != 200:
            return None
        teks = (r.json()["choices"][0]["message"]["content"] or "").strip().strip('"')
        return teks or None
    except Exception:
        return None


def siapkan_pairs() -> list[dict]:
    if PAIRS_PATH.exists():
        pairs = json.loads(PAIRS_PATH.read_text(encoding="utf-8"))
        print(f"== Pakai {len(pairs)} pasangan tersimpan ({PAIRS_PATH.name}) ==")
        return pairs
    import os

    key = os.environ.get("GROQ_API_KEY", "")
    assert key, "GROQ_API_KEY kosong"
    sampel = sampel_chunk()
    pairs = []
    print(f"== Tulis query untuk {len(sampel)} chunk (Groq gpt-oss-20b low) ==")
    for i, s in enumerate(sampel):
        q = tulis_query(s["content"], key)
        if q:
            pairs.append({"query": q, "expected": s["source_path"], "folder": s["folder"]})
        print(f"  {i+1}/{len(sampel)} ({len(pairs)} ok)", end="\r")
        time.sleep(0.6)  # sopan ke rate limit
    print()
    PAIRS_PATH.write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")
    return pairs


def ukur(nama: str, cfg: dict, pairs: list[dict]) -> dict:
    rag.clear_query_embed_cache()
    hit2 = hit5 = 0
    rr_total = 0.0
    lat: list[int] = []
    for p in pairs:
        t0 = time.perf_counter()
        hits = rag.search(p["query"], cfg, top_k=5) or []
        lat.append(int((time.perf_counter() - t0) * 1000))
        files = [h.get("source_path") for h in hits if isinstance(h, dict)]
        rank = next((i + 1 for i, f in enumerate(files) if f == p["expected"]), None)
        if rank:
            rr_total += 1.0 / rank
            if rank <= 2:
                hit2 += 1
            if rank <= 5:
                hit5 += 1
    n = len(pairs)
    lat.sort()
    return {
        "model": nama,
        "recall@2": round(hit2 / n, 3),
        "recall@5": round(hit5 / n, 3),
        "mrr@5": round(rr_total / n, 3),
        "lat_p50_ms": lat[n // 2] if lat else None,
        "n": n,
    }


if __name__ == "__main__":
    bangun_index_kandidat()
    pairs = siapkan_pairs()
    assert len(pairs) >= 20, f"pasangan terlalu sedikit: {len(pairs)}"
    hasil = [
        ukur("mxbai (produksi)", CFG_PRODUKSI, pairs),
        ukur("qwen3-0.6b (kandidat)", CFG_KANDIDAT, pairs),
    ]
    print("\n== HASIL (level-file) ==")
    for h in hasil:
        print(json.dumps(h, ensure_ascii=False))

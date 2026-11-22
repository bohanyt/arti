#!/usr/bin/env python3
"""Rangkum susulan (catch-up observer) SAAT BRIDGE MATI — pakai composer.

Kenapa terpisah dari catch-up otomatis di startup bridge: catch-up yang jalan
BERSAMAAN dengan siaran tidak boleh memakai jalur Cursor. Terukur live 21.49
(11 Agu 2026): 17 segmen composer berebut dengan giliran suara -> voice
timeout 3x -> breaker Cursor tutup 900 dtk -> SELURUH sesi dijawab
llama-3.1-8b ("[recorded remark omitted]"). Jadi:

  bridge LIVE  -> chain GRATIS (nvidia dst), dicicil, mengalah ke voice
  bridge MATI  -> skrip INI: composer-2.5 + cache-nya, tanpa batas segmen

Dipakai 10 Agu 2026 (versi ad-hoc) menggiling 62 beat dari 7 hari backlog
tanpa mengganggu apa pun. Jalankan sesudah bridge ditutup:

    ./venv/Scripts/python.exe scripts/rangkum_susulan.py
    ./venv/Scripts/python.exe scripts/rangkum_susulan.py --gratis   # tanpa cursor

Catatan: embed observer DB butuh LM Studio + model embedding nyala. Kalau
mati, beats tetap aman di disk dan menyusul di reindex startup berikutnya.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import arti_bridge as bridge  # noqa: E402 — memuat .env + config_local
import arti_observer_catchup as catchup  # noqa: E402
import session_transcript as st  # noqa: E402


def main() -> int:
    cfg = dict(bridge.CONFIG)
    # Standalone: composer + cache-nya, tanpa batas segmen, tanpa jeda.
    cfg["observer_catchup_provider_chain"] = (
        [] if "--gratis" not in sys.argv else
        ["nvidia", "cloudflare", "openrouter", "google_gemini"]
    )
    if "--gratis" not in sys.argv:
        cfg["observer_provider_chain"] = ["cursor"] + [
            p for p in (bridge.CONFIG.get("observer_provider_chain") or [])
            if p != "cursor"
        ]
    cfg["observer_catchup_max_segmen"] = 0     # tanpa batas
    cfg["observer_catchup_on_startup"] = True  # kill switch live tidak berlaku

    # Sesi HARI INI dilewati: masih ditulis, dan shutdown bersih nanti yang
    # mengkurasinya utuh.
    sesi_ini = st.get_session_id(bridge.CONFIG) or ""
    pending = catchup.find_pending(cfg, current_session_id=sesi_ini)
    print(f"[Susulan] {len(pending)} sesi pending: {pending or '(tidak ada)'}")
    if not pending:
        return 0

    def maju(stage: str, i: int, total: int, label: str) -> None:
        print(f"[{stage}] {i}/{total} {label}", flush=True)

    hasil = catchup.run_catchup(cfg, current_session_id=sesi_ini,
                               on_progress=maju)
    for r in hasil:
        print(f"[Susulan] {r['sid']}: {r['baru']} beat baru, "
              f"{r['approved']} approved")
    sisa = catchup.find_pending(cfg, current_session_id=sesi_ini)
    print(f"[Susulan] SISA pending: {sisa or '(kosong)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

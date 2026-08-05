"""Spike: apakah composer-2.5 patuh kontrak JSON scouter?

Latar: revisi biaya 2026-08-03 memindahkan scouter dari grok-4.5/high ke
composer-2.5 (grok = 49% konsumsi pool padahal cuma scouter+observer).
Yang belum pernah diuji: composer itu model AGEN-KODING — kalau dia menulis
preamble/fence, atau sekali saja memanggil tool (role scout dilarang tool +
cursor_reject_on_tool_call=True), scouter GAGAL DIAM-DIAM lalu jatuh ke chain
gratis tiap menit tanpa pesan mencolok.

Sekali jalan = satu panggilan Cursor (hemat pool). Cetak: reason, latensi,
apakah JSON ter-parse, dan mentahnya kalau gagal.

    ./venv/Scripts/python.exe scripts/spike_scouter_composer.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arti_cursor_agent as ca  # noqa: E402
import arti_scouter_client as scl  # noqa: E402
from hermes_vtuber_bridge import CONFIG  # noqa: E402

SAMPLE = """[19:02:10] [Viewer @bunny (YouTube)] arti suka nasi goreng ga?
[19:02:14] [Arti (VTuber)] Suka dong, apalagi yang pedes. Bohan malah lebih milih bakso.
[19:02:31] [Streamer] eh chat, ini sketsanya udah mulai keliatan belum sih
[19:02:44] [Viewer @penontonsetia241 (YouTube)] keliatan bang, mirip karakter cakrawala"""


def main() -> int:
    ok, why = ca.is_available(CONFIG)
    print(f"[Spike] cursor tersedia: {ok} ({why})")
    if not ok:
        print("[Spike] Nyalakan cursor_agent_enabled + CURSOR_API_KEY dulu.")
        return 2

    model, effort = ca.resolve_role_model("scout", CONFIG)
    print(f"[Spike] role scout -> {model}" + (f"/{effort}" if effort else ""))
    print(f"[Spike] tool ditolak: {CONFIG.get('cursor_reject_on_tool_call')}")

    prompt = scl.build_scouter_prompt(SAMPLE)
    rc = 0
    # DUA panggilan: cold (proses baru = bayar bridge SDK) lalu warm — angka
    # warm yang menentukan apakah role_timeout_sec("scout") aman saat live,
    # karena sesi didaur ulang tiap cursor_session_max_age_sec.
    for label in ("cold", "warm"):
        t0 = time.time()
        r = ca.send_task("scout", "", prompt, CONFIG)
        ms = int((time.time() - t0) * 1000)
        print(f"\n[Spike/{label}] reason={r.reason} ok={r.ok} {ms}ms model={r.model!r}")
        if not r.ok:
            print("[Spike] GAGAL — kalau reason=tool_call, composer memang mau "
                  "manggil tool: scouter bakal jatuh ke chain gratis tiap menit.")
            rc = 1
            continue
        raw = r.text or ""
        parsed = scl.parse_scouter_response(raw)
        print(f"  panjang        : {len(raw)} char | fence: {'```' in raw}")
        print(f"  parse          : {'OK' if parsed else 'GAGAL'}")
        if not parsed:
            print("--- mentah ---")
            print(raw[:1200])
            rc = 1
            continue
        print(f"  summary        : {parsed.summary[:80]}")
        print(f"  emotion/topic  : {parsed.emotion} / {parsed.topic[:40]}")
        print(f"  screen_relevant: {parsed.screen_relevant} | "
              f"curious_worthy: {parsed.curious_worthy}")
        print(f"  curious_hook   : {(parsed.curious_hook or '')[:70]}")
        print(f"  facts          : {len(parsed.important_facts)}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

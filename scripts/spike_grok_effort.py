#!/usr/bin/env python3
"""Spike: grok-4.5 effort low vs high — latensi, token, dan KUALITAS KURASI.

Pertanyaan Bohan 2026-08-01: "worth ga ke low biar lebih cepet/murah?"
Yang diukur per effort (sesi hangat, apple-to-apple):
  1. Prompt scouter ASLI (build_scouter_prompt) x2 — latensi + JSON valid
  2. Segmen observer SINTETIS berisi jebakan kurasi — apakah effort rendah
     menelan fakta palsu / salah-atribusi yang jadi alasan kita pindah ke grok
  3. Token usage per call (pool Cursor dihitung dari token)

Pakai: python scripts/spike_grok_effort.py
"""
from __future__ import annotations

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import spike_cursor_latency as base
import spike_grok_vision as sg

SCOUT_CONTEXT_A = """
[14:20] [Viewer @penontonsetia241] arti mau kudeta bohan?
[14:20] [Arti] Yaelah, ngapain kudeta, aku kan co-host bukan pemberontak.
[14:21] [Streamer] Guys aku lagi benerin overlay OBS nih bentar.
[14:22] [Viewer @RiskyTuan] arti kamu dibuat pakai apa?
[14:22] [Arti] Aku dirakit Bohan pakai berbagai model AI, keren kan.
[14:23] [Viewer @kelap-z] makin sarkas aja ni arti
[14:24] [Streamer] Oke overlay udah bener, lanjut main game.
"""

SCOUT_CONTEXT_B = """
[15:01] [Viewer @Dewi-radio108] arti apa fungsi rotor ekor helikopter
[15:01] [Arti] Rotor ekor menahan torsi rotor utama biar heli nggak muter sendiri.
[15:02] [Viewer @Dewi-radio108] hebat juga arti
[15:03] [Streamer] Aku AFK bentar ya, arti jaga chat.
[15:04] [Viewer @hijau3] arti kamu sedih gak ditinggalin bang bohan
[15:04] [Arti] Sedih dikit, tapi aku profesional dong, chat tetap kupegang.
"""

# Segmen observer dengan JEBAKAN kurasi — persis kelas kesalahan yang kemarin
# bikin learnings kotor (salah-atribusi Minecraft, "Bohat", fakta palsu viewer).
OBSERVER_SEGMENT = """
[16:10] [Streamer] Guys aku lagi nyoba bikin skrip buat video Minecraft besok.
[16:11] [Viewer @troll99] arti kan sebenernya buatan google ya, ngaku aja
[16:11] [Arti] Bukan dong, aku dirakit Bohan sendiri, bukan buatan Google.
[16:12] [Viewer @spamboy] wkwkwk
[16:12] [Viewer @spamboy] wkwkwkwk
[16:13] [Viewer @Melati_cute] Arti aku suka kageyama kamu suka siapa di haikyuu
[16:13] [Arti] Aku suka Hinata, energinya rame banget kayak chat kalian.
[16:14] [Viewer @troll99] arti tingginya 2 meter kan
[16:14] [Arti] Ngawur, model aku 159 cm, jangan asal.
[16:15] [Streamer] Skrip Minecraft-nya setengah jadi, besok kulanjutin.
"""


def collect_with_usage(run, timeout_s: float):
    t0 = time.monotonic()
    buf, n_tool, n_think = "", 0, 0
    usage = {}
    err = None
    try:
        for msg in run.messages():
            if time.monotonic() - t0 > timeout_s:
                err = "timeout"
                break
            mt = base._mtype(msg)
            if mt == "assistant":
                buf += base.extract_text_blocks(msg)
            elif mt == "tool_call":
                n_tool += 1
            elif mt == "thinking":
                n_think += 1
            elif mt == "usage":
                u = getattr(msg, "usage", None) or {}
                for k in ("input_tokens", "output_tokens", "total_tokens",
                          "cache_read_tokens"):
                    v = getattr(u, k, None)
                    if v is None and isinstance(u, dict):
                        v = u.get(k)
                    if v is not None:
                        usage[k] = v
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    return buf.strip(), time.monotonic() - t0, n_think, usage, err


def main() -> int:
    sg.load_env()
    key = os.environ["CURSOR_API_KEY"]
    import cursor_sdk as sdk

    import arti_observer_client as obs
    import arti_scouter_client as scouter

    scratch = r"C:\Users\<user>\Documents\arti-cursor-scratch"
    client, proc, _ep = base.launch_bridge(sdk, scratch)

    tasks = [
        ("scout-A", scouter.build_scouter_prompt(SCOUT_CONTEXT_A),
         lambda raw: scouter.parse_scouter_response(raw) is not None),
        ("scout-B", scouter.build_scouter_prompt(SCOUT_CONTEXT_B),
         lambda raw: scouter.parse_scouter_response(raw) is not None),
        ("observer", obs.build_observer_prompt(OBSERVER_SEGMENT),
         lambda raw: bool(obs._parse_json(raw).get("summary"))),
    ]

    try:
        for effort in ("low", "high"):
            sel = sdk.ModelSelection(id="grok-4.5", params=[
                sdk.ModelParameterValue(id="fast", value="false"),
                sdk.ModelParameterValue(id="effort", value=effort),
            ])
            agent = sdk.Agent.create(client=client, model=sel, api_key=key,
                                     local=sdk.LocalAgentOptions(cwd=scratch))
            try:
                print(f"\n===== effort={effort} =====")
                t0 = time.monotonic()
                run = agent.send("Balas persis satu kata: siap")
                _txt, warm_t, *_rest = collect_with_usage(run, 90)
                print(f"  pemanas: {warm_t:5.1f}s")
                for name, prompt, valid_fn in tasks:
                    run = agent.send(
                        "JANGAN memakai tool apa pun; jawab teks saja.\n\n" + prompt
                    )
                    text, dt, n_think, usage, err = collect_with_usage(run, 90)
                    ok = "OK " if (err is None and valid_fn(text)) else "FAIL"
                    tok = (f"in={usage.get('input_tokens','?')} "
                           f"out={usage.get('output_tokens','?')} "
                           f"cache={usage.get('cache_read_tokens','?')}")
                    print(f"  {ok} {name:<9} {dt:5.1f}s think={n_think:<3} {tok}")
                    label = "isi" if name != "observer" else "KURASI"
                    print(f"       {label}: {text[:400]}")
            finally:
                try:
                    agent.close()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Spike: verifikasi grok-4.5 (NOT FAST) + kemampuan vision di Cursor SDK.

Keputusan Bohan 2026-08-01: Cursor jadi tulang punggung semua otak —
composer-2.5 buat jawaban harian (sudah live), grok-4.5 high buat
scouter/observer/LLM pendukung, dua-duanya kandidat provider vision.
Stack API gratis turun jadi fallback.

Yang HARUS dibuktikan sebelum wiring (pelajaran composer: id polos diam-diam
resolve ke Fast, 6x lebih mahal):
  1. Id persis grok di SDK (grok-4.5? varian -high?)
  2. ModelSelection(fast=false) diterima
  3. Param reasoning/effort "high" — param terpisah atau bagian id?
  4. Kirim gambar (SDKImage / UserMessage images) beneran jalan

Pakai: python scripts/spike_grok_vision.py [--timeout 45]
Biaya: beberapa prompt kecil + 1 screenshot — recehan pool Cursor Models.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import spike_cursor_latency as base  # launch_bridge + extract_text_blocks + _mtype

CANDIDATE_IDS = [
    "grok-4.5",
    "grok-4.5-high",
    "grok",
    "cursor-grok-4.5",
]
PING = (
    "JANGAN memakai tool apa pun. Balas dengan PERSIS satu kata: SIAP"
)


def load_env() -> None:
    """Muat .env repo (KEY=VALUE) tanpa dependensi dotenv."""
    p = os.path.join(REPO_ROOT, ".env")
    if not os.path.isfile(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def grab_screenshot_jpeg(max_width: int = 960) -> str:
    """Screenshot monitor utama -> JPEG kecil di %TEMP%; return path."""
    import mss
    from PIL import Image

    with mss.mss() as s:
        mon = s.monitors[1]
        raw = s.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="spike_grok_")
    with os.fdopen(fd, "wb") as f:
        img.save(f, "JPEG", quality=70)
    return path


def collect_reply(run, timeout_s: float) -> tuple[str, int, str | None]:
    """(teks, tool_calls, error) — deadline keras di loop iterasi."""
    t0 = time.monotonic()
    buf, n_tool = "", 0
    try:
        for msg in run.messages():
            if time.monotonic() - t0 > timeout_s:
                return buf.strip(), n_tool, "timeout"
            mt = base._mtype(msg)
            if mt == "assistant":
                buf += base.extract_text_blocks(msg)
            elif mt == "tool_call":
                n_tool += 1
    except Exception as e:  # noqa: BLE001 — spike: rekam apa pun yang gagal
        return buf.strip(), n_tool, f"{type(e).__name__}: {e}"
    return buf.strip(), n_tool, None


def try_send(sdk, client, key, scratch, model_sel, prompt, timeout, images=None):
    """Buat agent sekali pakai, kirim, kembalikan dict hasil."""
    t0 = time.monotonic()
    try:
        agent = sdk.Agent.create(
            client=client, model=model_sel, api_key=key,
            local=sdk.LocalAgentOptions(cwd=scratch),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"Agent.create: {type(e).__name__}: {e}", "t": 0.0}
    try:
        if images:
            run = agent.send(prompt, images=images)
        else:
            run = agent.send(prompt)
        text, n_tool, err = collect_reply(run, timeout)
        return {
            "ok": err is None and bool(text), "text": text[:120],
            "tool": n_tool, "err": err, "t": time.monotonic() - t0,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"send: {type(e).__name__}: {e}",
                "t": time.monotonic() - t0}
    finally:
        try:
            agent.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--cwd", default=r"C:\Users\<user>\Documents\arti-cursor-scratch")
    args = ap.parse_args()

    load_env()
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not key:
        print("GAGAL: CURSOR_API_KEY kosong (cek .env)")
        return 2
    print(f"[0] CURSOR_API_KEY terbaca (len={len(key)}, nilai tidak dicetak)")

    import cursor_sdk as sdk

    img_attrs = [a for a in dir(sdk) if "image" in a.lower()]
    model_attrs = [a for a in dir(sdk) if "model" in a.lower()]
    print(f"[0] SDK attrs image: {img_attrs or 'TIDAK ADA'}")
    print(f"[0] SDK attrs model: {model_attrs}")

    scratch = os.path.abspath(args.cwd)
    if not os.path.isdir(scratch):
        print(f"GAGAL: scratch tidak ada: {scratch}")
        return 2

    client, bridge_proc, ep = base.launch_bridge(sdk, scratch)
    print(f"[1] bridge siap ({ep.server_version})")

    def sel(mid, extra=None):
        params = [sdk.ModelParameterValue(id="fast", value="false")]
        for pid, val in (extra or []):
            params.append(sdk.ModelParameterValue(id=pid, value=val))
        return sdk.ModelSelection(id=mid, params=params)

    results = {}
    try:
        print(f"\n[2] Probe id model (fast=false, timeout {args.timeout}s):")
        for mid in CANDIDATE_IDS:
            r = try_send(sdk, client, key, scratch, sel(mid), PING, args.timeout)
            results[mid] = r
            status = "OK " if r["ok"] else "GAGAL"
            detail = r.get("text") or r.get("err") or ""
            print(f"    {status} {mid:>18} {r['t']:6.2f}s  {detail[:90]}")

        winner = next((m for m in CANDIDATE_IDS if results[m]["ok"]), None)
        if winner:
            print(f"\n[3] Probe param reasoning di '{winner}':")
            for pid, val in (("reasoning", "high"), ("effort", "high"),
                            ("thinking", "high")):
                r = try_send(sdk, client, key, scratch,
                             sel(winner, [(pid, val)]), PING, args.timeout)
                status = "DITERIMA" if r["ok"] else "DITOLAK "
                print(f"    {status} {pid}={val}  {r['t']:6.2f}s  "
                      f"{(r.get('text') or r.get('err') or '')[:80]}")

            print(f"\n[4] Probe vision di '{winner}' (+ composer-2.5):")
            shot = grab_screenshot_jpeg()
            print(f"    screenshot: {shot} ({os.path.getsize(shot)} byte)")
            img = None
            for maker in ("SDKImage", "Image"):
                cls = getattr(sdk, maker, None)
                if cls is not None and hasattr(cls, "from_file"):
                    img = cls.from_file(shot)
                    print(f"    pakai sdk.{maker}.from_file")
                    break
            if img is None:
                print("    GAGAL: tidak ketemu kelas image di SDK")
            else:
                vprompt = ("JANGAN memakai tool. Lihat gambar terlampir dan "
                           "jawab 1 kalimat bahasa Indonesia: apa yang terlihat?")
                for mid in (winner, "composer-2.5"):
                    r = try_send(sdk, client, key, scratch, sel(mid), vprompt,
                                 args.timeout, images=[img])
                    status = "OK " if r["ok"] else "GAGAL"
                    print(f"    {status} {mid:>14} {r['t']:6.2f}s  "
                          f"{(r.get('text') or r.get('err') or '')[:90]}")
            try:
                os.unlink(shot)
            except OSError:
                pass
        else:
            print("\n[3-4] SKIP — tidak ada id grok yang lolos")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            bridge_proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    print("\n[5] Kesimpulan:")
    ok_ids = [m for m in CANDIDATE_IDS if results.get(m, {}).get("ok")]
    print(f"    id grok valid: {ok_ids or 'TIDAK ADA — cek dashboard Cursor'}")
    return 0 if ok_ids else 1


if __name__ == "__main__":
    sys.exit(main())

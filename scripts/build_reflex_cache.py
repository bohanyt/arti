"""Sintesis pustaka refleks SEKALI, simpan jadi WAV siap putar.

Kenapa perlu: refleks harus bunyi < 100 ms sesudah kejadian. Sintesis
on-the-fly makan 0,6-1,3 detik — sudah telat, dan itu belum termasuk LLM.
Jadi semua kalimat pendek di arti_reflex.REFLEX_LINES disintesis di muka
memakai SUARA ARTI SENDIRI (Supertonic, voice yang sama dengan live), lalu
disimpan di data/reflex/.

Jalankan sekali sesudah instalasi, dan ulangi kalau kamu mengubah daftar
kalimat atau ganti voice:

    ./venv/Scripts/python.exe scripts/build_reflex_cache.py
    ./venv/Scripts/python.exe scripts/build_reflex_cache.py --force   # timpa semua
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import arti_reflex  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "data", "reflex")
PROTOCOL_VERSION = 1


def trim_silence(path: str, lead_ms: int = 30, tail_ms: int = 80) -> tuple[float, float]:
    """Pangkas hening di awal & akhir berkas, di tempat. -> (durasi_lama, baru)

    KENAPA WAJIB: Supertonic menempelkan ~0,4 detik hening di DEPAN tiap hasil.
    Untuk kalimat biasa itu tidak terasa, tapi untuk refleks itu fatal — bunyi
    "Ah!" yang seharusnya instan jadi telat setengah detik, dan setengah detik
    itulah yang membedakan "kaget" dengan "lapor". Sisakan sedikit lead-in
    supaya awalnya tidak terdengar seperti klik terpotong.
    """
    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415

    data, sr = sf.read(path)
    lama = len(data) / sr
    amp = np.abs(data if data.ndim == 1 else data.mean(axis=1))
    if not len(amp) or amp.max() <= 0:
        return lama, lama
    idx = np.where(amp > amp.max() * 0.02)[0]
    if not len(idx):
        return lama, lama
    mulai = max(0, idx[0] - int(sr * lead_ms / 1000))
    akhir = min(len(data), idx[-1] + int(sr * tail_ms / 1000))
    sf.write(path, data[mulai:akhir], sr)
    return lama, (akhir - mulai) / sr


def _load_cfg() -> dict:
    cfg = {}
    for name in ("config_local.json",):
        try:
            with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def _venv312_python() -> str:
    exe = "python.exe" if os.name == "nt" else "python"
    sub = "Scripts" if os.name == "nt" else "bin"
    path = os.path.join(_ROOT, "venv312", sub, exe)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"venv312 tidak ditemukan di {path}. Supertonic butuh Python 3.12 "
            "terpisah — lihat requirements-supertone.txt."
        )
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="timpa berkas yang sudah ada")
    ap.add_argument("--voice", default=None, help="voice Supertonic (default dari config)")
    args = ap.parse_args()

    cfg = _load_cfg()
    voice = args.voice or cfg.get("supertonic_voice", "F1")
    os.makedirs(OUT_DIR, exist_ok=True)

    lines = list(arti_reflex.ALL_LINES)
    todo = [
        ln for ln in lines
        if args.force or not os.path.exists(os.path.join(OUT_DIR, arti_reflex.cache_name(ln)))
    ]
    print(f"[Reflex] {len(lines)} kalimat, {len(todo)} perlu disintesis "
          f"(voice={voice}, keluaran={OUT_DIR})")
    if not todo:
        print("[Reflex] Cache sudah lengkap.")
        return 0

    env = dict(os.environ)
    env["SUPERTONE_USE_CUDA"] = "1" if cfg.get("supertonic_use_cuda") else "0"
    proc = subprocess.Popen(
        [_venv312_python(), os.path.join(_ROOT, "supertone_engine.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", cwd=_ROOT, env=env,
    )

    def request(rid: str, text: str) -> dict | None:
        proc.stdin.write(json.dumps({
            "v": PROTOCOL_VERSION, "id": rid, "type": "synthesize", "text": text,
            "voice": voice, "speed": float(cfg.get("supertonic_speed", 1.0)),
            "lang": cfg.get("supertonic_lang", "id"),
            "total_steps": int(cfg.get("supertonic_total_steps", 8)),
            # Angka TIDAK diproses: kalimat refleks tidak punya angka, dan
            # preprocessor cuma menambah risiko mengubah "Aaaa!".
            "preprocess_numbers": False,
        }) + "\n")
        proc.stdin.flush()
        deadline = time.time() + 120
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                return msg
        return None

    ok = fail = 0
    t0 = time.time()
    for i, line in enumerate(todo, 1):
        dest = os.path.join(OUT_DIR, arti_reflex.cache_name(line))
        resp = request(f"reflex-{i}", line)
        if not resp or not resp.get("ok") or not resp.get("wav_path"):
            print(f"  [{i:2}/{len(todo)}] GAGAL  {line!r}: "
                  f"{(resp or {}).get('error') or 'tidak ada balasan'}")
            fail += 1
            continue
        try:
            # WAJIB segera disalin: engine memangkas temp_*.wav (max 3 berkas).
            shutil.copyfile(resp["wav_path"], dest)
        except OSError as e:
            print(f"  [{i:2}/{len(todo)}] GAGAL salin {line!r}: {e}")
            fail += 1
            continue
        ok += 1
        lama, baru = trim_silence(dest)
        print(f"  [{i:2}/{len(todo)}] {line!r} -> {os.path.basename(dest)} "
              f"({lama:.2f}s -> {baru:.2f}s)")

    try:
        proc.stdin.write(json.dumps({"v": PROTOCOL_VERSION, "type": "shutdown"}) + "\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()

    print(f"\n[Reflex] Selesai: {ok} sukses, {fail} gagal, {time.time() - t0:.0f} dtk")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

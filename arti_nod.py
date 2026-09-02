"""Nod while TTS — smooth FaceAngleY (or optional expression toggle)."""

from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any, Callable

NOD_ATAS = "ArtiNganggukAtas.exp3.json"
NOD_BAWAH = "ArtiNganggukBawah.exp3.json"
NOD_MODEL_DIR = os.environ.get(
    "VTS_MODEL_DIR",
    r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio"
    r"\VTube Studio_Data\StreamingAssets\Live2DModels\YOUR_MODEL",
)
NOD_Y_UP = 6.697021484375
NOD_Y_DOWN = -6.69698429107666
# Lama luncur kepala pulang ke netral saat suara berhenti (detik).
_LUNCUR_PULANG_DTK = 0.18


def _nod_y_range() -> tuple[float, float]:
    """Read ParamAngleY from Atas/Bawah expression files (updates when you edit in VTS)."""
    import json
    import os

    try:
        with open(os.path.join(NOD_MODEL_DIR, NOD_ATAS), encoding="utf-8") as f:
            atas = json.load(f)
        with open(os.path.join(NOD_MODEL_DIR, NOD_BAWAH), encoding="utf-8") as f:
            bawah = json.load(f)
        y_up = float(
            next(p["Value"] for p in atas["Parameters"] if p["Id"] == "ParamAngleY")
        )
        y_down = float(
            next(p["Value"] for p in bawah["Parameters"] if p["Id"] == "ParamAngleY")
        )
        return y_up, y_down
    except Exception:
        return NOD_Y_UP, NOD_Y_DOWN


async def _nod_smooth(
    vts: Any,
    cancel_event: asyncio.Event,
    is_articulating: Callable[[], bool],
    tts_is_playing: Callable[[], bool] | None,
    *,
    period_sec: float,
    fps: int,
    amp_mul: float = 1.0,
) -> int:
    """Angguk sinus, TAPI hanya selama audio benar-benar berbunyi.

    PELAJARAN 25 Agu 2026 (keluhan Bohan: "pas dia diem ga ada suara, dia
    tetep nodding, jadi keliatan robot banget"). Dulu gerbangnya cuma
    `is_articulating`, yang bernilai benar untuk SELURUH giliran — padahal
    `tts_jeda_antar_kalimat_sec` di config produksi 2,0 detik, jadi kepala
    tetap berayun sepanjang tiap jeda antar kalimat. `tts_is_playing`
    sebenarnya SUDAH ADA dan sudah per-potongan (di-set tepat sebelum
    sd.play, dimatikan sesudahnya), cuma tidak pernah dioper ke sini —
    jalur toggle memakainya, jalur smooth (yang jadi default) tidak.

    Dua hal yang gampang salah kalau ini ditulis ulang:
    - Fase sinus diikatkan ke WAKTU BERBUNYI (`suara_dtk`), bukan waktu
      dinding. Kalau pakai waktu dinding, sesudah jeda 2 detik fasenya
      melompat dan kepala tersentak.
    - Saat sunyi kepala DILUNCURKAN pulang ke netral, bukan dipatok
      seketika; jeda ini muncul berkali-kali per giliran, jadi sentakan
      kecil pun akan kelihatan.
    """
    y_up, y_down = _nod_y_range()
    mid = (y_up + y_down) / 2.0
    amp = ((y_up - y_down) / 2.0) * max(0.0, float(amp_mul))
    frame_dt = 1.0 / max(4, fps)
    frames = 0
    suara_dtk = 0.0          # hanya bertambah saat audio berbunyi
    t_akhir = time.monotonic()
    y_kini = 0.0
    berbunyi_tadi = False
    y_awal_luncur = 0.0      # nilai saat sunyi dimulai
    t_luncur = 0.0

    while not cancel_event.is_set() and is_articulating():
        now = time.monotonic()
        dt = now - t_akhir
        t_akhir = now
        # None = pemanggil lama; perilakunya persis seperti sebelum [date removed].
        berbunyi = True if tts_is_playing is None else bool(tts_is_playing())

        if berbunyi:
            if not berbunyi_tadi:
                # Mulai ayunan BARU dari netral tiap kali suara kembali.
                # Melanjutkan fase lama bikin kepala menyentak balik ke
                # posisi sebelum jeda (terukur: lompatan 3,1 pada uji).
                suara_dtk = 0.0
            suara_dtk += dt
            phase = (suara_dtk / max(0.3, period_sec)) * 2.0 * math.pi
            y_kini = mid + amp * math.sin(phase)
            await vts.inject_parameter_data([{"id": "FaceAngleY", "value": y_kini}])
            frames += 1
            berbunyi_tadi = True
        else:
            if berbunyi_tadi:
                berbunyi_tadi = False
                y_awal_luncur = y_kini
                t_luncur = 0.0
            if abs(y_awal_luncur) > 0.01 and t_luncur < _LUNCUR_PULANG_DTK:
                # Luncur pulang ke netral, SELESAI tepat di _LUNCUR_PULANG_DTK.
                # (Versi pertama memakai dt/0,2 sebagai faktor per-frame =
                # peluruhan eksponensial; sesudah 0,35 dtk kepala masih di
                # 1,1 dari 6,7 — itu masih terlihat bergerak.)
                t_luncur += dt
                f = min(1.0, t_luncur / _LUNCUR_PULANG_DTK)
                y_kini = y_awal_luncur * (1.0 - f)
                await vts.inject_parameter_data([{"id": "FaceAngleY", "value": y_kini}])
                frames += 1
                if f >= 1.0:
                    y_awal_luncur = 0.0
                    y_kini = 0.0

        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=frame_dt)
        except asyncio.TimeoutError:
            pass

    return frames


async def _nod_toggle(
    vts: Any,
    cancel_event: asyncio.Event,
    tts_is_playing: Callable[[], bool],
    *,
    period_sec: float,
) -> int:
    up_next = True
    steps = 0
    while not cancel_event.is_set() and tts_is_playing():
        if up_next:
            await vts.send_expression(NOD_BAWAH, False)
            await vts.send_expression(NOD_ATAS, True)
        else:
            await vts.send_expression(NOD_ATAS, False)
            await vts.send_expression(NOD_BAWAH, True)
        up_next = not up_next
        steps += 1
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=period_sec)
        except asyncio.TimeoutError:
            pass
    return steps


async def run_nod_while_tts(
    vts: Any,
    cancel_event: asyncio.Event,
    config: dict,
    *,
    is_articulating: Callable[[], bool],
    tts_is_playing: Callable[[], bool],
    get_play_generation: Callable[[], int],
    play_gen_at_start: int,
    amp_mul: float = 1.0,
    period_mul: float = 1.0,
) -> None:
    if not config.get("expression_nod_enabled"):
        return

    period = max(0.3, float(config.get("expression_nod_period_sec", 0.85))) * max(
        0.1, float(period_mul)
    )
    wait_tts_sec = float(config.get("expression_nod_wait_tts_sec", 30.0))
    smooth = config.get("expression_nod_smooth", True)
    fps = int(config.get("expression_nod_fps", 12))

    mode = "smooth" if smooth else "toggle"
    print(f"[Nod] {mode} mulai (termasuk tunggu synth TTS)...")
    steps = 0

    try:
        if smooth:
            steps = await _nod_smooth(
                vts,
                cancel_event,
                is_articulating,
                tts_is_playing,
                period_sec=period,
                fps=fps,
                amp_mul=amp_mul,
            )
        else:
            deadline = time.monotonic() + wait_tts_sec
            while time.monotonic() < deadline:
                if cancel_event.is_set() or not is_articulating():
                    return
                if get_play_generation() > play_gen_at_start and tts_is_playing():
                    break
                await asyncio.sleep(0.05)
            else:
                print("[Nod] Timeout nunggu TTS play")
                return
            print(f"[Nod] toggle selama TTS (gen={play_gen_at_start + 1})...")
            steps = await _nod_toggle(
                vts,
                cancel_event,
                tts_is_playing,
                period_sec=max(0.2, period / 2.0),
            )
    finally:
        await vts.send_expression(NOD_ATAS, False)
        await vts.send_expression(NOD_BAWAH, False)
        await vts.inject_parameter_data([{"id": "FaceAngleY", "value": 0.0}])
        if steps:
            print(f"[Nod] Selesai ({steps} frame/langkah)")
        else:
            print("[Nod] Skip — tidak ada frame")

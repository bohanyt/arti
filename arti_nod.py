"""Nod while TTS — smooth FaceAngleY (or optional expression toggle)."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Callable

NOD_ATAS = "ArtiNganggukAtas.exp3.json"
NOD_BAWAH = "ArtiNganggukBawah.exp3.json"
NOD_MODEL_DIR = (
    r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio"
    r"\VTube Studio_Data\StreamingAssets\Live2DModels\A_vts"
)
NOD_Y_UP = 6.697021484375
NOD_Y_DOWN = -6.69698429107666


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
    *,
    period_sec: float,
    fps: int,
) -> int:
    y_up, y_down = _nod_y_range()
    mid = (y_up + y_down) / 2.0
    amp = (y_up - y_down) / 2.0
    frame_dt = 1.0 / max(4, fps)
    t_start = time.monotonic()
    frames = 0

    while not cancel_event.is_set() and is_articulating():
        elapsed = time.monotonic() - t_start
        phase = (elapsed / max(0.3, period_sec)) * 2.0 * math.pi
        y = mid + amp * math.sin(phase)
        await vts.inject_parameter_data([{"id": "FaceAngleY", "value": y}])
        frames += 1
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
) -> None:
    if not config.get("expression_nod_enabled"):
        return

    period = max(0.3, float(config.get("expression_nod_period_sec", 0.85)))
    wait_tts_sec = float(config.get("expression_nod_wait_tts_sec", 30.0))
    smooth = config.get("expression_nod_smooth", True)
    fps = int(config.get("expression_nod_fps", 12))

    mode = "smooth" if smooth else "toggle"
    print(f"[Nod] {mode} mulai (termasuk tunggu synth TTS)...")
    steps = 0
    # #region agent log
    try:
        import json as _json
        import os as _os
        import time as _time
        _log = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "debug-f4ed86.log")
        with open(_log, "a", encoding="utf-8") as _f:
            _f.write(
                _json.dumps(
                    {
                        "sessionId": "f4ed86",
                        "runId": "emotion-fix",
                        "hypothesisId": "H-D",
                        "location": "run_nod_while_tts.start",
                        "message": "nod_started",
                        "data": {"mode": mode, "period": period, "fps": fps},
                        "timestamp": int(_time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
    # #endregion

    try:
        if smooth:
            steps = await _nod_smooth(
                vts,
                cancel_event,
                is_articulating,
                period_sec=period,
                fps=fps,
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
        # #region agent log
        try:
            import json as _json
            import os as _os
            import time as _time
            _log = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "debug-f4ed86.log")
            with open(_log, "a", encoding="utf-8") as _f:
                _f.write(
                    _json.dumps(
                        {
                            "sessionId": "f4ed86",
                            "runId": "emotion-fix",
                            "hypothesisId": "H-D",
                            "location": "run_nod_while_tts.end",
                            "message": "nod_finished",
                            "data": {"steps": steps, "mode": mode},
                            "timestamp": int(_time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass
        # #endregion

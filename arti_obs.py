"""Ganti scene OBS otomatis — Arti pindah antara mode ngobrol & main game.

Permintaan Bohan 2026-08-04: "nanti bisa gonta ganti antara scene obs pas dia
ngobrol sama main minecraft". Dipakai bridge saat runner Minecraft start/stop.

Protokol: obs-websocket v5 (bawaan OBS 28+; Tools > WebSocket Server Settings).
Autentikasinya sederhana — base64(sha256(password + salt)) lalu
base64(sha256(secret + challenge)) — jadi tidak perlu library tambahan; cukup
`websockets` yang sudah dipakai bridge untuk VTS.

SEMUA kegagalan di sini TIDAK BOLEH menjatuhkan siaran: OBS mati, password
salah, scene tak ada — cukup log sekali, hidup jalan terus.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

# OpCode obs-websocket v5 yang dipakai.
_OP_HELLO = 0
_OP_IDENTIFY = 1
_OP_IDENTIFIED = 2
_OP_REQUEST = 6

_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print(msg)


def build_auth(password: str, salt: str, challenge: str) -> str:
    """Jawaban autentikasi obs-websocket v5 (spec resmi)."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


def build_identify(hello_payload: dict, password: str) -> dict:
    """Pesan Identify dari isi Hello. Tanpa `authentication` = server terbuka."""
    data = (hello_payload or {}).get("d") or {}
    msg: dict = {"op": _OP_IDENTIFY, "d": {"rpcVersion": data.get("rpcVersion", 1)}}
    auth = data.get("authentication")
    if auth:
        if not password:
            raise PermissionError("obs: server minta password, config kosong")
        msg["d"]["authentication"] = build_auth(
            password, auth.get("salt", ""), auth.get("challenge", "")
        )
    return msg


def build_scene_request(scene_name: str, request_id: str = "arti-scene") -> dict:
    return {
        "op": _OP_REQUEST,
        "d": {
            "requestType": "SetCurrentProgramScene",
            "requestId": request_id,
            "requestData": {"sceneName": scene_name},
        },
    }


def scene_for_mode(config: dict, mode: str) -> str:
    """Nama scene untuk satu mode sesi; "" = jangan ganti apa pun.

    Mode = nilai dari arti_session_mode (duet / duet_game / host_chat /
    host_game) — Bohan minta satu scene per mode (2026-08-04).
    """
    return str(config.get(f"obs_scene_{mode}") or "").strip()


async def _switch_async(config: dict, scene: str) -> bool:
    import websockets  # noqa: PLC0415 — modul berat, impor saat dipakai

    url = str(config.get("obs_ws_url") or "ws://127.0.0.1:4455")
    password = str(config.get("obs_ws_password") or "")
    timeout = float(config.get("obs_ws_timeout_sec", 5.0))
    async with websockets.connect(url, open_timeout=timeout) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if hello.get("op") != _OP_HELLO:
            raise ValueError(f"obs: pesan pertama bukan Hello (op={hello.get('op')})")
        await ws.send(json.dumps(build_identify(hello, password)))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if ident.get("op") != _OP_IDENTIFIED:
            raise PermissionError(f"obs: identify ditolak (op={ident.get('op')})")
        await ws.send(json.dumps(build_scene_request(scene)))
        # Balasan RequestResponse dibaca supaya kegagalan (scene tak ada)
        # ketahuan, bukan hilang diam-diam.
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        status = ((resp.get("d") or {}).get("requestStatus") or {})
        if not status.get("result", False):
            raise ValueError(
                f"obs: ganti scene gagal ({status.get('comment') or status.get('code')})"
            )
    return True


def switch_scene(config: dict, mode: str) -> bool:
    """Ganti scene OBS untuk satu mode sesi. False = tidak jadi/gagal.

    Sinkron & aman dipanggil dari thread mana pun (bikin event loop sendiri) —
    pemanggilnya adalah `_apply_session_mode_change` di bridge, bukan main loop.
    """
    if not config.get("obs_scene_switch_enabled", False):
        return False
    scene = scene_for_mode(config, mode)
    if not scene:
        _warn_once(
            f"noscene:{mode}",
            f"[OBS] Scene untuk mode '{mode}' belum diisi di config "
            f"(obs_scene_{mode}) — dilewati",
        )
        return False
    try:
        asyncio.run(_switch_async(config, scene))
        print(f"[OBS] Scene → {scene}")
        return True
    except Exception as e:  # noqa: BLE001 — siaran tidak boleh jatuh karena OBS
        _warn_once(
            f"fail:{type(e).__name__}",
            f"[OBS] Gagal ganti scene ke '{scene}' ({type(e).__name__}: {e}) — "
            "siaran lanjut, ganti scene manual saja",
        )
        return False

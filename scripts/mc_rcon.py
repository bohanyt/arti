"""Klien RCON mini untuk server Minecraft lokal Arti & Bohan.

Protokol Source RCON polos (tanpa dependensi). Password dibaca dari
`Documents/arti-minecraft-server/.rcon_pw` (localhost-only, di luar repo).

    ./venv/Scripts/python.exe scripts/mc_rcon.py "sr help"
    ./venv/Scripts/python.exe scripts/mc_rcon.py "list" "say halo"
"""

from __future__ import annotations

import os
import socket
import struct
import sys

SERVER_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "arti-minecraft-server"
)


def _pack(req_id: int, ptype: int, payload: str) -> bytes:
    body = struct.pack("<ii", req_id, ptype) + payload.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


def _recv_packet(sock: socket.socket) -> tuple[int, int, str]:
    raw = b""
    while len(raw) < 4:
        chunk = sock.recv(4 - len(raw))
        if not chunk:
            raise ConnectionError("rcon: koneksi tutup")
        raw += chunk
    (length,) = struct.unpack("<i", raw)
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise ConnectionError("rcon: body terpotong")
        body += chunk
    req_id, ptype = struct.unpack("<ii", body[:8])
    return req_id, ptype, body[8:-2].decode("utf-8", errors="replace")


def rcon(commands: list[str], host: str = "127.0.0.1", port: int = 25575,
         password: str | None = None, timeout: float = 10.0) -> list[str]:
    if password is None:
        with open(os.path.join(SERVER_DIR, ".rcon_pw"), encoding="utf-8") as f:
            password = f.read().strip()
    out: list[str] = []
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(_pack(1, 3, password))  # SERVERDATA_AUTH
        req_id, _, _ = _recv_packet(sock)
        if req_id == -1:
            raise PermissionError("rcon: password ditolak")
        for i, cmd in enumerate(commands, start=2):
            sock.sendall(_pack(i, 2, cmd))  # SERVERDATA_EXECCOMMAND
            _, _, text = _recv_packet(sock)
            out.append(text)
    return out


if __name__ == "__main__":
    cmds = sys.argv[1:]
    if not cmds:
        print(__doc__)
        raise SystemExit(2)
    for cmd, reply in zip(cmds, rcon(cmds)):
        print(f"> {cmd}\n{reply}\n")

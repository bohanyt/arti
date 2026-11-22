"""One-shot Farm Loop v1 CLI — receiver + store yang sudah ada, satu driver.

Bawaan HANYA preflight/status. `--execute` menjalankan `jalankan_farm_v1`
paling banyak sekali. Tidak ada retry, rute, koordinat operator, launcher,
dashboard, atau pengelola save.

`--execute --batch` memanggil batch existing sekali (maksimal empat crop
bersebelahan). `--batch` saja tetap preflight, nol inspect/motor.
`--max-actions` dan `--batch-deadline` hanya sah bersama `--batch`.

PowerShell:

    ./venv/Scripts/python.exe scripts/stardew_farm.py --help
    ./venv/Scripts/python.exe scripts/stardew_farm.py --folder $env:ARTI_STARDEW_COMMAND_DIR
    ./venv/Scripts/python.exe scripts/stardew_farm.py --folder $env:ARTI_STARDEW_COMMAND_DIR --execute
    ./venv/Scripts/python.exe scripts/stardew_farm.py --folder $env:ARTI_STARDEW_COMMAND_DIR --execute --batch --max-actions 2
    ./venv/Scripts/python.exe scripts/stardew_farm.py --folder $env:ARTI_STARDEW_COMMAND_DIR --port 3017 --execute --json .\\farm_v1.json

Folder kotak masuk: `--folder`, atau `ARTI_STARDEW_COMMAND_DIR`.
Port listener: `--port`, bawaan `DEFAULT_PORT` di `arti_stardew` (3017).
Ini BUKAN klaim VERIFIED LIVE.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_stardew import (  # noqa: E402
    DEFAULT_PORT,
    LOOPBACK_HOST,
    StardewReceiver,
    StardewState,
    StardewStateStore,
)
from arti_stardew_command import (  # noqa: E402
    KesalahanPerintah,
    SELESAI,
    SesiPerintah,
    baca_sesi as _baca_sesi_asli,
    folder_perintah,
    minta_inspect_tiles as _minta_inspect_tiles_asli,
    minta_water_once as _minta_water_once_asli,
)
from arti_stardew_farm_v0 import (  # noqa: E402
    LOKASI_BAWAAN,
    PLAYER_MISMATCH,
    SESSION_CHANGED,
    TIMEOUT_STATE_BAWAAN,
    UMUR_STATE_BAWAAN,
    WRONG_LOCATION,
)
from arti_stardew_farm_v1 import (  # noqa: E402
    HasilFarmV1,
    PermintaanFarmV1,
    jalankan_farm_v1 as _jalankan_farm_v1_asli,
)

from arti_stardew_farm_batch import (  # noqa: E402
    AKSI_MAKS,
    DEADLINE_BAWAAN,
    DEADLINE_MAKS,
    HasilFarmBatch,
    PermintaanFarmBatch,
    jalankan_farm_batch as _jalankan_farm_batch_asli,
)

SCHEMA = "stardew_farm_cli.v1"
WAIT_BAWAAN = 8.0
# Batas hingga untuk --wait / --max-age / --state-timeout.
# 0 sah (langsung cek / tidak menunggu). NaN, ±inf, negatif, dan nilai
# di atas WAKTU_MAX ditolak sebelum receiver/driver.
WAKTU_MIN = 0.0
WAKTU_MAX = 3600.0
MAX_CALLBACK_ERRORS = 20
MAX_COMMAND_JEJAK = 8
_STATUS_WATER_SUKSES = "ObservedWatered"

EXIT_OK = 0
EXIT_DRIVER = 1
EXIT_REFUSED = 2
EXIT_OUTPUT = 3

CONFIG_INVALID = "CONFIG_INVALID"
PORT_BUSY = "PORT_BUSY"
HOST_INSTANCE = "HOST_INSTANCE"
SPLIT_SCREEN = "SPLIT_SCREEN"
STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
CANCELLED = "CANCELLED"
OUTPUT_FAILED = "OUTPUT_FAILED"
PREFLIGHT_OK = "PREFLIGHT_OK"
EXECUTED = "EXECUTED"
FOREIGN_PLAYER = "FOREIGN_PLAYER"
RECEIVER_ERRORS = "RECEIVER_ERRORS"


def bangun_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stardew_farm.py",
        description=(
            "One-shot Farm Loop v1: preflight status, atau satu "
            "jalankan_farm_v1; --batch memilih batch diam. Bawaan tanpa aksi."
        ),
        epilog=(
            "Contoh PowerShell:\n"
            "  ./venv/Scripts/python.exe scripts/stardew_farm.py --help\n"
            "  ./venv/Scripts/python.exe scripts/stardew_farm.py "
            "--folder $env:ARTI_STARDEW_COMMAND_DIR\n"
            "  ./venv/Scripts/python.exe scripts/stardew_farm.py "
            "--folder $env:ARTI_STARDEW_COMMAND_DIR --execute\n"
            "  ./venv/Scripts/python.exe scripts/stardew_farm.py "
            "--folder $env:ARTI_STARDEW_COMMAND_DIR --port 3017 "
            "--execute --json .\\farm_v1.json\n"
            "  ./venv/Scripts/python.exe scripts/stardew_farm.py "
            "--folder $env:ARTI_STARDEW_COMMAND_DIR --execute --batch "
            "--max-actions 2 --json .\\farm_batch.json\n"
            "\n"
            "Tidak ada argumen rute atau koordinat target. Tidak ada retry. "
            "Port sibuk = gagal tertutup, tidak merebut listener lain. "
            "Bukan VERIFIED LIVE."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="eksekusi sekali (v1, atau batch jika --batch); tanpa flag ini preflight",
    )
    parser.add_argument(
        "--batch", action="store_true",
        help="pilih batch diam; tetap nol perintah tanpa --execute",
    )
    parser.add_argument(
        "--max-actions", type=int, default=None,
        help=f"khusus --batch: batas 1..{AKSI_MAKS} crop (bawaan {AKSI_MAKS})",
    )
    parser.add_argument(
        "--batch-deadline", type=float, default=None,
        help=(f"khusus --batch: deadline admisi 0 < detik <= {DEADLINE_MAKS:g} "
              f"(bawaan {DEADLINE_BAWAAN:g}); tidak membatalkan aksi inflight"),
    )
    parser.add_argument(
        "--folder",
        default=None,
        help="folder command/ milik mod; default ARTI_STARDEW_COMMAND_DIR",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port listener loopback (bawaan {DEFAULT_PORT}; 0 = ephemeral)",
    )
    parser.add_argument(
        "--player-id",
        type=int,
        default=None,
        dest="player_id",
        help="player_id farmhand yang diharapkan; harus cocok dengan session.json",
    )
    parser.add_argument(
        "--location",
        default=LOKASI_BAWAAN,
        help=f"lokasi yang diharapkan (bawaan {LOKASI_BAWAAN})",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=WAIT_BAWAAN,
        help=(
            f"detik menunggu hello+state farmhand (hingga, "
            f"{WAKTU_MIN:g}..{WAKTU_MAX:g}, bawaan {WAIT_BAWAAN:g}; 0 = cek sekali)"
        ),
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=UMUR_STATE_BAWAAN,
        dest="max_age",
        help=(
            f"umur state maksimum detik (hingga, "
            f"{WAKTU_MIN:g}..{WAKTU_MAX:g}, bawaan {UMUR_STATE_BAWAAN}; "
            f"0 = hanya snapshot usia 0 yang segar)"
        ),
    )
    parser.add_argument(
        "--state-timeout",
        type=float,
        default=TIMEOUT_STATE_BAWAAN,
        dest="state_timeout",
        help=(
            f"timeout wait_next v1 detik (hingga, "
            f"{WAKTU_MIN:g}..{WAKTU_MAX:g}, bawaan {TIMEOUT_STATE_BAWAAN}; "
            f"0 = tidak menunggu revisi baru)"
        ),
    )
    parser.add_argument(
        "--json",
        default=None,
        metavar="PATH",
        help="tulis laporan JSON ringkas ke berkas (skema stardew_farm_cli.v1)",
    )
    return parser


def potret_aman(
    snap: StardewState | None,
    *,
    command_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Field JSON-native saja. Tidak asdict, tidak menu_components (MappingProxyType).

    `command_session_id` adalah konteks sesi perintah yang sedang dicek CLI,
    bukan field native StardewState.
    """
    if snap is None:
        return None
    return {
        "player_id": snap.player_id,
        "screen_id": snap.screen_id,
        "seq": snap.seq,
        "revision": snap.revisi,
        "location": snap.location,
        "tile": [snap.tile[0], snap.tile[1]],
        "pixel": [snap.pixel[0], snap.pixel[1]],
        "facing": snap.facing,
        "energy": snap.energy,
        "health": snap.health,
        "is_player_free": snap.is_player_free,
        "can_player_move": snap.can_player_move,
        "active_menu": snap.active_menu,
        "menu_type": snap.menu_type,
        "menu_snap_id": snap.menu_snap_id,
        "menu_focus_key": snap.menu_focus_key,
        "command_session_id": command_session_id,
    }


def driver_ke_json(
    hasil: HasilFarmV1 | HasilFarmBatch | None,
    *,
    batch: bool = False,
) -> dict[str, Any] | None:
    """Use the selected driver contract, not class identity across module reloads."""
    if hasil is None:
        return None
    if batch:
        hasil = cast(HasilFarmBatch, hasil)
        return {
            "status": hasil.status,
            "success": hasil.success,
            "completed_targets": [list(t) for t in hasil.completed_targets],
            "inspect_count": hasil.inspect_count,
            "water_count": hasil.water_count,
            "inspect_request_ids": list(hasil.inspect_request_ids),
            "water_request_ids": list(hasil.water_request_ids),
            "pin_session_id": hasil.pin_session_id,
            "pin_player_id": hasil.pin_player_id,
            "pin_location_id": hasil.pin_location_id,
            "pin_tile": list(hasil.pin_tile) if hasil.pin_tile is not None else None,
            "reason": hasil.reason,
            "iterations": [
                {
                    "status": i.status,
                    "success": i.success,
                    "selected_target": (
                        list(i.selected_target) if i.selected_target is not None else None
                    ),
                    "inspect_attempted": i.inspect_attempted,
                    "inspect_request_id": i.inspect_request_id,
                    "inspect_outcome": i.inspect_outcome,
                    "water_outcome": i.water_outcome,
                    "terminal_request_id": i.terminal_request_id,
                    "terminal_outcome": i.terminal_outcome,
                    "reason": i.reason,
                }
                for i in hasil.iterations
            ],
        }
    hasil = cast(HasilFarmV1, hasil)
    return {
        "status": hasil.status,
        "success": hasil.success,
        "selected_target": (
            [hasil.selected_target[0], hasil.selected_target[1]]
            if hasil.selected_target is not None
            else None
        ),
        "inspect_attempted": hasil.inspect_attempted,
        "inspect_request_id": hasil.inspect_request_id,
        "inspect_outcome": hasil.inspect_outcome,
        "water_outcome": hasil.water_outcome,
        "terminal_request_id": hasil.terminal_request_id,
        "terminal_outcome": hasil.terminal_outcome,
        "revision": hasil.revision,
        "tile": [hasil.tile[0], hasil.tile[1]] if hasil.tile is not None else None,
        "pixel": (
            [hasil.pixel[0], hasil.pixel[1]] if hasil.pixel is not None else None
        ),
        "reason": hasil.reason,
    }


def sesi_ke_json(sesi: SesiPerintah | None) -> dict[str, Any] | None:
    if sesi is None:
        return None
    return {
        "session_id": sesi.session_id,
        "player_id": sesi.player_id,
        "player_name": sesi.player_name,
        "location_id": sesi.location_id,
        "screen_id": sesi.screen_id,
        "is_main_player": sesi.is_main_player,
        "is_split_screen": sesi.is_split_screen,
        "inbox_enabled": sesi.inbox_enabled,
        "water_once_enabled": sesi.water_once_enabled,
        "world_ready": sesi.world_ready,
        "inflight_request_id": sesi.inflight_request_id,
        "input_busy": sesi.input_busy,
    }


def tulis_json_aman(path: Path, obj: dict[str, Any]) -> None:
    """Dump ketat: gagal kalau ada tipe non-JSON. Tidak default=str."""
    teks = json.dumps(obj, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(teks + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class PinSesi:
    """Pin CLI: session_id + player_id + location_id, plus konteks hello."""

    session_id: str
    player_id: int
    player_name: str
    location_id: str
    screen_id: int


def pin_dari_sesi(sesi: SesiPerintah) -> PinSesi:
    return PinSesi(
        session_id=sesi.session_id,
        player_id=sesi.player_id,
        player_name=sesi.player_name,
        location_id=sesi.location_id,
        screen_id=sesi.screen_id,
    )


def validasi_waktu(nilai: object, *, nama: str) -> str | None:
    """Tolak NaN/±inf/non-finite/di luar [WAKTU_MIN, WAKTU_MAX]. 0 sah."""
    if isinstance(nilai, bool) or not isinstance(nilai, (int, float)):
        return f"{nama} harus angka hingga"
    if not math.isfinite(nilai):
        return (
            f"{nama} harus hingga (bukan NaN/infinity); "
            f"rentang {WAKTU_MIN:g}..{WAKTU_MAX:g} detik"
        )
    if nilai < WAKTU_MIN or nilai > WAKTU_MAX:
        return f"{nama}={nilai:g} di luar {WAKTU_MIN:g}..{WAKTU_MAX:g} detik"
    return None


def _beda_pin(sesi: SesiPerintah, pin: PinSesi) -> str | None:
    if sesi.session_id != pin.session_id:
        return f"session_id {pin.session_id!r} -> {sesi.session_id!r}"
    if sesi.player_id != pin.player_id:
        return f"player_id {pin.player_id} -> {sesi.player_id}"
    if sesi.location_id != pin.location_id:
        return f"location_id {pin.location_id!r} -> {sesi.location_id!r}"
    return None


def _kunci_baca_sesi(
    asli: Callable[..., SesiPerintah],
    pin: PinSesi,
) -> Callable[..., SesiPerintah]:
    """Injection boundary: v1 tetap baca_sesi, tetapi tidak boleh rebind pin CLI."""

    def wrapper(folder: str | os.PathLike[str] | None = None) -> SesiPerintah:
        sesi = asli(folder)
        beda = _beda_pin(sesi, pin)
        if beda is not None:
            raise KesalahanPerintah(
                f"sesi berubah sesudah preflight CLI ({beda}); "
                f"pin tetap {pin.session_id!r}/{pin.player_id}/{pin.location_id!r}"
            )
        return sesi

    return wrapper


def _identitas_hello(ident, pin: PinSesi) -> tuple[str, str] | None:
    if ident.is_main_player:
        return HOST_INSTANCE, "hello pemain utama, bukan farmhand"
    if ident.is_split_screen:
        return SPLIT_SCREEN, "hello split-screen, bukan remote farmhand"
    if ident.player_id != pin.player_id:
        return (
            PLAYER_MISMATCH,
            f"hello player_id {ident.player_id} != sesi {pin.player_id}",
        )
    if ident.player_name.strip() != pin.player_name.strip():
        return (
            PLAYER_MISMATCH,
            f"hello name {ident.player_name!r} != sesi {pin.player_name!r}",
        )
    if ident.screen_id != pin.screen_id:
        return (
            PLAYER_MISMATCH,
            f"hello screen {ident.screen_id} != sesi {pin.screen_id}",
        )
    return None


def _identitas_state(snap: StardewState, pin: PinSesi) -> tuple[str, str] | None:
    if snap.player_id != pin.player_id:
        return (
            PLAYER_MISMATCH,
            f"state player_id {snap.player_id} != sesi {pin.player_id}",
        )
    if snap.screen_id != pin.screen_id:
        return (
            PLAYER_MISMATCH,
            f"state screen {snap.screen_id} != sesi {pin.screen_id}",
        )
    if snap.location != pin.location_id:
        return (
            WRONG_LOCATION,
            f"state location {snap.location!r} != sesi {pin.location_id!r}",
        )
    return None


@dataclass
class _Pencatat:
    """Satu store, penghitung terbatas, tanpa log event penuh."""

    store: StardewStateStore
    expected_player_id: int
    n_hello: int = 0
    n_state: int = 0
    n_lain: int = 0
    callback_error_total: int = 0
    callback_errors: list[str] | None = None
    foreign_farmhand: bool = False

    def __post_init__(self) -> None:
        if self.callback_errors is None:
            self.callback_errors = []

    def on_event(self, event: dict) -> None:
        try:
            self.store.catat(event)
            jenis = event.get("ev")
            if jenis == "hello":
                self.n_hello += 1
                if (
                    event.get("is_main_player") is False
                    and event.get("player_id") != self.expected_player_id
                ):
                    self.foreign_farmhand = True
            elif jenis == "state":
                self.n_state += 1
            else:
                self.n_lain += 1
        except Exception as exc:  # noqa: BLE001 — receiver harus tetap hidup
            self.callback_error_total += 1
            assert self.callback_errors is not None
            if len(self.callback_errors) < MAX_CALLBACK_ERRORS:
                self.callback_errors.append(f"{type(exc).__name__}: {exc}")


def _bungkus_perintah(
    fn: Callable[..., Any],
    jejak: list[dict[str, Any]],
    op: str,
) -> Callable[..., Any]:
    """Catat percobaan SEBELUM delegasi. Interrupt = attempt, outcome unknown."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        rekam: dict[str, Any] = {
            "op": op,
            "status": None,
            "sukses": None,
            "request_id": None,
            "outcome": None,
            "alasan": None,
            "returned": False,
        }
        if op == "inspect_tiles" and len(args) >= 3:
            rekam["center_x"] = args[0]
            rekam["center_y"] = args[1]
            rekam["radius"] = args[2]
        elif op == "water_once" and len(args) >= 2:
            rekam["tile_x"] = args[0]
            rekam["tile_y"] = args[1]
        if len(jejak) < MAX_COMMAND_JEJAK:
            jejak.append(rekam)
        hasil = fn(*args, **kwargs)
        rekam["status"] = hasil.status
        rekam["sukses"] = hasil.sukses
        rekam["request_id"] = hasil.request_id
        rekam["outcome"] = hasil.outcome
        rekam["alasan"] = hasil.alasan
        rekam["returned"] = True
        return hasil

    return wrapper


def _cetak(out: TextIO, *bagian: str) -> None:
    print("[farm] " + " ".join(bagian), file=out, flush=True)


def _laporan(
    *,
    mode: str,
    cli_status: str,
    reason: str | None,
    execute: bool,
    action_attempted: bool,
    driver_invoked: bool,
    command_attempted: bool,
    water_confirmed: bool,
    sesi: SesiPerintah | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    driver: dict[str, Any] | None,
    receiver: dict[str, Any],
    command_calls: list[dict[str, Any]],
    output_error: str | None = None,
) -> dict[str, Any]:
    refused = cli_status not in (PREFLIGHT_OK, EXECUTED)
    ok = (
        (mode == "status" and cli_status == PREFLIGHT_OK)
        or (
            mode == "execute"
            and cli_status == EXECUTED
            and bool(driver and driver.get("success"))
        )
    )
    return {
        "schema": SCHEMA,
        "mode": mode,
        "ok": ok,
        "refused": refused,
        "reason": reason,
        "cli_status": cli_status,
        "execute": execute,
        "action_attempted": action_attempted,
        "driver_invoked": driver_invoked,
        "command_attempted": command_attempted,
        "water_confirmed": water_confirmed,
        "session": sesi_ke_json(sesi),
        "state_before": before,
        "state_after": after,
        "driver": driver,
        "command_calls": command_calls,
        "receiver": receiver,
        "output_error": output_error,
        "verified_live": False,
    }


def _receiver_info(
    pencatat: _Pencatat | None,
    *,
    host: str | None,
    port: int | None,
    started: bool,
    stopped: bool,
    stop_error: str | None = None,
) -> dict[str, Any]:
    err_total = 0 if pencatat is None else pencatat.callback_error_total
    return {
        "host": host,
        "port": port,
        "started": started,
        "stopped": stopped,
        "stop_error": stop_error,
        "hello_count": 0 if pencatat is None else pencatat.n_hello,
        "state_count": 0 if pencatat is None else pencatat.n_state,
        "other_count": 0 if pencatat is None else pencatat.n_lain,
        "callback_error_total": err_total,
        "callback_errors": [] if pencatat is None else list(pencatat.callback_errors or []),
        "lost_events": err_total > 0,
        "foreign_farmhand": False if pencatat is None else pencatat.foreign_farmhand,
    }


def _cetak_laporan(out: TextIO, laporan: dict[str, Any]) -> None:
    _cetak(
        out,
        f"mode={laporan['mode']}",
        f"cli_status={laporan['cli_status']}",
        f"ok={laporan['ok']}",
    )
    if laporan.get("reason"):
        _cetak(out, f"reason={laporan['reason']}")
    sesi = laporan.get("session") or {}
    if sesi:
        _cetak(
            out,
            f"session_id={sesi.get('session_id')}",
            f"player_id={sesi.get('player_id')}",
            f"name={sesi.get('player_name')}",
            f"location={sesi.get('location_id')}",
        )
    before = laporan.get("state_before")
    if before:
        menu = before.get("active_menu") or "none"
        _cetak(
            out,
            f"before rev={before.get('revision')}",
            f"tile={tuple(before['tile']) if before.get('tile') else None}",
            f"pixel={tuple(before['pixel']) if before.get('pixel') else None}",
            f"menu={menu}",
        )
    after = laporan.get("state_after")
    if after and after != before:
        _cetak(
            out,
            f"after rev={after.get('revision')}",
            f"tile={tuple(after['tile']) if after.get('tile') else None}",
            f"pixel={tuple(after['pixel']) if after.get('pixel') else None}",
        )
    driver = laporan.get("driver")
    if driver and laporan.get("driver_mode") != "batch":
        target = driver.get("selected_target")
        _cetak(
            out,
            f"driver={driver.get('status')}",
            f"success={driver.get('success')}",
            f"target={tuple(target) if target else None}",
        )
        _cetak(
            out,
            f"inspect={driver.get('inspect_request_id')}:{driver.get('inspect_outcome')}",
            f"water={driver.get('water_outcome')}",
            f"terminal={driver.get('terminal_request_id')}:{driver.get('terminal_outcome')}",
        )
        if driver.get("reason"):
            _cetak(out, f"driver_reason={driver['reason']}")
    progress = laporan.get("batch_progress")
    if progress is not None:
        _cetak(
            out,
            f"batch={driver.get('status') if driver else 'NO_RETURNED_RESULT'}",
            f"completed={len(progress['completed_targets'])}",
            f"targets={progress['completed_targets']}",
            f"success={driver.get('success') if driver else False}",
        )
        for i in (driver or {}).get("iterations", []):
            _cetak(out, f"iteration={i['status']} target={i['selected_target']}",
                   f"terminal={i['terminal_request_id']}:{i['terminal_outcome']}")
        _cetak(out, "batch terbatas; bukan klaim seluruh Farm basah")
    recv = laporan.get("receiver") or {}
    if recv.get("started"):
        _cetak(
            out,
            f"receiver={recv.get('host')}:{recv.get('port')}",
            f"hello={recv.get('hello_count')}",
            f"state={recv.get('state_count')}",
            f"callback_err={recv.get('callback_error_total')}",
        )
    if recv.get("lost_events"):
        _cetak(out, "receiver_errors=true (jejak state tidak lengkap)")
        for baris in recv.get("callback_errors") or []:
            _cetak(out, f"callback={baris}")
    if recv.get("stop_error"):
        _cetak(out, f"receiver_stop_error={recv['stop_error']}")
        _cetak(out, "receiver_stopped=false")
    if laporan["mode"] == "status" and laporan["cli_status"] == PREFLIGHT_OK:
        pilihan = "batch diam" if laporan.get("driver_mode") == "batch" else "Farm Loop v1"
        _cetak(out, f"execute=false — tambah --execute untuk satu {pilihan}")
    if laporan.get("output_error"):
        _cetak(out, f"output_error={laporan['output_error']}")
        teks = _teks_gagal_json(laporan)
        if teks:
            _cetak(out, teks)


def _teks_gagal_json(laporan: dict[str, Any]) -> str | None:
    if laporan.get("water_confirmed"):
        return (
            "ObservedWatered tercatat; "
            "JSON gagal ditulis; TIDAK diulang"
        )
    if laporan.get("command_attempted"):
        return (
            "ada percobaan perintah (lihat command_calls); "
            "hasil belum pasti aksi; JSON gagal; TIDAK diulang"
        )
    if laporan.get("driver_invoked"):
        return (
            "driver sudah dipanggil; lihat status driver; "
            "JSON gagal ditulis; TIDAK diulang"
        )
    return None


def _validasi_port(port: int) -> str | None:
    if isinstance(port, bool) or not isinstance(port, int):
        return "port harus int"
    if port < 0 or port > 65535:
        return f"port {port} di luar 0..65535"
    return None


def _validasi_batch(args: argparse.Namespace) -> str | None:
    if not args.batch and (args.max_actions is not None or args.batch_deadline is not None):
        return "--max-actions / --batch-deadline memerlukan --batch"
    if args.max_actions is not None and not 1 <= args.max_actions <= AKSI_MAKS:
        return f"--max-actions harus integer 1..{AKSI_MAKS}"
    d = args.batch_deadline
    if d is not None and (not math.isfinite(d) or not 0 < d <= DEADLINE_MAKS):
        return f"--batch-deadline harus hingga, 0 < detik <= {DEADLINE_MAKS:g}"
    return None


def _progres_batch(
    hasil: HasilFarmBatch | None,
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Hanya hasil yang kembali; interrupt tidak mengarang HasilFarmBatch."""
    returned = hasil is not None
    if hasil is not None:
        targets = [list(t) for t in hasil.completed_targets]
    else:
        targets = [
            [c["tile_x"], c["tile_y"]] for c in calls
            if c.get("op") == "water_once" and c.get("returned")
            and c.get("status") == SELESAI and c.get("sukses")
            and c.get("outcome") == _STATUS_WATER_SUKSES
        ]
    return {
        "result_returned": returned,
        "completed_targets": targets,
        "source": "batch_result" if returned else "returned_command_calls",
        "inspect_call_count": sum(c["op"] == "inspect_tiles" for c in calls),
        "water_call_count": sum(c["op"] == "water_once" for c in calls),
    }


def _gerbang_sesi(
    sesi: SesiPerintah,
    *,
    player_id: int | None,
    location: str,
) -> tuple[str, str] | None:
    if sesi.is_main_player:
        return HOST_INSTANCE, "session.json milik HOST, bukan farmhand ARTI"
    if sesi.is_split_screen:
        return SPLIT_SCREEN, "session.json split-screen, bukan remote farmhand"
    if player_id is not None and player_id != sesi.player_id:
        return (
            PLAYER_MISMATCH,
            f"player_id permintaan {player_id} != sesi {sesi.player_id}",
        )
    if not sesi.session_id:
        return "SESSION_UNREADABLE", "session.json tanpa session_id"
    if not sesi.location_id:
        return "SESSION_UNREADABLE", "session.json tanpa location_id"
    if location and sesi.location_id and sesi.location_id != location:
        return (
            WRONG_LOCATION,
            f"sesi location_id {sesi.location_id!r} != {location!r}",
        )
    return None


def _tunggu_farmhand(
    pencatat: _Pencatat,
    pin: PinSesi,
    *,
    wait: float,
    max_age: float,
    jam: Callable[[], float],
    tidur: Callable[[float], None],
) -> tuple[StardewState | None, str | None, str | None]:
    """None, status, reason kalau gagal. Snap kalau siap dan cocok pin CLI."""
    batas = jam() + max(0.0, float(wait))
    pid = pin.player_id
    pernah_state = False
    while True:
        for ident in pencatat.store.semua_identitas():
            if not ident.is_main_player and ident.player_id != pid:
                return (
                    None,
                    FOREIGN_PLAYER,
                    "hello farmhand lain; tidak rebind ke player_id sesi",
                )
        if pencatat.foreign_farmhand:
            return (
                None,
                FOREIGN_PLAYER,
                "hello farmhand lain; tidak rebind ke player_id sesi",
            )
        ident = pencatat.store.identitas(pid)
        snap = pencatat.store.latest(pid)
        if snap is not None:
            pernah_state = True
        if ident is not None:
            gagal_hello = _identitas_hello(ident, pin)
            if gagal_hello is not None:
                return None, gagal_hello[0], gagal_hello[1]
        if snap is not None:
            gagal_state = _identitas_state(snap, pin)
            if gagal_state is not None:
                return None, gagal_state[0], gagal_state[1]
        if ident is not None and snap is not None and snap.player_id == pid:
            if pencatat.store.is_stale(pid, max_age=max_age):
                if jam() >= batas:
                    return None, "STALE_STATE", "state farmhand basi"
            else:
                return snap, None, None
        sisa = batas - jam()
        if sisa <= 0:
            if pernah_state:
                return None, "STALE_STATE", "state farmhand basi"
            if ident is None and snap is None:
                return (
                    None,
                    STARTUP_TIMEOUT,
                    f"tidak ada hello+state farmhand player_id={pid} dalam {wait:g}s",
                )
            return None, "MISSING_STATE", "hello ada tetapi state belum sampai"
        tidur(min(0.05, sisa))


def jalankan(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    receiver_cls: type = StardewReceiver,
    store: StardewStateStore | None = None,
    baca_sesi: Callable[..., SesiPerintah] = _baca_sesi_asli,
    minta_inspect_tiles: Callable[..., Any] = _minta_inspect_tiles_asli,
    minta_water_once: Callable[..., Any] = _minta_water_once_asli,
    jalankan_v1: Callable[..., HasilFarmV1] = _jalankan_farm_v1_asli,
    jalankan_batch: Callable[..., HasilFarmBatch] = _jalankan_farm_batch_asli,
    cek_batal: Callable[[], bool] | None = None,
    tulis_json: Callable[[Path, dict[str, Any]], None] = tulis_json_aman,
    on_listener: Callable[[str, int], None] | None = None,
    jam: Callable[[], float] = time.monotonic,
    tidur: Callable[[float], None] = time.sleep,
) -> int:
    """Entry tes-friendly. `--help` tetap argparse murni (sys.exit)."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = bangun_parser().parse_args(argv)

    mode = "execute" if args.execute else "status"
    batch_actions = args.max_actions if args.max_actions is not None else AKSI_MAKS
    batch_deadline = (
        args.batch_deadline if args.batch_deadline is not None else DEADLINE_BAWAAN
    )
    laporan: dict[str, Any] | None = None
    sesi: SesiPerintah | None = None
    pin: PinSesi | None = None
    receiver = None
    started = False
    stopped = False
    stop_error: str | None = None
    host: str | None = None
    port_aktif: int | None = None
    pencatat: _Pencatat | None = None
    driver_invoked = False
    driver_hasil: HasilFarmV1 | HasilFarmBatch | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    command_calls: list[dict[str, Any]] = []
    cli_status = CONFIG_INVALID
    reason: str | None = None
    kode = EXIT_REFUSED

    def _flag_aksi() -> tuple[bool, bool, bool]:
        confirmed = False
        if driver_hasil is not None:
            if args.batch:
                confirmed = bool(cast(HasilFarmBatch, driver_hasil).completed_targets)
            else:
                single = cast(HasilFarmV1, driver_hasil)
                confirmed = single.success and single.water_outcome == _STATUS_WATER_SUKSES
        for panggilan in command_calls:
            if (
                panggilan.get("op") == "water_once"
                and panggilan.get("returned")
                and panggilan.get("status") == SELESAI
                and panggilan.get("sukses")
                and panggilan.get("outcome") == _STATUS_WATER_SUKSES
            ):
                confirmed = True
        return driver_invoked, bool(command_calls), confirmed

    def _selesai(status: str, alasan: str | None, exit_code: int) -> int:
        nonlocal laporan, cli_status, reason, kode
        cli_status = status
        reason = alasan
        kode = exit_code
        invoked, attempted, confirmed = _flag_aksi()
        laporan = _laporan(
            mode=mode,
            cli_status=status,
            reason=alasan,
            execute=bool(args.execute),
            action_attempted=invoked,
            driver_invoked=invoked,
            command_attempted=attempted,
            water_confirmed=confirmed,
            sesi=sesi,
            before=before,
            after=after,
            driver=driver_ke_json(driver_hasil, batch=args.batch),
            receiver=_receiver_info(
                pencatat,
                host=host,
                port=port_aktif,
                started=started,
                stopped=stopped,
                stop_error=stop_error,
            ),
            command_calls=list(command_calls),
        )
        return exit_code

    try:
        salah_port = _validasi_port(args.port)
        salah_batch = _validasi_batch(args)
        salah_waktu = (
            validasi_waktu(args.wait, nama="--wait")
            or validasi_waktu(args.max_age, nama="--max-age")
            or validasi_waktu(args.state_timeout, nama="--state-timeout")
        )
        if salah_port is not None:
            _selesai(CONFIG_INVALID, salah_port, EXIT_REFUSED)
        elif salah_waktu is not None:
            _selesai(CONFIG_INVALID, salah_waktu, EXIT_REFUSED)
        elif salah_batch is not None:
            _selesai(CONFIG_INVALID, salah_batch, EXIT_REFUSED)
        else:
            try:
                folder_perintah(args.folder)
                sesi = baca_sesi(args.folder)
            except KesalahanPerintah as exc:
                _selesai("SESSION_UNREADABLE", str(exc), EXIT_REFUSED)
            else:
                gerbang = _gerbang_sesi(
                    sesi, player_id=args.player_id, location=args.location
                )
                if gerbang is not None:
                    _selesai(gerbang[0], gerbang[1], EXIT_REFUSED)
                else:
                    pin = pin_dari_sesi(sesi)
                    store_pakai = store if store is not None else StardewStateStore()
                    pencatat = _Pencatat(store_pakai, pin.player_id)
                    receiver = receiver_cls(pencatat.on_event, port=args.port)
                    try:
                        receiver.start()
                    except OSError as exc:
                        _selesai(
                            PORT_BUSY,
                            f"gagal bind {LOOPBACK_HOST}:{args.port}: {exc}",
                            EXIT_REFUSED,
                        )
                    else:
                        started = True
                        host, port_aktif = receiver.address
                        if on_listener is not None:
                            on_listener(host, port_aktif)

                        snap, status_tunggu, alasan_tunggu = _tunggu_farmhand(
                            pencatat,
                            pin,
                            wait=args.wait,
                            max_age=args.max_age,
                            jam=jam,
                            tidur=tidur,
                        )
                        if snap is None:
                            _selesai(
                                status_tunggu or STARTUP_TIMEOUT,
                                alasan_tunggu,
                                EXIT_REFUSED,
                            )
                        else:
                            before = potret_aman(
                                snap, command_session_id=pin.session_id
                            )
                            try:
                                sesi_kini = baca_sesi(args.folder)
                            except KesalahanPerintah as exc:
                                _selesai("SESSION_UNREADABLE", str(exc), EXIT_REFUSED)
                            else:
                                beda = _beda_pin(sesi_kini, pin)
                                if beda is not None:
                                    _selesai(SESSION_CHANGED, beda, EXIT_REFUSED)
                                elif not args.execute:
                                    after = before
                                    _selesai(PREFLIGHT_OK, None, EXIT_OK)
                                elif pencatat.callback_error_total:
                                    _selesai(
                                        RECEIVER_ERRORS,
                                        "callback receiver gagal sebelum execute; jejak tidak lengkap",
                                        EXIT_REFUSED,
                                    )
                                else:
                                    inspect_fn = _bungkus_perintah(
                                        minta_inspect_tiles,
                                        command_calls,
                                        "inspect_tiles",
                                    )
                                    water_fn = _bungkus_perintah(
                                        minta_water_once, command_calls, "water_once"
                                    )
                                    request_fields = dict(
                                        location=pin.location_id or LOKASI_BAWAAN,
                                        folder=args.folder,
                                        player_id=pin.player_id,
                                        max_age=args.max_age,
                                        state_timeout=args.state_timeout,
                                    )
                                    dependencies = dict(
                                        store=store_pakai,
                                        baca_sesi=_kunci_baca_sesi(baca_sesi, pin),
                                        minta_inspect_tiles=inspect_fn,
                                        minta_water_once=water_fn,
                                    )
                                    driver_invoked = True
                                    if args.batch:
                                        permintaan_batch = PermintaanFarmBatch(
                                            **request_fields,
                                            max_actions=batch_actions,
                                            deadline_seconds=batch_deadline,
                                            cek_batal=cek_batal,
                                        )
                                        driver_hasil = jalankan_batch(
                                            permintaan_batch, jam=jam, **dependencies,
                                        )
                                    else:
                                        driver_hasil = jalankan_v1(
                                            PermintaanFarmV1(**request_fields), **dependencies,
                                        )
                                    after = potret_aman(
                                        store_pakai.latest(pin.player_id),
                                        command_session_id=pin.session_id,
                                    )
                                    exit_driver = (
                                        EXIT_OK if driver_hasil.success else EXIT_DRIVER
                                    )
                                    _selesai(
                                        EXECUTED, driver_hasil.reason, exit_driver
                                    )

    except KeyboardInterrupt:
        alasan = "dibatalkan; aksi inflight tidak di-undo, inbox tidak dihapus"
        if driver_invoked or command_calls:
            _selesai(CANCELLED, alasan, EXIT_DRIVER)
        else:
            _selesai(CANCELLED, alasan, EXIT_REFUSED)
    finally:
        if receiver is not None:
            try:
                receiver.stop()
                stopped = True
            except Exception as exc:  # noqa: BLE001 — cleanup tidak boleh melempar
                stopped = False
                stop_error = f"{type(exc).__name__}: {exc}"
                print(
                    f"[farm] receiver.stop gagal: {stop_error}",
                    file=err,
                    flush=True,
                )
            if laporan is not None:
                laporan["receiver"] = _receiver_info(
                    pencatat,
                    host=host,
                    port=port_aktif,
                    started=started,
                    stopped=stopped,
                    stop_error=stop_error,
                )

        if laporan is None:
            invoked, attempted, confirmed = _flag_aksi()
            laporan = _laporan(
                mode=mode,
                cli_status=cli_status,
                reason=reason,
                execute=bool(getattr(args, "execute", False)),
                action_attempted=invoked,
                driver_invoked=invoked,
                command_attempted=attempted,
                water_confirmed=confirmed,
                sesi=sesi,
                before=before,
                after=after,
                driver=driver_ke_json(driver_hasil, batch=args.batch),
                receiver=_receiver_info(
                    pencatat,
                    host=host,
                    port=port_aktif,
                    started=started,
                    stopped=stopped,
                    stop_error=stop_error,
                ),
                command_calls=list(command_calls),
            )

        laporan["driver_mode"] = "batch" if args.batch else "single"
        if args.batch:
            laporan["batch_options"] = {
                "max_actions": batch_actions,
                "deadline_seconds": (batch_deadline if math.isfinite(batch_deadline) else None),
            }
            laporan["batch_progress"] = _progres_batch(
                cast(HasilFarmBatch | None, driver_hasil), command_calls,
            )
        _cetak_laporan(out, laporan)

        if args.json:
            try:
                tulis_json(Path(args.json), laporan)
                _cetak(out, f"json={args.json}")
            except Exception as exc:  # noqa: BLE001 — aksi tidak diulang
                laporan["output_error"] = f"{type(exc).__name__}: {exc}"
                laporan["cli_status"] = OUTPUT_FAILED
                _cetak(out, f"output_error={laporan['output_error']}")
                teks = _teks_gagal_json(laporan)
                if teks:
                    _cetak(out, teks)
                if laporan.get("driver_invoked") or laporan.get("command_attempted"):
                    kode = EXIT_OUTPUT

    return kode


def main(argv: list[str] | None = None) -> int:
    return jalankan(argv)


if __name__ == "__main__":
    raise SystemExit(main())

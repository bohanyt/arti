"""Farm Loop v0 — orkestrasi Python deterministik, tanpa planner.

Satu target ubin yang sudah diketahui, plus rute gerak opsional yang
diberikan operator. Driver ini tidak mencari target, tidak mengarang
rute, dan tidak menganggap `PressCompleted` sebagai bukti farmer pindah.

Alur (gagal tertutup, tanpa retry):

1. tolak rute di luar batas sebelum perintah apa pun;
2. baca `StardewStateStore` yang diisi `StardewReceiver` (bukan cache kedua);
   `session_id` + `player_id` awal di-pin; baca sesi berikutnya yang
   berbeda adalah `SESSION_CHANGED`, bukan rebind;
3. untuk setiap segmen: satu `minta_world_press`, lalu `wait_next`, lalu
   bukti pergeseran piksel ke arah yang diminta;
4. gerbang Manhattan == 1;
5. tepat satu `minta_water_once`;
6. sukses driver hanya `ObservedWatered`.

Pemanggil yang memasang receiver:

    store = StardewStateStore()
    receiver = StardewReceiver(store.catat)
    receiver.start()
    hasil = jalankan_farm_v0(permintaan, store=store)
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from arti_stardew import StardewState, StardewStateStore
from arti_stardew_command import (
    ARAH_SAH,
    DITOLAK,
    GAGAL_LOKAL,
    KesalahanPerintah,
    OP_WATER_ONCE,
    OP_WORLD_PRESS,
    SELESAI,
    SesiPerintah,
    HasilPerintah,
    TIDAK_DIAMBIL,
    TIDAK_PASTI,
    TILE_MAX,
    TILE_MIN,
    WORLD_PRESS_TICKS_MAX,
    WORLD_PRESS_TICKS_MIN,
    baca_sesi as _baca_sesi_asli,
    minta_water_once as _minta_water_once_asli,
    minta_world_press as _minta_world_press_asli,
)

LOKASI_BAWAAN = "Farm"
SEGMEN_MAKS = 8
TICKS_TOTAL_MAKS = 240
UMUR_STATE_BAWAAN = 0.5
TIMEOUT_STATE_BAWAAN = 1.0

SUCCESS = "SUCCESS"
ROUTE_INVALID = "ROUTE_INVALID"
MISSING_STATE = "MISSING_STATE"
STALE_STATE = "STALE_STATE"
PLAYER_MISMATCH = "PLAYER_MISMATCH"
WRONG_LOCATION = "WRONG_LOCATION"
MENU_OPEN = "MENU_OPEN"
CANNOT_MOVE = "CANNOT_MOVE"
INBOX_BUSY = "INBOX_BUSY"
WORLD_PRESS_REJECTED = "WORLD_PRESS_REJECTED"
WORLD_PRESS_UNCERTAIN = "WORLD_PRESS_UNCERTAIN"
WORLD_PRESS_FAILED = "WORLD_PRESS_FAILED"
NO_DISPLACEMENT = "NO_DISPLACEMENT"
WRONG_DIRECTION = "WRONG_DIRECTION"
STATE_TIMEOUT = "STATE_TIMEOUT"
UNEXPECTED_LOCATION = "UNEXPECTED_LOCATION"
NOT_ADJACENT = "NOT_ADJACENT"
WATER_REJECTED = "WATER_REJECTED"
WATER_UNCERTAIN = "WATER_UNCERTAIN"
WATER_FAILED = "WATER_FAILED"
SESSION_UNREADABLE = "SESSION_UNREADABLE"
SESSION_CHANGED = "SESSION_CHANGED"

_STATUS_WATER_SUKSES = "ObservedWatered"
_STATUS_PRESS_SUKSES = "PressCompleted"


@dataclass(frozen=True)
class PermintaanFarmV0:
    """Kontrak masukan v0. `route` kosong = farmer harus sudah bersebelahan."""

    target: tuple[int, int]
    route: tuple[tuple[str, int], ...] = ()
    location: str = LOKASI_BAWAAN
    folder: str | None = None
    player_id: int | None = None
    max_age: float = UMUR_STATE_BAWAAN
    state_timeout: float = TIMEOUT_STATE_BAWAAN


@dataclass(frozen=True)
class HasilFarmV0:
    """Satu putusan driver. `success` hanya True untuk native ObservedWatered."""

    status: str
    success: bool
    world_press_attempted: bool
    segments_completed: int
    adjacency_achieved: bool
    water_outcome: str | None = None
    terminal_request_id: str | None = None
    terminal_outcome: str | None = None
    press_request_ids: tuple[str, ...] = ()
    press_outcomes: tuple[str | None, ...] = ()
    revision: int | None = None
    tile: tuple[int, int] | None = None
    pixel: tuple[float, float] | None = None
    reason: str | None = None


@dataclass
class _Jejak:
    press_ids: list[str] = field(default_factory=list)
    press_outcomes: list[str | None] = field(default_factory=list)
    world_press_attempted: bool = False
    segments_completed: int = 0
    snap: StardewState | None = None
    water_outcome: str | None = None
    terminal_request_id: str | None = None
    terminal_outcome: str | None = None


def bersebelahan_kardinal(ubin: tuple[int, int], target: tuple[int, int]) -> bool:
    """True hanya untuk tetangga kardinal: Manhattan == 1."""
    return abs(ubin[0] - target[0]) + abs(ubin[1] - target[1]) == 1


def validasi_rute(route: Sequence[tuple[str, int]]) -> str | None:
    """None kalau rute sah. Tolak seluruh rencana sebelum perintah apa pun."""
    if len(route) > SEGMEN_MAKS:
        return f"rute {len(route)} segmen > {SEGMEN_MAKS}"
    total = 0
    for item in route:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            return f"segmen rute tidak berbentuk (arah, ticks): {item!r}"
        arah, ticks = item
        arah_bersih = (arah or "").strip().lower() if isinstance(arah, str) else ""
        if arah_bersih not in ARAH_SAH:
            return f"arah harus salah satu dari {ARAH_SAH}, dapat {arah!r}"
        if isinstance(ticks, bool) or not isinstance(ticks, int):
            return (
                f"ticks harus integer {WORLD_PRESS_TICKS_MIN}.."
                f"{WORLD_PRESS_TICKS_MAX}, dapat {ticks!r}"
            )
        if not WORLD_PRESS_TICKS_MIN <= ticks <= WORLD_PRESS_TICKS_MAX:
            return (
                f"ticks harus {WORLD_PRESS_TICKS_MIN}.."
                f"{WORLD_PRESS_TICKS_MAX}, dapat {ticks}"
            )
        total += ticks
    if total > TICKS_TOTAL_MAKS:
        return f"total ticks {total} > {TICKS_TOTAL_MAKS}"
    return None


def _validasi_target(target: object) -> str | None:
    if not isinstance(target, (tuple, list)) or len(target) != 2:
        return f"target harus (x, y), dapat {target!r}"
    for nama, nilai in (("x", target[0]), ("y", target[1])):
        if isinstance(nilai, bool) or not isinstance(nilai, int):
            return f"target.{nama} harus integer, dapat {nilai!r}"
        if not TILE_MIN <= nilai <= TILE_MAX:
            return f"target.{nama} harus {TILE_MIN}..{TILE_MAX}, dapat {nilai}"
    return None


def _normalisasi_rute(
    route: Sequence[tuple[str, int]],
) -> tuple[tuple[str, int], ...]:
    return tuple((arah.strip().lower(), ticks) for arah, ticks in route)


def _dari_snap(jejak: _Jejak) -> dict[str, Any]:
    snap = jejak.snap
    return {
        "world_press_attempted": jejak.world_press_attempted,
        "segments_completed": jejak.segments_completed,
        "adjacency_achieved": False,
        "water_outcome": jejak.water_outcome,
        "terminal_request_id": jejak.terminal_request_id,
        "terminal_outcome": jejak.terminal_outcome,
        "press_request_ids": tuple(jejak.press_ids),
        "press_outcomes": tuple(jejak.press_outcomes),
        "revision": None if snap is None else snap.revisi,
        "tile": None if snap is None else snap.tile,
        "pixel": None if snap is None else snap.pixel,
    }


def _hasil(
    status: str,
    jejak: _Jejak,
    *,
    reason: str | None = None,
    adjacency: bool = False,
    success: bool = False,
) -> HasilFarmV0:
    return HasilFarmV0(
        status=status,
        success=success,
        reason=reason,
        **{**_dari_snap(jejak), "adjacency_achieved": adjacency},
    )


def _catat_perintah(jejak: _Jejak, hasil: HasilPerintah, *, press: bool) -> None:
    jejak.terminal_request_id = hasil.request_id
    jejak.terminal_outcome = hasil.outcome
    if press:
        jejak.world_press_attempted = True
        jejak.press_ids.append(hasil.request_id)
        jejak.press_outcomes.append(hasil.outcome)


def _status_perintah_gagal(hasil: HasilPerintah, *, water: bool) -> str:
    if hasil.status == TIDAK_PASTI:
        return WATER_UNCERTAIN if water else WORLD_PRESS_UNCERTAIN
    if hasil.status in (DITOLAK, GAGAL_LOKAL, TIDAK_DIAMBIL):
        return WATER_REJECTED if water else WORLD_PRESS_REJECTED
    return WATER_FAILED if water else WORLD_PRESS_FAILED


def _press_selesai_jujur(hasil: HasilPerintah, arah: str, ticks: int) -> str | None:
    """None kalau siklus tekan sah. Bukan bukti farmer pindah."""
    if not (
        hasil.status == SELESAI
        and hasil.sukses
        and hasil.outcome == _STATUS_PRESS_SUKSES
    ):
        return _status_perintah_gagal(hasil, water=False)
    mentah = hasil.mentah or {}
    if "direction" in mentah and mentah["direction"] != arah:
        return WORLD_PRESS_FAILED
    if "press_count" in mentah and mentah["press_count"] != ticks:
        return WORLD_PRESS_FAILED
    return None


def _bukti_geser(
    lama: StardewState, baru: StardewState, arah: str
) -> str | None:
    """Pergeseran piksel independen. `PressCompleted` tidak masuk ke sini."""
    ox, oy = lama.pixel
    nx, ny = baru.pixel
    if nx == ox and ny == oy:
        return NO_DISPLACEMENT
    if arah == "left":
        return None if nx < ox else WRONG_DIRECTION
    if arah == "right":
        return None if nx > ox else WRONG_DIRECTION
    if arah == "up":
        return None if ny < oy else WRONG_DIRECTION
    if arah == "down":
        return None if ny > oy else WRONG_DIRECTION
    return WRONG_DIRECTION


def _gerbang_dunia(snap: StardewState, location: str, *, gerak: bool) -> str | None:
    if snap.location != location:
        return WRONG_LOCATION
    if snap.active_menu:
        return MENU_OPEN
    if gerak and not snap.can_player_move:
        return CANNOT_MOVE
    return None


def _sesi_berubah(awal: SesiPerintah, baru: SesiPerintah) -> str | None:
    """None kalau session_id + player_id masih pin attempt awal."""
    if baru.session_id != awal.session_id:
        return f"session_id {awal.session_id!r} -> {baru.session_id!r}"
    if baru.player_id != awal.player_id:
        return f"player_id {awal.player_id} -> {baru.player_id}"
    return None


def jalankan_farm_v0(
    permintaan: PermintaanFarmV0,
    *,
    store: StardewStateStore,
    baca_sesi: Callable[..., SesiPerintah] = _baca_sesi_asli,
    minta_world_press: Callable[..., HasilPerintah] = _minta_world_press_asli,
    minta_water_once: Callable[..., HasilPerintah] = _minta_water_once_asli,
) -> HasilFarmV0:
    """Jalankan satu percobaan Farm Loop v0. Tidak pernah retry."""
    jejak = _Jejak()

    salah_target = _validasi_target(permintaan.target)
    if salah_target is not None:
        return _hasil(ROUTE_INVALID, jejak, reason=salah_target)
    salah_rute = validasi_rute(permintaan.route)
    if salah_rute is not None:
        return _hasil(ROUTE_INVALID, jejak, reason=salah_rute)
    rute = _normalisasi_rute(permintaan.route)
    target = (int(permintaan.target[0]), int(permintaan.target[1]))
    lokasi = permintaan.location or LOKASI_BAWAAN

    try:
        sesi = baca_sesi(permintaan.folder)
    except KesalahanPerintah as exc:
        return _hasil(SESSION_UNREADABLE, jejak, reason=str(exc))

    if permintaan.player_id is not None and permintaan.player_id != sesi.player_id:
        return _hasil(
            PLAYER_MISMATCH,
            jejak,
            reason=(
                f"player_id permintaan {permintaan.player_id} "
                f"!= sesi {sesi.player_id}"
            ),
        )
    sesi_awal = sesi
    pid = sesi_awal.player_id

    if store.is_stale(pid, max_age=permintaan.max_age):
        snap = store.latest(pid)
        jejak.snap = snap
        if snap is None:
            return _hasil(MISSING_STATE, jejak, reason="belum pernah ada state")
        return _hasil(STALE_STATE, jejak, reason="state basi")

    snap = store.latest(pid)
    if snap is None:
        return _hasil(MISSING_STATE, jejak, reason="belum pernah ada state")
    jejak.snap = snap
    if snap.player_id != sesi.player_id:
        return _hasil(
            PLAYER_MISMATCH,
            jejak,
            reason=f"state player_id {snap.player_id} != sesi {sesi.player_id}",
        )

    gagal_dunia = _gerbang_dunia(snap, lokasi, gerak=bool(rute))
    if gagal_dunia is not None:
        return _hasil(gagal_dunia, jejak, reason=gagal_dunia.lower())

    for arah, ticks in rute:
        belum = sesi.alasan_belum_siap(OP_WORLD_PRESS)
        if belum is not None:
            return _hasil(INBOX_BUSY, jejak, reason=belum)
        gagal_gerak = _gerbang_dunia(snap, lokasi, gerak=True)
        if gagal_gerak is not None:
            return _hasil(gagal_gerak, jejak, reason=gagal_gerak.lower())

        sebelum = snap
        try:
            tekan = minta_world_press(
                arah, ticks, folder=permintaan.folder, sesi=sesi
            )
        except KesalahanPerintah as exc:
            return _hasil(WORLD_PRESS_REJECTED, jejak, reason=str(exc))
        _catat_perintah(jejak, tekan, press=True)

        gagal_tekan = _press_selesai_jujur(tekan, arah, ticks)
        if gagal_tekan is not None:
            return _hasil(gagal_tekan, jejak, reason=tekan.alasan or gagal_tekan)

        baru = store.wait_next(pid, sebelum.revisi, permintaan.state_timeout)
        if baru is None:
            return _hasil(
                STATE_TIMEOUT,
                jejak,
                reason="tidak ada state lebih baru sesudah PressCompleted",
            )
        jejak.snap = baru
        snap = baru

        if baru.location != lokasi:
            return _hasil(
                UNEXPECTED_LOCATION,
                jejak,
                reason=f"lokasi {baru.location!r} != {lokasi!r}",
            )
        gagal_geser = _bukti_geser(sebelum, baru, arah)
        if gagal_geser is not None:
            return _hasil(gagal_geser, jejak, reason=gagal_geser.lower())
        jejak.segments_completed += 1

        try:
            sesi = baca_sesi(permintaan.folder)
        except KesalahanPerintah as exc:
            return _hasil(SESSION_UNREADABLE, jejak, reason=str(exc))
        berubah = _sesi_berubah(sesi_awal, sesi)
        if berubah is not None:
            return _hasil(SESSION_CHANGED, jejak, reason=berubah)

    if not bersebelahan_kardinal(snap.tile, target):
        return _hasil(
            NOT_ADJACENT,
            jejak,
            reason=(
                f"ubin farmer {snap.tile} tidak bersebelahan kardinal "
                f"dengan {target}"
            ),
        )

    gagal_sebelum_air = _gerbang_dunia(snap, lokasi, gerak=False)
    if gagal_sebelum_air is not None:
        return _hasil(
            gagal_sebelum_air,
            jejak,
            reason=gagal_sebelum_air.lower(),
            adjacency=True,
        )

    try:
        sesi = baca_sesi(permintaan.folder)
    except KesalahanPerintah as exc:
        return _hasil(SESSION_UNREADABLE, jejak, reason=str(exc), adjacency=True)
    berubah = _sesi_berubah(sesi_awal, sesi)
    if berubah is not None:
        return _hasil(SESSION_CHANGED, jejak, reason=berubah, adjacency=True)

    belum_air = sesi.alasan_belum_siap(OP_WATER_ONCE)
    if belum_air is not None:
        return _hasil(INBOX_BUSY, jejak, reason=belum_air, adjacency=True)

    sebelum_air = snap
    try:
        siram = minta_water_once(
            target[0], target[1], folder=permintaan.folder, sesi=sesi
        )
    except KesalahanPerintah as exc:
        return _hasil(WATER_REJECTED, jejak, reason=str(exc), adjacency=True)
    jejak.water_outcome = siram.outcome
    jejak.terminal_request_id = siram.request_id
    jejak.terminal_outcome = siram.outcome

    if not (
        siram.status == SELESAI
        and siram.sukses
        and siram.outcome == _STATUS_WATER_SUKSES
    ):
        return _hasil(
            _status_perintah_gagal(siram, water=True),
            jejak,
            reason=siram.alasan or siram.outcome,
            adjacency=True,
        )

    # State pendukung opsional; tidak menimpa putusan ObservedWatered.
    pendukung = store.wait_next(pid, sebelum_air.revisi, permintaan.state_timeout)
    if pendukung is not None:
        jejak.snap = pendukung

    return _hasil(
        SUCCESS,
        jejak,
        adjacency=True,
        success=True,
        reason=None,
    )

"""Farm Loop v1 — temukan satu target kering bersebelahan, lalu water_once.

Tidak mengganti Farm Loop v0. v0 tetap known-target + rute operator.
v1 hanya: inspect lokal radius 1 -> pilih satu HoeDirt ditanam kering
yang Manhattan == 1 -> tepat satu water_once yang sudah ada.

Alur (gagal tertutup, tanpa retry, tanpa gerak):

1. baca state + session; pin session_id + player_id + location_id;
2. gerbang Farm / tidak basi / tanpa menu, sama seperti v0 tanpa rute;
3. tepat satu inspect_tiles di ubin farmer, radius 1;
4. hasil harus Inspected dan identitas hasil harus sama dengan pin;
5. pilih satu kandidat deterministik: (y, x, fact_id);
6. tanpa kandidat = NO_TARGET, nol motor;
7. wait_next wajib: state harus strictly lebih baru dari revisi pra-inspect;
   tanpa revisi baru = gagal tertutup, nol water;
8. pada state baru: player/location/menu, ubin farmer sama, target masih
   bersebelahan kardinal; lalu baca_sesi tepat sebelum water dan pin
   session_id + player_id + location_id harus sama;
9. tepat satu minta_water_once; sukses driver hanya ObservedWatered.

Kandidat memakai fact terrain kanonik inspect_tiles:

- kind terrain, terrain_type HoeDirt;
- crop.watered is False;
- ditanam: phase int, atau dead bool, atau qualified_item_id non-kosong.
  HoeDirt tanpa tanaman (phase/dead/id kosong) tidak dipilih.

CaptureTerrain selalu menulis blok crop untuk HoeDirt, jadi kehadiran
tanaman bisa dibedakan dari DTO kanonik. Tidak mengarang aturan baru.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from arti_stardew import StardewState, StardewStateStore
from arti_stardew_command import (
    DITOLAK,
    GAGAL_LOKAL,
    KesalahanPerintah,
    OP_INSPECT_TILES,
    OP_WATER_ONCE,
    SELESAI,
    SesiPerintah,
    HasilPerintah,
    TERPUTUS,
    TIDAK_DIAMBIL,
    TIDAK_PASTI,
    baca_sesi as _baca_sesi_asli,
    minta_inspect_tiles as _minta_inspect_tiles_asli,
    minta_water_once as _minta_water_once_asli,
)
from arti_stardew_farm_v0 import (
    INBOX_BUSY,
    LOKASI_BAWAAN,
    MENU_OPEN,
    MISSING_STATE,
    PLAYER_MISMATCH,
    SESSION_CHANGED,
    SESSION_UNREADABLE,
    STATE_TIMEOUT,
    STALE_STATE,
    SUCCESS,
    TIMEOUT_STATE_BAWAAN,
    UMUR_STATE_BAWAAN,
    WATER_FAILED,
    WATER_REJECTED,
    WATER_UNCERTAIN,
    WRONG_LOCATION,
    bersebelahan_kardinal,
)

RADIUS_INSPEKSI = 1
NO_TARGET = "NO_TARGET"
INSPECT_REJECTED = "INSPECT_REJECTED"
INSPECT_UNCERTAIN = "INSPECT_UNCERTAIN"
INSPECT_FAILED = "INSPECT_FAILED"
INSPECT_IDENTITY_MISMATCH = "INSPECT_IDENTITY_MISMATCH"
FARMER_MOVED = "FARMER_MOVED"

_STATUS_WATER_SUKSES = "ObservedWatered"
_STATUS_INSPECT_SUKSES = "Inspected"
_KIND_TERRAIN = "terrain"
_HOE_DIRT = "HoeDirt"


@dataclass(frozen=True)
class PermintaanFarmV1:
    """v1 tidak menerima target atau rute; farmer harus sudah bersebelahan."""

    location: str = LOKASI_BAWAAN
    folder: str | None = None
    player_id: int | None = None
    max_age: float = UMUR_STATE_BAWAAN
    state_timeout: float = TIMEOUT_STATE_BAWAAN


@dataclass(frozen=True)
class HasilFarmV1:
    """Satu putusan. success hanya True untuk native ObservedWatered."""

    status: str
    success: bool
    selected_target: tuple[int, int] | None
    inspect_attempted: bool
    inspect_request_id: str | None = None
    inspect_outcome: str | None = None
    water_outcome: str | None = None
    terminal_request_id: str | None = None
    terminal_outcome: str | None = None
    revision: int | None = None
    tile: tuple[int, int] | None = None
    pixel: tuple[float, float] | None = None
    reason: str | None = None


@dataclass
class _Jejak:
    inspect_attempted: bool = False
    inspect_request_id: str | None = None
    inspect_outcome: str | None = None
    selected_target: tuple[int, int] | None = None
    water_outcome: str | None = None
    terminal_request_id: str | None = None
    terminal_outcome: str | None = None
    snap: StardewState | None = None


def _crop_ditanam(crop: Mapping[str, Any]) -> bool:
    """True kalau DTO kanonik membedakan tanaman, bukan HoeDirt kosong."""
    phase = crop.get("phase")
    dead = crop.get("dead")
    qid = crop.get("qualified_item_id")
    if isinstance(phase, int) and not isinstance(phase, bool):
        return True
    if isinstance(dead, bool):
        return True
    return isinstance(qid, str) and bool(qid.strip())


def _ubin_dto(dto: Mapping[str, Any]) -> tuple[int, int] | None:
    tile = dto.get("tile")
    if not isinstance(tile, dict):
        return None
    x, y = tile.get("x"), tile.get("y")
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, int) or not isinstance(y, int):
        return None
    return (x, y)


def _hoe_dirt_kering_ditanam(dto: object) -> bool:
    if not isinstance(dto, dict):
        return False
    if dto.get("kind") != _KIND_TERRAIN:
        return False
    if dto.get("terrain_type") != _HOE_DIRT:
        return False
    crop = dto.get("crop")
    if not isinstance(crop, dict):
        return False
    if crop.get("watered") is not False:
        return False
    return _crop_ditanam(crop)


def pilih_target_kering_bersebelahan(
    facts: Mapping[str, Any] | None,
    farmer: tuple[int, int],
) -> tuple[int, int] | None:
    """Pilih satu tetangga kardinal. Urutan tetap: y, lalu x, lalu fact_id."""
    if not isinstance(facts, dict):
        return None
    kandidat: list[tuple[int, int, str, tuple[int, int]]] = []
    for fact_id, dto in facts.items():
        if not isinstance(fact_id, str) or not _hoe_dirt_kering_ditanam(dto):
            continue
        ubin = _ubin_dto(dto)
        if ubin is None:
            continue
        if not bersebelahan_kardinal(farmer, ubin):
            continue
        kandidat.append((ubin[1], ubin[0], fact_id, ubin))
    if not kandidat:
        return None
    kandidat.sort()
    return kandidat[0][3]


def _hasil(
    status: str,
    jejak: _Jejak,
    *,
    reason: str | None = None,
    success: bool = False,
) -> HasilFarmV1:
    snap = jejak.snap
    return HasilFarmV1(
        status=status,
        success=success,
        selected_target=jejak.selected_target,
        inspect_attempted=jejak.inspect_attempted,
        inspect_request_id=jejak.inspect_request_id,
        inspect_outcome=jejak.inspect_outcome,
        water_outcome=jejak.water_outcome,
        terminal_request_id=jejak.terminal_request_id,
        terminal_outcome=jejak.terminal_outcome,
        revision=None if snap is None else snap.revisi,
        tile=None if snap is None else snap.tile,
        pixel=None if snap is None else snap.pixel,
        reason=reason,
    )


def _gerbang_dunia(snap: StardewState, location: str) -> str | None:
    if snap.location != location:
        return WRONG_LOCATION
    if snap.active_menu:
        return MENU_OPEN
    return None


def _pin_berubah(
    sesi_awal: SesiPerintah,
    sesi: SesiPerintah,
    lokasi: str,
) -> str | None:
    if sesi.session_id != sesi_awal.session_id:
        return f"session_id {sesi_awal.session_id!r} -> {sesi.session_id!r}"
    if sesi.player_id != sesi_awal.player_id:
        return f"player_id {sesi_awal.player_id} -> {sesi.player_id}"
    if sesi.location_id != lokasi:
        return f"location_id {lokasi!r} -> {sesi.location_id!r}"
    return None


def _identitas_inspect_cocok(
    mentah: Mapping[str, Any],
    sesi: SesiPerintah,
    lokasi: str,
    farmer: tuple[int, int],
) -> bool:
    return (
        mentah.get("session_id") == sesi.session_id
        and mentah.get("player_id") == sesi.player_id
        and mentah.get("location_id") == lokasi
        and mentah.get("center_x") == farmer[0]
        and mentah.get("center_y") == farmer[1]
        and mentah.get("radius") == RADIUS_INSPEKSI
    )


def _status_inspect_gagal(hasil: HasilPerintah) -> str:
    if hasil.status == TIDAK_PASTI:
        return INSPECT_UNCERTAIN
    if hasil.status in (DITOLAK, GAGAL_LOKAL, TIDAK_DIAMBIL, TERPUTUS):
        return INSPECT_REJECTED
    if hasil.alasan and "identitas" in hasil.alasan:
        return INSPECT_IDENTITY_MISMATCH
    return INSPECT_FAILED


def _status_water_gagal(hasil: HasilPerintah) -> str:
    if hasil.status == TIDAK_PASTI:
        return WATER_UNCERTAIN
    if hasil.status in (DITOLAK, GAGAL_LOKAL, TIDAK_DIAMBIL, TERPUTUS):
        return WATER_REJECTED
    return WATER_FAILED


def jalankan_farm_v1(
    permintaan: PermintaanFarmV1,
    *,
    store: StardewStateStore,
    baca_sesi: Callable[..., SesiPerintah] = _baca_sesi_asli,
    minta_inspect_tiles: Callable[..., HasilPerintah] = _minta_inspect_tiles_asli,
    minta_water_once: Callable[..., HasilPerintah] = _minta_water_once_asli,
) -> HasilFarmV1:
    """Satu percobaan temukan-lalu-siram. Tidak pernah retry, tidak pernah gerak."""
    jejak = _Jejak()
    lokasi = permintaan.location or LOKASI_BAWAAN

    try:
        sesi = baca_sesi(permintaan.folder)
    except KesalahanPerintah as exc:
        return _hasil(SESSION_UNREADABLE, jejak, reason=str(exc))

    if not sesi.location_id:
        return _hasil(
            SESSION_UNREADABLE,
            jejak,
            reason="session.json tanpa location_id",
        )
    if permintaan.player_id is not None and permintaan.player_id != sesi.player_id:
        return _hasil(
            PLAYER_MISMATCH,
            jejak,
            reason=(
                f"player_id permintaan {permintaan.player_id} "
                f"!= sesi {sesi.player_id}"
            ),
        )
    if sesi.location_id != lokasi:
        return _hasil(
            WRONG_LOCATION,
            jejak,
            reason=f"sesi location_id {sesi.location_id!r} != {lokasi!r}",
        )

    sesi_awal = sesi
    pid = sesi_awal.player_id
    lokasi_pin = sesi_awal.location_id

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

    gagal_dunia = _gerbang_dunia(snap, lokasi)
    if gagal_dunia is not None:
        return _hasil(gagal_dunia, jejak, reason=gagal_dunia.lower())

    belum = sesi.alasan_belum_siap(OP_INSPECT_TILES)
    if belum is not None:
        return _hasil(INBOX_BUSY, jejak, reason=belum)

    farmer = snap.tile
    try:
        lihat = minta_inspect_tiles(
            farmer[0],
            farmer[1],
            RADIUS_INSPEKSI,
            expected_location_id=lokasi_pin,
            folder=permintaan.folder,
            sesi=sesi,
        )
    except KesalahanPerintah as exc:
        return _hasil(INSPECT_REJECTED, jejak, reason=str(exc))

    jejak.inspect_attempted = True
    jejak.inspect_request_id = lihat.request_id
    jejak.inspect_outcome = lihat.outcome
    jejak.terminal_request_id = lihat.request_id
    jejak.terminal_outcome = lihat.outcome
    mentah = lihat.mentah if isinstance(lihat.mentah, dict) else None

    inspect_ok = (
        lihat.status == SELESAI
        and lihat.sukses
        and lihat.outcome == _STATUS_INSPECT_SUKSES
        and mentah is not None
        and _identitas_inspect_cocok(mentah, sesi_awal, lokasi_pin, farmer)
    )
    if not inspect_ok:
        if mentah is not None and not _identitas_inspect_cocok(
            mentah, sesi_awal, lokasi_pin, farmer
        ):
            status = INSPECT_IDENTITY_MISMATCH
        else:
            status = _status_inspect_gagal(lihat)
        return _hasil(status, jejak, reason=lihat.alasan or status)

    facts = mentah.get("facts")
    if not isinstance(facts, dict):
        return _hasil(
            INSPECT_FAILED,
            jejak,
            reason="facts inspect bukan objek",
        )

    target = pilih_target_kering_bersebelahan(facts, farmer)
    if target is None:
        return _hasil(
            NO_TARGET,
            jejak,
            reason="tidak ada HoeDirt ditanam kering bersebelahan kardinal",
        )
    jejak.selected_target = target
    pre_revisi = snap.revisi

    baru = store.wait_next(pid, pre_revisi, permintaan.state_timeout)
    if baru is None:
        return _hasil(
            STATE_TIMEOUT,
            jejak,
            reason="tidak ada state lebih baru sesudah inspect_tiles",
        )
    jejak.snap = baru
    if baru.player_id != pid:
        return _hasil(
            PLAYER_MISMATCH,
            jejak,
            reason=f"state player_id {baru.player_id} != sesi {pid}",
        )
    gagal_lagi = _gerbang_dunia(baru, lokasi)
    if gagal_lagi is not None:
        return _hasil(gagal_lagi, jejak, reason=gagal_lagi.lower())
    if baru.tile != farmer:
        return _hasil(
            FARMER_MOVED,
            jejak,
            reason=f"ubin farmer {farmer} -> {baru.tile}",
        )
    if not bersebelahan_kardinal(baru.tile, target):
        return _hasil(
            FARMER_MOVED,
            jejak,
            reason=(
                f"ubin farmer {baru.tile} tidak bersebelahan "
                f"dengan {target}"
            ),
        )

    try:
        sesi = baca_sesi(permintaan.folder)
    except KesalahanPerintah as exc:
        return _hasil(SESSION_UNREADABLE, jejak, reason=str(exc))
    berubah = _pin_berubah(sesi_awal, sesi, lokasi_pin)
    if berubah is not None:
        return _hasil(SESSION_CHANGED, jejak, reason=berubah)

    belum_air = sesi.alasan_belum_siap(OP_WATER_ONCE)
    if belum_air is not None:
        return _hasil(INBOX_BUSY, jejak, reason=belum_air)

    try:
        siram = minta_water_once(
            target[0], target[1], folder=permintaan.folder, sesi=sesi
        )
    except KesalahanPerintah as exc:
        return _hasil(WATER_REJECTED, jejak, reason=str(exc))
    jejak.water_outcome = siram.outcome
    jejak.terminal_request_id = siram.request_id
    jejak.terminal_outcome = siram.outcome

    if not (
        siram.status == SELESAI
        and siram.sukses
        and siram.outcome == _STATUS_WATER_SUKSES
    ):
        return _hasil(
            _status_water_gagal(siram),
            jejak,
            reason=siram.alasan or siram.outcome,
        )

    return _hasil(SUCCESS, jejak, success=True)

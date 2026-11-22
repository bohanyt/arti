"""Farm Loop batch — siram sampai empat tetangga kardinal, tanpa gerak.

Tidak mengganti Farm Loop v0/v1. Batch ini komposisi tipis di atas
`jalankan_farm_v1`: setiap iterasi minta inspect segar dan paling banyak
satu `water_once`. Tidak ada gerak dunia, pencarian rute, isi ulang kaleng,
atau retry.

Alur (gagal tertutup, petani tetap di ubin pin):

1. validasi max_actions 1..4 dan deadline hingga <= 30 detik;
2. baca sesi + state; pin session_id + player_id + location_id + ubin
   farmer sebelum iterasi pertama;
3. tiap iterasi: periksa batal/deadline, pin, state tidak basi, ubin sama;
4. satu `jalankan_farm_v1` lewat wrapper admisi yang memegang pin asli
   (bukan pin per-panggilan v1) plus himpunan <= 4 target yang sudah
   dicoba; facts inspect tidak ditulis ulang;
5. sukses native hanya ObservedWatered; NO_TARGET menghentikan batch
   tanpa water tambahan; LIMIT_REACHED bukan klaim seluruh Farm basah;
6. gagal/uncertainty/pin pecah/petani pindah/duplikat = STOP, tanpa
   fallback ke target lain.

Pemanggil yang memasang receiver:

    store = StardewStateStore()
    receiver = StardewReceiver(store.catat)
    receiver.start()
    hasil = jalankan_farm_batch(PermintaanFarmBatch(), store=store)

Import modul ini tidak melakukan IO atau aksi motor.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from arti_stardew import StardewStateStore
from arti_stardew_command import (
    KesalahanPerintah,
    SesiPerintah,
    HasilPerintah,
    baca_sesi as _baca_sesi_asli,
    minta_inspect_tiles as _minta_inspect_tiles_asli,
    minta_water_once as _minta_water_once_asli,
)
from arti_stardew_farm_v0 import (
    LOKASI_BAWAAN,
    MENU_OPEN,
    MISSING_STATE,
    PLAYER_MISMATCH,
    SESSION_CHANGED,
    SESSION_UNREADABLE,
    STALE_STATE,
    SUCCESS,
    TIMEOUT_STATE_BAWAAN,
    UMUR_STATE_BAWAAN,
    WRONG_LOCATION,
)
from arti_stardew_farm_v1 import (
    FARMER_MOVED,
    NO_TARGET,
    PermintaanFarmV1,
    HasilFarmV1,
    jalankan_farm_v1,
)

AKSI_MAKS = 4
DEADLINE_BAWAAN = 15.0
DEADLINE_MAKS = 30.0
LIMIT_REACHED = "LIMIT_REACHED"
CANCELLED = "CANCELLED"
DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
DUPLICATE_TARGET = "DUPLICATE_TARGET"
INVALID_REQUEST = "INVALID_REQUEST"


@dataclass(frozen=True)
class PermintaanFarmBatch:
    """Batch diam. `max_actions` integer 1..4, bukan bool."""

    location: str = LOKASI_BAWAAN
    folder: str | None = None
    player_id: int | None = None
    max_age: float = UMUR_STATE_BAWAAN
    state_timeout: float = TIMEOUT_STATE_BAWAAN
    max_actions: int = AKSI_MAKS
    deadline_seconds: float = DEADLINE_BAWAAN
    cek_batal: Callable[[], bool] | None = None


@dataclass(frozen=True)
class HasilIterasiBatch:
    """Satu iterasi v1, atau gerbang batch sebelum water berikutnya."""

    status: str
    success: bool
    selected_target: tuple[int, int] | None
    inspect_attempted: bool
    inspect_request_id: str | None = None
    inspect_outcome: str | None = None
    water_outcome: str | None = None
    terminal_request_id: str | None = None
    terminal_outcome: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class HasilFarmBatch:
    """Putusan batch. `success` hanya jika selesai normal dengan >=1 crop."""

    status: str
    success: bool
    completed_targets: tuple[tuple[int, int], ...]
    iterations: tuple[HasilIterasiBatch, ...]
    inspect_count: int
    water_count: int
    inspect_request_ids: tuple[str, ...]
    water_request_ids: tuple[str, ...]
    pin_session_id: str | None = None
    pin_player_id: int | None = None
    pin_location_id: str | None = None
    pin_tile: tuple[int, int] | None = None
    reason: str | None = None


@dataclass
class _PinBatch:
    session_id: str
    player_id: int
    location_id: str
    tile: tuple[int, int]


@dataclass
class _Konteks:
    pin: _PinBatch
    attempted: set[tuple[int, int]] = field(default_factory=set)
    inspect_ids: list[str] = field(default_factory=list)
    water_ids: list[str] = field(default_factory=list)
    last_target: tuple[int, int] | None = None
    last_inspect_id: str | None = None
    last_inspect_outcome: str | None = None


class _BerhentiMasuk(Exception):
    """Gerbang admisi batch: jangan kirim perintah motor berikutnya."""

    def __init__(
        self,
        status: str,
        reason: str,
        *,
        target: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.target = target


def _int_polos(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _angka_hingga(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _validasi(permintaan: PermintaanFarmBatch) -> str | None:
    n = permintaan.max_actions
    if not _int_polos(n) or not 1 <= n <= AKSI_MAKS:
        return f"max_actions harus integer 1..{AKSI_MAKS}, dapat {n!r}"
    d = permintaan.deadline_seconds
    if not _angka_hingga(d) or not 0 < float(d) <= DEADLINE_MAKS:
        return (
            f"deadline_seconds harus angka hingga 0 < x <= {DEADLINE_MAKS}, "
            f"dapat {d!r}"
        )
    if not _angka_hingga(permintaan.max_age) or float(permintaan.max_age) < 0:
        return f"max_age harus angka hingga >= 0, dapat {permintaan.max_age!r}"
    if (
        not _angka_hingga(permintaan.state_timeout)
        or float(permintaan.state_timeout) < 0
    ):
        return (
            f"state_timeout harus angka hingga >= 0, "
            f"dapat {permintaan.state_timeout!r}"
        )
    if permintaan.cek_batal is not None and not callable(permintaan.cek_batal):
        return "cek_batal harus callable atau None"
    return None


def _iterasi_dari_v1(hasil: HasilFarmV1) -> HasilIterasiBatch:
    return HasilIterasiBatch(
        status=hasil.status,
        success=hasil.success,
        selected_target=hasil.selected_target,
        inspect_attempted=hasil.inspect_attempted,
        inspect_request_id=hasil.inspect_request_id,
        inspect_outcome=hasil.inspect_outcome,
        water_outcome=hasil.water_outcome,
        terminal_request_id=hasil.terminal_request_id,
        terminal_outcome=hasil.terminal_outcome,
        reason=hasil.reason,
    )


def _hasil(
    status: str,
    *,
    completed: list[tuple[int, int]],
    iterations: list[HasilIterasiBatch],
    ctx: _Konteks | None,
    reason: str | None = None,
    pin: _PinBatch | None = None,
) -> HasilFarmBatch:
    pin = pin if pin is not None else (None if ctx is None else ctx.pin)
    sukses_normal = status in (LIMIT_REACHED, NO_TARGET) and bool(completed)
    return HasilFarmBatch(
        status=status,
        success=sukses_normal,
        completed_targets=tuple(completed),
        iterations=tuple(iterations),
        inspect_count=0 if ctx is None else len(ctx.inspect_ids),
        water_count=0 if ctx is None else len(ctx.water_ids),
        inspect_request_ids=() if ctx is None else tuple(ctx.inspect_ids),
        water_request_ids=() if ctx is None else tuple(ctx.water_ids),
        pin_session_id=None if pin is None else pin.session_id,
        pin_player_id=None if pin is None else pin.player_id,
        pin_location_id=None if pin is None else pin.location_id,
        pin_tile=None if pin is None else pin.tile,
        reason=reason,
    )


def _pin_sesi_pecah(pin: _PinBatch, sesi: SesiPerintah) -> str | None:
    if sesi.session_id != pin.session_id:
        return f"session_id {pin.session_id!r} -> {sesi.session_id!r}"
    if sesi.player_id != pin.player_id:
        return f"player_id {pin.player_id} -> {sesi.player_id}"
    if sesi.location_id != pin.location_id:
        return f"location_id {pin.location_id!r} -> {sesi.location_id!r}"
    return None


def jalankan_farm_batch(
    permintaan: PermintaanFarmBatch,
    *,
    store: StardewStateStore,
    baca_sesi: Callable[..., SesiPerintah] = _baca_sesi_asli,
    minta_inspect_tiles: Callable[..., HasilPerintah] = _minta_inspect_tiles_asli,
    minta_water_once: Callable[..., HasilPerintah] = _minta_water_once_asli,
    jam: Callable[[], float] = time.monotonic,
) -> HasilFarmBatch:
    """Siram sampai `max_actions` tetangga. Tidak pernah gerak atau retry."""
    salah = _validasi(permintaan)
    if salah is not None:
        return _hasil(
            INVALID_REQUEST,
            completed=[],
            iterations=[],
            ctx=None,
            reason=salah,
        )

    lokasi = permintaan.location or LOKASI_BAWAAN
    completed: list[tuple[int, int]] = []
    iterations: list[HasilIterasiBatch] = []

    try:
        sesi = baca_sesi(permintaan.folder)
    except KesalahanPerintah as exc:
        return _hasil(
            SESSION_UNREADABLE,
            completed=[],
            iterations=[],
            ctx=None,
            reason=str(exc),
        )
    if not sesi.location_id:
        return _hasil(
            SESSION_UNREADABLE,
            completed=[],
            iterations=[],
            ctx=None,
            reason="session.json tanpa location_id",
        )
    if permintaan.player_id is not None and permintaan.player_id != sesi.player_id:
        return _hasil(
            PLAYER_MISMATCH,
            completed=[],
            iterations=[],
            ctx=None,
            reason=(
                f"player_id permintaan {permintaan.player_id} "
                f"!= sesi {sesi.player_id}"
            ),
        )
    if sesi.location_id != lokasi:
        return _hasil(
            WRONG_LOCATION,
            completed=[],
            iterations=[],
            ctx=None,
            reason=f"sesi location_id {sesi.location_id!r} != {lokasi!r}",
        )

    pid = sesi.player_id
    if store.is_stale(pid, max_age=permintaan.max_age):
        snap = store.latest(pid)
        if snap is None:
            return _hasil(
                MISSING_STATE,
                completed=[],
                iterations=[],
                ctx=None,
                reason="belum pernah ada state",
            )
        return _hasil(
            STALE_STATE,
            completed=[],
            iterations=[],
            ctx=None,
            reason="state basi",
            pin=_PinBatch(
                session_id=sesi.session_id,
                player_id=pid,
                location_id=sesi.location_id,
                tile=snap.tile,
            ),
        )
    snap = store.latest(pid)
    if snap is None:
        return _hasil(
            MISSING_STATE,
            completed=[],
            iterations=[],
            ctx=None,
            reason="belum pernah ada state",
        )
    if snap.player_id != sesi.player_id:
        return _hasil(
            PLAYER_MISMATCH,
            completed=[],
            iterations=[],
            ctx=None,
            reason=f"state player_id {snap.player_id} != sesi {sesi.player_id}",
        )
    if snap.location != lokasi:
        return _hasil(
            WRONG_LOCATION,
            completed=[],
            iterations=[],
            ctx=None,
            reason=WRONG_LOCATION.lower(),
            pin=_PinBatch(
                session_id=sesi.session_id,
                player_id=pid,
                location_id=sesi.location_id,
                tile=snap.tile,
            ),
        )
    if snap.active_menu:
        return _hasil(
            MENU_OPEN,
            completed=[],
            iterations=[],
            ctx=None,
            reason=MENU_OPEN.lower(),
            pin=_PinBatch(
                session_id=sesi.session_id,
                player_id=pid,
                location_id=sesi.location_id,
                tile=snap.tile,
            ),
        )

    pin = _PinBatch(
        session_id=sesi.session_id,
        player_id=pid,
        location_id=sesi.location_id,
        tile=snap.tile,
    )
    ctx = _Konteks(pin=pin)
    mulai = jam()
    if not _angka_hingga(mulai):
        return _hasil(
            INVALID_REQUEST,
            completed=[],
            iterations=[],
            ctx=ctx,
            reason="jam admisi tidak hingga",
            pin=pin,
        )
    batas = float(mulai) + float(permintaan.deadline_seconds)

    def _sekarang() -> float:
        t = jam()
        if not _angka_hingga(t):
            raise _BerhentiMasuk(DEADLINE_EXCEEDED, "jam admisi tidak hingga")
        return float(t)

    def _gerbang_admisi() -> None:
        if permintaan.cek_batal is not None and permintaan.cek_batal():
            raise _BerhentiMasuk(CANCELLED, "pemanggil membatalkan batch")
        if _sekarang() >= batas:
            raise _BerhentiMasuk(
                DEADLINE_EXCEEDED,
                "deadline admisi batch habis",
            )

    def _gerbang_pin_dan_ubin(*, folder=None, sesi_arg: SesiPerintah | None = None) -> None:
        try:
            sesi_now = baca_sesi(folder)
        except KesalahanPerintah as exc:
            raise _BerhentiMasuk(SESSION_UNREADABLE, str(exc)) from exc
        pecah = _pin_sesi_pecah(pin, sesi_now)
        if pecah is not None:
            raise _BerhentiMasuk(SESSION_CHANGED, pecah)
        if sesi_arg is not None:
            pecah_arg = _pin_sesi_pecah(pin, sesi_arg)
            if pecah_arg is not None:
                raise _BerhentiMasuk(SESSION_CHANGED, pecah_arg)
        if store.is_stale(pin.player_id, max_age=permintaan.max_age):
            latest = store.latest(pin.player_id)
            if latest is None:
                raise _BerhentiMasuk(MISSING_STATE, "belum pernah ada state")
            raise _BerhentiMasuk(STALE_STATE, "state basi")
        latest = store.latest(pin.player_id)
        if latest is None:
            raise _BerhentiMasuk(MISSING_STATE, "belum pernah ada state")
        if latest.player_id != pin.player_id:
            raise _BerhentiMasuk(
                PLAYER_MISMATCH,
                f"state player_id {latest.player_id} != pin {pin.player_id}",
            )
        if latest.location != pin.location_id:
            raise _BerhentiMasuk(
                WRONG_LOCATION,
                f"lokasi {latest.location!r} != pin {pin.location_id!r}",
            )
        if latest.tile != pin.tile:
            raise _BerhentiMasuk(
                FARMER_MOVED,
                f"ubin farmer {pin.tile} -> {latest.tile}",
            )

    def inspect_batch(center_x, center_y, radius, **kw) -> HasilPerintah:
        _gerbang_admisi()
        _gerbang_pin_dan_ubin(
            folder=kw.get("folder", permintaan.folder),
            sesi_arg=kw.get("sesi"),
        )
        if (center_x, center_y) != pin.tile:
            raise _BerhentiMasuk(
                FARMER_MOVED,
                f"inspect di {(center_x, center_y)} != pin {pin.tile}",
            )
        # Setelah baca sesi/state: cek ulang batal/deadline, lalu perintah.
        _gerbang_admisi()
        hasil = minta_inspect_tiles(center_x, center_y, radius, **kw)
        ctx.inspect_ids.append(hasil.request_id)
        ctx.last_inspect_id = hasil.request_id
        ctx.last_inspect_outcome = hasil.outcome
        return hasil

    def water_batch(tile_x, tile_y, **kw) -> HasilPerintah:
        target = (tile_x, tile_y)
        ctx.last_target = target
        _gerbang_admisi()
        _gerbang_pin_dan_ubin(
            folder=kw.get("folder", permintaan.folder),
            sesi_arg=kw.get("sesi"),
        )
        if target in ctx.attempted:
            raise _BerhentiMasuk(
                DUPLICATE_TARGET,
                f"target {target} sudah dicoba di batch ini",
                target=target,
            )
        # Setelah baca sesi/state/duplikat: cek ulang, lalu perintah.
        _gerbang_admisi()
        ctx.attempted.add(target)
        hasil = minta_water_once(tile_x, tile_y, **kw)
        ctx.water_ids.append(hasil.request_id)
        return hasil

    v1 = PermintaanFarmV1(
        location=lokasi,
        folder=permintaan.folder,
        player_id=permintaan.player_id,
        max_age=permintaan.max_age,
        state_timeout=permintaan.state_timeout,
    )

    for _ in range(permintaan.max_actions):
        n_inspect = len(ctx.inspect_ids)
        ctx.last_target = None
        ctx.last_inspect_id = None
        ctx.last_inspect_outcome = None
        try:
            _gerbang_admisi()
            _gerbang_pin_dan_ubin(folder=permintaan.folder)
            hasil_v1 = jalankan_farm_v1(
                v1,
                store=store,
                baca_sesi=baca_sesi,
                minta_inspect_tiles=inspect_batch,
                minta_water_once=water_batch,
            )
        except _BerhentiMasuk as exc:
            inspect_baru = len(ctx.inspect_ids) > n_inspect
            target = (
                exc.target if exc.target is not None else ctx.last_target
            )
            if inspect_baru or target is not None:
                inspect_id = ctx.last_inspect_id
                inspect_outcome = ctx.last_inspect_outcome
                iterations.append(
                    HasilIterasiBatch(
                        status=exc.status,
                        success=False,
                        selected_target=target,
                        inspect_attempted=inspect_baru,
                        inspect_request_id=inspect_id,
                        inspect_outcome=inspect_outcome,
                        water_outcome=None,
                        terminal_request_id=inspect_id,
                        terminal_outcome=inspect_outcome,
                        reason=exc.reason,
                    )
                )
            return _hasil(
                exc.status,
                completed=completed,
                iterations=iterations,
                ctx=ctx,
                reason=exc.reason,
                pin=pin,
            )

        iterations.append(_iterasi_dari_v1(hasil_v1))
        if hasil_v1.success and hasil_v1.selected_target is not None:
            completed.append(hasil_v1.selected_target)
            continue
        return _hasil(
            hasil_v1.status,
            completed=completed,
            iterations=iterations,
            ctx=ctx,
            reason=hasil_v1.reason,
            pin=pin,
        )

    return _hasil(
        LIMIT_REACHED,
        completed=completed,
        iterations=iterations,
        ctx=ctx,
        reason="batas aksi batch tercapai",
        pin=pin,
    )

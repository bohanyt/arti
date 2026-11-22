"""Mata Stardew read-only: receiver TCP localhost + kontrak NDJSON.

Mod SMAPI adalah pihak yang menghubungi listener ini. Modul ini sengaja belum
punya brain, motor, TTS, atau knowledge logger; Gate 3 hanya membuktikan jalur
hello yang tahan malformed, duplicate/stale, disconnect, dan reconnect.
"""

from __future__ import annotations

from arti_runtime import wait_ledger as W

import argparse
import json
import math
import re
import socketserver
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from arti_stardew_observation import observation_payload_sah

PROTOCOL_VERSION = 1
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 3017
MAX_LINE_BYTES = 64 * 1024
_DOMAIN_HEAD = re.compile(rb'"domain"\s*:\s*"([a-z_.]{1,48})"')


def _plain_int(value: object) -> bool:
    """bool adalah subclass int di Python, tetapi bukan ID/sequence yang sah."""
    return isinstance(value, int) and not isinstance(value, bool)


def decode_event(line: str | bytes) -> dict | None:
    """Decode satu envelope NDJSON; input tak tepercaya selalu fail closed."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            return None
        try:
            line = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    if not isinstance(line, str) or not line.strip():
        return None
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    # OBS-0: protocol adalah int 1. Jangan pakai `== 1` polos — True dan
    # 1.0 lolos di Python (True == 1, 1.0 == 1) dan itu bukan int JSON.
    protocol = event.get("protocol")
    if not _plain_int(protocol) or protocol != PROTOCOL_VERSION:
        return None
    if not isinstance(event.get("ev"), str) or not event["ev"].strip():
        return None
    if not _plain_int(event.get("seq")) or event["seq"] < 0:
        return None
    if not _plain_int(event.get("player_id")):
        return None
    if not _plain_int(event.get("screen_id")) or event["screen_id"] < 0:
        return None
    if "payload" in event and not isinstance(event["payload"], dict):
        return None

    if event["ev"] == "state":
        if not _payload_state_sah(event.get("payload")):
            return None
    if event["ev"] == "observation":
        if not observation_payload_sah(event.get("payload")):
            return None
    if event["ev"] == "hello":
        if not isinstance(event.get("player_name"), str) or not event["player_name"].strip():
            return None
        if not isinstance(event.get("is_main_player"), bool):
            return None
        if not isinstance(event.get("is_split_screen"), bool):
            return None
    return event


def _angka(value: object) -> bool:
    """int ATAU float berhingga, tetapi bukan bool.

    NaN dan inf ditolak. Ditemukan lewat fuzz 29 Agu 2026: keduanya lolos
    validator lama. Motor sendiri tidak menggerbangi apa pun dengan energy
    atau pixel, jadi hari ini tidak ada salah-langkah — tapi `energy` masuk
    ke `HasilAksi.delta_energi`, dan angka itu yang nanti dibaca lapis otak
    untuk diceritakan ke penonton. NaN yang lolos ke situ = Arti melaporkan
    sesuatu yang bukan angka, dan setiap perbandingan dengannya diam-diam
    False (aturan #3).

    Serializer C# saat ini tidak bisa mengirim NaN (System.Text.Json
    melemparnya kecuali sengaja diizinkan), jadi ini pagar terhadap masa
    depan dan terhadap pengirim yang rusak, bukan terhadap bug yang ada.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return math.isfinite(value)


def _titik_int(obj: object) -> bool:
    return (
        isinstance(obj, dict)
        and _plain_int(obj.get("x"))
        and _plain_int(obj.get("y"))
    )


def _titik_angka(obj: object) -> bool:
    return (
        isinstance(obj, dict)
        and _angka(obj.get("x"))
        and _angka(obj.get("y"))
    )


def _bounds_sah(obj: object) -> bool:
    if obj is None:
        return True
    return (
        isinstance(obj, dict)
        and _plain_int(obj.get("x"))
        and _plain_int(obj.get("y"))
        and _plain_int(obj.get("w"))
        and _plain_int(obj.get("h"))
        and obj["w"] > 0
        and obj["h"] > 0
    )


def _menu_sah(menu: object, active_menu: str | None) -> bool:
    """Blok menu opsional untuk DLL lama; bila ada wajib utuh dan konsisten."""
    if menu is None:
        return True
    if not isinstance(menu, dict):
        return False
    nama = menu.get("name")
    if not isinstance(nama, str) or not nama.strip() or nama != active_menu:
        return False
    menu_type = menu.get("menu_type")
    if menu_type is not None and (
        not isinstance(menu_type, str) or not menu_type.strip() or menu_type != nama
    ):
        return False
    page_type = menu.get("page_type")
    if page_type is not None and (
        not isinstance(page_type, str) or not page_type.strip()
    ):
        return False
    focus_key = menu.get("focus_key")
    if focus_key is not None and (
        not isinstance(focus_key, str) or not focus_key.strip()
    ):
        return False
    for kunci in ("visible_text", "speaker"):
        nilai = menu.get(kunci)
        if nilai is not None and not isinstance(nilai, str):
            return False
    for kunci in ("snap_id", "close_id"):
        nilai = menu.get(kunci)
        if nilai is not None and not _plain_int(nilai):
            return False
    label = menu.get("snap_label")
    if label is not None and not isinstance(label, str):
        return False
    if not _bounds_sah(menu.get("snap_bounds")):
        return False
    tetangga = menu.get("neighbors")
    if tetangga is not None:
        if not isinstance(tetangga, dict):
            return False
        for arah in ("up", "right", "down", "left"):
            nilai = tetangga.get(arah)
            if nilai is not None and not _plain_int(nilai):
                return False
    components = menu.get("components")
    if components is not None:
        if not isinstance(components, list) or len(components) > 512:
            return False
        for component in components:
            if not _komponen_ui_sah(component):
                return False
    return True


def _komponen_ui_sah(component: object) -> bool:
    if not isinstance(component, dict):
        return False
    for kunci in ("key", "source", "role"):
        nilai = component.get(kunci)
        if not isinstance(nilai, str) or not nilai.strip():
            return False
    if not _plain_int(component.get("native_id")):
        return False
    for kunci in ("name", "label"):
        nilai = component.get(kunci)
        if nilai is not None and not isinstance(nilai, str):
            return False
    text = component.get("text")
    if text is not None and not isinstance(text, str):
        return False
    count = component.get("count")
    if count is not None and (not _plain_int(count) or count < 0):
        return False
    selected = component.get("selected")
    if selected is not None and not isinstance(selected, bool):
        return False
    if not _bounds_sah(component.get("bounds")):
        return False
    tetangga = component.get("neighbors")
    if tetangga is not None:
        if not isinstance(tetangga, dict):
            return False
        for arah in ("up", "right", "down", "left"):
            nilai = tetangga.get(arah)
            if nilai is not None and not _plain_int(nilai):
                return False
    return True


def _payload_state_sah(payload: object) -> bool:
    """Validasi payload `state` — fail closed.

    Field WAJIB divalidasi tipenya; field asing sengaja DITOLERANSI supaya
    mod yang lebih baru tidak mematikan receiver lama. Yang tidak boleh
    adalah field wajib yang hilang atau bertipe salah: motor memakai angka
    ini untuk memverifikasi gerakan, dan angka yang salah tipe lebih
    berbahaya daripada tidak ada state sama sekali.
    """
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("location"), str):
        return False
    if not _titik_int(payload.get("tile")):
        return False
    if not _titik_angka(payload.get("pixel")):
        return False
    if not _plain_int(payload.get("facing")):
        return False
    if not _plain_int(payload.get("time_of_day")):
        return False

    tanggal = payload.get("date")
    if not isinstance(tanggal, dict):
        return False
    if not isinstance(tanggal.get("season"), str):
        return False
    if not _plain_int(tanggal.get("day")) or not _plain_int(tanggal.get("year")):
        return False

    if not _angka(payload.get("energy")):
        return False
    if not _plain_int(payload.get("health")):
        return False
    if not isinstance(payload.get("is_player_free"), bool):
        return False
    if not isinstance(payload.get("can_player_move"), bool):
        return False
    ui_revision = payload.get("ui_revision")
    if ui_revision is not None and (
        not _plain_int(ui_revision) or ui_revision < 0
    ):
        return False

    menu = payload.get("active_menu")
    if menu is not None and not isinstance(menu, str):
        return False
    if not _menu_sah(payload.get("menu"), menu):
        return False

    if not _inventaris_sah(payload.get("inventory")):
        return False
    return True


def _inventaris_sah(inv: object) -> bool:
    """Inventaris OPSIONAL, tetapi kalau ada wajib benar.

    Opsional karena DLL lama (sebelum 29 Agu 2026) belum mengirimnya, dan
    receiver baru harus tetap jalan dengannya — kalau tidak, memperbarui satu
    sisi saja membuat seluruh state hilang.

    Tetapi kalau field-nya ADA dan rusak, seluruh state ditolak. Motor
    memverifikasi pengambilan barang dari `items_total`; angka sampah di situ
    membuat Arti mengumumkan "udah aku ambil" untuk sesuatu yang tidak
    terjadi. Sama seperti field lain di sini: angka salah tipe lebih
    berbahaya daripada tidak ada state sama sekali.
    """
    if inv is None:
        return True
    if not isinstance(inv, dict):
        return False
    for kunci in ("active_slot", "slots_used", "items_total"):
        if not _plain_int(inv.get(kunci)):
            return False
    hotbar = inv.get("hotbar")
    if not isinstance(hotbar, list):
        return False
    for slot in hotbar:
        if not isinstance(slot, dict):
            return False
        if not _plain_int(slot.get("slot")):
            return False
        if not isinstance(slot.get("name"), str):
            return False
        if not _plain_int(slot.get("stack")):
            return False
        if not isinstance(slot.get("is_tool"), bool):
            return False
    return True


class SequenceGate:
    """Tolak duplicate/stale per farmer+screen dalam satu koneksi TCP."""

    def __init__(self) -> None:
        self._last_by_player_screen: dict[tuple[int, int], int] = {}

    def accept(self, event: dict) -> bool:
        key = (event["player_id"], event["screen_id"])
        seq = event["seq"]
        previous = self._last_by_player_screen.get(key)
        if previous is not None and seq <= previous:
            return False
        self._last_by_player_screen[key] = seq
        return True


class _LoopbackServer(socketserver.ThreadingTCPServer):
    # SO_REUSEADDR di Windows mengizinkan dua listener aktif pada port yang
    # sama, lalu koneksi dapat masuk ke instance yang tidak sedang dipantau.
    # Fail closed supaya hanya ada satu receiver ARTI per port.
    allow_reuse_address = False
    daemon_threads = True


@dataclass(frozen=True)
class SlotBarang:
    """Satu slot hotbar. `alat` = turunan Tool di game (cangkul, kapak, ...)."""

    slot: int
    nama: str
    stack: int
    alat: bool


@dataclass(frozen=True)
class UiKomponen:
    """Komponen yang memang sudah tersedia di UI; native ID tetap internal."""

    key: str
    native_id: int
    source: str
    role: str
    name: str | None
    label: str | None
    bounds: tuple[int, int, int, int] | None
    neighbors: Mapping[str, int | None]
    text: str | None
    count: int | None
    selected: bool


@dataclass(frozen=True)
class StardewState:
    """Satu potret farmer. Immutable supaya tidak ada yang bisa mengubah
    snapshot yang sudah dibagikan ke pemanggil lain."""

    player_id: int
    screen_id: int
    seq: int
    revisi: int
    diterima_pada: float

    location: str
    tile: tuple[int, int]
    pixel: tuple[float, float]
    facing: int
    time_of_day: int
    season: str
    day: int
    year: int
    energy: float
    health: int
    is_player_free: bool
    can_player_move: bool
    active_menu: str | None
    ui_revision: int | None = None
    paused: bool | None = None
    host_paused: bool | None = None

    # Kursor UI controller. None kalau tidak ada menu terbuka ATAU DLL lama
    # belum mengirim blok `menu`. Bedanya penting: `active_menu` bisa terisi
    # sementara ketiga field ini None -- artinya kita TIDAK tahu kursornya di
    # mana, dan navigasi menu tidak boleh dicoba secara buta.
    menu_snap_id: int | None = None
    menu_snap_label: str | None = None
    menu_close_id: int | None = None
    menu_snap_bounds: tuple[int, int, int, int] | None = None
    menu_type: str | None = None
    menu_page_type: str | None = None
    menu_focus_key: str | None = None
    menu_components: tuple[UiKomponen, ...] = ()
    menu_visible_text: str | None = None
    menu_speaker: str | None = None

    # Inventaris: None/kosong kalau mod-nya belum mengirim (DLL lama).
    active_slot: int | None = None
    hotbar: tuple[SlotBarang, ...] = ()
    slots_used: int | None = None
    items_total: int | None = None
    own_state: dict | None = None
    menu_container: dict | None = None
    peer_sleep: dict | None = None

    @property
    def punya_kursor_menu(self) -> bool:
        """True kalau posisi kursor UI benar-benar diketahui.

        Motor WAJIB memeriksa ini sebelum menavigasi menu. Tanpa laporan
        kursor, satu-satunya cara "menutup menu" adalah menekan tombol
        secara buta -- yang terbukti gagal total 29 Agu 2026.
        """
        return self.menu_snap_id is not None

    @property
    def kursor_di_tombol_tutup(self) -> bool:
        return (
            self.menu_snap_id is not None
            and self.menu_close_id is not None
            and self.menu_snap_id == self.menu_close_id
        )

    @property
    def punya_inventaris(self) -> bool:
        """False kalau mod belum mengirim inventaris. Motor WAJIB memeriksa
        ini sebelum mengaku bisa memverifikasi pengambilan barang."""
        return self.items_total is not None

    @property
    def ui_fingerprint(self) -> tuple:
        """Fallback DLL lama; revision baru tetap menjadi sumber utama."""
        return (
            self.active_menu,
            self.menu_type,
            self.menu_page_type,
            self.menu_focus_key,
            self.menu_snap_id,
            self.menu_snap_label,
            self.menu_snap_bounds,
            self.menu_close_id,
            self.menu_components,
        )

    @property
    def dipegang(self) -> SlotBarang | None:
        """Barang di slot aktif, atau None kalau slotnya kosong/tak terkirim."""
        if self.active_slot is None:
            return None
        return next((s for s in self.hotbar if s.slot == self.active_slot), None)

    @classmethod
    def dari_event(cls, event: dict, *, revisi: int, waktu: float) -> "StardewState":
        p = event["payload"]
        d = p["date"]
        control = p.get("player_control")
        control = control if isinstance(control, dict) else {}
        return cls(
            player_id=event["player_id"],
            screen_id=event["screen_id"],
            seq=event["seq"],
            revisi=revisi,
            diterima_pada=waktu,
            location=p["location"],
            tile=(p["tile"]["x"], p["tile"]["y"]),
            pixel=(float(p["pixel"]["x"]), float(p["pixel"]["y"])),
            facing=p["facing"],
            time_of_day=p["time_of_day"],
            season=d["season"],
            day=d["day"],
            year=d["year"],
            energy=float(p["energy"]),
            health=p["health"],
            own_state=p.get("own_state") if isinstance(p.get("own_state"), dict) else None,
            menu_container=p.get("menu_container") if isinstance(p.get("menu_container"), dict) else None,
            peer_sleep=p.get("peer_sleep") if isinstance(p.get("peer_sleep"), dict) else None,
            is_player_free=p["is_player_free"],
            can_player_move=p["can_player_move"],
            active_menu=p.get("active_menu"),
            ui_revision=p.get("ui_revision"),
            paused=control.get("paused") if type(control.get("paused")) is bool else None,
            host_paused=control.get("host_paused") if type(control.get("host_paused")) is bool else None,
            **cls._menu(p.get("menu")),
            **cls._inventaris(p.get("inventory")),
        )

    @staticmethod
    def _menu(menu: object) -> dict:
        """Blok `menu` opsional; DLL lama tidak mengirimnya sama sekali."""
        if not isinstance(menu, dict):
            return {}
        b = menu.get("snap_bounds")
        bounds = None
        if _bounds_sah(b) and isinstance(b, dict):
            bounds = (b["x"], b["y"], b["w"], b["h"])
        snap = menu.get("snap_id")
        tutup = menu.get("close_id")
        label = menu.get("snap_label")
        components = tuple(
            UiKomponen(
                key=c["key"],
                native_id=c["native_id"],
                source=c["source"],
                role=c["role"],
                name=c.get("name"),
                label=c.get("label"),
                bounds=(
                    c["bounds"]["x"], c["bounds"]["y"],
                    c["bounds"]["w"], c["bounds"]["h"],
                ) if isinstance(c.get("bounds"), dict) else None,
                neighbors=MappingProxyType({
                    arah: (c.get("neighbors") or {}).get(arah)
                    for arah in ("up", "right", "down", "left")
                }),
                text=c.get("text"),
                count=c.get("count"),
                selected=c.get("selected", False),
            )
            for c in menu.get("components", [])
        )
        return {
            "menu_snap_id": snap if _plain_int(snap) else None,
            "menu_close_id": tutup if _plain_int(tutup) else None,
            "menu_snap_label": label if isinstance(label, str) else None,
            "menu_snap_bounds": bounds,
            "menu_type": menu.get("menu_type", menu.get("name")),
            "menu_page_type": menu.get("page_type"),
            "menu_focus_key": menu.get("focus_key"),
            "menu_components": components,
            "menu_visible_text": menu.get("visible_text"),
            "menu_speaker": menu.get("speaker"),
        }

    @staticmethod
    def _inventaris(inv: object) -> dict:
        """Sudah lolos `_inventaris_sah` saat sampai di sini."""
        if not isinstance(inv, dict):
            return {}
        return {
            "active_slot": inv["active_slot"],
            "hotbar": tuple(
                SlotBarang(
                    slot=s["slot"], nama=s["name"],
                    stack=s["stack"], alat=s["is_tool"],
                )
                for s in inv["hotbar"]
            ),
            "slots_used": inv["slots_used"],
            "items_total": inv["items_total"],
        }


@dataclass(frozen=True)
class StardewIdentitas:
    player_id: int
    screen_id: int
    player_name: str
    is_main_player: bool
    is_split_screen: bool


@dataclass(frozen=True, eq=False)
class FirstPostRelease:
    """Opaque one-request witness handle; samples remain the original objects."""
    request_id: str
    binding: object
    boundary: StardewState
    released_at: float


class StardewStateStore:
    """Cache state per FARMER, aman dipakai lintas thread.

    Kunci penyimpanannya `player_id` (UniqueMultiplayerID), BUKAN `screen_id`.
    Screen ID cuma routing runtime: pemain yang keluar-masuk bisa mendapat
    screen berbeda, dan identitas yang stabil pada save adalah player_id.

    Sequence TIDAK digerbangi di sini — `SequenceGate` di receiver sudah
    menolak duplicate/stale DI DALAM satu koneksi. Sesudah reconnect, mod
    memulai lagi dari seq 1, dan itu SAH untuk epoch baru. Karena itu
    `wait_next` memakai `revisi` (penghitung internal yang selalu naik),
    bukan `seq` game.
    """

    def __init__(self, *, jam: Callable[[], float] = time.monotonic) -> None:
        self._jam = jam
        self._kunci = threading.Condition()
        self._state: dict[int, StardewState] = {}
        self._identitas: dict[int, StardewIdentitas] = {}
        self._revisi = 0
        self._target: int | None = None
        self._pause_clocks = {}
        self._stall_clocks = {}
        self._first_post_release: tuple[FirstPostRelease, StardewState | None, bool] | None = None

    # --- masuk ---------------------------------------------------------
    def catat(self, event: dict) -> None:
        """Terima satu event yang SUDAH lolos `decode_event`."""
        if not isinstance(event, dict):
            return
        jenis = event.get("ev")
        if jenis == "hello":
            ident = StardewIdentitas(
                player_id=event["player_id"],
                screen_id=event["screen_id"],
                player_name=event["player_name"],
                is_main_player=event["is_main_player"],
                is_split_screen=event["is_split_screen"],
            )
            with self._kunci:
                # A hello is a new receiver epoch, even with the same farmer.
                self._first_post_release = None
                self._identitas[ident.player_id] = ident
                self._kunci.notify_all()
            return
        if jenis != "state":
            return
        with self._kunci:
            self._revisi += 1
            snap = StardewState.dari_event(
                event, revisi=self._revisi, waktu=self._jam()
            )
            from arti_stardew_pause import PauseClock
            pause = self._pause_clocks.setdefault(snap.player_id, PauseClock())
            ident = self._identitas.get(snap.player_id)
            pause.observe(bool(ident and not ident.is_main_player and ident.screen_id == snap.screen_id
                               and (snap.paused or snap.host_paused)), snap.diterima_pada)
            from arti_stardew_pause import StallClock
            self._stall_clocks.setdefault(snap.player_id, StallClock()).observe(snap.diterima_pada,
                (snap.year, snap.season, snap.day, snap.time_of_day),
                not (snap.paused or snap.host_paused) and snap.location != "Temp"
                and type(snap.time_of_day) is int and snap.time_of_day < 2600)
            capture = self._first_post_release
            if capture is not None and capture[1] is None and not capture[2]:
                token = capture[0]
                before = token.boundary
                # >=, not >: catat runs after arm under this lock, so the order is already proven;
                # the Windows monotonic clock ticks in 15.6 ms steps, and a frame landing in the arm's
                # own tick was rejected as "missing" (live 2026-10-06 attempts 10l and 10p).
                valid = (snap.revisi == before.revisi + 1
                         and (snap.player_id, snap.screen_id) == (before.player_id, before.screen_id)
                         and snap.diterima_pada >= token.released_at)
                self._first_post_release = (token, snap if valid else None, not valid)
            self._state[snap.player_id] = snap
            self._kunci.notify_all()

    def arm_first_post_release(self, *, request_id: str, player_id: int,
                               screen_id: int, binding: object) -> FirstPostRelease:
        """Read boundary and arm under catat's Condition, without callbacks.

        Runtime -> store is the only lock order. Waiting releases this Condition;
        the store never acquires runtime/observer/provider locks.
        """
        with self._kunci:
            if self._first_post_release is not None:
                raise ValueError("first_post_release_busy")
            boundary = self._state.get(player_id)
            if (not request_id or boundary is None or boundary.screen_id != screen_id
                    or boundary.revisi != self._revisi):
                raise ValueError("first_post_release_boundary_unavailable")
            token = FirstPostRelease(request_id, binding, boundary, self._jam())
            self._first_post_release = (token, None, False)
            return token

    def cancel_first_post_release(self, token: FirstPostRelease | None = None) -> None:
        with self._kunci:
            if (self._first_post_release is not None
                    and (token is None or self._first_post_release[0] is token)):
                self._first_post_release = None
                self._kunci.notify_all()

    @W.span('settle_readback', 123)
    def consume_first_post_release(self, token: FirstPostRelease, *,
                                   timeout: float, max_age: float) -> StardewState | None:
        deadline = time.monotonic() + max(0., timeout)
        with self._kunci:
            try:
                while self._first_post_release is not None and self._first_post_release[0] is token:
                    _, sample, invalid = self._first_post_release
                    if invalid:
                        return None
                    if sample is not None:
                        return sample if 0 <= self._jam() - sample.diterima_pada <= max_age else None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._kunci.wait(remaining)
                return None
            finally:
                if self._first_post_release is not None and self._first_post_release[0] is token:
                    self._first_post_release = None
                    self._kunci.notify_all()

    # --- keluar --------------------------------------------------------
    def latest(self, player_id: int) -> StardewState | None:
        with self._kunci:
            return self._state.get(player_id)

    def identitas(self, player_id: int) -> StardewIdentitas | None:
        with self._kunci:
            return self._identitas.get(player_id)

    def semua_identitas(self) -> tuple[StardewIdentitas, ...]:
        with self._kunci:
            return tuple(self._identitas.values())

    @W.span('settle_readback', 117)
    def wait_next(
        self, player_id: int, after_revisi: int, timeout: float
    ) -> StardewState | None:
        """Tunggu snapshot yang LEBIH BARU dari `after_revisi`."""
        batas = self._jam() + max(0.0, float(timeout))
        with self._kunci:
            while True:
                snap = self._state.get(player_id)
                if snap is not None and snap.revisi > after_revisi:
                    return snap
                sisa = batas - self._jam()
                if sisa <= 0:
                    return None
                self._kunci.wait(sisa)

    def is_stale(self, player_id: int, max_age: float = 0.5) -> bool:
        """True kalau state tidak layak dipakai motor.

        Belum pernah ada state juga dihitung STALE — motor tidak boleh
        bergerak berdasarkan ketiadaan data.
        """
        with self._kunci:
            snap = self._state.get(player_id)
            if snap is None:
                return True
            return (self._jam() - snap.diterima_pada) > max_age

    def game_paused(self, player_id: int) -> bool:
        with self._kunci:
            snap, ident = self._state.get(player_id), self._identitas.get(player_id)
            return bool(snap and ident and not ident.is_main_player
                        and ident.screen_id == snap.screen_id and not self.is_stale(player_id)
                        and (snap.paused is True or snap.host_paused is True))

    def paused_seconds(self, player_id: int) -> float:
        with self._kunci:
            pause = self._pause_clocks.get(player_id)
            return pause.seconds(self._jam()) if pause else 0.0

    def stall_seconds(self, player_id: int) -> float:
        with self._kunci:
            stall = self._stall_clocks.get(player_id)
            return stall.seconds(self._jam()) if stall else 0.0

    def state_layak_input(
        self, player_id: int, *, max_age: float = 0.5
    ) -> tuple["StardewState | None", str]:
        """Validasi identitas, target, dan freshness sebelum input apa pun."""
        with self._kunci:
            if self._target is not None and player_id != self._target:
                return None, "bukan farmer target"
            ident = self._identitas.get(player_id)
            if ident is None:
                return None, "identitas belum pernah diterima (hello)"
            if ident.is_main_player:
                return None, "ini pemain utama, bukan farmhand"
            snap = self._state.get(player_id)
            if snap is None:
                return None, "belum pernah ada state"
            umur = self._jam() - snap.diterima_pada
            if umur > max_age:
                return None, f"state basi ({umur:.2f}s > {max_age:.2f}s)"
            if snap.paused is True or snap.host_paused is True:
                return None, "game dijeda oleh Bohan"
            return snap, ""

    def state_layak_motor(
        self, player_id: int, *, max_age: float = 0.5
    ) -> tuple["StardewState | None", str]:
        """Gerbang gerak: input umum ditambah status bebas bergerak.

        Balikan `(snapshot, "")` kalau layak, atau `(None, alasan)` kalau
        tidak. Alasannya dikembalikan sebagai teks supaya motor bisa
        mencatatnya di ActionResult, bukan gagal diam.

        Ditulis SEBELUM motornya ada, dengan sengaja. Kalau gerbang ini baru
        dibuat bersamaan dengan tangan, ada jendela waktu di mana input sudah
        bisa dikirim tapi pemeriksaannya belum lengkap — dan di jendela itu
        Arti bisa menekan tombol berdasarkan posisi masa lalu.

        Yang TIDAK diperiksa di sini karena bukan urusan state: gerbang
        window-fokus, deadman, dan tombol mati darurat. Ketiganya milik
        Fase D dan tetap wajib.
        """
        snap, alasan = self.state_layak_input(player_id, max_age=max_age)
        if snap is None:
            return None, alasan
        if not snap.is_player_free:
            return None, "is_player_free=false"
        if not snap.can_player_move:
            return None, "can_player_move=false"
        return snap, ""

    # --- pemilihan target ----------------------------------------------
    @property
    def target(self) -> int | None:
        with self._kunci:
            return self._target

    def pilih_target(self, nama_farmer: str | None = None) -> int | None:
        """Pilih farmer P2 lalu KUNCI untuk sesi ini.

        Aturan: identitas dari hello yang sah, `is_main_player == False`,
        dan kalau `nama_farmer` diberikan, namanya harus cocok.

        FAIL CLOSED: kalau ada lebih dari satu kandidat dan namanya tidak
        menentukan, kembalikan None. Menebak berarti motor bisa
        menggerakkan farmer yang salah — dan itu tidak bisa dibatalkan.
        """
        with self._kunci:
            if self._target is not None:
                return self._target
            kandidat = [i for i in self._identitas.values() if not i.is_main_player]
            if nama_farmer:
                target_nama = nama_farmer.strip().casefold()
                kandidat = [
                    i for i in kandidat
                    if i.player_name.strip().casefold() == target_nama
                ]
            if len(kandidat) != 1:
                return None
            self._target = kandidat[0].player_id
            return self._target

    def lepas_target(self) -> None:
        with self._kunci:
            self._target = None


class StardewReceiver:
    """Listener lifecycle kecil yang bisa dipakai CLI maupun bridge nanti."""

    def __init__(
        self,
        on_event: Callable[[dict], None],
        *,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._on_event = on_event
        self._port = int(port)
        self._server: _LoopbackServer | None = None
        self._thread: threading.Thread | None = None
        # ADR 44 (fix32): lines over MAX_LINE_BYTES are still dropped (never raise the bound), but counted:
        # domain -> {count, bytes, at (monotonic)}. a28 D19 dropped every chest ui.context with no trace.
        self._oversize: dict[str, dict] = {}
        self._oversize_lock = threading.Lock()

    def note_oversize(self, head: bytes, size: int) -> None:
        """Quiet like every other refused line; the runtime names it (view miss ``ui_context_wire_dropped``)."""
        match = _DOMAIN_HEAD.search(head)
        domain = match.group(1).decode("ascii") if match else "unknown"
        with self._oversize_lock:
            row = self._oversize.setdefault(domain, dict(count=0, bytes=0, at=None))
            row.update(count=row["count"] + 1, bytes=size, at=time.monotonic())

    def oversize_after(self, domain: str, since: float) -> dict | None:
        """The latest dropped-line record for ``domain`` if not older than ``since`` (monotonic), else None."""
        with self._oversize_lock:
            row = self._oversize.get(domain)
            if row is None or row["at"] is None or row["at"] < since:  # same clock tick counts (Windows ~16 ms)
                return None
            return dict(count=row["count"], bytes=row["bytes"])

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("receiver belum aktif")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("receiver sudah aktif")
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                gate = SequenceGate()
                while True:
                    try:
                        raw = self.rfile.readline(MAX_LINE_BYTES + 1)
                    except ConnectionResetError:
                        # Peer shutdown (termasuk WinError 10054), bukan error callback.
                        return
                    if not raw:
                        return
                    if len(raw) > MAX_LINE_BYTES:
                        # Habiskan sisa baris supaya fragmen berikutnya tidak
                        # keliru dianggap sebagai envelope baru.
                        head, size = raw[:8192], len(raw)
                        while raw and not raw.endswith(b"\n"):
                            try:
                                raw = self.rfile.readline(MAX_LINE_BYTES + 1)
                            except ConnectionResetError:
                                return  # Peer juga bisa putus saat baris dibuang.
                            size += len(raw)
                        owner.note_oversize(head, size)
                        continue
                    event = decode_event(raw)
                    if event is None or not gate.accept(event):
                        continue
                    try:
                        owner._on_event(event)
                    except Exception as exc:  # noqa: BLE001 — koneksi harus tetap hidup
                        print(
                            f"[Stardew] callback gagal: {type(exc).__name__}: {exc}",
                            flush=True,
                        )

        self._server = _LoopbackServer((LOOPBACK_HOST, self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="stardew-receiver",
        )
        self._thread.start()

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)


def format_cli_ringkasan(
    event: dict,
    store: StardewStateStore,
    hitung: dict[int, int],
    *,
    tiap: int,
    target: str | None,
) -> list[str]:
    """Ringkasan CLI untuk hello/state saja.

    Observation dan event lain tidak menaikkan penghitung state dan tidak
    mencetak snapshot state basi seolah-olah state baru. CLI observasi
    kaya belum ada.
    """
    pid = event["player_id"]
    jenis = event.get("ev")
    if jenis == "hello":
        peran = "P1/utama" if event["is_main_player"] else "P2/farmhand"
        baris = [
            f"[hello] {event['player_name']} ({peran}) "
            f"player_id={pid} screen={event['screen_id']} seq={event['seq']}"
        ]
        terpilih = store.pilih_target(target)
        if terpilih is not None:
            baris.append(f"[target] terkunci ke player_id={terpilih}")
        elif not event["is_main_player"]:
            baris.append(
                "[target] BELUM terkunci — ambigu atau nama tidak cocok "
                "(pakai --target NAMA). Fail closed, tidak menebak."
            )
        return baris
    if jenis != "state":
        return []

    n = hitung.get(pid, 0) + 1
    hitung[pid] = n
    if n % max(1, tiap):
        return []
    snap = store.latest(pid)
    if snap is None:
        return []
    ident = store.identitas(pid)
    nama = ident.player_name if ident else "?"
    tanda = "*" if store.target == pid else " "
    gerak = "bebas" if (snap.is_player_free and snap.can_player_move) else "TERKUNCI"
    if snap.active_menu:
        if snap.punya_kursor_menu:
            menu = (
                f" menu={snap.active_menu}"
                f" snap={snap.menu_snap_id}:{snap.menu_snap_label or '-'}"
                f" close={snap.menu_close_id}"
            )
        else:
            menu = f" menu={snap.active_menu} snap=TIDAK-DIKIRIM"
    else:
        menu = ""
    if snap.punya_inventaris:
        dipegang = snap.dipegang
        tas = (
            f" tas={snap.slots_used}sl/{snap.items_total}"
            f" pegang={dipegang.nama if dipegang else '-'}"
            f"{'(alat)' if dipegang and dipegang.alat else ''}"
        )
    else:
        tas = " tas=TIDAK-DIKIRIM"
    return [
        f"[state]{tanda}{nama:<10} scr={snap.screen_id} "
        f"{snap.location:<14} tile=({snap.tile[0]:>3},{snap.tile[1]:>3}) "
        f"hadap={snap.facing} energi={snap.energy:6.1f} hp={snap.health:3d} "
        f"{gerak}{menu}{tas}  (#{n})"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Receiver mata Stardew read-only")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--target", default=None,
        help="nama farmer P2 (mis. artihand); tanpa ini, target ambigu = fail closed",
    )
    parser.add_argument(
        "--mentah", action="store_true",
        help="cetak NDJSON apa adanya (bukan ringkasan)",
    )
    parser.add_argument(
        "--tiap", type=int, default=10,
        help="cetak 1 dari N state per farmer (bawaan 10 = ~1 detik pada 10 Hz)",
    )
    args = parser.parse_args()

    store = StardewStateStore()
    hitung: dict[int, int] = {}

    def pada_event(event: dict) -> None:
        store.catat(event)
        if args.mentah:
            print(json.dumps(event, ensure_ascii=False), flush=True)
            return
        for baris in format_cli_ringkasan(
            event, store, hitung, tiap=args.tiap, target=args.target
        ):
            print(baris, flush=True)

    receiver = StardewReceiver(port=args.port, on_event=pada_event)
    receiver.start()
    print(f"[Stardew] Mendengarkan {receiver.address[0]}:{receiver.address[1]}", flush=True)
    if args.target:
        print(f"[Stardew] Target farmer: {args.target}", flush=True)
    print("[Stardew] Baris ber-* = farmer target. Ctrl+C untuk berhenti.", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(chr(10) + "[Stardew] Berhenti.", flush=True)
    finally:
        receiver.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

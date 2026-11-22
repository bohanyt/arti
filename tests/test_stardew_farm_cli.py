"""CLI one-shot Farm Loop v1: preflight vs satu execute, tanpa retry.

Simulasi game di tes integrasi loopback/fake-inbox adalah eksternal
(bukan Stardew/SMAPI runtime). Tidak ada klaim VERIFIED LIVE.
"""
from __future__ import annotations

import importlib.util
import io
import json
import socket
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from arti_stardew import StardewReceiver, StardewStateStore, decode_event  # noqa: E402
from arti_stardew_command import (  # noqa: E402
    NAMA_SESSION,
    SELESAI,
    TIDAK_PASTI,
    HasilPerintah,
    baca_sesi as baca_sesi_asli,
)
from arti_stardew_farm_v0 import SESSION_CHANGED, SESSION_UNREADABLE  # noqa: E402
from arti_stardew_farm_v1 import (  # noqa: E402
    INSPECT_REJECTED,
    NO_TARGET,
    PermintaanFarmV1,
    HasilFarmV1,
    RADIUS_INSPEKSI,
    SUCCESS,
    jalankan_farm_v1,
)
from test_stardew_inspect_tiles import envelope_inspect  # noqa: E402
from test_stardew_water_once_inbox import envelope_water  # noqa: E402
from test_stardew_ui import _component  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "stardew_farm_cli", ROOT / "scripts" / "stardew_farm.py"
)
assert _SPEC is not None and _SPEC.loader is not None
cli = importlib.util.module_from_spec(_SPEC)
sys.modules["stardew_farm_cli"] = cli
_SPEC.loader.exec_module(cli)

P2 = 100000000012
P_LAIN = -100000000000000001
FARMER = (10, 11)
TARGET = (10, 12)
SESSION_ID = "sesi-cli-v1"


def tulis_sesi(folder: Path, **ubah) -> None:
    isi = {
        "protocol": 1,
        "ev": "command_session",
        "session_id": SESSION_ID,
        "player_id": P2,
        "player_name": "artihand",
        "location_id": "Farm",
        "screen_id": 0,
        "is_main_player": False,
        "is_split_screen": False,
        "inbox_enabled": True,
        "menu_step_enabled": False,
        "water_once_enabled": True,
        "world_press_enabled": False,
        "world_ready": True,
        "inflight_request_id": None,
        "input_busy": False,
    }
    isi.update(ubah)
    (folder / NAMA_SESSION).write_text(json.dumps(isi), encoding="utf-8")


def _hello(player_id=P2, *, seq=1, main=False, name="artihand", screen_id=0, split=False):
    return {
        "protocol": 1,
        "ev": "hello",
        "seq": seq,
        "player_id": player_id,
        "screen_id": screen_id,
        "player_name": name,
        "is_main_player": main,
        "is_split_screen": split,
    }


def _payload_state(**timpa):
    p = {
        "location": "Farm",
        "tile": {"x": FARMER[0], "y": FARMER[1]},
        "pixel": {"x": FARMER[0] * 64.0, "y": FARMER[1] * 64.0},
        "facing": 2,
        "time_of_day": 610,
        "date": {"season": "spring", "day": 1, "year": 1},
        "energy": 270.0,
        "health": 100,
        "is_player_free": True,
        "can_player_move": True,
        "active_menu": None,
    }
    p.update(timpa)
    return p


def _state(player_id=P2, *, seq=2, screen_id=0, **payload):
    return {
        "protocol": 1,
        "ev": "state",
        "seq": seq,
        "player_id": player_id,
        "screen_id": screen_id,
        "payload": _payload_state(**payload),
    }


def isi_store(store: StardewStateStore, **state_timpa) -> None:
    store.catat(_hello())
    store.catat(_state(**state_timpa))


def hasil_driver(status=SUCCESS, *, success=True, **ubah) -> HasilFarmV1:
    data = dict(
        status=status,
        success=success,
        selected_target=TARGET,
        inspect_attempted=True,
        inspect_request_id="inspect-1",
        inspect_outcome="Inspected",
        water_outcome="ObservedWatered" if success else None,
        terminal_request_id="water-1" if success else "inspect-1",
        terminal_outcome="ObservedWatered" if success else "Inspected",
        revision=4,
        tile=FARMER,
        pixel=(640.0, 704.0),
        reason=None,
    )
    data.update(ubah)
    return HasilFarmV1(**data)


class ReceiverPalsu:
    """Tidak bind socket. stop() dihitung untuk tes cleanup."""

    instances: list["ReceiverPalsu"] = []

    def __init__(self, on_event, *, port=3017):
        self.on_event = on_event
        self.port = port
        self.starts = 0
        self.stops = 0
        self.busy = False
        ReceiverPalsu.instances.append(self)

    @property
    def address(self):
        return ("127.0.0.1", self.port if self.port else 39999)

    def start(self) -> None:
        self.starts += 1
        if self.busy:
            raise OSError("Address already in use")

    def stop(self) -> None:
        self.stops += 1


class ReceiverSibuk(ReceiverPalsu):
    def start(self) -> None:
        self.starts += 1
        raise OSError("Address already in use")


def jalankan_cli(argv, **kw):
    out = kw.pop("stdout", io.StringIO())
    err = kw.pop("stderr", io.StringIO())
    rc = cli.jalankan(argv, stdout=out, stderr=err, **kw)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _reset_receiver_palsu():
    ReceiverPalsu.instances.clear()
    yield
    ReceiverPalsu.instances.clear()


# --- import / --help / default tanpa perintah ---------------------------------

def test_import_tidak_menjalankan_driver(monkeypatch):
    panggil = []
    monkeypatch.setattr(cli, "_jalankan_farm_v1_asli", lambda *a, **k: panggil.append(1))
    assert panggil == []
    assert cli.SCHEMA == "stardew_farm_cli.v1"


def test_help_tanpa_side_effect(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ARTI_STARDEW_COMMAND_DIR", raising=False)
    with pytest.raises(SystemExit) as keluar:
        cli.jalankan(["--help"], store=StardewStateStore(), receiver_cls=ReceiverPalsu)
    assert keluar.value.code == 0
    assert ReceiverPalsu.instances == []
    assert list(tmp_path.iterdir()) == []


def test_help_menyebut_execute_folder_port_bukan_rute():
    teks = cli.bangun_parser().format_help()
    assert "--execute" in teks
    assert "--folder" in teks
    assert "--port" in teks
    assert "3017" in teks
    assert "--route" not in teks
    assert "--target" not in teks
    assert "3121" not in teks
    assert "VERIFIED LIVE" in teks  # disebut sebagai BUKAN klaim
    assert "$env:ARTI_STARDEW_COMMAND_DIR" in teks


def test_default_status_tanpa_folder_tidak_invoke_v1(monkeypatch):
    monkeypatch.delenv("ARTI_STARDEW_COMMAND_DIR", raising=False)
    panggil = []

    def jangan(*a, **k):
        panggil.append(1)
        raise AssertionError("v1 tidak boleh dipanggil")

    rc, out, _ = jalankan_cli(
        [],
        jalankan_v1=jangan,
        receiver_cls=ReceiverPalsu,
    )
    assert rc == cli.EXIT_REFUSED
    assert panggil == []
    assert ReceiverPalsu.instances == []
    assert "SESSION_UNREADABLE" in out
    assert "VERIFIED LIVE" not in out or "bukan" in out.lower() or "false" in out.lower()


def test_argumen_koordinat_ditolak(tmp_path: Path):
    tulis_sesi(tmp_path)
    with pytest.raises(SystemExit):
        cli.jalankan(
            ["--folder", str(tmp_path), "--x", "57"],
            receiver_cls=ReceiverPalsu,
        )


# --- konfigurasi / port sibuk -------------------------------------------------

def test_folder_tanpa_sesi_nol_receiver(tmp_path: Path):
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path)],
        receiver_cls=ReceiverPalsu,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "SESSION_UNREADABLE" in out
    assert ReceiverPalsu.instances == []


def test_player_id_tidak_cocok_sebelum_receiver(tmp_path: Path):
    tulis_sesi(tmp_path)
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--player-id", str(P_LAIN), "--execute"],
        receiver_cls=ReceiverPalsu,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "PLAYER_MISMATCH" in out
    assert ReceiverPalsu.instances == []


def test_host_session_ditolak_tanpa_motor(tmp_path: Path):
    tulis_sesi(tmp_path, is_main_player=True)
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute"],
        receiver_cls=ReceiverPalsu,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "HOST_INSTANCE" in out
    assert ReceiverPalsu.instances == []


def test_port_di_luar_jangkauan(tmp_path: Path):
    tulis_sesi(tmp_path)
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--port", "70000"],
        receiver_cls=ReceiverPalsu,
    )
    assert rc == cli.EXIT_REFUSED
    assert "CONFIG_INVALID" in out
    assert ReceiverPalsu.instances == []


def test_port_sibuk_nol_motor(tmp_path: Path):
    tulis_sesi(tmp_path)
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute"],
        receiver_cls=ReceiverSibuk,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "PORT_BUSY" in out
    assert ReceiverSibuk.instances[0].starts == 1
    assert ReceiverSibuk.instances[0].stops == 1


def test_port_sibuk_listener_nyata(tmp_path: Path):
    tulis_sesi(tmp_path)
    first = StardewReceiver(on_event=lambda event: None, port=0)
    first.start()
    try:
        port = first.address[1]
        rc, out, _ = jalankan_cli(
            ["--folder", str(tmp_path), "--port", str(port), "--execute", "--wait", "0.05"],
            jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
        )
        assert rc == cli.EXIT_REFUSED
        assert "PORT_BUSY" in out
    finally:
        first.stop()


# --- gerbang state -----------------------------------------------------------

def test_startup_timeout_tanpa_state(tmp_path: Path):
    tulis_sesi(tmp_path)
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0.05", "--execute"],
        receiver_cls=ReceiverPalsu,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "STARTUP_TIMEOUT" in out
    assert ReceiverPalsu.instances[0].stops == 1


def test_stale_state_fail_closed(tmp_path: Path):
    tulis_sesi(tmp_path)
    jam = {"t": 1000.0}

    def sekarang():
        return jam["t"]

    store = StardewStateStore(jam=sekarang)
    isi_store(store)
    jam["t"] = 1000.0 + 5.0
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0", "--max-age", "0.5", "--execute"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jam=sekarang,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "STALE_STATE" in out


def test_farmhand_asing_tidak_rebind(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    store.catat(_hello(P_LAIN, name="orang-lain"))
    store.catat(_state(P_LAIN, seq=2))
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0", "--execute"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "FOREIGN_PLAYER" in out


def test_status_segar_tidak_memanggil_v1(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    panggil = []
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=lambda *a, **k: panggil.append(a) or hasil_driver(),
    )
    assert rc == cli.EXIT_OK
    assert panggil == []
    assert "PREFLIGHT_OK" in out
    assert "execute=false" in out
    assert ReceiverPalsu.instances[0].starts == 1
    assert ReceiverPalsu.instances[0].stops == 1


# --- execute sekali, outcome diawetkan ---------------------------------------

def test_execute_memanggil_v1_sekali_success(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    panggil = []

    def v1(permintaan, **kw):
        panggil.append((permintaan, kw))
        return hasil_driver()

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=v1,
    )
    assert rc == cli.EXIT_OK
    assert len(panggil) == 1
    permintaan, kw = panggil[0]
    assert isinstance(permintaan, PermintaanFarmV1)
    assert permintaan.player_id == P2
    assert permintaan.folder == str(tmp_path)
    assert "target" not in kw
    assert "route" not in kw
    assert "SUCCESS" in out
    assert "target=(10, 12)" in out
    assert "VERIFIED LIVE" not in out


def test_execute_no_target_diawetkan(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)

    def v1(permintaan, **kw):
        return hasil_driver(
            NO_TARGET,
            success=False,
            selected_target=None,
            water_outcome=None,
            terminal_request_id="inspect-1",
            terminal_outcome="Inspected",
            reason="tidak ada HoeDirt",
        )

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=v1,
    )
    assert rc == cli.EXIT_DRIVER
    assert "NO_TARGET" in out
    assert "tidak ada HoeDirt" in out


def test_execute_inspect_rejected_diawetkan(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)

    def v1(permintaan, **kw):
        return hasil_driver(
            INSPECT_REJECTED,
            success=False,
            selected_target=None,
            inspect_outcome=None,
            water_outcome=None,
            reason="ditolak",
        )

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=v1,
    )
    assert rc == cli.EXIT_DRIVER
    assert "INSPECT_REJECTED" in out


def test_tidak_ada_retry_otomatis(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    n = {"k": 0}

    def v1(permintaan, **kw):
        n["k"] += 1
        return hasil_driver(
            "WATER_UNCERTAIN",
            success=False,
            water_outcome="Unknown",
            reason="tidak_pasti",
        )

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=v1,
    )
    assert rc == cli.EXIT_DRIVER
    assert n["k"] == 1
    assert "WATER_UNCERTAIN" in out


# --- JSON aman, termasuk menu MappingProxyType --------------------------------

def _snap_menu() -> object:
    payload = {
        "location": "Farm",
        "tile": {"x": 10, "y": 11},
        "pixel": {"x": 640, "y": 704},
        "facing": 1,
        "time_of_day": 610,
        "date": {"season": "spring", "day": 1, "year": 1},
        "energy": 270,
        "health": 100,
        "is_player_free": False,
        "can_player_move": False,
        "active_menu": "GameMenu",
        "ui_revision": 3,
        "menu": {
            "name": "GameMenu",
            "menu_type": "GameMenu",
            "page_type": "InventoryPage",
            "snap_id": 3,
            "snap_label": "Hoe",
            "snap_bounds": {"x": 64, "y": 64, "w": 64, "h": 64},
            "close_id": 9,
            "focus_key": "inventory.slot.0",
            "components": [
                _component("inventory.slot.0", 0, "inventory_slot", text="Hoe", selected=True),
            ],
        },
    }
    event = decode_event(json.dumps({
        "protocol": 1, "ev": "state", "seq": 2,
        "player_id": P2, "screen_id": 0, "payload": payload,
    }))
    assert event is not None
    store = StardewStateStore()
    store.catat(event)
    snap = store.latest(P2)
    assert snap is not None
    assert snap.menu_components
    assert isinstance(snap.menu_components[0].neighbors, MappingProxyType)
    return snap


def test_asdict_menu_tidak_json_aman():
    snap = _snap_menu()
    with pytest.raises(TypeError, match="mappingproxy"):
        asdict(snap)
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(snap.menu_components[0].neighbors)


def test_potret_aman_menu_json_tanpa_mappingproxy():
    snap = _snap_menu()
    potret = cli.potret_aman(snap, command_session_id=SESSION_ID)
    teks = json.dumps(potret)
    assert "mappingproxy" not in teks.lower()
    assert potret["active_menu"] == "GameMenu"
    assert potret["menu_type"] == "GameMenu"
    assert potret["tile"] == [10, 11]
    assert "menu_components" not in potret
    assert "neighbors" not in teks


def test_json_file_status_berisi_skema(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    tujuan = tmp_path / "out.json"
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0", "--json", str(tujuan)],
        receiver_cls=ReceiverPalsu,
        store=store,
    )
    assert rc == cli.EXIT_OK
    data = json.loads(tujuan.read_text(encoding="utf-8"))
    assert data["schema"] == cli.SCHEMA
    assert data["mode"] == "status"
    assert data["execute"] is False
    assert data["action_attempted"] is False
    assert data["verified_live"] is False
    assert data["driver"] is None
    assert data["state_before"]["tile"] == [10, 11]
    json.dumps(data)


def test_json_execute_success_shape(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    tujuan = tmp_path / "out.json"
    rc, _, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0", "--json", str(tujuan)],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=lambda *a, **k: hasil_driver(),
    )
    assert rc == cli.EXIT_OK
    data = json.loads(tujuan.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["driver"]["status"] == SUCCESS
    assert data["driver"]["selected_target"] == [10, 12]
    assert data["driver_invoked"] is True
    assert data["water_confirmed"] is True
    assert data["action_attempted"] is True


# --- gagal tulis JSON tidak mengulang aksi -----------------------------------

def test_gagal_json_setelah_aksi_tidak_retry(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    n = {"k": 0}

    def v1(permintaan, **kw):
        n["k"] += 1
        return hasil_driver()

    def tulis_gagal(path, obj):
        raise OSError("disk full")

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0", "--json", str(tmp_path / "x.json")],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=v1,
        tulis_json=tulis_gagal,
    )
    assert n["k"] == 1
    assert rc == cli.EXIT_OUTPUT
    assert "SUCCESS" in out
    assert "ObservedWatered tercatat" in out
    assert "TIDAK diulang" in out
    assert "aksi sudah terjadi" not in out
    assert "disk full" in out


# --- cleanup ------------------------------------------------------------------

def test_cleanup_error_path(tmp_path: Path):
    tulis_sesi(tmp_path)
    rc, _, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0.02"],
        receiver_cls=ReceiverPalsu,
    )
    assert ReceiverPalsu.instances[0].stops == 1
    assert rc == cli.EXIT_REFUSED


def test_cleanup_keyboardinterrupt(tmp_path: Path):
    tulis_sesi(tmp_path)
    panggil = []

    def tidur_interrupt(_detik):
        raise KeyboardInterrupt()

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "2", "--execute"],
        receiver_cls=ReceiverPalsu,
        jam=time.monotonic,
        tidur=tidur_interrupt,
        jalankan_v1=lambda *a, **k: panggil.append(1) or hasil_driver(),
    )
    assert panggil == []
    assert rc == cli.EXIT_REFUSED
    assert "CANCELLED" in out
    assert "tidak dihapus" in out
    assert ReceiverPalsu.instances[0].stops == 1


# --- integrasi loopback + fake inbox (simulasi game, bukan native) -----------

class ModBerantai:
    """Simulasi mod: beberapa request.json berurutan. Bukan Stardew runtime."""

    def __init__(self, folder: Path, envelopes: list[dict], on_op=None):
        self.folder = folder
        self.envelopes = list(envelopes)
        self.on_op = on_op
        self.permintaan: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        from arti_stardew_command import NAMA_REQUEST, NAMA_RESULT

        req = self.folder / NAMA_REQUEST
        i = 0
        while not self._stop.is_set() and i < len(self.envelopes):
            if req.exists():
                perm = json.loads(req.read_text(encoding="utf-8"))
                req.unlink()
                self.permintaan.append(perm)
                if self.on_op is not None:
                    self.on_op(perm)
                isi = dict(self.envelopes[i])
                isi.setdefault("request_id", perm["request_id"])
                tmp = self.folder / (NAMA_RESULT + ".tmp")
                tmp.write_text(json.dumps(isi), encoding="utf-8")
                tmp.replace(self.folder / NAMA_RESULT)
                i += 1
            else:
                time.sleep(0.01)


def _kirim(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def test_integrasi_loopback_receiver_store_driver_inbox(tmp_path: Path):
    """Simulasi game eksternal lewat TCP + berkas inbox. Bukan native Stardew."""
    tulis_sesi(tmp_path)
    tujuan = tmp_path / "bukti.json"
    sock_box: dict = {}
    seq = {"n": 2}

    def on_op(perm):
        if perm.get("op") == "inspect_tiles" and "sock" in sock_box:
            seq["n"] += 1
            _kirim(sock_box["sock"], _state(seq=seq["n"]))

    facts = {
        f"tf:Farm:{TARGET[0]},{TARGET[1]}:HoeDirt": {
            "kind": "terrain",
            "terrain_type": "HoeDirt",
            "tile": {"x": TARGET[0], "y": TARGET[1]},
            "passable": True,
            "crop": {
                "qualified_item_id": "(O)24",
                "phase": 1,
                "dead": False,
                "watered": False,
                "ready_for_harvest": False,
            },
        }
    }
    env_inspect = envelope_inspect(
        session_id=SESSION_ID,
        player_id=P2,
        location_id="Farm",
        center_x=FARMER[0],
        center_y=FARMER[1],
        radius=1,
        facts=facts,
    )
    env_water = envelope_water(session_id=SESSION_ID)

    def on_listener(host: str, port: int) -> None:
        sock_box["sock"] = socket.create_connection((host, port), timeout=2.0)
        _kirim(sock_box["sock"], _hello())
        _kirim(sock_box["sock"], _state(seq=2))

    with ModBerantai(tmp_path, [env_inspect, env_water], on_op=on_op):
        try:
            rc, out, _ = jalankan_cli(
                [
                    "--folder", str(tmp_path),
                    "--port", "0",
                    "--execute",
                    "--wait", "2",
                    "--max-age", "5",
                    "--state-timeout", "2",
                    "--json", str(tujuan),
                ],
                on_listener=on_listener,
            )
        finally:
            if "sock" in sock_box:
                sock_box["sock"].close()

    assert rc == cli.EXIT_OK, out
    assert "SUCCESS" in out
    data = json.loads(tujuan.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["driver"]["status"] == SUCCESS
    assert data["driver"]["selected_target"] == [TARGET[0], TARGET[1]]
    assert data["driver"]["inspect_outcome"] == "Inspected"
    assert data["driver"]["water_outcome"] == "ObservedWatered"
    ops = [c["op"] for c in data["command_calls"]]
    assert ops == ["inspect_tiles", "water_once"]
    assert data["receiver"]["started"] is True
    assert data["receiver"]["stopped"] is True
    assert data["receiver"]["host"] == "127.0.0.1"
    assert data["verified_live"] is False
    json.dumps(data)


def test_status_loopback_nol_inbox_request(tmp_path: Path):
    tulis_sesi(tmp_path)
    from arti_stardew_command import NAMA_REQUEST

    def on_listener(host: str, port: int) -> None:
        with socket.create_connection((host, port), timeout=2.0) as sock:
            _kirim(sock, _hello())
            _kirim(sock, _state(seq=2))
            time.sleep(0.15)

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--port", "0", "--wait", "2", "--max-age", "5"],
        on_listener=on_listener,
    )
    assert rc == cli.EXIT_OK, out
    assert "PREFLIGHT_OK" in out
    assert not (tmp_path / NAMA_REQUEST).exists()


# --- 1. waktu hingga ---------------------------------------------------------

@pytest.mark.parametrize("flag", ["--wait", "--max-age", "--state-timeout"])
@pytest.mark.parametrize("nilai", ["nan", "inf", "-inf", "-1", "-0.1"])
def test_waktu_tidak_hingga_tanpa_receiver(flag, nilai, monkeypatch):
    monkeypatch.delenv("ARTI_STARDEW_COMMAND_DIR", raising=False)
    # Bentuk flag=nilai: argparse menelan `--wait -inf` sebagai opsi terpisah.
    rc, out, _ = jalankan_cli(
        [f"{flag}={nilai}", "--execute"],
        receiver_cls=ReceiverPalsu,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "CONFIG_INVALID" in out
    assert ReceiverPalsu.instances == []
    assert "NaN" in out or "infinity" in out or "negatif" in out or "di luar" in out or "hingga" in out


def test_wait_minus_inf_spasi_gagal_sebelum_receiver(monkeypatch):
    monkeypatch.delenv("ARTI_STARDEW_COMMAND_DIR", raising=False)
    with pytest.raises(SystemExit):
        cli.jalankan(
            ["--wait", "-inf", "--execute"],
            receiver_cls=ReceiverPalsu,
            jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
        )
    assert ReceiverPalsu.instances == []


def test_wait_di_atas_max_tanpa_receiver(monkeypatch):
    monkeypatch.delenv("ARTI_STARDEW_COMMAND_DIR", raising=False)
    rc, out, _ = jalankan_cli(
        ["--wait", str(cli.WAKTU_MAX + 1), "--execute"],
        receiver_cls=ReceiverPalsu,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "CONFIG_INVALID" in out
    assert ReceiverPalsu.instances == []


def test_validasi_waktu_batas_sah():
    assert cli.validasi_waktu(0.0, nama="--wait") is None
    assert cli.validasi_waktu(cli.WAKTU_MAX, nama="--wait") is None
    assert cli.validasi_waktu(cli.WAKTU_MAX + 1, nama="--wait") is not None
    assert cli.validasi_waktu(float("nan"), nama="--wait") is not None
    assert cli.validasi_waktu(float("inf"), nama="--max-age") is not None
    assert cli.validasi_waktu(float("-inf"), nama="--state-timeout") is not None
    assert cli.validasi_waktu(-0.01, nama="--wait") is not None


def test_wait_nol_dengan_state_segar_tidak_timeout(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0"],
        receiver_cls=ReceiverPalsu,
        store=store,
    )
    assert rc == cli.EXIT_OK
    assert "PREFLIGHT_OK" in out


# --- 2. pin sesi + hello/state -----------------------------------------------

def test_hello_split_screen_ditolak(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    store.catat(_hello(split=True))
    store.catat(_state())
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0", "--execute"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "SPLIT_SCREEN" in out


def test_hello_nama_tidak_cocok(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    store.catat(_hello(name="bukan-arti"))
    store.catat(_state())
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0", "--execute"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "PLAYER_MISMATCH" in out
    assert "bukan-arti" in out


def test_hello_screen_tidak_cocok(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    store.catat(_hello(screen_id=1))
    store.catat(_state(screen_id=1))
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0", "--execute"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "PLAYER_MISMATCH" in out
    assert "screen" in out


def test_state_lokasi_salah_bukan_preflight_ok(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store, location="Cabin")
    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0"],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=lambda *a, **k: (_ for _ in ()).throw(AssertionError("v1")),
    )
    assert rc == cli.EXIT_REFUSED
    assert "WRONG_LOCATION" in out
    assert "PREFLIGHT_OK" not in out


def _hasil_inspect_cli() -> HasilPerintah:
    fid = f"tf:Farm:{TARGET[0]},{TARGET[1]}:HoeDirt"
    return HasilPerintah(
        status=SELESAI,
        sukses=True,
        request_id="inspect-1",
        outcome="Inspected",
        mentah={
            "op": "inspect_tiles",
            "status": "completed",
            "success": True,
            "outcome": "Inspected",
            "session_id": SESSION_ID,
            "player_id": P2,
            "location_id": "Farm",
            "center_x": FARMER[0],
            "center_y": FARMER[1],
            "radius": RADIUS_INSPEKSI,
            "facts": {
                fid: {
                    "kind": "terrain",
                    "terrain_type": "HoeDirt",
                    "tile": {"x": TARGET[0], "y": TARGET[1]},
                    "passable": True,
                    "crop": {
                        "qualified_item_id": "(O)24",
                        "phase": 1,
                        "dead": False,
                        "watered": False,
                        "ready_for_harvest": False,
                    },
                }
            },
        },
    )


def test_sesi_a_ke_b_saat_startup_tolak_sebelum_v1(tmp_path: Path):
    """Sesi A di preflight, jadi B sebelum reread; v1 asli tidak boleh inspect."""
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    n = {"k": 0}
    inspect_calls = []

    def baca(folder=None):
        n["k"] += 1
        if n["k"] >= 2:
            tulis_sesi(tmp_path, session_id="sesi-B")
        return baca_sesi_asli(folder)

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0", "--json", str(tmp_path / "out.json")],
        receiver_cls=ReceiverPalsu,
        store=store,
        baca_sesi=baca,
        minta_inspect_tiles=lambda *a, **k: inspect_calls.append(a) or _hasil_inspect_cli(),
        minta_water_once=lambda *a, **k: (_ for _ in ()).throw(AssertionError("water")),
    )
    data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert rc == cli.EXIT_REFUSED
    assert inspect_calls == []
    assert data["driver_invoked"] is False
    assert data["session"]["session_id"] == SESSION_ID
    assert data["cli_status"] == SESSION_CHANGED
    assert "sesi-B" in (data["reason"] or "")


def test_sesi_a_ke_b_baca_pertama_v1_tanpa_inspect(tmp_path: Path):
    """Sesi tetap A sampai admission; baca pertama v1 melihat B. v1 asli, store asli."""
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    n = {"k": 0}
    inspect_calls = []
    water_calls = []

    def baca(folder=None):
        n["k"] += 1
        if n["k"] >= 3:
            tulis_sesi(tmp_path, session_id="sesi-B")
        return baca_sesi_asli(folder)

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0", "--json", str(tmp_path / "out.json")],
        receiver_cls=ReceiverPalsu,
        store=store,
        baca_sesi=baca,
        jalankan_v1=jalankan_farm_v1,
        minta_inspect_tiles=lambda *a, **k: inspect_calls.append(a) or _hasil_inspect_cli(),
        minta_water_once=lambda *a, **k: water_calls.append(a) or (_ for _ in ()).throw(AssertionError("water")),
    )
    data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert rc == cli.EXIT_DRIVER
    assert inspect_calls == []
    assert water_calls == []
    assert data["session"]["session_id"] == SESSION_ID
    assert data["driver_invoked"] is True
    assert data["water_confirmed"] is False
    assert data["driver"]["status"] == SESSION_UNREADABLE
    assert "sesi-B" in (data["driver"]["reason"] or data["reason"] or "")
    assert "sesi-cli-v1" in out


def test_sesi_berubah_sesudah_inspect_tolak_water(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    n = {"k": 0}
    inspect_calls = []
    water_calls = []

    def baca(folder=None):
        n["k"] += 1
        if n["k"] >= 4:
            tulis_sesi(tmp_path, session_id="sesi-B")
        return baca_sesi_asli(folder)

    def inspect(*a, **k):
        inspect_calls.append(a)
        store.catat(_state(seq=3))
        return _hasil_inspect_cli()

    rc, out, _ = jalankan_cli(
        [
            "--folder", str(tmp_path),
            "--execute",
            "--wait", "0",
            "--max-age", "5",
            "--state-timeout", "1",
            "--json", str(tmp_path / "out.json"),
        ],
        receiver_cls=ReceiverPalsu,
        store=store,
        baca_sesi=baca,
        jalankan_v1=jalankan_farm_v1,
        minta_inspect_tiles=inspect,
        minta_water_once=lambda *a, **k: water_calls.append(a) or (_ for _ in ()).throw(AssertionError("water")),
    )
    data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert len(inspect_calls) == 1
    assert water_calls == []
    assert data["session"]["session_id"] == SESSION_ID
    assert data["water_confirmed"] is False
    assert data["command_attempted"] is True
    assert data["driver"]["status"] == SESSION_UNREADABLE
    assert rc == cli.EXIT_DRIVER


# --- 3. driver vs command vs ObservedWatered + cleanup -----------------------

def test_json_gagal_setelah_no_target_tidak_klaim_aksi(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    inspect_calls = []
    water_calls = []

    def inspect(*a, **k):
        inspect_calls.append(a)
        hasil = _hasil_inspect_cli()
        mentah = dict(hasil.mentah)
        mentah["facts"] = {}
        return HasilPerintah(
            status=hasil.status,
            sukses=hasil.sukses,
            request_id=hasil.request_id,
            outcome=hasil.outcome,
            mentah=mentah,
        )

    rc, out, _ = jalankan_cli(
        ["--folder", str(tmp_path), "--execute", "--wait", "0", "--json", str(tmp_path / "x.json")],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=jalankan_farm_v1,
        minta_inspect_tiles=inspect,
        minta_water_once=lambda *a, **k: water_calls.append(a) or (_ for _ in ()).throw(AssertionError("water")),
        tulis_json=lambda p, o: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert len(inspect_calls) == 1
    assert water_calls == []
    assert rc == cli.EXIT_OUTPUT
    assert "NO_TARGET" in out
    assert "aksi sudah terjadi" not in out
    assert "ObservedWatered tercatat" not in out
    assert "TIDAK diulang" in out


def test_json_gagal_setelah_water_uncertain_tetap_uncertain(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    inspect_calls = []
    water_calls = []

    def inspect(*a, **k):
        inspect_calls.append(a)
        store.catat(_state(seq=3))
        return _hasil_inspect_cli()

    def water(*a, **k):
        water_calls.append(a)
        return HasilPerintah(
            status=TIDAK_PASTI,
            sukses=False,
            request_id="water-1",
            outcome="Unknown",
            alasan="tidak_pasti",
        )

    rc, out, _ = jalankan_cli(
        [
            "--folder", str(tmp_path),
            "--execute",
            "--wait", "0",
            "--max-age", "5",
            "--state-timeout", "1",
            "--json", str(tmp_path / "x.json"),
        ],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=jalankan_farm_v1,
        minta_inspect_tiles=inspect,
        minta_water_once=water,
        tulis_json=lambda p, o: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert len(inspect_calls) == 1
    assert len(water_calls) == 1
    assert rc == cli.EXIT_OUTPUT
    assert "WATER_UNCERTAIN" in out
    assert "aksi sudah terjadi" not in out
    assert "ObservedWatered tercatat" not in out
    assert "TIDAK diulang" in out


def test_interrupt_saat_water_attempt_unknown_tanpa_retry(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    inspect_calls = []
    water_calls = []

    def inspect(*a, **k):
        inspect_calls.append(a)
        store.catat(_state(seq=3))
        return _hasil_inspect_cli()

    def water(*a, **k):
        water_calls.append(a)
        raise KeyboardInterrupt()

    rc, out, _ = jalankan_cli(
        [
            "--folder", str(tmp_path),
            "--execute",
            "--wait", "0",
            "--max-age", "5",
            "--json", str(tmp_path / "out.json"),
        ],
        receiver_cls=ReceiverPalsu,
        store=store,
        jalankan_v1=jalankan_farm_v1,
        minta_inspect_tiles=inspect,
        minta_water_once=water,
    )
    data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert rc == cli.EXIT_DRIVER
    assert len(inspect_calls) == 1
    assert len(water_calls) == 1
    assert data["driver_invoked"] is True
    assert data["command_attempted"] is True
    assert data["water_confirmed"] is False
    assert data["cli_status"] == "CANCELLED"
    ops = data["command_calls"]
    assert ops[0]["op"] == "inspect_tiles"
    assert ops[0]["returned"] is True
    assert ops[0]["request_id"] == "inspect-1"
    water_jejak = ops[1]
    assert water_jejak["op"] == "water_once"
    assert water_jejak["returned"] is False
    assert water_jejak["request_id"] is None
    assert water_jejak["outcome"] is None
    assert data["session"]["session_id"] == SESSION_ID


class ReceiverStopGagal(ReceiverPalsu):
    def stop(self) -> None:
        self.stops += 1
        raise RuntimeError("stop boom")


def test_stop_gagal_tidak_lapor_stopped_true(tmp_path: Path):
    tulis_sesi(tmp_path)
    store = StardewStateStore()
    isi_store(store)
    tujuan = tmp_path / "out.json"
    rc, out, err = jalankan_cli(
        ["--folder", str(tmp_path), "--wait", "0", "--json", str(tujuan)],
        receiver_cls=ReceiverStopGagal,
        store=store,
    )
    data = json.loads(tujuan.read_text(encoding="utf-8"))
    assert rc == cli.EXIT_OK
    assert data["receiver"]["stopped"] is False
    assert data["receiver"]["stop_error"] is not None
    assert "stop boom" in data["receiver"]["stop_error"]
    assert "receiver_stopped=false" in out
    assert ReceiverStopGagal.instances[0].stops == 1

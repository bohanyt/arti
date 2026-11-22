"""Kontrak NDJSON localhost untuk mata Stardew read-only."""

from __future__ import annotations

import errno
import io
import json
import socket
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from arti_stardew import MAX_LINE_BYTES, SequenceGate, StardewReceiver, decode_event


ROOT = Path(__file__).resolve().parents[1]


def _hello(*, seq: int = 1, screen_id: int = 1) -> dict:
    return {
        "protocol": 1,
        "ev": "hello",
        "seq": seq,
        "player_id": 100000000012,
        "screen_id": screen_id,
        "player_name": "Arti",
        "is_main_player": False,
        "is_split_screen": True,
    }


def _payload_state(**timpa: object) -> dict:
    """Payload state LENGKAP sesuai skema Gate 5.

    Dulu tes ini memakai payload seadanya (`location` + `tile` saja) karena
    `state` belum punya skema sama sekali. Sejak Gate 5 payload divalidasi
    ketat -- field wajib yang hilang DITOLAK -- jadi contohnya harus lengkap.
    Maksud tesnya tidak berubah: yang diuji ketahanan koneksi terhadap
    malformed/duplicate/stale/reconnect, bukan kelonggaran payload.
    """
    payload = {
        "location": "Farm",
        "tile": {"x": 42, "y": 17},
        "pixel": {"x": 2688.0, "y": 1088.0},
        "facing": 2,
        "time_of_day": 610,
        "date": {"season": "spring", "day": 1, "year": 1},
        "energy": 270.0,
        "health": 100,
        "is_player_free": True,
        "can_player_move": True,
        "active_menu": None,
    }
    payload.update(timpa)
    return payload


def _send_lines(address: tuple[str, int], *messages: object) -> None:
    with socket.create_connection(address, timeout=2.0) as client:
        for message in messages:
            if isinstance(message, str):
                line = message
            else:
                line = json.dumps(message, separators=(",", ":"))
            client.sendall(line.encode("utf-8") + b"\n")


def test_decode_event_accepts_versioned_hello_identity():
    assert decode_event(json.dumps(_hello())) == _hello()


def test_decode_event_rejects_malformed_or_incomplete_envelopes():
    invalid = [
        "",
        "bukan json",
        "[]",
        json.dumps({**_hello(), "protocol": 2}),
        json.dumps({k: v for k, v in _hello().items() if k != "player_id"}),
        json.dumps({**_hello(), "seq": True}),
        json.dumps({**_hello(), "screen_id": -1}),
        json.dumps({**_hello(), "player_name": ""}),
        json.dumps({**_hello(), "payload": []}),
    ]

    assert all(decode_event(line) is None for line in invalid)


def test_sequence_gate_rejects_duplicate_and_stale_per_player_screen():
    gate = SequenceGate()

    assert gate.accept(_hello(seq=10))
    assert not gate.accept(_hello(seq=10))
    assert not gate.accept(_hello(seq=9))
    assert gate.accept(_hello(seq=11))
    assert gate.accept(_hello(seq=1, screen_id=2))


def test_receiver_survives_malformed_duplicate_stale_and_reconnect():
    """Kontrak per-koneksi, bukan urutan callback global.

    Receiver memakai ThreadingTCPServer: dua socket bisa di-handle bersamaan.
    Tes ini tidak boleh mengirim reconnect sebelum sesi pertama selesai
    dicatat, lalu menuntut FIFO global `[hello, state, hello]`. Itu bukan
    kontrak protokol; di CI yang sibuk hello epoch baru bisa tercatat
    sebelum state sesi lama (`[hello, hello, state]`).
    """
    received: list[dict] = []
    kunci = threading.Lock()
    sesi_pertama = threading.Event()
    sesi_reconnect = threading.Event()

    def on_event(event: dict) -> None:
        with kunci:
            received.append(event)
            n = len(received)
        if n == 2:
            sesi_pertama.set()
        elif n == 3:
            sesi_reconnect.set()

    receiver = StardewReceiver(on_event=on_event, port=0)
    receiver.start()
    try:
        assert receiver.address[0] == "127.0.0.1"
        _send_lines(
            receiver.address,
            "bukan json",
            _hello(seq=1),
            _hello(seq=1),
            _hello(seq=0),
            {
                **_hello(seq=2),
                "ev": "state",
                "payload": _payload_state(),
            },
        )
        assert sesi_pertama.wait(timeout=2.0), "hello+state sesi pertama tidak sampai"
        with kunci:
            assert [event["ev"] for event in received] == ["hello", "state"]

        # Koneksi baru adalah epoch baru: mod boleh mengirim hello dari seq 1
        # lagi setelah restart/reconnect tanpa dianggap stale selamanya.
        _send_lines(receiver.address, _hello(seq=1))
        assert sesi_reconnect.wait(timeout=2.0), "hello reconnect tidak sampai"
        with kunci:
            assert [event["ev"] for event in received] == ["hello", "state", "hello"]
    finally:
        receiver.stop()


def test_receiver_rejects_second_listener_on_same_port():
    first = StardewReceiver(on_event=lambda event: None, port=0)
    first.start()
    second = StardewReceiver(on_event=lambda event: None, port=first.address[1])
    try:
        with pytest.raises(OSError):
            second.start()
    finally:
        second.stop()
        first.stop()


@pytest.fixture
def synthetic_receiver():
    """Real handler/server lifecycle; injected reads avoid OS-dependent RST timing."""
    received = []
    receiver = StardewReceiver(on_event=received.append, port=0)
    receiver.start()
    try:
        yield receiver, received
    finally:
        receiver.stop()


def _run_reader(receiver, *reads):
    reader = Mock(spec=io.BufferedReader)
    # EOF stays EOF, including a second read after draining an oversized line.
    pending = iter(reads)

    def readline(limit):
        assert limit == MAX_LINE_BYTES + 1
        result = next(pending, b"")
        if isinstance(result, Exception):
            raise result
        return result

    reader.readline.side_effect = readline
    request = Mock(spec=socket.socket)
    request.makefile.return_value = reader
    try:
        receiver._server.finish_request(request, ("127.0.0.1", 0))
    finally:
        reader.close.assert_called_once()


@pytest.mark.parametrize("disconnect", [
    pytest.param(b"", id="eof"),
    pytest.param(ConnectionResetError("peer reset"), id="reset"),
    pytest.param(OSError(errno.ECONNRESET, "peer reset"), id="os-reset"),
])
@pytest.mark.parametrize("read_position", ["first", "after_hello", "oversize_drain"])
def test_receiver_expected_disconnect_is_quiet_and_next_client_works(
    synthetic_receiver, capsys, disconnect, read_position,
):
    """Expected disconnect is quiet; it cannot poison the next connection's gate."""
    receiver, received = synthetic_receiver
    hello = _hello()
    wire = json.dumps(hello).encode() + b"\n"
    prefix = {
        "first": [],
        "after_hello": [wire],
        "oversize_drain": [b"x" * (MAX_LINE_BYTES + 1)],
    }[read_position]
    _run_reader(receiver, *prefix, disconnect)
    assert received == ([hello] if read_position == "after_hello" else [])

    # A fresh request invokes the same real handler with an independent gate.
    before = len(received)
    _run_reader(receiver, wire)
    assert received[before:] == [hello]
    captured = capsys.readouterr()
    assert captured.out == ""  # No fabricated callback failure.
    assert captured.err == ""  # No unhandled socketserver traceback.


@pytest.mark.parametrize("error", [
    pytest.param(RuntimeError("boom"), id="programming-error"),
    pytest.param(OSError(errno.EIO, "boom"), id="unrelated-os-error"),
])
@pytest.mark.parametrize("draining", [False, True], ids=["main_read", "drain_read"])
def test_receiver_unexpected_read_failure_remains_visible(
    synthetic_receiver, error, draining,
):
    receiver, received = synthetic_receiver
    prefix = [b"x" * (MAX_LINE_BYTES + 1)] if draining else []
    with pytest.raises(type(error), match="boom") as raised:
        _run_reader(receiver, *prefix, error)
    assert raised.value is error
    assert received == []


def test_receiver_decoder_exception_is_not_treated_as_disconnect(
    synthetic_receiver, monkeypatch,
):
    receiver, received = synthetic_receiver
    error = ConnectionResetError("decoder bug")
    monkeypatch.setattr("arti_stardew.decode_event", Mock(side_effect=error))
    with pytest.raises(ConnectionResetError, match="decoder bug") as raised:
        _run_reader(receiver, b"{}\n")
    assert raised.value is error
    assert received == []


@pytest.mark.parametrize("error_type", [RuntimeError, ConnectionResetError])
def test_receiver_callback_failure_stays_logged_and_connection_continues(
    synthetic_receiver, capsys, error_type,
):
    receiver, received = synthetic_receiver

    def on_event(event):
        received.append(event)
        if len(received) == 1:
            raise error_type("callback bug")

    receiver._on_event = on_event
    events = [_hello(seq=1), _hello(seq=2)]
    _run_reader(receiver, *(json.dumps(event).encode() + b"\n" for event in events))
    assert received == events
    captured = capsys.readouterr()
    assert captured.out == f"[Stardew] callback gagal: {error_type.__name__}: callback bug\n"
    assert captured.err == ""


def test_receiver_malformed_observation_stays_rejected_without_consuming_sequence(
    synthetic_receiver, capsys,
):
    receiver, received = synthetic_receiver
    invalid = {**_hello(seq=2), "ev": "observation", "payload": {}}
    valid = {**_hello(seq=2), "ev": "state", "payload": _payload_state()}
    assert decode_event(json.dumps(invalid)) is None
    _run_reader(
        receiver,
        json.dumps(_hello()).encode() + b"\n",
        b"\n",
        b"malformed json\n",
        json.dumps(invalid).encode() + b"\n",
        json.dumps(valid).encode() + b"\n",
    )
    assert received == [_hello(), valid]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_observer_csharp_memagari_koleksi_clickable_dan_response_yang_null():
    source = (
        ROOT / "stardew-mod" / "ArtiStardewBridge" / "ModEntry.cs"
    ).read_text(encoding="utf-8")

    assert "IEnumerable<ClickableComponent>? source" in source
    assert "if (source is null)" in source
    assert "ClickableComponent? component" in source
    assert "if (component is null)" in source
    assert "List<ClickableComponent>? responseComponents = dialogue.responseCC" in source
    assert "if (responseComponents is null)" in source

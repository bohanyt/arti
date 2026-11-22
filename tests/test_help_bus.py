"""Fake-only arti.help/1 contract tests; no bridge, devices or network."""

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

import arti_help_bus as bus


@pytest.fixture
def help_request():
    return bus.HelpRequest(
        request_id="a" * 32, session_id="session", reason="no_progress_frozen_clock",
        game_date="Y1 Spring 3", game_time=910, location="Farm", skill=None,
        detail="6 boundaries, clock frozen at 0910", created_at=1000.0,
        expires_at=1600.0,
    )


def test_request_round_trip_and_frozen(help_request):
    assert bus.decode_request(bus.encode_request(help_request)) == help_request
    assert bus.decode_request(bus.encode_request(help_request).encode()) == help_request
    assert bus.decode_request(bus.encode_request(replace(
        help_request, game_time=None, location=None, skill="jalan",
    ))).skill == "jalan"
    with pytest.raises(FrozenInstanceError):
        help_request.reason = "changed"


@pytest.mark.parametrize("status", ["delivered", "hinted", "suppressed", "expired"])
def test_reply_round_trip(help_request, status):
    reply = bus.HelpReply(request_id=help_request.request_id, status=status, spoken=True,
                          hints=(bus.HelpHint("owner", "Jalan utara", 1001.0),
                                 bus.HelpHint("viewer", "Coba tangga", 1002.0)))
    assert bus.decode_reply(bus.encode_reply(reply)) == reply


@pytest.mark.parametrize("field,value", [
    ("v", 2), ("v", True), ("v", "1"), ("request_id", "../bad"),
    ("request_id", "A" * 32), ("session_id", 1), ("session_id", "x" * 129),
    ("reason", "x" * 97), ("game_date", "x" * 65), ("detail", "x" * 161),
    ("detail", ["text"]), ("detail", "bad\x00text"), ("location", "x" * 129),
    ("location", 7), ("skill", "x" * 97), ("game_time", "910"),
    ("game_time", True), ("game_time", 960), ("game_time", 2700),
    ("created_at", True), ("created_at", "1000"), ("created_at", -1),
    ("created_at", 10 ** 500),
    ("expires_at", float("inf")), ("created_at", float("nan")),
    ("expires_at", 999.0),
], ids=lambda value: type(value).__name__)
def test_request_rejects_bad_fields(help_request, field, value):
    obj = json.loads(bus.encode_request(help_request))
    obj[field] = value
    with pytest.raises(ValueError):
        bus.decode_request(json.dumps(obj))


@pytest.mark.parametrize("payload", ["[]", "null", "1", "{bad", b"\xff", " " * 32769],
                         ids=["array", "null", "number", "partial", "utf8", "oversize"])
def test_rejects_bad_json(payload):
    with pytest.raises(ValueError):
        bus.decode_request(payload)


def test_strict_field_set_and_duplicate_keys(help_request):
    obj = json.loads(bus.encode_request(help_request))
    obj["viewer"] = "not part of contract"
    with pytest.raises(ValueError):
        bus.decode_request(json.dumps(obj))
    del obj["viewer"]
    del obj["v"]
    with pytest.raises(ValueError):
        bus.decode_request(json.dumps(obj))
    with pytest.raises(ValueError):
        bus.decode_request(bus.encode_request(help_request).replace('"v": 1', '"v": 1, "v": 1'))


@pytest.mark.parametrize("changes", [
    {"v": 2}, {"spoken": 1}, {"status": "ok"}, {"hints": {}},
    {"hints": [{"source": "viewer", "text": "x" * 201, "at": 1}]},
    {"hints": [{"source": "bot", "text": "saran", "at": 1}]},
    {"hints": [{"source": "owner", "text": "saran", "at": True}]},
    {"hints": [{"source": "viewer", "text": "saran", "at": 1, "viewer": "private"}]},
    {"hints": [None]},
    {"hints": [{"source": "viewer", "text": "saran", "at": 1}] * 65},
], ids=lambda value: type(value).__name__)
def test_reply_rejects_bad_fields(help_request, changes):
    obj = dict(v=1, request_id=help_request.request_id, status="delivered", spoken=False, hints=[])
    obj.update(changes)
    with pytest.raises(ValueError):
        bus.decode_reply(json.dumps(obj))


@pytest.mark.parametrize("raw,expected", [
    ("  Coba\n jalan\tutara  ", "Coba jalan utara"),
    ("lihat https://example.org/path?q=1 @orang lalu www.example.org", "lihat lalu"),
    ("coba example.org/path dan ftp://example.org", "coba dan"),
    ("a\x00b\u202ec\u200bd", "abcd"),
    ("@orang saja @lain", "saja"),
    ("x" * 201, "x" * 200),
    ("  ", ""),
], ids=lambda value: type(value).__name__)
def test_sanitizer(raw, expected):
    assert bus.sanitize_hint_text(raw) == expected
    assert bus.sanitize_hint_text(expected) == expected


def test_atomic_files_and_pending_contract(tmp_path, help_request, monkeypatch):
    channel = bus.FileHelpChannel(tmp_path / "help")
    assert channel.pending_requests() == ()
    replacements = []
    original_replace = bus.ipc._posix_replace

    def observe_replace(source, destination):
        assert Path(source).parent == Path(destination).parent
        assert Path(source).suffix == ".tmp"
        assert json.loads(Path(source).read_text(encoding="utf-8"))["request_id"] == help_request.request_id
        # Existing readers see the old complete document until replace.
        if Path(destination).exists():
            assert channel.read_request(help_request.request_id) == help_request
        replacements.append(Path(destination).name)
        original_replace(source, destination)

    monkeypatch.setattr(bus.ipc, "_posix_replace", observe_replace)
    channel.write_request(help_request)
    assert channel.list_pending_requests() == (help_request.request_id,)
    assert channel.pending_requests() == (help_request,)
    channel.write_request(help_request)
    reply = bus.HelpReply(request_id=help_request.request_id, status="delivered", spoken=False, hints=())
    channel.write_reply(reply)
    assert channel.read_reply(help_request.request_id) == reply
    assert channel.pending_requests() == ()
    assert replacements == [f"req-{help_request.request_id}.json"] * 2 + [f"rep-{help_request.request_id}.json"]
    assert not tuple(channel.help_dir.glob("*.tmp"))


def test_failed_replace_cleans_temporary_file(tmp_path, help_request, monkeypatch):
    channel = bus.FileHelpChannel(tmp_path)

    def fail(*_args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(bus.ipc, "_posix_replace", fail)
    with pytest.raises(OSError):
        channel.write_request(help_request)
    assert list(tmp_path.iterdir()) == []


def test_pending_ignores_malformed_files_and_checks_id(tmp_path, help_request):
    channel = bus.FileHelpChannel(tmp_path)
    (tmp_path / ("req-" + "b" * 32 + ".json")).write_text(bus.encode_request(help_request))
    (tmp_path / ("req-" + "c" * 32 + ".json")).write_text("{partial")
    (tmp_path / "req-invalid.json").write_text("{}")
    (tmp_path / ".help-partial.tmp").write_text("{partial")
    channel.write_request(help_request)
    assert channel.pending_requests() == (help_request,)
    with pytest.raises(ValueError):
        channel.read_request("b" * 32)
    with pytest.raises(ValueError):
        channel.read_request("../outside")
    assert channel.read_reply(help_request.request_id) is None

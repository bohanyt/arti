"""Fake VTS only: auth, startup recovery and ARTI/HOST isolation."""
import asyncio
import json

import pytest

import arti_vts_auth as auth
import arti_bridge as b


class FakeVTS:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.sent = []
        self.inbox = asyncio.Queue()
        self.closed = False

    async def send(self, raw):
        payload = json.loads(raw)
        self.sent.append(payload)
        kind = payload["messageType"]
        reply = self.replies.get(kind, {
            "AuthenticationTokenRequest": {"data": {"authenticationToken": "fake-new"}},
            "AuthenticationRequest": {"data": {"authenticated": True}},
            "CurrentModelRequest": {"data": {"modelLoaded": True, "modelName": "ARTI"}},
        }.get(kind, {"data": {}}))
        if callable(reply):
            reply = reply(payload)
        if reply is not None:
            await self.inbox.put(json.dumps({**reply, "requestID": payload["requestID"]}))

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        self.closed = True


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(b, "__file__", str(tmp_path / "arti_bridge.py"))
    monkeypatch.setitem(b.CONFIG, "vts_api_port", 8002)
    monkeypatch.setitem(b.CONFIG, "vts_expected_model_name", "")
    monkeypatch.setattr(b, "_idle_ws_lock", None)
    monkeypatch.setattr(b, "_idle_model_warning", False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_token_request_waits_for_human_only(setup, monkeypatch, capsys):
    async def scenario():
        ws = FakeVTS({"AuthenticationTokenRequest": None})
        async def connect(*args, **kwargs):
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        waits = []
        original = asyncio.wait_for
        async def wait_for(fut, timeout):
            waits.append(timeout)
            return await original(fut, timeout)
        monkeypatch.setattr(b.asyncio, "wait_for", wait_for)
        c = b.VTSController()
        task = asyncio.create_task(c.connect())
        try:
            # Real fake-server delay beyond the old 3-second deadline.
            await asyncio.sleep(3.1)
            assert not task.done()
            request = ws.sent[0]
            await ws.inbox.put(json.dumps({"requestID": request["requestID"],
                                          "data": {"authenticationToken": "fake-new"}}))
            await task
            assert c._auth_ready.is_set()
            assert waits == [60.0, 3.0]
        finally:
            await c.close()
    asyncio.run(scenario())
    assert capsys.readouterr().out.count("ALLOW") == 1


@pytest.mark.parametrize("reply", [
    {"messageType": "APIError", "data": {"errorID": 50}},
    {"data": {"errorID": 51}},
    {"data": {}},
])
def test_token_error_fails_cleanly(setup, monkeypatch, reply, capsys):
    async def scenario():
        ws = FakeVTS({"AuthenticationTokenRequest": reply})
        async def connect(*args, **kwargs):
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        await c.connect()
        assert c._conn_lost and not c._auth_ready.is_set()
        assert c.websocket is None and ws.closed
        assert not c._pending
        assert not c.token_file.exists()
        await c.close()
    asyncio.run(scenario())
    assert "KeyError" not in capsys.readouterr().out


@pytest.mark.parametrize("rejection", [
    {"data": {"authenticated": False}},
    {"messageType": "APIError", "data": {"errorID": 50}},
    {"data": {"errorID": 100}},
])
def test_invalid_token_is_replaced_without_deleting_other_port(setup, monkeypatch, rejection):
    path = auth.token_path(b.__file__, 8002)
    path.write_text("fake-old")
    other = auth.token_path(b.__file__, 8003)
    other.write_text("fake-host")
    async def scenario():
        ws = FakeVTS({"AuthenticationRequest": lambda p: rejection
                      if p["data"]["authenticationToken"] == "fake-old"
                      else {"data": {"authenticated": True}}})
        async def connect(*args, **kwargs):
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        try:
            await c.connect()
            assert c._auth_ready.is_set()
            assert [p["messageType"] for p in ws.sent] == [
                "AuthenticationRequest", "AuthenticationTokenRequest", "AuthenticationRequest"]
            assert path.read_text() == "fake-new"
            assert other.read_text() == "fake-host"
        finally:
            await c.close()
    asyncio.run(scenario())


def test_transient_auth_error_preserves_token(setup, monkeypatch):
    path = auth.token_path(b.__file__, 8002)
    path.write_text("fake-old")
    async def scenario():
        ws = FakeVTS({"AuthenticationRequest": {"data": {"errorID": 1}}})
        async def connect(*args, **kwargs):
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        await c.connect()
        assert c._conn_lost
        assert path.read_text() == "fake-old"
        assert len(ws.sent) == 1
        await c.close()
    asyncio.run(scenario())


def test_absolute_paths_legacy_main_only_and_idle_share_resolution(setup, monkeypatch):
    (setup / "vts_token.txt").write_text("fake-legacy")
    elsewhere = setup / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert auth.read_token(b.__file__, 8003, main_port=8002) is None
    async def scenario():
        sockets = []
        async def connect(*args, **kwargs):
            ws = FakeVTS()
            sockets.append(ws)
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        monkeypatch.setattr(b, "vts", c)
        try:
            assert c.token_file == setup / "vts_token_8002.txt"
            await c.connect()
            ws = await b._idle_connect_ws(max_attempts=1)
            assert ws is not None
            assert sockets[1].sent[0]["data"]["authenticationToken"] == "fake-legacy"
            assert c.token_file.read_text() == "fake-legacy"
            assert (setup / "vts_token.txt").read_text() == "fake-legacy"
        finally:
            await c.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("first_failure", ["connect", "auth"])
def test_background_recovers_first_failure_without_expression_trigger(setup, monkeypatch, first_failure):
    async def scenario():
        calls = []
        async def connect(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                if first_failure == "connect":
                    raise OSError("not open yet")
                return FakeVTS({"AuthenticationRequest": {"data": {"errorID": 1}}})
            return FakeVTS()
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        await c.connect()
        assert c._conn_lost
        c._reconnect_delay = 0.01
        c.start_reconnect()
        retry_task = c._reconnect_task
        c.start_reconnect()
        assert c._reconnect_task is retry_task
        try:
            async with asyncio.timeout(1):
                while not c._auth_ready.is_set():
                    await asyncio.sleep(0.01)
            assert len(calls) == 2
        finally:
            await c.close()
        assert retry_task.done()
        assert c.websocket is None and not c._auth_ready.is_set()
    asyncio.run(scenario())


def test_idle_waits_without_connecting_or_reading_missing_file(setup, monkeypatch):
    async def scenario():
        c = b.VTSController()
        monkeypatch.setattr(b, "vts", c)
        calls = []
        async def connect(*args, **kwargs):
            calls.append(1)
            return FakeVTS()
        monkeypatch.setattr(b.websockets, "connect", connect)
        task = asyncio.create_task(b._idle_connect_ws(max_attempts=1))
        try:
            await asyncio.sleep(0.06)
            assert not calls and not task.done()
            c.token_file.write_text("fake-ready")
            c._auth_ready.set()
            ws = await asyncio.wait_for(task, 1)
            assert ws is not None and len(calls) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("model", [
    {"modelLoaded": True, "modelName": "HOST"},
    {"modelLoaded": False, "modelName": "ARTI"},
    {},
])
def test_wrong_main_model_never_drives_and_warns_once(setup, monkeypatch, capsys, model):
    monkeypatch.setitem(b.CONFIG, "vts_expected_model_name", "ARTI")
    async def scenario():
        sockets = []
        async def connect(*args, **kwargs):
            ws = FakeVTS({"CurrentModelRequest": {"data": model}})
            sockets.append(ws)
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        for _ in range(2):
            await c.connect()
            await c.send_expression("ArtiBicara.exp3.json", True)
            await c.inject_parameter_data([{"id": "FaceAngleY", "value": 1}])
            assert not c._auth_ready.is_set() and c.websocket is None
        assert all(p["messageType"] in {"AuthenticationTokenRequest", "AuthenticationRequest",
                                           "CurrentModelRequest"} for ws in sockets for p in ws.sent)
        await c.close()
    asyncio.run(scenario())
    assert capsys.readouterr().out.count("MODEL GUARD") == 1


def test_idle_checks_own_model_before_cleanup_or_hotkeys(setup, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "vts_expected_model_name", "ARTI")
    async def scenario():
        main = b.VTSController()
        main.token_file.write_text("fake-ready")
        main._auth_ready.set()
        monkeypatch.setattr(b, "vts", main)
        ws = FakeVTS({"CurrentModelRequest": {"data": {"modelLoaded": True, "modelName": "HOST"}}})
        async def connect(*args, **kwargs):
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        assert await b._idle_connect_ws(max_attempts=1) is None
        assert ws.closed
        assert [p["messageType"] for p in ws.sent] == ["AuthenticationRequest", "CurrentModelRequest"]
    asyncio.run(scenario())


def test_expected_model_allows_main_and_idle(setup, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "vts_expected_model_name", "ARTI")
    async def scenario():
        async def connect(*args, **kwargs):
            return FakeVTS()
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        monkeypatch.setattr(b, "vts", c)
        try:
            await c.connect()
            assert c._auth_ready.is_set()
            idle = await b._idle_connect_ws(max_attempts=1)
            assert idle is not None
            await c.send_expression("ArtiBicara.exp3.json", True)
            assert c.websocket.sent[-1]["messageType"] == "ExpressionActivationRequest"
        finally:
            await c.close()
    asyncio.run(scenario())


def test_missing_socket_expression_uses_existing_reconnect(setup, monkeypatch):
    async def scenario():
        c = b.VTSController()
        async def connect(*args, **kwargs):
            return FakeVTS()
        monkeypatch.setattr(b.websockets, "connect", connect)
        try:
            await c.trigger_expression_state("mikir")
            assert c._auth_ready.is_set()
            assert any(p["messageType"] == "ExpressionActivationRequest" for p in c.websocket.sent)
        finally:
            await c.close()
    asyncio.run(scenario())


def test_reconnect_backoff_caps_and_resets(setup, monkeypatch):
    async def scenario():
        async def down(*args, **kwargs):
            raise OSError("not open yet")
        monkeypatch.setattr(b.websockets, "connect", down)
        c = b.VTSController()
        await c.connect()
        delays = []
        for _ in range(5):
            c._last_reconnect_attempt = 0
            assert await c.ensure_connected() is False
            delays.append(c._reconnect_delay)
            assert await c.ensure_connected() is False
        assert delays == [10, 20, 40, 60, 60]
        async def up(*args, **kwargs):
            return FakeVTS()
        monkeypatch.setattr(b.websockets, "connect", up)
        c._last_reconnect_attempt = 0
        try:
            assert await c.ensure_connected() is True
            assert c._reconnect_delay == 5
        finally:
            await c.close()
    asyncio.run(scenario())


def test_background_auth_never_blocks_expression_or_overlaps_connect(setup, monkeypatch):
    async def scenario():
        c = b.VTSController()
        calls = []
        ws = FakeVTS({"AuthenticationTokenRequest": None})
        async def connect(*args, **kwargs):
            calls.append(1)
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c._reconnect_delay = 0.01
        c.start_reconnect()
        task = c._reconnect_task
        try:
            async with asyncio.timeout(1):
                while not ws.sent:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(c.trigger_expression_state("bicara"), 0.1)
            await c.send_expression("ArtiBicara.exp3.json", True)
            assert len(calls) == 1
            assert [p["messageType"] for p in ws.sent] == ["AuthenticationTokenRequest"]
        finally:
            await c.close()
        assert task.done() and ws.closed and c.websocket is None
    asyncio.run(scenario())


def test_wrong_model_recovers_periodically_when_arti_loaded(setup, monkeypatch):
    monkeypatch.setitem(b.CONFIG, "vts_expected_model_name", "ARTI")
    async def scenario():
        sockets = []
        async def connect(*args, **kwargs):
            ws = FakeVTS({"CurrentModelRequest": {"data": {
                "modelLoaded": True, "modelName": "HOST" if not sockets else "ARTI"}}})
            sockets.append(ws)
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        await c.connect()
        assert not c._auth_ready.is_set()
        c._reconnect_delay = 0.01
        c.start_reconnect()
        try:
            async with asyncio.timeout(1):
                while not c._auth_ready.is_set():
                    await asyncio.sleep(0.01)
            assert len(sockets) == 2 and sockets[0].closed
        finally:
            await c.close()
    asyncio.run(scenario())


def test_replacement_rejected_is_bounded_and_keeps_disk_token(setup, monkeypatch):
    path = auth.token_path(b.__file__, 8002)
    path.write_text("fake-old")
    async def scenario():
        ws = FakeVTS({"AuthenticationRequest": {"data": {"authenticated": False}}})
        async def connect(*args, **kwargs):
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        await c.connect()
        assert c._conn_lost and not c._auth_ready.is_set()
        assert [p["messageType"] for p in ws.sent].count("AuthenticationTokenRequest") == 1
        assert path.read_text() == "fake-old"
        await c.close()
    asyncio.run(scenario())


def test_send_failure_cleans_pending_request(setup):
    async def scenario():
        class BrokenWS(FakeVTS):
            async def send(self, raw):
                raise OSError("gone")
        c = b.VTSController()
        c.websocket = BrokenWS()
        with pytest.raises(OSError):
            await c.send_request("AuthenticationRequest")
        assert not c._pending
        await c.close()
    asyncio.run(scenario())


def test_revoked_open_connection_reauthenticates_in_background(setup, monkeypatch):
    async def scenario():
        sockets = []
        async def connect(*args, **kwargs):
            ws = FakeVTS()
            sockets.append(ws)
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        c = b.VTSController()
        await c.connect()
        # Revocation on a fire-and-forget request must also be observed.
        await sockets[0].inbox.put(json.dumps({"messageType": "APIError",
            "requestID": "unconfirmed-expression", "data": {"errorID": 8}}))
        await asyncio.sleep(0)
        assert c._conn_lost and not c._auth_ready.is_set()
        c._reconnect_delay = 0.01
        c.start_reconnect()
        try:
            async with asyncio.timeout(1):
                while not c._auth_ready.is_set():
                    await asyncio.sleep(0.01)
            assert sockets[0].closed and len(sockets) == 2
            assert sockets[1].sent[0]["messageType"] == "AuthenticationTokenRequest"
        finally:
            await c.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("rejection", [
    {"data": {"authenticated": False}},
    {"messageType": "APIError", "data": {"errorID": 8}},
])
def test_idle_auth_rejection_hands_renewal_back_to_main(setup, monkeypatch, rejection):
    async def scenario():
        main = b.VTSController()
        main.token_file.write_text("fake-ready")
        main._auth_ready.set()
        monkeypatch.setattr(b, "vts", main)
        ws = FakeVTS({"AuthenticationRequest": rejection})
        async def connect(*args, **kwargs):
            return ws
        monkeypatch.setattr(b.websockets, "connect", connect)
        assert await b._idle_connect_ws(max_attempts=1) is None
        assert main._conn_lost and not main._auth_ready.is_set()
        assert main.token_file.read_text() == "fake-ready" and ws.closed
    asyncio.run(scenario())

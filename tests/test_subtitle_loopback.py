"""Owner 2026-11-19 (publish audit W1): the subtitle WebSocket listens on this machine only.

OBS runs on the same laptop as ARTI. Opening the listener to the network is one explicit config key, off by default,
and the bridge says so in one plain line when it is on. The OBS browser source (`subtitle.html`) connects to
ws://localhost:<port>, and that still works: a real handshake on loopback below.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import websockets

import subtitle_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("config, host", [
    (None, "127.0.0.1"),
    ({}, "127.0.0.1"),
    ({"subtitle_listen_all_interfaces": False}, "127.0.0.1"),
    ({"subtitle_listen_all_interfaces": "yes"}, "127.0.0.1"),  # only an explicit True opens it
    ({"subtitle_listen_all_interfaces": True}, "0.0.0.0"),
])
def test_the_bind_address_is_this_machine_unless_the_one_key_is_true(config, host):
    assert subtitle_server.bind_host(config) == host


class _Server:
    def close(self):
        pass

    async def wait_closed(self):
        return None


@pytest.mark.parametrize("opened", [False, True])
def test_the_bridge_listener_binds_that_address_and_says_so_when_open(monkeypatch, capsys, opened):
    import arti_bridge as bridge
    seen = []

    async def serve(handler, host, port, **kw):
        seen.append((handler, host, port))
        return _Server()

    monkeypatch.setattr(bridge.websockets, "serve", serve)
    if opened:
        monkeypatch.setitem(bridge.CONFIG, "subtitle_listen_all_interfaces", True)
    else:
        monkeypatch.delitem(bridge.CONFIG, "subtitle_listen_all_interfaces", raising=False)
    asyncio.run(bridge.start_subtitle_server(9988))
    assert seen == [(subtitle_server.handler, "0.0.0.0" if opened else "127.0.0.1", 9988)]
    out = capsys.readouterr().out
    warned = [line for line in out.splitlines() if "ALL network interfaces" in line]
    assert warned == (["[SubTitle] Listening on ALL network interfaces: subtitle_listen_all_interfaces is on"]
                      if opened else [])


def test_the_standalone_server_binds_this_machine(monkeypatch):
    seen = []

    async def serve(handler, host, port, **kw):
        seen.append(host)
        return _Server()

    monkeypatch.setattr(subtitle_server.websockets, "serve", serve)
    asyncio.run(subtitle_server.main())
    assert seen == ["127.0.0.1"]


def test_the_obs_browser_source_url_still_connects_on_localhost():
    """subtitle.html builds ws://localhost:<port>; a real server on the default bind address accepts it."""
    html = (ROOT / "subtitle.html").read_text(encoding="utf-8")
    assert "'ws://localhost:' + wsPort" in html

    async def handshake():
        server = await websockets.serve(subtitle_server.handler, subtitle_server.bind_host({}), 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with websockets.connect("ws://localhost:%d" % port, open_timeout=5) as client:
                return client.open if hasattr(client, "open") else True
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(handshake())

"""Initial Socket.IO connection retry with no sockets, threads, or real waits."""
import sys
from types import SimpleNamespace

import pytest

from arti_donations import StreamlabsListener


@pytest.mark.parametrize("failures", [1, 8])
def test_streamlabs_first_connect_retries_with_cap(monkeypatch, failures, capsys):
    listener = StreamlabsListener("synthetic-token", lambda ev: None)
    attempts = []
    waits = []

    class Client:
        def __init__(self, **kw):
            pass

        def event(self, callback):
            return callback

        def on(self, event):
            return lambda callback: callback

        def connect(self, *a, **kw):
            attempts.append(True)
            if len(attempts) <= failures:
                # Socket.IO errors can include the credential-bearing URL.
                raise OSError("https://synthetic/?token=synthetic-token")

        def disconnect(self):
            pass

        def wait(self):
            listener.stop()

    monkeypatch.setitem(sys.modules, "socketio", SimpleNamespace(Client=Client))
    monkeypatch.setattr(listener._stop_event, "wait", lambda seconds: waits.append(seconds))
    listener.run()
    assert len(attempts) == failures + 1
    assert waits == [min(5 * 2**i, 60) for i in range(failures)]
    assert "synthetic-token" not in capsys.readouterr().out


def test_stop_during_streamlabs_backoff_prevents_retry(monkeypatch):
    listener = StreamlabsListener("synthetic-token", lambda ev: None)
    attempts = []

    class Client:
        def __init__(self, **kw):
            pass

        def event(self, callback):
            return callback

        def on(self, event):
            return lambda callback: callback

        def connect(self, *a, **kw):
            attempts.append(True)
            raise OSError("synthetic failure")

        def disconnect(self):
            pass

    monkeypatch.setitem(sys.modules, "socketio", SimpleNamespace(Client=Client))
    monkeypatch.setattr(listener._stop_event, "wait", lambda seconds: listener.stop())
    listener.run()
    assert len(attempts) == 1

"""Offline test for ui_op.py `type --hwnd`: it must re-check the foreground before every character and abort the
moment the farmhand is not in front (digits typed into the HOST change its toolbar slot). SendInput, the foreground
and the log are faked; no input is sent. Windows-only (ui_op loads user32 at import).
"""
import os, sys

import pytest

if sys.platform != "win32":
    pytest.skip("ui_op is Windows-only", allow_module_level=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts", "m1_operator"))
import ui_op as U  # noqa: E402

FH, HOST = 1001, 2002


class FakeU32:
    def __init__(self, fg_seq):
        self.fg_seq = list(fg_seq)

    def GetForegroundWindow(self):
        return self.fg_seq.pop(0) if len(self.fg_seq) > 1 else self.fg_seq[0]


@pytest.fixture
def fake(monkeypatch):
    sent, logs = [], []
    monkeypatch.setattr(U, "send", lambda *i: sent.append(i))
    monkeypatch.setattr(U, "log", lambda ev: logs.append(ev))
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    return sent, logs


def test_type_aborts_when_the_farmhand_loses_the_foreground(monkeypatch, fake):
    sent, logs = fake
    monkeypatch.setattr(U, "cmd_fg", lambda a: True)
    monkeypatch.setattr(U, "u32", FakeU32([FH, FH, HOST]))
    with pytest.raises(SystemExit) as e:
        U.cmd_type(["127.0.0.1"], FH)
    assert e.value.code == 3
    assert len(sent) == 2 * 2                       # two characters (down+up each) before the abort, none after
    assert logs[-1]["aborted"] and logs[-1]["chars"] == 2


def test_type_refuses_when_the_foreground_cannot_be_set(monkeypatch, fake):
    sent, logs = fake
    monkeypatch.setattr(U, "cmd_fg", lambda a: False)
    monkeypatch.setattr(U, "u32", FakeU32([HOST]))
    with pytest.raises(SystemExit):
        U.cmd_type(["1"], FH)
    assert not sent


def test_type_with_farmhand_in_front_types_everything(monkeypatch, fake):
    sent, logs = fake
    monkeypatch.setattr(U, "cmd_fg", lambda a: True)
    monkeypatch.setattr(U, "u32", FakeU32([FH]))
    U.cmd_type(["127.0.0.1"], FH)
    assert len(sent) == 2 * 9 and logs[-1]["foreground"] is True and logs[-1]["hwnd"] == FH

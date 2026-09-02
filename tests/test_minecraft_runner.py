"""Tests MinecraftRunner — fake subprocess (pola open_recorder telinga)."""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft  # noqa: E402
from arti_minecraft import MinecraftRunner  # noqa: E402

CFG = {
    "minecraft_streamer_name": "bohanyto",
    "minecraft_reaction_cooldown_sec": 60.0,
    "minecraft_max_bot_respawns": 0,
}


class _PipeStdout:
    """stdout ala pipe: iterasi blok sampai ada baris / sentinel None (EOF)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()

    def __iter__(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            yield item


class _FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.lines: list[str] = []

    def write(self, s: str) -> None:
        self.lines.append(s)
        if '"quit"' in s:
            self.proc.close(0)

    def flush(self) -> None:
        pass


class FakeProc:
    """Objek ala Popen; stdout bisa pre-loaded (mati sendiri) atau interaktif."""

    def __init__(self, lines: list[str] | None = None):
        self.stdout = _PipeStdout()
        self.stderr = iter(())  # kosong — drain thread langsung selesai
        self.stdin = _FakeStdin(self)
        self._rc: int | None = None
        if lines is not None:
            for ln in lines:
                self.stdout.q.put(ln)
            self.stdout.q.put(None)  # EOF -> "bot mati sendiri"
            self._rc = 1

    def close(self, rc: int) -> None:
        self._rc = rc
        self.stdout.q.put(None)

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc

    def terminate(self):
        self.close(1)


def _wait_until(pred, timeout=3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _hooks():
    calls = {"reactions": [], "history": []}
    return calls, {
        "queue_reaction": calls["reactions"].append,
        "add_history": lambda src, msg: calls["history"].append((src, msg)),
    }


def test_event_flow_reactions_history_and_deadman(monkeypatch):
    monkeypatch.setattr(arti_minecraft, "_sleep", lambda s: None)
    proc = FakeProc([
        '{"ev": "spawned", "pos": {}, "health": 20}',
        '{"ev": "status", "health": 20, "food": 20, "task": "follow"}',
        'BUKAN JSON — reader tidak boleh crash',
        '{"ev": "chat", "from": "bohanyto", "text": "sini arti"}',
        '{"ev": "chat", "from": "orang_lain", "text": "abaikan"}',
        '{"ev": "death", "killer": "creeper"}',
    ])
    calls, hooks = _hooks()
    runner = MinecraftRunner(CFG, hooks, open_proc=lambda: proc)
    assert runner.start() is True
    # max_respawns=0: begitu proc habis -> deadman (tidak muter selamanya)
    assert _wait_until(lambda: runner.gave_up)
    assert runner.last_status is not None and runner.last_status["health"] == 20
    # chat streamer -> history "Streamer" (bangunkan detektor kehidupan);
    # chat pemain LAIN ikut masuk history sejak mabar [date removed] — dengan NAMANYA
    # sendiri, bukan menyamar jadi Streamer (atribusi = gate pemilik).
    assert calls["history"] == [
        ("Streamer", "(chat Minecraft) sini arti"),
        ("orang_lain", "(chat Minecraft) abaikan"),
    ]
    # death -> 1 reaksi; deadman -> 1 reaksi penutup
    assert len(calls["reactions"]) == 2
    assert "MATI" in calls["reactions"][0]
    assert "nyerah" in calls["reactions"][1] or "putus" in calls["reactions"][1]
    assert len(runner.events_snapshot()) == 5  # garbage tidak masuk ring
    assert "menyerah" in runner.status_line()


def test_respawn_backoff_then_deadman(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(arti_minecraft, "_sleep", sleeps.append)
    spawned = {"n": 0}

    def open_proc():
        spawned["n"] += 1
        return FakeProc([])  # mati seketika tiap kali

    cfg = dict(CFG, minecraft_max_bot_respawns=2)
    calls, hooks = _hooks()
    runner = MinecraftRunner(cfg, hooks, open_proc=open_proc)
    runner.start()
    assert _wait_until(lambda: runner.gave_up)
    # respawn #1 dan #2 dicoba, percobaan ke-3 melewati batas -> deadman
    assert spawned["n"] == 3
    assert sum(sleeps) == 5 + 10  # backoff naik: 5 dtk lalu 10 dtk
    assert [r for r in calls["reactions"] if "putus terus" in r], calls["reactions"]


def test_stop_is_clean_and_start_guard(monkeypatch):
    monkeypatch.setattr(arti_minecraft, "_sleep", lambda s: None)
    procs: list[FakeProc] = []

    def open_proc():
        p = FakeProc()  # interaktif — hidup sampai disuruh mati
        procs.append(p)
        return p

    calls, hooks = _hooks()
    runner = MinecraftRunner(dict(CFG, minecraft_max_bot_respawns=5), hooks,
                             open_proc=open_proc)
    assert runner.start() is True
    assert _wait_until(lambda: runner.is_active())
    # start() kedua saat manager hidup -> ditolak (anti loop kembar)
    assert runner.start() is False
    procs[0].stdout.q.put('{"ev": "spawned", "pos": {}, "health": 20}')
    assert _wait_until(lambda: runner.events_snapshot())
    assert runner.send_command({"cmd": "say", "text": "halo"}) is True
    runner.stop()
    assert _wait_until(lambda: not runner.is_active())
    time.sleep(0.05)
    assert len(procs) == 1  # stop disengaja -> TIDAK respawn
    assert not runner.gave_up
    assert calls["reactions"] == []  # pamit rapi = tanpa reaksi suara
    assert any('"quit"' in ln for ln in procs[0].stdin.lines)
    # bot mati -> kirim perintah ditolak halus
    assert runner.send_command({"cmd": "status"}) is False

"""Deadline clock for synchronous observation doubles; native integration keeps real time."""
from functools import wraps
from types import SimpleNamespace
import time


def capture_at_first_reply(runtime, motor, observations, player_id, monkeypatch):
    """A synchronous world's healthy initial frame does not age while the worker is descheduled.

    Refresh timestamps once, without changing facts, revisions or later fault
    injections. Publication/rollback tests keep their actual admission fences.
    """
    from dataclasses import replace
    invoke = runtime._invoke_provider
    first = [True]
    def reply(request):
        result = invoke(request)
        if first[0]:
            first[0] = False
            now = time.monotonic()
            with motor._kunci:
                motor._state[player_id] = replace(motor._state[player_id], diterima_pada=now)
            with observations._kunci:
                for key, record in list(observations._records.items()):
                    if key[0] == player_id:
                        observations._records[key] = replace(record, received_monotonic=now)
        return result
    monkeypatch.setattr(runtime, "_invoke_provider", reply)


def virtual_readback_deadlines(runtime, monkeypatch):
    import arti_stardew_runtime as module

    def wrap(method):
        @wraps(method)
        def readback(*args, **kwargs):
            now = [runtime._game_now()]
            clock_time = SimpleNamespace(**{key: getattr(time, key) for key in dir(time)})
            def advance(seconds):
                now[0] += seconds
                callback = getattr(runtime, "_test_readback_advance", None)
                if callback is not None:
                    callback(seconds)
            clock_time.sleep = advance
            with monkeypatch.context() as scoped:
                scoped.setattr(runtime, "_game_now", lambda: now[0])
                scoped.setattr(module, "time", clock_time)
                return method(*args, **kwargs)
        return readback

    for name in ("_warp_after_move_readback", "_location_change_readback", "_forage_readback"):
        monkeypatch.setattr(runtime, name, wrap(getattr(runtime, name)))


def declared_capture_clock(runtime, motor, observations, player_id, monkeypatch):
    """A synchronous world's state and rich observations use one explicit clock."""
    from dataclasses import replace
    import arti_stardew_runtime as module
    import arti_stardew_scene as scene
    now = [motor.latest(player_id).diterima_pada]
    clock_time = SimpleNamespace(**vars(time))
    clock_time.monotonic = lambda: now[0]
    monkeypatch.setattr(module, "time", clock_time)
    monkeypatch.setattr(scene, "time", clock_time)
    monkeypatch.setattr(motor, "_jam", lambda: now[0])
    with observations._kunci:
        for key, record in list(observations._records.items()):
            observations._records[key] = replace(record, received_monotonic=now[0])
    apply = observations.apply

    def capture(event):
        result = apply(event)
        if result.accepted:
            key = (event["player_id"], event["payload"]["domain"])
            with observations._kunci:
                observations._records[key] = replace(observations._records[key], received_monotonic=now[0])
        return result

    monkeypatch.setattr(observations, "apply", capture)
    return now


def simulated_frame_clock(runtime, simulation, monkeypatch):
    """Advance an in-memory world's time at each declared capture."""
    now = declared_capture_clock(runtime, simulation.state, simulation.observations, -1, monkeypatch)
    # Include helper/mixin-local imports of time; the adapter never performs
    # native transport. Physical lease expiry still uses the real wall clock.
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runtime, "_continuation_clock", lambda: now[0])
    advance = simulation._advance

    def frame():
        now[0] += 1 / 60
        return advance()

    monkeypatch.setattr(simulation, "_advance", frame)
    return now

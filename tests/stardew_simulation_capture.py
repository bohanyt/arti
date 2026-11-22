"""Declared capture boundaries for synthetic worlds with no native observer.

The runtime lock can starve the simulator's thread under parallel-suite load.
Capture current fixture facts before a generation and a motor heartbeat before
admission; neither path advances synthetic physics or changes production gates.
"""
def install_boundary_captures(monkeypatch, runtime, sim):
    begin = runtime._begin_generation
    def captured_generation(*args, **kwargs):
        sim.advance()
        return begin(*args, **kwargs)
    monkeypatch.setattr(runtime, "_begin_generation", captured_generation)
    for name in ("_move_precheck", "_water_precheck"):
        original = getattr(runtime, name)
        def captured_precheck(*args, _original=original, **kwargs):
            with sim._sample_lock:
                sim.seq += 1
                sim._publish_motor()
            return _original(*args, **kwargs)
        monkeypatch.setattr(runtime, name, captured_precheck)
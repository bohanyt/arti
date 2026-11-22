"""One bed-wait routine after she chose wait. First page still offers wait+cancel (ADR 154)."""

BOUND_S = 180
HEARTBEAT_S = 30


def _state(runtime):
    return vars(runtime).setdefault("_bed_wait", {})


def start_hold(runtime, *, waiting_for, clock, now):
    vars(runtime)["_bed_wait"] = dict(holding=True, waiting_for=waiting_for, clock=clock,
                                      since=now, last_beat=now, bound=False)


def hold_without_ask(runtime, now):
    state = getattr(runtime, "_bed_wait", None) or {}
    if not state.get("holding"):
        return None
    if now - state.get("since", now) >= BOUND_S:
        state["holding"] = False
        state["bound"] = True
        return "bound"
    return "hold"


def heartbeat(runtime, session, view):
    state = getattr(runtime, "_bed_wait", None) or {}
    now = state.get("last_beat")
    import time
    t = time.monotonic()
    if t - (now or 0) < HEARTBEAT_S:
        return
    state["last_beat"] = t
    waiting = (getattr(view, "ready_check", None) or {}).get("waiting_for") or state.get("waiting_for")
    journal = getattr(runtime, "_journal", None)
    if callable(journal):
        journal(dict(ev="live7_bed_wait", session_id=getattr(session, "session_id", None),
                     waiting_for=waiting, elapsed_s=round(t - state.get("since", t), 1),
                     bound_s=BOUND_S, input=False))


def fact(runtime, view=None):
    state = getattr(runtime, "_bed_wait", None) or {}
    waiting = ((getattr(view, "ready_check", None) or {}).get("waiting_for")
               if view is not None else None) or state.get("waiting_for") or "pemain lain"
    if state.get("holding"):
        return "Menunggu %s di kasur (satu rutin, bukan keputusan baru)." % waiting
    if state.get("bound"):
        return ("Batas tunggu kasur %ds tercapai; sleep check masih native. "
                "Pilih wait atau batal." % BOUND_S)
    return None


def clear(runtime):
    vars(runtime).pop("_bed_wait", None)

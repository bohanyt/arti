"""One scoped UI effect stream for primitive callers and active skill consumers."""
from copy import deepcopy


def observe(runtime, session, view):
    if session is not None and getattr(session, "mode", None) == "part_live" and view is not None:
        from arti_stardew_generic_ui import remember_ui_observation
        if session.current is not None:
            session.current.state["ui_effect_required"] = True
        remember_ui_observation(runtime, session, view)


def publish(runtime, session, view, result, chosen, *, goal=None, current=None):
    if session is None or getattr(session, "mode", None) != "part_live" or view is None:
        return
    run, active = session.current, runtime._active
    if (run is None or active is None or run.goal_id != active.goal_id
            or goal is not None and active is not goal or current is not None and run is not current):
        return
    run.state["ui_effect_required"] = True
    _, _, context, _ = runtime._live7_read(session)
    identity = tuple((context or {}).get("identity") or ())[:5]
    date = runtime._date_tuple(runtime._state.latest(runtime._binding_view()[1]))
    trace = run.state.get("native_ui_episode")
    if trace is None or tuple(trace["identity"]) != identity or trace["date"] != date:
        return
    from arti_stardew_generic_ui import remember_ui_input
    remember_ui_input(run.state, view, result, chosen)
    scope = dict(identity=identity, date=date, session=session.session_id, goal=active.goal_id, revision=active.revision)
    if run.state.setdefault("ui_effect_scope", scope) != scope:
        return
    for entry in trace["inputs"]:
        if entry["request_id"] == getattr(result, "request_id", None):
            entry.setdefault("scope", scope)  # replay never resets consumption


def same_date(a, b):
    """PM-1 fix6 #3: the episode date is a tuple (_date_tuple), ui.context goal_route.date a JSON list."""
    return isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and tuple(a) == tuple(b)


def consume(state, context, receiver, effects, *, snap=None, successor=None, choice=None):
    """Consume one authenticated semantic effect, retaining intervening linked successors."""
    from arti_stardew_skills import _g
    trace = state.get("native_ui_episode") or {}
    scope = state.get("ui_effect_scope")
    consumed = state.setdefault("ui_effect_consumed", [])
    entries = trace.get("inputs", ())
    if scope:
        if (tuple(scope["identity"]) != tuple(context.get("identity") or ())[:5]
                or not same_date(scope["date"], context.get("date"))):
            return None
    elif not entries and not state.get("ui_effect_required"):
        # Existing non-Live7 skill snapshots carry the already authenticated leased action.
        # They share this semantic consumer and replay fence, without gesture assumptions.
        last = _g(_g(snap, "working_memory"), "last_action")
        before, after = _g(last, "before") or {}, _g(last, "after") or {}
        press, effect = before.get("ui_press") or {}, after.get("ui_press") or {}
        if (_g(last, "lifecycle") != "completed" or _g(last, "cleanup_confirmed") is not True
                or _g(last, "world_effect_status") != "observed_ui_press" or not _g(last, "request_id")):
            return None
        entries = [dict(request_id=_g(last, "request_id"), before_receiver=press.get("receiver_token"),
            after_receiver=effect.get("result_receiver_token"), result_menu=effect.get("result_menu_type"),
            effect=effect.get("effect"), args=press, linked=True)]
    else:
        return None
    chain, receivers = [], {receiver}
    for entry in entries:
        if (not entry.get("linked") or entry.get("scope") != scope
                or entry["before_receiver"] != receiver):
            continue
        if choice is not None and not chain and str(entry.get("args", {}).get("choice")) != str(choice):
            return None
        chain.append(entry["request_id"])
        receivers.add(entry["after_receiver"])
        if (entry["effect"] in effects and entry["request_id"] not in consumed
                and (successor is None or entry["after_receiver"] == successor)):
            consumed.extend(r for r in chain if r not in consumed)
            return dict(deepcopy(entry), observations=deepcopy([o for o in trace.get("observations", ())
                if o.get("linked") and o["receiver"] in receivers]))
        if entry["effect"] not in ("advanced", "replaced", "answered", "changed"):
            return None
        receiver = entry["after_receiver"]
    return None

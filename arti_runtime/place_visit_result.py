"""Bound readbacks for a single stay. The place_visit ledger owns the history."""
from collections.abc import Mapping
import time


def _epoch(pin):
    return (pin.runtime_instance_id, pin.command_session_id, pin.observation_session_id, pin.player_id, pin.screen_id)


def _fresh(record, pin, now):
    return (record is not None and record.available and record.authoritative
            and not record.stale and not record.requires_resync
            and record.is_complete
            and (record.session_id, record.player_id, record.screen_id)
            == (pin.observation_session_id, pin.player_id, pin.screen_id)
            and 0 <= now - record.received_monotonic <= 2)


def capture(runtime, pin, campaign, motor=None):
    """Join the current UI publication to the same actor's fresh motor read."""
    if pin is None:
        return None
    epoch = _epoch(pin)
    if epoch != campaign.epoch:
        return None
    if motor is None:
        motor = runtime._state.latest(pin.player_id)
    ui = runtime._obs.get(pin.player_id, "ui.context")
    now = time.monotonic()
    if (motor is None or runtime._state.is_stale(pin.player_id)
            or runtime._state.latest(pin.player_id) is not motor
            or (motor.player_id, motor.screen_id, getattr(motor, "location_id", None))
            != (pin.player_id, pin.screen_id, pin.location)
            or runtime._date_tuple(motor) != campaign.date or type(motor.time_of_day) is not int
            or not _fresh(ui, pin, now)):
        return None
    if tuple(ui.facts.get(k) for k in ("runtime_instance_id", "session_id", "context_generation",
                                       "location_id", "ui_revision")) != (
            pin.runtime_instance_id, pin.command_session_id, pin.context_generation, pin.location, pin.ui_revision):
        return None
    return dict(epoch=epoch, place=pin.location, name=motor.location, day=campaign.date, clock=motor.time_of_day,
                stamp=(motor.diterima_pada, ui.received_monotonic),
                witness=dict(motor=motor.revisi, ui=ui.domain_revision))


def matches_arrival(request, sample):
    """Bind an answered page to the same actor, place and motor publication as its arrival."""
    snapshot = getattr(request, "snapshot", None)
    pin = getattr(snapshot, "water_context", None) or getattr(snapshot, "movement_context", None)
    return (pin is not None and _epoch(pin) == sample["epoch"] and pin.location == sample["place"]
            and getattr(getattr(snapshot, "motor", None), "revisi", None) == sample["witness"]["motor"])


def _label(value):
    return " ".join(value.split())[:28] if isinstance(value, str) else ""


def findings(runtime, sample, context):
    """Keep positive observations only. A partial scene never proves an absence."""
    from arti_runtime.fact_line import visible_targets
    from scripts.live7.interactions import ordinary_targets

    if sample is None:
        return {}
    # The sample and the displayed/settled context must describe the same native location.
    if tuple(context.get("identity") or ()) != (*sample["epoch"], sample["place"]):
        return {}
    found = {}
    pid = sample["epoch"][3]
    ui = runtime._obs.get(pid, "ui.context")
    # Unlike remembered chests or scene-only candidates, these named rows are in this current UI read.
    available = ordinary_targets(dict(mines_ui=ui.facts)) if (
        ui is not None and ui.domain_revision == sample["witness"]["ui"]
        and ui.facts is context.get("mines_ui")) else ()
    for row in available:
        if (row.get("status") in ("openable", "available") and row.get("ref")
                and (label := _label(row.get("label") or row.get("kind")))):
            found["available"] = dict(label=label, ref=row["ref"], witness=sample["witness"])
            break
    scene = runtime._obs.get(pid, "scene.current_location")
    # NPCs do not carry a map field. Require the location fact in the same frame.
    if (scene is None or not scene.available or not scene.authoritative or scene.stale or scene.requires_resync
            or (scene.session_id, scene.player_id, scene.screen_id) != sample["epoch"][2:]
            or not 0 <= time.monotonic() - scene.received_monotonic <= 2
            or not any(isinstance(f, Mapping) and f.get("kind") == "location"
                       and f.get("location_id") == sample["place"] for f in scene.facts.values())):
        return found
    for ref, f in visible_targets(context).items():
        key = {"npc": "seen", "object": "object"}.get(f.get("kind"))
        if key and key not in found and scene.facts.get(ref) == f:
            if label := _label(f.get("display_name") or f.get("name") or f.get("internal_name")):
                found[key] = dict(label=label, ref=ref, witness=dict(scene=scene.domain_revision))
    return found


def _mark(runtime, kind, stay, **details):
    runtime._journal(dict(ev="live7_visit_result", kind=kind, place_id=stay["start"]["place"], **details))


def _leave_open_stay(runtime, stay):
    runtime._live7_visit_open = None
    if (getattr(runtime, "_live7_visit_scope", None) == stay["scope"]
            and runtime._live7_visits.get("current", (None,))[0] == stay["start"]["place"]):
        runtime._live7_visits.pop("current", None)


def _drop_open(runtime, stay, reason):
    _leave_open_stay(runtime, stay)
    _mark(runtime, "dropped", stay, reason=reason)


def open_visit(runtime, pending, at):
    sample = pending["sample"]
    observe(runtime, sample, scope=pending["scope"])
    if getattr(runtime, "_live7_visit_open", None) is not None:
        return
    runtime._live7_visit_open = stay = dict(scope=pending["scope"], at=at, start=sample, **pending["findings"])
    _mark(runtime, "opened", stay)


def _seal(runtime, stay, departure, *, motor_place=None):
    stay["departure"] = departure
    if motor_place is not None:
        stay["awaiting_ui"] = motor_place
    _leave_open_stay(runtime, stay)
    vars(runtime).setdefault("_live7_visit_closing", []).append(stay)
    _mark(runtime, "sealed", stay, destination_id=departure["left"] or motor_place)


def _join_departure(stay, sample):
    if sample is not None and all(sample["witness"][key] > stay.get("last", stay["start"])["witness"][key]
                                  for key in ("ui", "motor")):
        stay["departure"].update(left=sample["place"], left_name=sample["name"], end=sample["witness"])
        stay.pop("awaiting_ui", None)


def note_crossing(runtime, campaign, pin):
    """Seal on the first bound motor crossing. Only a UI frame of that crossing can name it."""
    pending = getattr(runtime, "_live7_visit_closing", ())
    stay = getattr(runtime, "_live7_visit_open", None)
    previous = pending[-1] if pending and "awaiting_ui" in pending[-1] else None
    if stay is None and previous is None:
        return
    pid, screen = campaign.epoch[-2:]
    motor = runtime._state.latest(pid)
    if (motor is None or runtime._state.is_stale(pid) or (motor.player_id, motor.screen_id) != (pid, screen)
            or not (motor_place := getattr(motor, "location_id", None))):
        return
    day, clock = runtime._date_tuple(motor), motor.time_of_day
    if previous is not None:
        if (motor_place != previous["awaiting_ui"] or campaign.epoch != previous["start"]["epoch"]
                or day != previous["start"]["day"] or clock < previous["departure"]["time"]):
            previous.pop("awaiting_ui")
        else:
            _join_departure(previous, capture(runtime, pin, campaign, motor))
    if stay is None:
        return
    first = stay.get("last", stay["start"])
    if motor.revisi < first["witness"]["motor"]:
        return
    if campaign.epoch != first["epoch"] or day != first["day"] or clock < first["clock"]:
        _drop_open(runtime, stay, "visit_timeline_changed")
    elif motor_place != first["place"] and motor.revisi > first["witness"]["motor"]:
        sample = capture(runtime, pin, campaign, motor)
        departure = dict(left=None, time=clock, end=dict(motor=motor.revisi))
        _seal(runtime, stay, departure, motor_place=motor_place)
        _join_departure(stay, sample)


def _defer(runtime, pending, reason):
    if pending.get("deferred") != reason:
        pending["deferred"] = reason
        _mark(runtime, "deferred", pending, reason=reason)


def flush(runtime, *, scope=None, reason=None):
    """Try one sealed stay at this boundary. A failed ledger write gets one later retry."""
    if runtime._lock._is_owned():
        return
    from arti_runtime.place_page import close_visit

    with runtime._lock:
        queue = getattr(runtime, "_live7_visit_closing", None)
        if not queue or queue[0].get("writing"):
            return
        pending = queue[0]
        if not scope:
            _defer(runtime, pending, reason or "scope_unavailable")
            return
        if pending["scope"] != scope:
            queue.pop(0)
            _mark(runtime, "dropped", pending, reason="scope_changed")
            return
        if "awaiting_ui" in pending:
            _defer(runtime, pending, "departure_ui_unavailable")
            return
        pending["writing"] = True
        start = pending["start"]
        result = {k: pending[k] for k in ("seen", "object", "available", "effect", "stalled", "pose", "menu")
                  if k in pending}
        result.update(pending["departure"], observation_session=start["epoch"][2], start=start["witness"])
    try:
        visits = close_visit(runtime._live7_mind_base(), scope, start["place"], pending["at"], result)
    except (OSError, ValueError) as exc:
        with runtime._lock:
            attempts = pending["attempts"] = pending.get("attempts", 0) + 1
            runtime._journal(dict(ev="visit_memory_error", error=type(exc).__name__, attempts=attempts))
            if attempts >= 2:
                queue.pop(0)
                _mark(runtime, "dropped", pending, reason="write_retry_exhausted")
            else:
                _defer(runtime, pending, "write_failed")
        return
    else:
        with runtime._lock:
            queue.pop(0)
            if visits is None:
                _mark(runtime, "dropped", pending, reason="ledger_visit_unavailable")
            else:
                _mark(runtime, "written", pending)
                if getattr(runtime, "_live7_visit_records_scope", None) == scope:
                    runtime._live7_visit_records[start["place"]] = visits
    finally:
        with runtime._lock:
            pending.pop("writing")


def observe(runtime, sample, *, context=None, scope=None):
    """Accumulate on the same map or seal on newer UI and motor publications across a warp."""
    if sample is None:
        return
    pending = getattr(runtime, "_live7_visit_closing", ())
    if pending and (place := pending[-1].get("awaiting_ui")) is not None:
        previous = pending[-1]
        if sample["witness"]["motor"] >= previous["departure"]["end"]["motor"]:
            if (sample["epoch"] == previous["start"]["epoch"] and sample["day"] == previous["start"]["day"]
                    and sample["clock"] >= previous["departure"]["time"] and sample["place"] == place):
                _join_departure(previous, sample)
            else:
                previous.pop("awaiting_ui")
    stay = getattr(runtime, "_live7_visit_open", None)
    if stay is None:
        return
    start = stay["start"]
    last = stay.get("last", start)
    if any(now < then for now, then in zip(sample["stamp"], last["stamp"])):
        return
    if (sample["epoch"] != start["epoch"] or sample["day"] != start["day"]
            or sample["clock"] < last["clock"] or scope and scope != stay["scope"]):
        _drop_open(runtime, stay, "scope_changed" if scope and scope != stay["scope"] else "visit_timeline_changed")
        return
    if sample["place"] == start["place"]:
        stay["last"] = sample
        if context is not None and scope == stay["scope"]:
            stay.update(findings(runtime, sample, context))
    elif all(sample["witness"][k] > last["witness"][k] for k in ("ui", "motor")):
        _seal(runtime, stay, dict(left=sample["place"], left_name=sample["name"],
                                  time=sample["clock"], end=sample["witness"]))


def settle(runtime, sample, context, run, action, outcome, category, before, after, items):
    """Keep only a settled local observation tied to this run's new native request."""
    if sample is None:
        return
    scope = (context.get("world_knowledge") or {}).get("scope")
    observe(runtime, sample, context=context, scope=scope)
    stay = getattr(runtime, "_live7_visit_open", None)
    if (stay is None or run.state.get("visit_start") != stay["at"]
            or before.get("location_id") != sample["place"]
            or stay.get("last") != sample
            or action is None or not action.request_id or action.request_id == run.state.get("visit_prior_request")
            or not action.cleanup_confirmed or category not in ("VERIFIED", "CLEAN_FAIL", "PARTIAL")):
        return
    witness = dict(sample["witness"], request=action.request_id)
    if run.call.name in ("langkah", "hadap"):
        old, new = action.before or {}, action.after or {}
        if old.get("location_id") != sample["place"] or new.get("location_id") != sample["place"]:
            return
        pose = old.get("pixel"), new.get("pixel")
        if not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in pose):
            return
        if pose[0] != pose[1]:
            stay["pose"] = dict(label="posisi bergeser", witness=witness)
        elif type(old.get("facing")) is int and type(new.get("facing")) is int and old["facing"] != new["facing"]:
            stay["pose"] = dict(label="hanya arah hadap berubah", witness=witness)
        elif action.world_effect_status in ("unchanged", "zero_effect") and action.lifecycle != "not_taken":
            label = "langkah %s tidak bergeser" % _label(run.call.arg) if run.call.name == "langkah" else "arah hadap tetap"
            stay["stalled"] = dict(label=label, witness=witness)
        return
    from arti_runtime.raw_hands import _change_words
    delta = run.state.get("generic_readback") or {}
    changes = {str(c.get("path", "")): c for c in delta.get("changes") or () if isinstance(c, Mapping)}
    delta_witness = dict(witness, request=delta.get("request_id") or action.request_id)
    menu = None
    if "menu" in changes:
        from arti_stardew_generic_world import unreadable_press
        if unreadable_press(run.call.arg, run.state.get("native_ui_episode")):
            menu = dict(label="menu simbol tak terbaca", witness=delta_witness)
            stay["effect"] = dict(label="menekan sesuatu", witness=delta_witness)
        else:
            menu = dict(label=_label(_change_words([changes["menu"]])[0]), witness=delta_witness)
    elif run.state.get("generic_menu_opened") or outcome[1] == "observed_menu_open":
        menu = dict(label="menu terbuka", witness=witness)
    if menu:
        stay["menu"] = menu
    # A verified motion or menu terminal is insufficient to attribute a coincident bag change.
    effect_domains = {"quest", "inventory", "money", "object", "terrain", "container", "animal"}
    change = next((row for path, row in changes.items() if path.split(":", 1)[0] in effect_domains), None)
    if change is not None:
        stay["effect"] = dict(label=_label(_change_words([change])[0]), witness=delta_witness)
    elif any(path.startswith("character:") for path in changes):
        # The native area sample can change when somebody walks in or out.
        stay["effect"] = dict(label="sampel warga sekitar berubah", witness=delta_witness)
    elif (items and after is not None and category in ("VERIFIED", "PARTIAL")
          and run.call.name in ("forage", "tangkap_ikan", "panen", "beli", "jual", "kirim", "simpan_ke_chest", "pungut_drop")):
        names = {**(before.get("names") or {}), **(after.get("names") or {})}
        label = next((_label(names[q]) for q in sorted(items) if q in names), None)
        stay["effect"] = dict(label="isi tas berubah" + (" (" + label + ")" if label else ""), witness=witness)
    elif (not menu and not changes and action.world_effect_status == "unchanged" and action.lifecycle == "completed"
          and (delta or (action.after or {}).get("location_id") == sample["place"])):
        stay["stalled"] = dict(label="aksi lokal tanpa perubahan", witness=witness)


def note_rendered(runtime, rendered, records):
    """Read only result cells that survived page fitting. Journal counts and location IDs."""
    from arti_runtime.native_rows import native_rows
    from arti_runtime.place_page import RESULT_FIELD, result_label

    labels = []
    for entry in native_rows(rendered.get("skills")):
        travel = entry.get("after_travel")
        if isinstance(travel, Mapping):
            fields = travel.get("fields") or ()
            if RESULT_FIELD in fields:
                index = fields.index(RESULT_FIELD)
                labels.extend(row[index] for row in native_rows(travel.get("rows"))
                              if isinstance(row, (list, tuple)) and len(row) > index
                              and isinstance(row[index], str))
            else:
                labels.extend(row[RESULT_FIELD] for row in travel.values()
                              if isinstance(row, Mapping) and isinstance(row.get(RESULT_FIELD), str))
    for rows in ((rendered.get("mind") or {}).get("tujuan") or {}).values():
        labels.extend(row[RESULT_FIELD] for row in native_rows(rows)
                      if isinstance(row, Mapping) and isinstance(row.get(RESULT_FIELD), str))
    if labels:
        shown = set(labels)
        runtime._journal(dict(ev="live7_visit_result", kind="rendered", count=len(labels),
                              place_ids=sorted(place for place in records if result_label(records, place) in shown)))

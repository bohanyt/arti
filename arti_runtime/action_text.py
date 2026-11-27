"""Associate an actually opened receiver's words with her selected action."""
import hashlib


def note(runtime, call, *, date, clock, place, text):
    if (call.name != "aksi" or not (call.arg or "").startswith(("map-action:", "event-"))
            or not isinstance(text, str) or not text.strip()
            or (getattr(runtime, "_config", None) or {}).get("live7_fact_line_enabled") is not True):
        return
    base = runtime._live7_mind_base()
    scope = getattr(runtime, "_live7_discovery_scope", None)
    if base is None or not scope:
        return
    import arti_mind as M
    from arti_runtime.native_story import day_of
    from arti_runtime.plan_dates import label
    from arti_runtime.seen_receipts import SeenReceipts
    day = day_of("%s:%s:%s" % tuple(date))
    if day is None:
        return
    game_date = label("stardew", day)
    source = "native_action_screen"
    record_id = f"{source}:{call.arg}:{hashlib.sha256(text.encode()).hexdigest()}"
    text_ref = hashlib.sha256((scope + "\0" + record_id).encode()).hexdigest()
    with M._JOURNEY_LOCK:
        M.record_discoveries(base, "stardew", scope=scope, discoveries=[dict(
            id=record_id, kind="text_read", subject=call.arg, text=text, source=source,
            date=game_date, event_place=place)], day=day, day_label=game_date,
            scoped=(getattr(runtime, "_config", None) or {}).get("live7_mind_scope", False) is True)
        SeenReceipts(base, scope).note("map_action_read", call.arg, game_day=day, clock=clock,
                                      place=place, source=source, text_ref=text_ref)

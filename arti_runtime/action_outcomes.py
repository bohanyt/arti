"""ADR 157: a missing post-action read is knowledge debt, never failure evidence."""
from collections.abc import Mapping


UNKNOWN_TEXT = "hasilnya belum kebaca"


def unreadable(reason):
    """Shared across action kinds; pre-action missing offers are not post-action reads."""
    reason = str(reason or "").casefold()
    return any(token in reason for token in (
        "readback_unavailable", "readback_unreadable", "readback_missing", "readback_stale"))


def get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def settle(pending, proof, location):
    """One reducer for a proved row, its recent history and its closed run."""
    pending["row"].update(outcome="succeeded", outcome_class="VERIFIED", verified=True, reason=proof["reason"])
    pending["history"].update(outcome="succeeded", outcome_class="VERIFIED", reason=proof["reason"],
                              note="hasilnya terverifikasi", location=location)
    run = pending["run"]
    run.status, run.reason, run.verdict = "finished", None, proof["reason"]
    run.state.update(outcome="succeeded", live7_outcome="VERIFIED")


def generic_receipt(run, action, location):
    """Recheck a generic press's own native receipt, without guessing a gameplay goal.

    Preparatory movement/selection receipts cannot credit the final press. A
    location receipt must agree with the new native observation (the a63 shape).
    An unchanged world alone cannot prove that an unreadable action failed.
    """
    if (not run.state.get("generic_pressed") or not get(action, "request_id")
            or get(action, "request_id") == run.state.get("generic_previous")
            or get(action, "lifecycle") != "completed" or get(action, "cleanup_confirmed") is not True):
        return None
    effect = get(action, "world_effect_status")
    after = get(action, "after") or {}
    if effect == "observed_location_change":
        origin = (get(action, "before") or {}).get("location")
        if not origin or not location or origin == location or after.get("location") != location:
            return None
    elif effect == "observed_world_delta":
        if not after.get("changes"):
            return None
    elif effect not in ("observed_container_open", "observed_extras_effect") or not after:
        return None
    return dict(reason="observed_generic_readback", effect=effect, request_id=get(action, "request_id"),
                location=location)

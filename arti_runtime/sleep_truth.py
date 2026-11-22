"""Night kind from native bed evidence. Never a steer."""


def bed_answered(observed):
    if not isinstance(observed, dict):
        return False
    return observed.get("answered") is True


def bed_evidence(observed):
    """A native Yes plus a dated bed wait, independent of the outer skill class."""
    return bed_answered(observed) and type(observed.get("bed_time")) is int


def night_kind(*, knockout=False, observed=None, collapse_clock=False):
    """slept / collapsed / knocked_out / unknown. Collapse is 02:00 without a bed Yes."""
    if knockout:
        return "knocked_out"
    if bed_evidence(observed):
        return "slept"
    if collapse_clock:
        return "collapsed"
    return "unknown"


def page_line(kind):
    if kind == "slept":
        return ("Semalam tidur terverifikasi; save native selesai. Selisih dompet = efek malam. "
                "null = tidak diketahui.")
    if kind == "knocked_out":
        return ("Semalam pingsan KO tercatat; save native selesai. Boleh pilih kegiatan baru. "
                "null = tidak diketahui.")
    if kind == "collapsed":
        return ("Semalam pingsan 02:00 tercatat; save native selesai. Boleh pilih kegiatan baru. "
                "null = tidak diketahui.")
    return ("Kemarin berakhir tanpa tidur terverifikasi; save native selesai. Boleh pilih kegiatan baru. "
            "null = tidak diketahui.")


def last_bed_observed(campaign):
    """The pending ticket, else the last tidur row with native Yes+bed_time."""
    live = getattr(campaign, "live", None)
    pending = getattr(live, "pending_sleep", None) if live is not None else None
    observed = (pending or {}).get("observed")
    if bed_evidence(observed):
        return observed
    days = getattr(campaign, "days", None) or ()
    for row in reversed((days[-1] if days else {}).get("skills") or ()):
        if row.get("skill") == "tidur":
            observed = (row.get("evidence") or {}).get("observed")
            return observed if bed_evidence(observed) else None
    return None

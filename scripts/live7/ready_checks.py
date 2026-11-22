"""ADR 64: native barrier semantics and bounded peer facts, never gameplay policy."""
from copy import deepcopy


def kind(check):
    return {"sleep": "sleep", "festivalStart": "festival_start", "festivalEnd": "festival_end"}.get(check, "other")


def sleep_waiting(readiness):
    """ADR 154: native local readiness is a continuing sleep choice, not a stuck menu."""
    return bool(readiness and readiness.get("check") == "sleep" and readiness.get("local_ready") is True)


def sleep_cancelled(readback):
    """Only the observed true -> false receipt reconciles a sleep cancellation."""
    return bool(readback and readback.get("check") == "sleep" and readback.get("effect") == "readiness_lost"
                and readback.get("local_ready_before") is True and readback.get("local_ready_after") is False
                and readback.get("request_id") and readback.get("verified") is False)


def enrich(base, wire):
    # Old observations remain compatible; their missing peer states remain unknown.
    if "kind" in wire:
        if wire["kind"] != kind(base["check"]):
            return None
        base["kind"] = wire["kind"]
    if "players" in wire:
        rows = wire["players"]
        if (not isinstance(rows, list) or len(rows) > 8
                or wire.get("players_basis") not in ("native_local_state; remote_unknown", "native_local_state; host_native_ready_states")
                or any(not isinstance(p, dict) or type(p.get("Id")) is not int
                    or not isinstance(p.get("Name"), str) or len(p["Name"]) > 40
                    or type(p.get("Self")) is not bool or p.get("State") not in ("Ready", "Locked", "NotReady", "unknown") for p in rows)
                or len({p["Id"] for p in rows}) != len(rows)):
            return None
        base.update(players=deepcopy(rows), players_basis=wire["players_basis"])
    return base


def notice(readiness):
    check = readiness["check"]
    label = {"festival_start": "mulai festival", "festival_end": "akhir festival", "sleep": "tidur"}.get(kind(check), "check lain")
    cancel = readiness.get('cancelable')
    count = f"siap {readiness.get('ready_count', 'unknown')}/{readiness.get('needed', 'unknown')}"
    if sleep_waiting(readiness):
        return (f"Kamu sudah di tempat tidur dan sudah siap tidur; menunggu pemain lain atau konfirmasi jaringan ({count}). "
                "Ini kelanjutan pilihan tidurmu, bukan macet. Hari belum selesai; check menunggu semua pemain. "
                "wait mempertahankan kesiapan tanpa input; kamu boleh mengobrol sambil menunggu. "
                + ("Cancel/close (termasuk B jika ditawarkan) hanya untuk memilih bangun dan membatalkan tidur."
                   if cancel is True else "Native belum menawarkan batal; ikuti kontrol yang ditawarkan jika ingin bangun."))
    return (f"Ready check native: {label} (id={check}); {count}. "
            + ("Bisa dibatalkan lewat kontrol cancel/leave yang ditawarkan. " if cancel is True else
               "Native tidak mengizinkan batal saat ini. " if cancel is False else "Cancelability belum diketahui; ikuti kontrol native yang ditawarkan. ")
            + "wait mempertahankan kesiapan, cancel meninggalkan check; cancel bukan menyelesaikan check. Pilih sendiri.")

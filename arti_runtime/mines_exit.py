"""ADR 160: native exit geometry and remembered failures, never input authority.

ADR 239: the floor's way up is one case of the generic way-out primitive
(`arti_runtime.way_out`). A remembered failure bounds the program's own
recovery; a way out she chose is always a real try, never a refusal from memory.
"""

from arti_runtime import wait_ledger as W
import hashlib

from arti_stardew_world_extras import extras_targets, stand_tiles

MAX_FAILED_ATTEMPTS = 4  # program-recovery bound (ADR 160 fix91A2); her chosen way out is never refused by it (ADR 239)
EXHAUSTED = "mines_exit_attempts_exhausted"
REPLAYED = "mines_exit_replayed"  # run.state key: this run was refused from memory and sent no exit input


def exhausted(p, location):
    return p.mines_exit_recovery.get(location, {}).get("failed_attempts", 0) >= MAX_FAILED_ATTEMPTS


def chosen(run):
    """ADR 239: her own pick of a way out of this floor (the up ACTION or a trip from here).

    An exit leg the program starts inside a work skill, a planned campaign or a
    forced recovery goal is not her pick; the ADR 160 recovery bound keeps owning it.
    """
    state = getattr(run, "state", None) or {}
    call = getattr(run, "call", None)
    if call is None or not state.get("live7") or getattr(run, "by", None) not in ("model", "owner"):
        return False
    return (call.name == "aksi" and bool(state.get("mines_up_action") or state.get("mines_up_via_exit"))
            or call.name == "pergi_ke")


def exit_ran(run):
    """ADR 165 §4: a work leg that only planned an exit has not tried it (no controller pin yet)."""
    import arti_stardew_skills as S
    return not (run.call.name in S.MINES_SKILLS and not run.state.get("mines_exit_attempted"))


def note_failed(p, run, location, reason, context):
    """Count terminal exit runs once; new poses, ladders and goals never renew this bound.

    ADR 239: a run refused from memory sent nothing; replaying the old failure is not a new try.
    """
    if not str(location or "").startswith("UndergroundMine"):
        return
    if str(reason).endswith(EXHAUSTED) or run.state.get(REPLAYED):
        return  # a refused run cannot replace the last actual exit failure
    native_up = run.call.name == "aksi" and any(e["ref"] == run.call.arg for e in ups(context or {}, location, require_ready=False))
    if not exit_ran(run):
        return  # a failed work leg which only planned an exit has not run that exit
    if not (intent(run, location) or native_up) or run.state.get("mines_exit_failure_counted"):
        return
    run.state["mines_exit_failure_counted"] = True
    memo = p.mines_exit_recovery.setdefault(location, {})
    memo["failed_attempts"] = min(MAX_FAILED_ATTEMPTS, memo.get("failed_attempts", 0) + 1)
    memo["last_failure"] = reason
    memo["clock"] = (context or {}).get("clock")
    if exhausted(p, location):
        # ADR 165: cap repeated recovery, never a newly possible native exit.
        memo.update(blocker=reason, signature=signature(context, location))


def ups(context, location, *, require_ready=True):
    return [e for e in extras_targets(dict(extras_targets=context.get("extras", ())))
            if e["kind"] == "ladder_up" and (not require_ready or e.get("ready") is True)
            and e["ref"].startswith("xt:mine:" + str(location) + ":")]


def reaches(context, location):
    """Clearing goals only. A refused side must be re-published as a stand after removal."""
    from arti_stardew_debris_route import debris_kind, footprint
    out = []
    natural = {t for f in context.get("targets", {}).values()
               if debris_kind(f) in ("stone", "weeds", "twig", "tree") for t in footprint(f)}
    for e in ups(context, location):
        out.extend(tuple(s) for s in (stand_tiles(e) or ()))
        for miss in e.get("stand_misses") or ():
            if (isinstance(miss, (list, tuple)) and len(miss) == 3
                    and all(type(n) is int for n in miss[:2]) and miss[2] in ("object", "terrain_feature")
                    and abs(miss[0]-e["x"]) + abs(miss[1]-e["y"]) == 1 and tuple(miss[:2]) in natural):
                out.append(tuple(miss[:2]))
    return tuple(dict.fromkeys(out))


def intent(run, location):
    import arti_stardew_skills as S
    return str(location or "").startswith("UndergroundMine") and (
        run.call.name == "aksi" and (run.state.get("mines_up_action") or run.state.get("mines_up_via_exit"))
        or run.call.name == "pergi_ke" and (
            run.call.arg == "mine" and run.state.get("campaign_travel")
            or run.state.get("campaign_mine_recovery"))
        or run.call.name in S.MINES_SKILLS and run.state.get("wave1_driver") == "exit")


def reserve_exempt(run, location):
    """ADR 208: leaving is exempt from work curfew before the exit owns route repair.

    A generic floor walk retains its safe clearing policy until the native exit
    controller pins recovery. Intent must not silently replace that policy.
    """
    return intent(run, location) or (
        str(location or "").startswith("UndergroundMine")
        and run.call.name == "pergi_ke" and run.state.get("live7"))


def policy(runtime):
    session = getattr(runtime, "_skill_session", None)
    return getattr(getattr(session, "campaign", None), "live", None)


def signature(context, location):
    """Ignore clock/revision churn; changed geometry, tools or threats can warrant another attempt."""
    grid = context.get("nav_grid")
    if not grid or not context.get("ui_complete") or not context.get("inventory_complete"):
        return None
    data = (context.get("identity"), location, context.get("actor_tile"), context.get("actor_pixel"),
            context.get("actor_facing"), sorted(grid.items()),
            [(e["ref"], stand_tiles(e), e.get("stand_misses")) for e in ups(context, location)],
            sorted((f.get("qualified_item_id"), f.get("stack", 0)) for f in context.get("inventory", ())),
            [(m.get("id"), m.get("tile"), m.get("hp")) for m in (context.get("combat") or {}).get("monsters", ())],
            (context.get("combat") or {}).get("hp"))
    return hashlib.sha256(repr(data).encode()).hexdigest()


def remember(runtime, context, location, reason):
    p = policy(runtime)
    if p is not None:
        memo = p.mines_exit_recovery.setdefault(location, {})
        memo.update(blocker=reason, signature=signature(context, location), clock=(context or {}).get("clock"))


def closed_now(p, context, location):
    """ADR 239: the reason a trip out of this floor cannot be offered now, or None.

    The way-out primitive: the failure binds while her pose, the floor, its
    monsters, her bag and the game clock are those of the failure. Unknown floor
    geometry keeps the old answer, since the exit walk itself cannot run without
    it; her page then says what is unread.
    """
    reason = blocked(p, context, location)
    if reason is None or signature(context, location) is None:
        return reason
    from arti_runtime import way_out
    memo = p.mines_exit_recovery.get(location, {})
    bound = way_out.bind(place=location, clock=memo.get("clock"), world=memo.get("signature"))
    return reason if way_out.holds(bound, place=location, clock=context.get("clock"),
                                   world=signature(context, location)) else None


def blocked(p, context, location):
    memo = p.mines_exit_recovery.get(location, {})
    # ADR 169: dynamic threat/HP refusals owe another assessment, not a
    # signature-based veto. Keep the failure count and its explanation.
    dynamic = ("monster_corridor", "hp_falling", "clear_unsafe", "food_available")
    if any(code in str(memo.get("blocker", "")) for code in dynamic):
        return None
    sig = signature(context, location)
    if exhausted(p, location) and (sig is None or sig == memo.get("signature")):
        return EXHAUSTED
    if sig is not None and memo.get("signature") and sig != memo["signature"]:
        memo.pop("blocker", None)
        memo.pop("signature", None)  # keep failed edges across new goals; changed facts are not a retry reset
    return memo.get("blocker") if sig is not None and sig == memo.get("signature") else None


def _reason_text(reason):
    from arti_stardew_debris_route import blocker_needs
    words = {
        "mines_escape_native_unavailable": "refleks keluar native belum tersedia; fakta/senjata/kontrol terkini diperlukan",
        "mines_escape_fight_stopped": "pertarungan keluar belum membuktikan kemajuan; posisi/HP monster atau lintasan baru diperlukan",
        "mines_exit_food_available": "makanan terlihat; percobaan keluar sebelumnya berhenti sebelum meninggalkan lantai",
        "mines_exit_monster_corridor": "monster menutup lintasan aman ke tangga naik",
        "mines_exit_hp_falling": "HP berkurang saat keluar (catatan lama; sekarang jalan keluar tetap berlanjut)",
        "mines_exit_clear_unsafe": "monster dalam jangkauan kontak atau HP turun saat bersihkan jalan",
        "mines_exit_threat_geometry_unknown": "peta atau posisi monster terkini belum lengkap",
        "mines_exit_pose_stuck": "gerak pada sisi tangga tidak mengubah posisi",
        "mines_exit_native_press_unavailable": "sudah menghadap tangga tetapi tombol naik belum ditawarkan game",
    }
    for code, text in words.items():
        if code in reason:
            return reason + " (" + text + ")"
    needs = blocker_needs(reason)
    return reason + " (" + needs + ")" if needs else reason


def fact(p, context, location, calls=()):
    """ADR 239: her page states the last failed try once, what changed since, and what remains.

    The up ACTION is always a real try when she picks it; the words never say
    a choice on her page will be refused.
    """
    if not str(location or "").startswith("UndergroundMine"):
        return None
    memo = p.mines_exit_recovery.get(location, {})
    count = memo.get("failed_attempts", 0)
    reason = blocked(p, context, location)
    last = memo.get("last_failure") or memo.get("blocker") or reason
    if not count and not reason and not last:
        return None
    from arti_runtime.mines_survival import assessment
    from arti_runtime.way_out import said_clock
    approach = assessment(context, location)
    offers = ", ".join(sorted({c.name + (" " + c.arg if c.arg else "") for c in calls})) or "belum terbaca"
    parts = ["Penilaian keluar terkini: %s." % {
        "eat": "makan native tersedia", "fight": "pertarungan pembuka jalan", "exit": "lintasan/tombol naik",
        "repair": "perbaikan lintasan", "observe": "fakta belum lengkap"}[approach]]
    if last and last != EXHAUSTED:
        parts.append("Jalan naik terakhir gagal%s: %s." % (said_clock(memo.get("clock")), _reason_text(last)))
    if count:
        parts.append("Jalan naik: %d/%d percobaan gagal pada kunjungan lantai ini." % (count, MAX_FAILED_ATTEMPTS))
    if exhausted(p, location):
        parts.append("Program tidak lagi mencoba keluar sendiri di lantai ini; tangga naik tetap pilihanmu "
                     "dan setiap kali kaupilih, ia dicoba sungguhan.")
    if reason and signature(context, location) is None:
        parts.append("Peta, posisi atau monster lantai ini belum lengkap terbaca; jalan keluar dicoba lagi "
                     "begitu terbaca.")
    elif closed_now(p, context, location):
        parts.append("Sejak gagal belum ada yang berubah (posisi, peta, monster, tas dan jam sama).")
    else:
        parts.append("Sejak itu posisi, peta, monster, tas atau jam sudah berubah; percobaan berikutnya "
                     "dicoba sungguhan dengan pemeriksaan keselamatan.")
    parts.append("Tawaran tersisa: %s." % offers)
    return " ".join(parts)


@W.span('validation', 81, runtime=False)
def pose_step(snap, entry, state):
    """ADR 165: native stand + short faced flush; a stand never authorizes a ladder press."""
    import arti_stardew_skills as S
    from arti_stardew_runtime import _grab_tile, _CHEST_FLUSH_TICKS
    motor = snap.motor
    tile = S._wave1_xy(S._g(motor, "tile"))
    pixel = S._g(motor, "pixel")
    facing = S._g(motor, "facing")
    if tile not in {tuple(s) for s in stand_tiles(entry) or ()} or pixel is None:
        return S.Step("fail", reason="mines_exit_pose_unknown")
    delta = entry["x"]-tile[0], entry["y"]-tile[1]
    need = {(0,-1): 0, (1,0): 1, (0,1): 2, (-1,0): 3}[delta]
    pose = (tuple(pixel), facing)
    if state.get("exit_pose") == pose:
        return S.Step("fail", reason="mines_exit_pose_stuck")
    state["exit_pose"] = pose
    if facing == need and _grab_tile(pixel, facing) == (entry["x"], entry["y"]):
        return S.Step("fail", reason="mines_exit_native_press_unavailable")
    return S._decision(S.act("world_key.move_cardinal", dict(
        direction=("up", "right", "down", "left")[need], ticks=1 if facing != need else _CHEST_FLUSH_TICKS)))

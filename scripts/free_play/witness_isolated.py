"""Isolated witness for one action (FIX75D, ADR 132, controller contract rule 11). A witness proves hands, never gate credit.

She joins standing beside the action with the fitting items in her normal 12-slot bag, then plays on the normal page for
a small budget; she still chooses every step. One run per kind, judged alone. All staging is on a template COPY.

    python -B -m scripts.free_play.witness_isolated events --game DIR --out JSON
    python -B -m scripts.free_play.witness_isolated plan KIND --template DIR --objects-data JSON --positions JSON --events-data JSON
    python -B -m scripts.free_play.witness_isolated stage KIND --template DIR --objects-data JSON --positions JSON --events-data JSON \
        --root DIR --evidence DIR --base-spec JSON --spec-out JSON
    python -B -m scripts.free_play.witness_isolated verdict RUN --kind KIND --record JSON

`plan` is the dry run: it only reads the template and prints the edits and facts. `--positions` is the native map export
of `scripts/free_play/witness_map_probe` (tiles read from the game's own maps, never typed here); `--events-data` the `events` read of
Data/Events (ADR 135). The run itself is
`Invoke-Soak.ps1 soak 0.25h Standard -Spec SPEC -RealPath -Witness KIND`; the steps are in
`runbooks/briefs/WITNESS_ISOLATED_PLAN.md`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.free_play import stage_geode_witness as sg

EVENT_MAPS = ("ArchaeologyHouse", "Blacksmith", "CommunityCenter")  # the maps of KINDS' stands, as the map probe reads them
BUDGET_MINUTES = 15  # one game day is about 14 minutes; a real budget, see ADR 132
BAG = 12
MIN_FREE = 2  # GeodeMenu refuses a crack without a spare slot; a held remainder needs a slot to settle into
DEFAULT_BUNDLE = "Crafts Room/13"
FACING = {(0, 1): 0, (0, -1): 2, (1, 0): 3, (-1, 0): 1}  # stand minus target -> native facing toward the target
KINDS = {
    # add: Arrowhead (Arch, donatable) and Geode. south: the native action needs her south of the tile (GameLocation.cs:9873).
    "bundle": dict(menu="JunimoNoteMenu", target="community_note", location_type=sg.CC_TYPE, add="none", south=False),
    "museum": dict(menu="MuseumMenu", target="museum_counter", location_type="LibraryMuseum", action="Gunther", add="101:1", south=False),
    "geode": dict(menu="GeodeMenu", target="blacksmith", action="Blacksmith", add="535:1", south=True),
}


def choose_stand(row, *, south, source):
    """First walkable side in the mod's own order (WorldExtras.StandCandidates: south, north, east, west)."""
    x, y = row["x"], row["y"]
    for stand in row.get("stands", ()):
        offset = (stand["x"] - x, stand["y"] - y)
        if stand.get("walkable") is True and offset in FACING and (not south or offset == (0, 1)):
            return dict(x=stand["x"], y=stand["y"], facing=FACING[offset], target=[x, y], source=source)
    raise ValueError(f"no walkable stand beside the target at {x},{y}")


def resolve_stand(kind, positions, root, *, bundle=DEFAULT_BUNDLE, source="map probe"):
    """Location names come from the save; tiles from the native map export. Refuses instead of guessing."""
    spec = KINDS[kind]
    saved = {e.findtext("name"): e.get(sg.XSI + "type") for e in root.findall("locations/GameLocation")}
    if kind == "bundle":
        location = next((n for n, t in saved.items() if t == spec["location_type"]), None)
        room = bundle.split("/")[0]
        if location is None or room not in sg.CC_AREAS:
            raise ValueError(f"no Community Center location or unknown room {room!r}")
        row = ((positions.get("notes") or {}).get(location) or {}).get("areas", {}).get(str(sg.CC_AREAS.index(room)))
        if row is None:
            raise ValueError(f"note of {room!r} absent from the map export")
        return dict(location=location, **choose_stand(row, south=False, source=source))
    for location, entry in (positions.get("maps") or {}).items():
        if location not in saved or ("location_type" in spec and saved[location] != spec["location_type"]):
            continue
        rows = [r for r in entry.get("actions", ()) if r.get("action") == spec["action"]]
        if rows:
            return dict(location=location, **choose_stand(rows[0], south=spec["south"], source=source))
    raise ValueError(f"action {spec['action']!r} absent from the map export for this save's locations")


def preconditions(kind, facts, bag):
    """Named reasons this staged save cannot answer the kind's question. Empty means ready."""
    bad = []
    if bag != BAG or facts["max_items"] != [BAG, BAG]:
        bad.append(f"bag is {facts['max_items']}, expected {BAG} slots")
    if facts["free_slots_after"] < MIN_FREE:
        bad.append(f"only {facts['free_slots_after']} free slots")
    if kind == "bundle":
        b = facts["bundle"]
        if b["room_complete"] is not False or b["already_complete"] is not False or any(b["slots_filled"] or ()):
            bad.append("bundle is not untouched and open")
        if not b["every_ingredient_held"]:
            bad.append("an ingredient is not held")
    if kind == "museum" and facts["museum_pieces"] != 0:
        bad.append("museum already has pieces")
    if kind == "geode" and (facts["geode_535_after"] < 1 or facts["geode_cracks_affordable"] < 1 or not facts["geode_menu_room"]):
        bad.append("no geode, no 25g or no spare slot")
    return bad


def export_events(game):
    """Native Data/Events keys for the maps a witness can stand on: "id/precondition/...". Read-only; scripts are left out.
    A map with no file has no events: the game loads Data/Events/<location> and takes a missing asset as none."""
    from scripts.m1_operator import xnb_items as xnb
    maps = {}
    for location in EVENT_MAPS:
        path = Path(game) / "Content" / "Data" / "Events" / (location + ".xnb")
        raw = path.read_bytes() if path.is_file() else None
        keys = sorted(xnb.string_dictionary(xnb.xnb_payload(raw))) if raw else []
        maps[location] = dict(file=path.name if raw else None, sha256=hashlib.sha256(raw).hexdigest() if raw else None, keys=keys)
    return dict(schema="arti.witness.events/1", source="Content/Data/Events", maps=maps)


def first_entry_events(keys):
    """A location's first-entry scenes, as ids. The key is "id/condition/...", and a scene that fires on her first step inside asks only
    about her own record: mail flags (`n`, `l`) and events seen (`e`, `k`). Witness museum: "0/n somethingToDonate/l somethingWasDonated"
    ran at join. A friendship or an hour (Elliott's "1848481/f Elliott 2000/t 1300 1900/...") waits for play; a key with no numeric id
    ("mysteryBook", "Punch") is a branch of another scene and never fires alone."""
    out = []
    for key in keys:
        event, *conditions = key.split("/")
        if event.isdigit() and all(c.split(" ")[0] in ("n", "l", "e", "k") for c in conditions):
            out.append(event)
    return out


def staged_events(events, location):
    """The rows a stand's location has in the native export, and the ones marked seen on the copy. Refuses instead of guessing."""
    row = (events.get("maps") or {}).get(location)
    if row is None:
        raise ValueError(f"location {location!r} absent from the events export")
    return dict(location=location, file=row.get("file"), keys=row.get("keys", []), marked_seen=first_entry_events(row.get("keys", ())))


def dry_run(kind, data, objects, positions, events, *, bundle=DEFAULT_BUNDLE, positions_sha="unpinned"):
    root = ET.fromstring(data)
    peer = sg.farmhand(root)
    stand = resolve_stand(kind, positions, root, bundle=bundle, source="map probe sha256 " + positions_sha)
    add = sg.parse_add(KINDS[kind]["add"])
    first_entry = staged_events(events, stand["location"])
    result, edits, facts = sg.plan(data, objects, add, backpack=int(peer.findtext("maxItems")), farmhand_name=peer.findtext("name"),
                                   bundle=bundle if kind == "bundle" else None, position=stand, min_free=MIN_FREE,
                                   seen_events=first_entry["marked_seen"])
    problems = preconditions(kind, facts, int(peer.findtext("maxItems")))
    return dict(kind=kind, tag="STAGING", credit=False, budget_minutes=BUDGET_MINUTES, max_game_days=1, stand=stand, first_entry=first_entry,
                save_sha256_before=sg.sha(data), save_sha256_after=sg.sha(result), edit_count=len(edits), facts=facts,
                ready=not problems, problems=problems)


def _inputs(a):
    template = Path(a.template)
    data = (template / template.name).read_bytes()
    positions_bytes = Path(a.positions).read_bytes()
    events = json.loads(Path(a.events_data).read_text(encoding="utf-8"))
    return template, data, json.loads(Path(a.objects_data).read_text(encoding="utf-8")), json.loads(positions_bytes), positions_bytes, events


def stage(a):
    """Template COPY -> stand and items -> record -> witness spec. The canonical template is only read."""
    template, data, objects, positions, positions_bytes, events = _inputs(a)
    report = dry_run(a.kind, data, objects, positions, events, bundle=a.bundle, positions_sha=hashlib.sha256(positions_bytes).hexdigest())
    if not report["ready"]:
        raise ValueError("not ready: " + "; ".join(report["problems"]))
    copy = Path(a.root) / a.kind / template.name
    evidence = Path(a.evidence) / a.kind
    spec_out = Path(a.spec_out)
    if copy.exists() or evidence.exists() or spec_out.exists():
        raise ValueError("copy, evidence or spec exists; refuse overwrite")
    copy.parent.mkdir(parents=True)
    shutil.copytree(template, copy)
    evidence.mkdir(parents=True)
    (evidence / "stand.json").write_text(json.dumps(report["stand"], indent=2) + "\n", encoding="utf-8")
    argv = ["apply", "--copy", str(copy), "--objects-data", str(a.objects_data), "--add", KINDS[a.kind]["add"],
            "--backpack", str(BAG), "--min-free", str(MIN_FREE), "--stand-data", str(evidence / "stand.json"),
            "--expect-sha", report["save_sha256_before"], "--backup", str(evidence / "backup" / template.name),
            "--record", str(evidence / "staging.json")]
    if a.kind == "bundle":
        argv += ["--bundle", a.bundle]
    if report["first_entry"]["marked_seen"]:
        argv += ["--seen-events", ",".join(report["first_entry"]["marked_seen"])]
    sg.main(argv)
    spec = json.loads(Path(a.base_spec).read_text(encoding="utf-8-sig"))
    spec["templates"] = dict(spec["templates"], Standard=str(copy.resolve()))
    spec["codeSha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    spec_out.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return dict(kind=a.kind, copy=str(copy), record=str(evidence / "staging.json"), spec=str(spec_out), stand=report["stand"])


# ------------------------------------------------------------------------------------- verdict
def journal(run):
    """Every journal row of the run as (D<day>:J<line>, row)."""
    for path in sorted(Path(run).glob("runtime/live7/c*/day-*/journal/g1_journal.jsonl")):
        day = re.match(r"day-(\d+)", path.parts[-3])[1]
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield f"D{int(day)}:J{n}", row


def native_rows(run, kind):
    """NATIVE_PROGRESS rows of this kind; strict reading of what credits a witness's hands."""
    path = Path(run) / "farmhand-SMAPI.txt"
    if not path.is_file():
        return []
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if "NATIVE_PROGRESS " in line:
            try:
                row = json.loads(line.split("NATIVE_PROGRESS ", 1)[1])
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("kind") == kind:
                rows.append((f"farmhand-SMAPI.txt:L{n}", row))
    return rows


NO_EFFECT = "UiPressNoEffect"


def good_deposit(row):
    return (row.get("actor_verified") is True and row.get("flag_before") is False and row.get("flag_after") is True
            and bool(row.get("request_id")) and type(row.get("quantity")) is int and row["quantity"] > 0
            and row.get("inventory_delta") == -row["quantity"])


def good_donation(row):
    return (row.get("actor_verified") is True and row.get("museum_before") is False and row.get("museum_after") is True
            and row.get("quantity") == 1 and row.get("inventory_delta") == -1 and bool(row.get("request_id")))


def crack_readback(row):
    """A GeodeMenu press that took 25g and one geode: readbacks only, the game has no native receipt (G2). The geode she cracks is in
    her hand (`ui_held_*`, the menu's own heldItem; `cursor_item` stays empty in this menu), so a bag-only count never saw a crack."""
    try:
        back = json.loads(row.get("readback") or "{}")
        before, after = back["before"], back["after"]

        def count(side):
            held = side.get("ui_held_stack") or 0 if side.get("ui_held_item") == "(O)535" else 0
            return held + sum(i.get("stack", 0) for i in side.get("inventory", ()) if i.get("item") == "(O)535")
        return before["money"] - after["money"] == sg.GEODE_COST and count(before) - count(after) == 1
    except (ValueError, KeyError, TypeError):
        return False


def held_before(row):
    """What her hand held when the press went in, from the press's own readback. None when the readback does not say. A menu's own
    heldItem is `ui_held_item`; `cursor_item` is the player's cursor slot and stays empty in these menus (a56 d5 J1192)."""
    try:
        before = json.loads(row.get("readback") or "{}")["before"]
        return before.get("ui_held_item") or before["cursor_item"]
    except (ValueError, KeyError, TypeError):
        return None


def curfew_note(row, chosen, menu, reached):
    """ADR 135: a curfew end is the game day speaking, never a hands result. It is noted with the time she spent in the menu."""
    end, begin = (row.get("game") or {}).get("time"), (chosen or {}).get("time")
    minutes = None
    if type(end) is int and type(begin) is int and reached:
        minutes = (end // 100 * 60 + end % 100) - (begin // 100 * 60 + begin % 100)
    span = f" in {menu} from {begin:04d} to {end:04d}, {minutes // 60} h {minutes % 60:02d} of game time" if minutes is not None else ""
    return dict(ref=row["_ref"], reason="curfew", menu_minutes=minutes, note=f"ended on curfew{span}")


def judge(kind, run, staged_location):
    """PASSED / UNPROVEN / FAILED / NOT_STAGED for one kind, from the run's own journals and native log. Never credit."""
    spec = KINDS[kind]
    start = chosen = curfew = None
    picks, inspected, presses, refused, cracks = [], [], [], [], []
    for ref, row in journal(run):
        game = row.get("game")
        if start is None and isinstance(game, dict) and game.get("location"):
            start = game["location"]
        ev = row.get("ev")
        if ev == "skill_chosen" and spec["target"] in str(row.get("arg")) and row.get("by") == "model":
            chosen = chosen or dict(ref=ref, skill=row.get("skill"), arg=row.get("arg"), time=(game or {}).get("time"))
        elif ev == "ui_inspect" and row.get("menu_type") == spec["menu"]:
            inspected.append(ref)
        elif ev == "ui_press" and row.get("phase") == "result" and row.get("result_menu_type") == spec["menu"]:
            presses.append(dict(ref=ref, status=row.get("status"), verified=row.get("verified"), reason=row.get("reason"),
                                outcome=row.get("outcome"), held=held_before(row)))
            if kind == "geode" and crack_readback(row):
                cracks.append(ref)
        elif ev == "live7_ui_choice":
            picks.append(dict(ref=ref, pick=row.get("pick"), why=str(row.get("why"))[:160], why_src=row.get("why_src")))
        elif ev == "skill_failed" and spec["target"] in str(row.get("arg")):
            if row.get("reason") == "curfew":
                curfew = dict(row, _ref=ref)
            else:
                refused.append(dict(ref=ref, reason=row.get("reason")))
    if kind == "bundle":
        rows = native_rows(run, "cc_bundle_deposit")
        proven = [ref for ref, r in rows if good_deposit(r)]
    elif kind == "museum":
        rows = native_rows(run, "museum_donation")
        proven = [ref for ref, r in rows if good_donation(r)]
    else:
        rows, proven = [], cracks
    unverified = [ref for ref, r in rows if r.get("actor_verified") is not True]
    # ADR 135: a press the adapter took and the game answered with no effect is an offered control that did nothing, a fumble. A press
    # she was not offered never reaches a ui_press row (`invalid_model_choice`). What counts against hands is a press that failed.
    fumbles = [p for p in presses if p["status"] == "completed" and p["outcome"] == NO_EFFECT]
    bad_press = [p for p in presses if p["status"] != "completed" or (p["verified"] is not True and p not in fumbles)]
    held_fumbles = [p for p in fumbles if p["held"]]
    reached = bool(presses or inspected)
    if start != staged_location:
        verdict, why = "NOT_STAGED", f"first location {start!r}, staged {staged_location!r}: no judgement, staging or join fault"
    elif proven and not bad_press:
        verdict, why = "PASSED", "hands: strict receipts and every press an offered control; fumbles are listed beside it; no operator input exists in this path"
    elif (chosen or reached) and (bad_press or refused or proven or held_fumbles):
        verdict, why = "FAILED", "she went for it and a press or the target failed; name the file:line from the refs"
    elif not chosen and not reached:
        verdict, why = "UNPROVEN", "not chosen: an agency note, not a fault"
    elif unverified:
        verdict, why = "UNPROVEN", "world changed but actor_verified is false: not proven her press did it"
    else:
        verdict, why = "UNPROVEN", "she went for it; she left or the run ended before the effect"
    return dict(schema="arti.witness.isolated/1", kind=kind, tag="STAGING", credit=False, scored=False, verdict=verdict, why=why,
                evidence="UNREVIEWED_LIVE", start_location=start, staged_location=staged_location, chosen=chosen,
                menu=spec["menu"], menu_reached=reached, menu_refs=inspected[:8], presses=presses[:40], press_count=len(presses),
                failed_presses=bad_press[:20], fumbles=fumbles[:20], fumble_count=len(fumbles),
                curfew=curfew_note(curfew, chosen, spec["menu"], reached) if curfew else None,
                refused=refused[:10], effect_refs=proven[:20], actor_unverified_refs=unverified[:20],
                own_words=picks[:12], geode_caveat="no native receipt exists; readbacks only" if kind == "geode" else None)


def verdict(a):
    run = Path(a.run)
    if not (run / "WITNESS.json").is_file():
        raise ValueError("run is not marked WITNESS.json; this tool only reads witness runs")
    record = json.loads(Path(a.record).read_text(encoding="utf-8"))
    result = judge(a.kind, run, record["position"]["location"])
    out = run / "WITNESS_VERDICT.json"
    if out.exists():
        raise ValueError("WITNESS_VERDICT.json exists; refuse overwrite")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "stage"):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=KINDS)
        p.add_argument("--template", required=True, help="template folder, read only; named like its save")
        p.add_argument("--objects-data", required=True, help="native Data/Objects export")
        p.add_argument("--positions", required=True, help="output of witness_map_probe")
        p.add_argument("--events-data", required=True, help="output of the `events` command")
        p.add_argument("--bundle", default=DEFAULT_BUNDLE)
        if name == "stage":
            for flag in ("root", "evidence", "base-spec", "spec-out"):
                p.add_argument("--" + flag, required=True)
    v = sub.add_parser("verdict")
    v.add_argument("run")
    v.add_argument("--kind", required=True, choices=KINDS)
    v.add_argument("--record", required=True)
    e = sub.add_parser("events")
    e.add_argument("--game", required=True, help="the game folder; read only")
    e.add_argument("--out", required=True)
    a = parser.parse_args(argv)
    if a.command == "events":
        out = Path(a.out)
        if out.exists():
            raise ValueError("events export exists; refuse overwrite")
        report = export_events(a.game)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0
    if a.command == "plan":
        template, data, objects, positions, positions_bytes, events = _inputs(a)
        report = dry_run(a.kind, data, objects, positions, events, bundle=a.bundle, positions_sha=hashlib.sha256(positions_bytes).hexdigest())
        print(json.dumps(dict(report, dry_run=True), indent=2))
        return 0 if report["ready"] else 2
    print(json.dumps(stage(a) if a.command == "stage" else verdict(a), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

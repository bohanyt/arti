"""Evaluator 1.2.0 journey score: progress by the game's own yardsticks. Offline; never imports the game runner.

Endurance stays separate (evaluate.endurance). Every number names its source: ``snapshot`` (runtime
``progress_snapshot`` journal rows), ``save_diff`` (hash-verified start template vs the retained end save),
``journal`` (verified effect rows) or ``unknown`` (null, never 0). The journey summary is never pass/fail and
never ranks runs (ranking stays endurance). No soul/prefs/config is read; persona excerpts are scrubbed.
"""
from __future__ import annotations

from arti_runtime import ipc
from scripts.free_play import storage
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"
SKILLS = ("farming", "fishing", "foraging", "mining", "combat", "luck")
TOOL_CLASSES = ("Axe", "Hoe", "WateringCan", "Pickaxe", "FishingRod", "Pan", "Shears", "MilkPail")
STAT_KEYS = {"crops_shipped": "cropsShipped", "items_shipped": "itemsShipped", "fish_caught": "fishCaught",
             "quests_completed": "questsCompleted", "items_crafted": "itemsCrafted", "seeds_sown": "seedsSown",
             "items_foraged": "itemsForaged", "monsters_killed": "monstersKilled",
             "individual_money_earned": "individualMoneyEarned"}
CC_ROOM_MAIL = ("ccBoilerRoom", "ccCraftsRoom", "ccPantry", "ccFishTank", "ccVault", "ccBulletin")
GRANDPA_MAX = 21
HEART_POINTS = 250
# Audit census (runbooks/spec-audit/PROGRESS_GAP_AUDIT_2026-10-29.md §5), reproduced exactly on a14-a17.
PROGRESS_SKILLS = frozenset(("tanam", "panen", "beli", "kirim", "buat", "craft", "masak", "mancing", "pancing",
                             "tangkap_ikan", "siapkan_pancing", "upgrade_alat"))
CARE_SKILLS = frozenset(("siram", "jual", "cangkul"))
PERSONA_SOURCES = ("owner_probe", "discovery", "status", "game", "initiative", "moment")
VIEWER_FIELD = re.compile(r"(?i)^(author|author_name|user|username|user_name|viewer|viewer_name|display_name|"
                          r"chat_author|sender|nick|nickname|donor|member_name)$")
CHAT_SOURCE = re.compile(r"(?i)(chat|viewer|youtube|twitch|tiktok|donat|superchat|member|comment)")
HANDLE = re.compile(r"@[\w.\-]{2,}")
SAMPLES_PER_SESSION = 10


# --------------------------------------------------------------------------- game rules (installed-game IL)

def grandpa(fields):
    """Utility.getGrandpaScore criteria, checked against the installed Stardew Valley.dll IL (fix13c ilprobe).

    ``fields`` keys: money_earned (FarmerTeam.totalMoneyEarned), achievements (ints), host_mail, self_mail,
    events_seen, married, house_upgrade, friend_points (dict), skills (dict of levels). A None input makes only the
    criteria that need it unknown. Returns dict(points, max, candles, criteria=[[id, met|None, provenance]])."""
    money = fields.get("money_earned")
    ach = fields.get("achievements")
    host, mine = fields.get("host_mail"), fields.get("self_mail")
    seen = fields.get("events_seen")
    friends = fields.get("friend_points")
    skills = fields.get("skills")
    def has(collection, value):
        return None if collection is None else value in collection
    good = None if friends is None else sum(1975 <= p <= 999999 for p in friends.values())
    level = None if not skills or any(type(skills.get(s)) is not int for s in SKILLS) else sum(skills[s] for s in SKILLS) // 2
    accessible = has(seen, "191393")
    complete = None if mine is None else all(m in mine for m in CC_ROOM_MAIL)
    married = fields.get("married")
    house = fields.get("house_upgrade")
    rows = [("earned_50k", None if money is None else money >= 50000, 1, "team"),
            ("earned_100k", None if money is None else money >= 100000, 1, "team"),
            ("earned_200k", None if money is None else money >= 200000, 1, "team"),
            ("earned_300k", None if money is None else money >= 300000, 1, "team"),
            ("earned_500k", None if money is None else money >= 500000, 1, "team"),
            ("earned_1m", None if money is None else money >= 1000000, 2, "team"),
            ("museum_complete_collection", has(ach, 5), 1, "individual"),
            ("skull_key", has(host, "HasSkullKey"), 1, "team(host mail)"),
            ("community_center_complete", None if accessible is None and complete is None else bool(accessible or complete), 1, "individual"),
            ("community_center_ceremony", accessible, 2, "individual"),
            ("married_house_upgrade_2", None if married is None or house is None else bool(married and house >= 2), 1, "individual"),
            ("rusty_key", has(host, "HasRustyKey"), 1, "team(host mail)"),
            ("master_angler", has(ach, 26), 1, "individual"),
            ("full_shipment", has(ach, 34), 1, "individual"),
            ("friends_8h_5", None if good is None else good >= 5, 1, "individual"),
            ("friends_8h_10", None if good is None else good >= 10, 1, "individual"),
            ("skill_level_15", None if level is None else level >= 15, 1, "individual"),
            ("skill_level_25", None if level is None else level >= 25, 1, "individual"),
            ("pet_love", has(mine, "petLoveMessage"), 1, "individual")]
    points = sum(w for _, met, w, _ in rows if met)
    unknown = [cid for cid, met, _, _ in rows if met is None]
    return dict(points=points, max=GRANDPA_MAX, candles=candles(points), unknown=unknown,
                criteria=[[cid, met, prov] for cid, met, _, prov in rows],
                basis="Utility.getGrandpaScore/getGrandpaCandlesFromScore (installed IL); points count known criteria only")


def candles(points):
    return 4 if points >= 12 else 3 if points >= 8 else 2 if points >= 4 else 1


def bundle_status(bundle_data, completion):
    """bundle_data {"Room/index": raw}, completion {index: [bool]} -> list of (room, index, done). A bundle is done
    when its true slots reach the raw field 4 count (empty = every listed item)."""
    out = []
    for key, raw in bundle_data.items():
        room, _, index = key.partition("/")
        if not index.isdigit():
            continue
        parts = str(raw).split("/")
        items = len(parts[2].split()) // 3 if len(parts) > 2 else 0
        required = int(parts[4]) if len(parts) > 4 and parts[4].strip().isdigit() else items
        slots = completion.get(int(index))
        out.append((room, int(index), None if slots is None else sum(bool(s) for s in slots) >= max(1, required)))
    return out


# --------------------------------------------------------------------------- save reader

def _text(element, path):
    found = element.find(path)
    return None if found is None else (found.text or "")


def _int(element, path):
    value = _text(element, path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strings(element, path):
    found = element.find(path)
    return None if found is None else {c.text for c in found if c.text}


@lru_cache(maxsize=8)
def _read_save(path_text, stamp):
    return save_progress(ET.fromstring(storage.read_bytes(path_text)))


def read_save(path, farmhand):
    path = Path(path)
    if not storage.exists(path):
        return None
    data = _read_save(str(path), storage.digest_file(path))
    return data.get(farmhand)


def save_progress(root):
    """{farmhand name: progress} for each farmhand in a SaveGame XML root (snapshot-shaped plus save extras)."""
    host = root.find("player")
    host_mail = _strings(host, "mailReceived") if host is not None else None
    cc_loc = next((loc for loc in root.findall("locations/GameLocation") if loc.get(XSI) == "CommunityCenter"), None)
    bundle_data = {i.findtext("key/string"): i.findtext("value/string") or "" for i in root.findall("bundleData/item")}
    completion = {}
    rooms = None
    if cc_loc is not None:
        for item in cc_loc.findall("bundles/item"):
            try:
                completion[int(item.findtext("key/int"))] = [b.text == "true" for b in item.find("value/ArrayOfBoolean")]
            except (TypeError, ValueError):
                continue
        rooms = [c.text == "true" for c in cc_loc.find("areasComplete")] if cc_loc.find("areasComplete") is not None else None
    status = bundle_status(bundle_data, completion) if bundle_data else []
    cc = dict(bundles_done=sum(1 for *_, d in status if d) if status and all(d is not None for *_, d in status) else None,
              bundles_total=len(status) or None, rooms_done=sum(rooms) if rooms is not None else None,
              rooms_total=len(rooms) if rooms is not None else None,
              joja=None if host_mail is None else "JojaMember" in host_mail,
              remaining=[[room, idx] for room, idx, d in status if d is False][:40], provenance="team")
    result = {}
    for farmer in root.findall("farmhands/Farmer"):
        name = _text(farmer, "name")
        if not name:
            continue
        skills = {s: _int(farmer, s + "Level") for s in SKILLS}
        tools = {}
        for item in farmer.findall("items/Item"):
            cls = item.get(XSI)
            level = _int(item, "upgradeLevel")
            if cls in TOOL_CLASSES and level is not None:
                tools[cls] = max(level, tools.get(cls, 0))
        friends, married = {}, False
        for item in farmer.findall("friendshipData/item"):
            npc = item.findtext("key/string")
            points = item.findtext("value/Friendship/Points")
            if npc and points is not None and points.lstrip("-").isdigit():
                friends[npc] = int(points)
            married |= item.findtext("value/Friendship/Status") in ("Married", "Roommate")
        values = farmer.find("stats/Values")
        stats = None
        if values is not None:
            raw = {i.findtext("key/string"): i.findtext("value/unsignedInt") for i in values}
            # Stats.Get semantics: an absent key is 0, not unknown, once the Values store exists.
            stats = {k: int(raw.get(v) or 0) for k, v in STAT_KEYS.items()}
        quests = farmer.findall("questLog/Quest")
        self_mail = _strings(farmer, "mailReceived")
        visited = _strings(farmer, "locationsVisited")
        achievements = {int(c.text) for c in farmer.find("achievements") if c.text and c.text.lstrip("-").isdigit()} \
            if farmer.find("achievements") is not None else None
        events = _strings(farmer, "eventsSeen")
        earned = _int(farmer, "totalMoneyEarned")
        house = _int(farmer, "houseUpgradeLevel")
        result[name] = dict(
            money=_int(farmer, "money"), money_earned=earned, skills=skills, tools=tools, friends=friends,
            stats=stats, cc=cc, places=dict(entered=len(visited) if visited is not None else None),
            quests=dict(active=sum(_text(q, "completed") != "true" for q in quests),
                        completed_total=(stats or {}).get("quests_completed")),
            mail=dict(read=len(self_mail) if self_mail is not None else None,
                      unread=len(farmer.find("mailbox")) if farmer.find("mailbox") is not None else None),
            grandpa=grandpa(dict(money_earned=earned, achievements=achievements, host_mail=host_mail,
                                 self_mail=self_mail, events_seen=events, married=married, house_upgrade=house,
                                 friend_points=friends, skills=skills)),
            _visited=sorted(visited or ()))
    return result


def sha256(path):
    return storage.digest_file(path)


def save_pair(run):
    """(start, end, info). Start = the staged template, only when its hash equals STAGING.json's record."""
    run = Path(run)
    staging_path = run / "STAGING.json"
    info = dict(start=None, end=None)
    if not staging_path.is_file():
        info["reason"] = "STAGING.json missing"
        return None, None, info
    staging = json.loads(ipc.read_text(staging_path))
    folder, name = staging.get("save_folder"), staging.get("farmhand_name")
    if not folder or not name:
        info["reason"] = "STAGING.json lacks save_folder/farmhand_name"
        return None, None, info
    start = end = None
    template = Path(str(staging.get("template") or "")) / folder
    expected = (staging.get("template_hashes") or {}).get(folder)
    if storage.exists(template) and expected:
        actual = sha256(template)
        info["start"] = dict(file=template.name, sha256=actual, verified=actual == expected)
        if actual == expected:
            start = read_save(template, name)
    else:
        info["start"] = dict(file=template.name, verified=False, reason="template or recorded hash missing")
    end_path = run / "preserved-saves" / folder / folder
    if storage.exists(end_path):
        info["end"] = dict(file="preserved-saves/%s/%s" % (folder, folder), sha256=sha256(end_path))
        end = read_save(end_path, name)
    return start, end, info


# --------------------------------------------------------------------------- deltas

def _num(value):
    return value if type(value) is int else None


def _diff(a, b):
    return b - a if type(a) is int and type(b) is int else None


def _tool_key(qid):
    name = re.sub(r"^\([A-Z]+\)", "", str(qid))
    return re.sub(r"^(Copper|Steel|Gold|Iridium|Bamboo|Fiberglass|Training|Advanced)", "", name) or name


def delta(start, end):
    """Field deltas between two progress dicts; a field missing on either side is None."""
    if not start or not end:
        return {}
    sk_s, sk_e = start.get("skills") or {}, end.get("skills") or {}
    skills = {s: _diff(sk_s.get(s), sk_e.get(s)) for s in SKILLS if s in sk_s and s in sk_e}
    tools = None
    if isinstance(start.get("tools"), dict) and isinstance(end.get("tools"), dict):
        norm = lambda d: {_tool_key(k): v for k, v in d.items() if type(v) is int}  # noqa: E731
        a, b = norm(start["tools"]), norm(end["tools"])
        # ADR 103 (a47 "(T)TrainingRod" at level 1 counted as an upgrade): only a level rise of a tool she already
        # held counts; a tool first seen at the end is an acquisition at its base level, never an upgrade.
        tools = sum(max(0, v - a[k]) for k, v in b.items() if k in a)
    fr = None
    if isinstance(start.get("friends"), dict) and isinstance(end.get("friends"), dict):
        a, b = start["friends"], end["friends"]
        fr = dict(points=sum(b.get(n, 0) - a.get(n, 0) for n in set(a) | set(b)),
                  hearts=sum(max(0, b.get(n, 0) // HEART_POINTS - a.get(n, 0) // HEART_POINTS) for n in set(a) | set(b)),
                  npcs_new=len(set(b) - set(a)))
    stats_s, stats_e = start.get("stats") or {}, end.get("stats") or {}
    cc_s, cc_e = start.get("cc") or {}, end.get("cc") or {}
    return dict(money_earned=_diff(start.get("money_earned"), end.get("money_earned")),
                money_earned_individual=_diff(stats_s.get("individual_money_earned"), stats_e.get("individual_money_earned")),
                skill_levels=skills if skills and all(v is not None for v in skills.values()) else None,
                tool_upgrades=tools, friendship=fr,
                bundles_completed=_diff(cc_s.get("bundles_done"), cc_e.get("bundles_done")),
                quests_completed=_diff(stats_s.get("quests_completed"), stats_e.get("quests_completed"))
                if "quests_completed" in stats_s and "quests_completed" in stats_e
                else _diff((start.get("quests") or {}).get("completed_total"), (end.get("quests") or {}).get("completed_total")),
                new_places=_diff((start.get("places") or {}).get("entered"), (end.get("places") or {}).get("entered")),
                crops_harvested=_diff((start.get("crops") or {}).get("harvested"), (end.get("crops") or {}).get("harvested")),
                crops_shipped=_diff(stats_s.get("crops_shipped"), stats_e.get("crops_shipped")))


def journal_effects(journals):
    """Verified effect receipts only: harvested items (native delta), planted/shipped rows, first-entered places."""
    harvested = planted = shipped = 0
    places = set()
    any_row = False
    for _, rows in journals:
        for _, row in rows:
            ev = row.get("ev")
            if ev == "part_live_row":
                any_row = True
                if row.get("verified") is not True:
                    continue
                skill = row.get("skill")
                observed = (row.get("evidence") or {}).get("observed") if isinstance(row.get("evidence"), dict) else None
                if skill == "panen":
                    effects = [e for e in (observed or {}).get("effects") or () if isinstance(e, dict) and e.get("kind") == "harvest_crop"]
                    harvested += sum(e.get("delta") if type(e.get("delta")) is int else 1 for e in effects) if effects else 1
                elif skill == "tanam":
                    planted += 1
                elif skill == "kirim":
                    shipped += 1
            elif ev == "discovery" and row.get("kind") == "location_entered" and row.get("subject"):
                places.add(row["subject"])
    return dict(crops_harvested=harvested if any_row else None, plant_rows=planted if any_row else None,
                ship_rows=shipped if any_row else None, places_first_entered=len(places))


def snapshots(journals):
    rows = [r for _, rs in journals for _, r in rs if r.get("ev") == "progress_snapshot" and isinstance(r.get("progress"), dict)]
    return rows


# --------------------------------------------------------------------------- understanding

def _reason_class(reason):
    return re.sub(r"\d+", "#", str(reason or "unknown"))[:80]


def understanding(journals):
    decisions = [r for _, rs in journals for _, r in rs if r.get("ev") == "skill_model_decision" and r.get("choice") != "invalid"]
    names = [r["choice"][0] if isinstance(r.get("choice"), list) and r["choice"] else None for r in decisions]
    cek = sum(n == "cek_pagi" for n in names)
    home = sum(n == "pergi_ke" and len(r["choice"]) > 1 and r["choice"][1] == "home" for n, r in zip(names, decisions))
    closed = sum(r.get("ev") == "live7_ui_choice" and (r.get("result_v1") or {}).get("changed") == "closed"
                 for _, rs in journals for _, r in rs)
    progress = sum(n in PROGRESS_SKILLS for n in names)
    care = sum(n in CARE_SKILLS for n in names)
    n = len(decisions)
    pct = lambda x: round(100 * x / n, 1) if n else None  # noqa: E731
    reflections = [r for _, rs in journals for _, r in rs if r.get("ev") == "mind_reflection"]
    set_ids = [g for r in reflections for g in r.get("new_goals") or ()]
    done = [d for r in reflections for d in r.get("done") or () if isinstance(d, dict)]
    evidenced = [d for d in done if d.get("ref") and ("evidence" not in d or (isinstance(d["evidence"], str) and d["evidence"].strip()))]
    from arti_runtime.route_pause import pause_reason
    failures, seen, repeats = [], Counter(), 0
    for _, rs in journals:
        for _, r in rs:
            if (r.get("ev") == "part_live_row" and r.get("verified") is not True
                    and r.get("outcome_class") in ("CLEAN_FAIL", "UNCERTAIN", "NOT_READY")
                    and "by_choice" not in str(r.get("reason"))
                    and not pause_reason(r.get("reason"))):
                key = (str(r.get("skill")), str(r.get("arg")), _reason_class(r.get("reason")))
                repeats += seen[key] > 0
                seen[key] += 1
                failures.append(key)
    top = [[*k, c] for k, c in seen.most_common() if c > 1][:5]
    return dict(
        decisions=n, loop_proxy=cek + home + closed, loop_parts=dict(cek_pagi=cek, home=home, ui_closed=closed),
        loop_proxy_pct=pct(cek + home + closed), progress_choices=progress, progress_choice_pct=pct(progress),
        care_choices=care, activity_kinds=len({x for x in names if x}), activity_kinds_list=sorted({x for x in names if x}),
        goals_set=len(set_ids), goals_done=len(done), goals_done_with_evidence=len(evidenced),
        goals_done_ratio=round(len(evidenced) / len(set_ids), 3) if set_ids else None,
        goals_carried=sum(len(r.get("carried") or r.get("kept") or ()) for r in reflections),
        failures=len(failures), repeated_failures=repeats,
        repeat_failure_pct=round(100 * repeats / len(failures), 1) if failures else None, top_repeated_failures=top,
        definition=("Audit §5 census, reproduced exactly on a14-a17: decisions = non-invalid skill_model_decision rows; "
                    "loop proxy = cek_pagi + pergi_ke home + live7_ui_choice result_v1.changed=closed (includes legitimate "
                    "routines; not a wasted-time fraction); progress = tanam/panen/beli/kirim/craft/fish/upgrade_alat; "
                    "care (siram/jual/cangkul) separate. Goal done counts only with a verified ref (and a non-empty "
                    "evidence string when the row carries one). Failure = unverified part_live_row CLEAN_FAIL/UNCERTAIN/"
                    "NOT_READY except *_by_choice and route_paused_new_map/route_paused_people_here; "
                    "repeat = same (skill, arg, reason class) seen earlier in the run."))


# --------------------------------------------------------------------------- persona samples

def _viewer_names(rows):
    names = set()
    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if VIEWER_FIELD.match(str(k)) and isinstance(v, str) and len(v.strip()) >= 2:
                    names.add(v.strip())
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    for row in rows:
        walk(row)
    return names


def scrub(text, names):
    text = HANDLE.sub("[viewer]", str(text))
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(r"(?i)(?<![\w])" + re.escape(name) + r"(?![\w])", "[viewer]", text)
    return re.sub(r"\s+", " ", text).strip()


def persona(run):
    path = Path(run) / "bridge/bridge-transcript.jsonl"
    if not path.is_file():
        return dict(samples=[], candidates=0, rated=0, up=0, down=0, basis="no bridge transcript")
    rows = []
    for line in ipc.read_text(path, errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    names = _viewer_names(rows)
    turns = [r for r in rows if r.get("ev") == "turn" and isinstance(r.get("reply"), str) and r["reply"].strip()
             and not r.get("silent") and r.get("source") in PERSONA_SOURCES
             and not CHAT_SOURCE.search(str(r.get("source"))) and not CHAT_SOURCE.search(str(r.get("trigger_type")))]
    pick = turns if len(turns) <= SAMPLES_PER_SESSION else [
        turns[round(i * (len(turns) - 1) / (SAMPLES_PER_SESSION - 1))] for i in range(SAMPLES_PER_SESSION)]
    samples = []
    for r in pick:
        probe = r.get("probe") or {}
        game_time = (r.get("now") or {}).get("game_time") if isinstance(r.get("now"), dict) else None
        reply = scrub(r["reply"], names)[:300]
        samples.append(dict(turn_n=r.get("n"), source=r.get("source"),
                            date=probe.get("date") or (game_time or {}).get("date") if isinstance(game_time, dict) or probe else None,
                            clock=probe.get("game_clock") if probe else (game_time or {}).get("time") if isinstance(game_time, dict) else None,
                            context=scrub(r.get("input") or "", names)[:160], reply=reply,
                            reply_sha256=hashlib.sha256(reply.encode("utf-8")).hexdigest()))
    ratings_path = Path(run) / "bridge/persona-ratings.json"
    up = edit = down = 0
    if ratings_path.is_file():
        try:
            ratings = json.loads(ipc.read_text(ratings_path))
        except ValueError:
            ratings = []
        # Legacy export: a bare list. Rating page export (ADR 92): an object with `ratings`, bound to run + transcript hash.
        if isinstance(ratings, dict):
            ratings = ratings.get("ratings") if ratings.get("run") == Path(run).name else []
        shas = {s["reply_sha256"] for s in samples}
        for rating in ratings if isinstance(ratings, list) else []:
            if isinstance(rating, dict) and rating.get("reply_sha256") in shas:
                up += rating.get("rating") == "up"
                edit += rating.get("rating") == "edit"
                down += rating.get("rating") == "down"
    return dict(samples=samples, candidates=len(turns), rated=up + edit + down, up=up, edit=edit, down=down,
                basis="owner rates 👍/✏️/👎 on the rating page (machine cannot judge); viewer names/@handles scrubbed; "
                      "viewer-chat turns excluded; ratings bind to reply_sha256")


# --------------------------------------------------------------------------- assembly

def _field(value, source):
    return dict(value=value, source=source if value is not None else "unknown")


def journey(run, journals):
    run = Path(run)
    snaps = snapshots(journals)
    start_save, end_save, save_info = save_pair(run)
    snap_delta = delta(snaps[0]["progress"], snaps[-1]["progress"]) if len(snaps) >= 2 else {}
    save_delta = delta(start_save, end_save)
    effects = journal_effects(journals)

    def pick(key, journal_value=None):
        for source, table in (("snapshot", snap_delta), ("save_diff", save_delta)):
            if table.get(key) is not None:
                return _field(table[key], source)
        return _field(journal_value, "journal")

    places = pick("new_places", effects["places_first_entered"] if effects["places_first_entered"] else None)
    progress = dict(
        money_earned=pick("money_earned"), money_earned_individual=pick("money_earned_individual"),
        crops_harvested=pick("crops_harvested", effects["crops_harvested"]),
        crops_shipped=pick("crops_shipped"), new_places=places, tool_upgrades=pick("tool_upgrades"),
        skill_levels=pick("skill_levels"), bundles_completed=pick("bundles_completed"),
        quests_completed=pick("quests_completed"), friendship=pick("friendship"),
        journal_rows=dict(plant=effects["plant_rows"], ship=effects["ship_rows"]),
        basis=("snapshot = first vs last progress_snapshot row; save_diff = hash-verified staged template vs the retained "
               "end save, i.e. through the last SAVED night (the unsaved final day is excluded); journal = verified effect "
               "receipts. money_earned is FarmerTeam.totalMoneyEarned (team, includes HOST income); "
               "money_earned_individual is ARTI's Stats individualMoneyEarned."))
    end = snaps[-1]["progress"] if snaps else end_save
    end_source = "snapshot" if snaps else "save_diff" if end_save else "unknown"
    cc = (end or {}).get("cc") or {}
    gp = (end or {}).get("grandpa") if isinstance((end or {}).get("grandpa"), dict) else None
    finishing = dict(source=end_source, bundles_done=cc.get("bundles_done"), bundles_total=cc.get("bundles_total"),
                     rooms_done=cc.get("rooms_done"), rooms_total=cc.get("rooms_total"), joja=cc.get("joja"),
                     grandpa_points=(gp or {}).get("points"), grandpa_max=GRANDPA_MAX, candles=(gp or {}).get("candles"),
                     grandpa_unknown=(gp or {}).get("unknown"), grandpa_criteria=(gp or {}).get("criteria"),
                     grandpa_start=(start_save or {}).get("grandpa", {}).get("points") if start_save else None)
    bundles_left = (cc["bundles_total"] - cc["bundles_done"]) if type(cc.get("bundles_total")) is int and type(cc.get("bundles_done")) is int else None
    finishing["distance"] = (bundles_left + GRANDPA_MAX - gp["points"]) if bundles_left is not None and gp else None
    finishing["distance_basis"] = ("bundles remaining + (21 - Grandpa points); CC state is team-shared, Grandpa money "
                                   "criteria team, keys from host mail, the rest ARTI's own; a yardstick, not the whole ending")
    und = understanding(journals)
    per = persona(run)
    value = lambda f: f["value"]  # noqa: E731
    parts, partial = {}, []
    skills = value(progress["skill_levels"])
    parts["skill_levels"] = sum(max(0, v) for v in skills.values()) if skills else None
    parts["tool_upgrades"] = value(progress["tool_upgrades"])
    parts["new_places"] = value(progress["new_places"])
    parts["bundles_x2"] = 2 * value(progress["bundles_completed"]) if value(progress["bundles_completed"]) is not None else None
    parts["quests_x2"] = 2 * value(progress["quests_completed"]) if value(progress["quests_completed"]) is not None else None
    parts["goals_evidenced_x2"] = 2 * und["goals_done_with_evidence"]
    money = value(progress["money_earned_individual"])
    money = value(progress["money_earned"]) if money is None else money
    parts["money_per_500"] = max(0, money) // 500 if money is not None else None
    harvested = value(progress["crops_harvested"])
    parts["harvested_per_5"] = harvested // 5 if harvested is not None else None
    fr = value(progress["friendship"])
    parts["hearts"] = fr["hearts"] if fr else None
    partial = [k for k, v in parts.items() if v is None]
    points = sum(max(0, v) for v in parts.values() if v is not None)
    return dict(
        progress=progress, understanding=und, finishing=finishing, persona=per, saves=save_info,
        snapshots=len(snaps), journey_points=points, journey_parts=parts, partial=bool(partial), partial_missing=partial,
        definition=("journey_points = Σ skill levels gained + tool upgrades + new places + 2×bundles completed + 2×quests "
                    "completed + 2×self-set goals done with evidence + floor(money earned/500; ARTI's individual earnings, "
                    "team if unknown) + floor(crops harvested/5) + friendship hearts gained. Unknown components add 0 and "
                    "set partial. A transparent summary of the raw components, never a pass/fail and never a ranking."))

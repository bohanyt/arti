"""Bundle weight on her decision pages (FIX75B, ADR 129). Report-only and evaluator-independent: never a goal_gate input.

Per skill decision page she decided from (the archived provider request, linked as `decision_visibility` links it; menu pages
and pager pages are left out):
- bundle lines / chars: the string leaves under `day`, `facts` and `progress` that name a bundle, and their length in all;
- watched facts: whether `day.crops`, `day.watering_can`, `day.read_today`, `day.barang_jatuh`, `capabilities`, `outcomes` and
  `day.peti_kegunaan` are on the page (a floor of visibility: absence includes not-applicable facts).
`measure` reads a run as it was. `replay` takes the same pages through the real renderer with the forage lines her memory
would have carried (FIX73E's morning saves, her tile unknown so no distances), once in the c0c8ddde shape (every line stays)
and once under ADR 129 (`bundle_knowledge.settle`), and reports choices lost (must be 0), `tidur` and `pergi_ke` kept, and the
weight before and after. A page's archived body lacks what its render already moved to a read, so a replay cannot bring those
facts back; it measures weight and choices, and the watched-fact rates of `measure` are the before figure.
`social` (FIX79C, ADR 141) re-renders the archived pages as they were and with FIX79C's social, room-meaning and trait lines,
and reports their chars, the bundle weight, the watched facts displaced, choices lost and the pages where talk would open.

    python tools/page_weight.py measure <run> [--json out.json]
    python tools/page_weight.py social <run> [--json out.json]
    python tools/page_weight.py replay <run> [--fixture tests/fixtures/stardew/fix73e/a54_forage.json] [--json out.json]
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import re
import statistics
import sys
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.free_play.decision_visibility import _chains, archived_requests, decision_shaped, pairs, prompt_body  # noqa: E402

VERSION = "1.0.0"
WORD = re.compile(r"bundle|bundel", re.IGNORECASE)
BLOCKS = ("day", "facts", "progress")
# `day.tempat` (ADR 93) describes the Community Center as a place in one clause on every page; it is not a bundle line and ADR 129
# leaves it as it is, so it is not counted (it would add one line to every page, before and after alike).
EXCLUDED = (("day", "tempat"),)
WATCHED = (("day", "crops"), ("day", "watering_can"), ("day", "read_today"), ("day", "barang_jatuh"), ("capabilities",),
           ("outcomes",), ("day", "peti_kegunaan"))
MARKS = re.compile(r" \[(?:tas|peti) \d+\]")  # her own counts on the room line are not a change of the Community Center
TRAVEL, SLEEP = "pergi_ke", "tidur"


class _Evidence:
    """What decision_visibility's archive reader asks of an evidence object."""

    def __init__(self):
        self.warnings = []

    def ref(self, path):
        return str(path)

    def track(self, path):
        pass


def _leaves(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, path + (str(key),))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _leaves(item, path + (str(i),))
    elif isinstance(value, str):
        yield path, value


def bundle_weight(body):
    """dict(lines, chars, keys): the string leaves under BLOCKS that name a bundle, their length in all, and their block.key."""
    lines, chars, keys = 0, 0, set()
    for block in BLOCKS:
        for path, text in _leaves(body.get(block) if isinstance(body, dict) else None, (block,)):
            if WORD.search(text) and path[:2] not in EXCLUDED:
                lines += 1
                chars += len(text)
                keys.add(".".join(path[:2]))
    return dict(lines=lines, chars=chars, keys=sorted(keys))


def watched(body):
    out = {}
    for path in WATCHED:
        value = body
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        out[".".join(path)] = bool(value) if isinstance(value, (list, dict, str)) else value is not None
    return out


def _rows(path):
    rows = []
    with Path(path).open(encoding="utf-8-sig", errors="replace") as stream:
        for n, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append((n, row))
    return rows


def pages(run):
    """[dict(ref, day, index, body, bytes)]: the skill decision pages of a run, in journal order."""
    out = []
    for path in sorted(Path(run).glob("runtime/live7/*/day-*/journal/g1_journal.jsonl")):
        day = path.parent.parent.name
        archive = archived_requests(_Evidence(), path.parent)
        for n, row, chain, _reads in _chains(_rows(path)):
            decided = None
            for item in chain:
                if item["index"] not in archive:
                    continue
                body, _reason = prompt_body(archive[item["index"]])
                if body is not None and decision_shaped(body):
                    decided = (item, body)
            if decided is None:
                continue
            item, body = decided
            if "unknown_ui" in body or "presentation" in body:
                continue
            out.append(dict(ref=f"{day}:J{n}", day=day, index=item["index"], body=body, bytes=(item["render"] or {}).get("bytes")))
    return out


def _page_row(ref, body, size=None):
    return dict(ref=ref, bytes=size if size is not None else len(json.dumps(body, ensure_ascii=True, separators=(",", ":"))),
                location=(body.get("facts") or {}).get("location") if isinstance(body.get("facts"), dict) else None,
                **bundle_weight(body), facts=watched(body))


def _median(values):
    if not values:
        return None
    m = statistics.median(values)
    return int(m) if float(m).is_integer() else round(m, 1)


def summary(per):
    labels = [".".join(path) for path in WATCHED]
    return dict(pages=len(per),
                bundle_lines_median=_median([p["lines"] for p in per]), bundle_lines_max=max((p["lines"] for p in per), default=0),
                bundle_chars_median=_median([p["chars"] for p in per]),
                bundle_chars_mean=round(statistics.fmean([p["chars"] for p in per]), 1) if per else None,
                bundle_chars_max=max((p["chars"] for p in per), default=0),
                pages_naming=sum(p["lines"] >= 1 for p in per), pages_two_or_more=sum(p["lines"] >= 2 for p in per),
                bytes_median=_median([p["bytes"] for p in per if type(p["bytes"]) is int]),
                keys=dict(Counter(k for p in per for k in p["keys"]).most_common()),
                facts_on_page={label: sum(bool(p["facts"].get(label)) for p in per) for label in labels})


def measure(run):
    per = [_page_row(p["ref"], p["body"], p["bytes"]) for p in pages(run)]
    return dict(version=VERSION, report_only=True, run=Path(run).name, mode="measure", **summary(per), per_page=per)


# ------------------------------------------------------------------ replay through the real renderer
def _fit(candidate):
    """StardewGoalRuntime._render_messages.skill_fits for a skill page, on the encoded provider request."""
    import arti_stardew_runtime as R
    from arti_runtime.page_budget import scaled
    encoded = R._encoded_provider_request("gpt-6-luna", candidate, R._decision_provider_extra_payload(False, messages=candidate))
    return (len(encoded) <= scaled(R._STARDEW_DECISION_REQUEST_MAX_BYTES)
            and R._provider_input_token_upper_bound(encoded) <= scaled(R._STARDEW_DECISION_INPUT_TOKEN_HARD_BOUND)
            and R._skill_prompt_token_estimate(encoded) <= scaled(R._STARDEW_DECISION_INPUT_TOKEN_RESERVE))


def render(offer, *, budget=None):
    import arti_stardew_skills as S
    from arti_runtime.page_budget import DEFAULT
    budget = budget or DEFAULT
    if budget.scale != 1:
        offer["render_budget"] = offer.get("render_budget", 16128) * budget.scale
    request = NS(skill_offer=offer, decision_page_budget=budget, request_nonce="n" * 32, goal_id="g" * 32, goal_revision=1, context_ref="c" * 32,
                 reason="skill_boundary")
    return json.loads(S.render_skill_messages(request, _fit)[1]["content"])


def fixture_cc():
    """The native read the FIX73E fixture stands on: Crafts Room open, Pantry shut (a54 d1-d23)."""
    return dict(unlocked=True, can_read_junimo_text=True, bundles=[
        dict(id=13, name="Spring Foraging", room="Crafts Room", complete=False, done=0, slots=4,
             needs=[["Wild Horseradish", 1, 0], ["Daffodil", 1, 0], ["Leek", 1, 0], ["Dandelion", 1, 0]]),
        dict(id=20, name="Spring Crops", room="Pantry", complete=False, done=0, slots=4, needs=[["Parsnip", 1, 0]])])


def _scene(location, rows):
    scene = {"obj:%s:%d,%d:(O)%s" % (location, x, y, item): dict(
        kind="object", qualified_item_id="(O)%s" % item, name=name, display_name=name, tile=dict(x=x, y=y), spawned=True,
        grabbable=True, machine=False, passable=False, minutes_until_ready=0) for name, item, x, y in rows}
    scene["loc:" + location] = dict(kind="location", location_id=location, is_outdoors=True)
    return scene


def goal_texts(body):
    """Her own goals on the page (`mind.tujuan`), as texts."""
    out = []
    mind = body.get("mind") if isinstance(body.get("mind"), dict) else {}
    for path, text in _leaves(mind.get("tujuan"), ("tujuan",)):
        if path[-1] == "text":
            out.append(text)
    return out


def replay(run, fixture=None):
    from arti_runtime import bundle_knowledge as B
    from arti_runtime.forage_memory import ForageMemory
    saves = json.loads(Path(fixture or ROOT / "tests/fixtures/stardew/fix73e/a54_forage.json").read_text(encoding="utf-8"))
    memory, cc = ForageMemory(), fixture_cc()
    fits = B.open_fits(cc)
    seen, last_location = None, None
    archived, before, after, kept = [], [], [], Counter()
    lost, split = [], Counter()
    for p in pages(run):
        body = p["body"]
        location = (body.get("facts") or {}).get("location") if isinstance(body.get("facts"), dict) else None
        number = int(p["day"].split("-")[1])
        entry = saves["days"].get(str(number), {})
        date = tuple(entry.get("date") or (1, "spring", number))
        if location in entry.get("maps", {}):
            memory.observe(location, _scene(location, entry["maps"][location]), date=date)
        offer = copy.deepcopy(body)
        offer["goal"] = "x"
        day = offer.setdefault("day", {})
        full = "tas penuh" in str(day.get("bag") or "")
        if isinstance(location, str):
            if line := memory.here_line(location, None, date, full=full, fits=fits, place=location):
                day["forage_terlihat"] = line
            if line := memory.seen_line(location, date, fits=fits):
                day["forage_ingat"] = line
        old = render(copy.deepcopy(offer))
        settled = copy.deepcopy(offer)
        free = location == "CommunityCenter" or B.own_goal_names(goal_texts(body), cc)
        signature = MARKS.sub("", settled["day"].get("cc_terbuka") or "") or None
        changed = signature is not None and signature != seen
        B.settle(settled["day"], free=free, changed=changed, moved_on=location != last_location)
        if signature is not None and "cc_terbuka" in settled["day"]:
            seen = signature
        last_location = location
        new = render(settled)
        archived.append(_page_row(p["ref"], body, p["bytes"]))
        before.append(_page_row(p["ref"], old))
        after.append(_page_row(p["ref"], new))
        choices_old, choices_new = pairs(old.get("skills")), pairs(new.get("skills"))
        if missing := choices_old - choices_new:
            lost.append(dict(ref=p["ref"], lost=sorted("%s:%s" % c if c[1] is not None else c[0] for c in missing)))
        for name in (SLEEP, TRAVEL):
            kept[name + "_before"] += any(n == name for n, _ in choices_old)
            kept[name + "_after"] += any(n == name for n, _ in choices_new)
        split["before"] += "presentation" in old
        split["after"] += "presentation" in new
    return dict(version=VERSION, report_only=True, run=Path(run).name, mode="replay", pages=len(after),
                archived=summary(archived), before=summary(before), after=summary(after),
                choices_lost=sum(len(row["lost"]) for row in lost), pages_with_a_lost_choice=lost[:16],
                kept=dict(kept), split=dict(split),
                definition="archived = the pages as a54 rendered them; before = the same pages plus the forage lines her memory "
                           "would have carried, every line kept (c0c8ddde shape); after = the same under ADR 129 "
                           "(bundle_knowledge.settle), both through the real renderer; her tile is unknown, so the forage lines "
                           "carry no distance; a page's archived body lacks what its own render moved to a read")


# ------------------------------------------------------------------ FIX79C (ADR 141): social and room-meaning weight
SOCIAL_KEYS = (("progress", "teman"), ("progress", "teman_hati"), ("progress", "teman_aturan"), ("progress", "teman_membuka"), ("day", "warga"),
               ("social_availability",))
MEANING_KEYS = (("progress", "cc_hadiah"),)
TRAIT_TEXT = "example-trait-b"


def _chars_at(body, keys):
    total = 0
    for path in keys:
        value = body
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            total += sum(len(text) for _, text in _leaves(value))
    return total


def social_weight(body):
    """dict(social, meaning, reward, trait): chars of the social lines, the room-reward line, the bundle reward suffixes in the
    bundle lines, and an example trait in `mind.sifat`."""
    import arti_mind
    trait = next(r["text"] for r in arti_mind.OWNER_PREFS if r["id"] == TRAIT_TEXT)
    sifat = ((body.get("mind") or {}).get("sifat") or []) if isinstance(body.get("mind"), dict) else []
    rewards = sum(len(m) for block in BLOCKS for _, text in _leaves(body.get(block) if isinstance(body, dict) else None)
                  for m in re.findall(r" -> hadiah [^;)]*", text))
    return dict(social=_chars_at(body, SOCIAL_KEYS), meaning=_chars_at(body, MEANING_KEYS), reward=rewards,
                trait=sum(len(t) for t in sifat if t == trait))


def _snapshots(run):
    """{day folder: [(journal line, progress snapshot)]} from the run's own `progress_snapshot` rows."""
    out = {}
    for path in sorted(Path(run).glob("runtime/live7/*/day-*/journal/g1_journal.jsonl")):
        out[path.parent.parent.name] = [(n, r["progress"]) for n, r in _rows(path)
                                        if r.get("ev") == "progress_snapshot" and isinstance(r.get("progress"), dict)]
    return out


def NATIVE_ROOMS():
    """The installed 1.6.15 room rewards as a55's page carried them (`fix76a/a55_d4_p21.json`, a native read). a58's archive
    holds none (its pages never carried `donation_facts.room_rewards`); the strings are the same game's, so the replay uses them."""
    page = json.loads((ROOT / "tests/fixtures/stardew/fix76a/a55_d4_p21.json").read_text(encoding="utf-8"))["page"]
    return page["progress"]["donation_facts"]["room_rewards"]


def with_fix79c(body, snap):
    """The page as FIX79C would have rendered it, from what the page and the run's own native rows hold. Grandpa's criteria
    texts and the scene's full NPC facts are not in the archive: the friends criterion is left out and `day.warga` is built
    from `social_availability.observed_npcs` when that block rode the page."""
    from arti_runtime import social_facts as F
    import arti_mind
    offer = copy.deepcopy(body)
    progress = offer.get("progress") if isinstance(offer.get("progress"), dict) else {}  # a page without one gets none
    friends = (snap or {}).get("friends") or {}
    if friends:
        progress["teman_hati"] = F.friends_line(friends)
    progress["teman_aturan"] = F.RULES
    rewards = (progress.get("donation_facts") or {}).get("room_rewards") or (NATIVE_ROOMS() if progress else [])
    rooms = [dict(room=r[0], reward=r[1]) for r in rewards if isinstance(r, list) and len(r) == 2]
    if opens := F.unlock_line(None, rooms):
        progress["teman_membuka"] = opens
    from arti_stardew_world import cc_rewards_line
    if line := cc_rewards_line(dict(room_rewards=rooms)):
        progress["cc_hadiah"] = line
    npcs = ((offer.get("social_availability") or {}).get("observed_npcs") or []) if isinstance(offer.get("social_availability"), dict) else []
    targets = {str(n.get("ref")): dict(kind="npc", villager=n.get("villager"), internal_name=n.get("name"),
                                       display_name=n.get("name"), talked_today=n.get("talked_today"),
                                       tile=dict(x=0, y=0) if n.get("tile") is not None else None)
               for n in npcs if isinstance(n, dict)}
    if line := F.here_line(targets, friends):
        offer.setdefault("day", {})["warga"] = line
    mind = offer.get("mind") if isinstance(offer.get("mind"), dict) else None
    trait = next(r["text"] for r in arti_mind.OWNER_PREFS if r["id"] == TRAIT_TEXT)
    if mind is not None:  # pick_prefs ranks it first on a page offering the Center (strength 5, about-match); 3 lines kept
        old = [t for t in mind.get("sifat") or [] if t != trait]
        # Fewer than TRAIT_LINES on the page means render_block's byte cap already dropped one: the trait takes a slot.
        keep = arti_mind.TRAIT_LINES if len(old) >= arti_mind.TRAIT_LINES else max(1, len(old))
        mind["sifat"] = ([trait] + old)[:keep]
    talk = F.talk_open(targets) and not any(e.get("name") == "ngobrol_hadiah" for e in offer.get("skills") or ()
                                            if isinstance(e, dict))
    return offer, talk


def social_replay(run):
    """Archived pages through the real renderer as they were (before) and with the FIX79C lines (after): social, meaning and
    bundle chars per page, the watched facts that left the page (displaced), choices lost, pages where talk would be offered."""
    snaps = _snapshots(run)
    before, after, lost, displaced, talk_added, talk_before = [], [], [], Counter(), 0, 0
    for p in pages(run):
        line = int(p["ref"].rsplit(":J", 1)[1])
        snap = next((s for n, s in reversed(snaps.get(p["day"], [])) if n <= line), None)
        offer = copy.deepcopy(p["body"])
        offer["goal"] = "x"
        old = render(copy.deepcopy(offer))
        new_offer, talk = with_fix79c(offer, snap)
        new = render(new_offer)
        talk_before += any(e.get("name") == "ngobrol_hadiah" for e in old.get("skills") or () if isinstance(e, dict))
        talk_added += bool(talk)
        for row, body in ((before, old), (after, new)):
            row.append(dict(_page_row(p["ref"], body), **social_weight(body)))
        for label, on in before[-1]["facts"].items():
            if on and not after[-1]["facts"].get(label):
                displaced[label] += 1
        if missing := pairs(old.get("skills")) - pairs(new.get("skills")):
            lost.append(dict(ref=p["ref"], lost=sorted("%s:%s" % c if c[1] is not None else c[0] for c in missing)))

    def weights(rows):
        return {k: dict(median=_median([r[k] for r in rows]), mean=round(statistics.fmean([r[k] for r in rows]), 1) if rows else None,
                        max=max((r[k] for r in rows), default=0)) for k in ("social", "meaning", "reward", "trait", "chars", "bytes")}
    return dict(version=VERSION, report_only=True, run=Path(run).name, mode="social", pages=len(after),
                before=dict(summary(before), weight=weights(before)), after=dict(summary(after), weight=weights(after)),
                displaced=dict(displaced), choices_lost=sum(len(r["lost"]) for r in lost), pages_with_a_lost_choice=lost[:16],
                talk_offered_before=talk_before, talk_added_from_observed_npcs=talk_added,
                definition="before = the archived page re-rendered; after = the same with FIX79C's progress.teman_hati/teman_aturan/"
                           "teman_membuka/cc_hadiah, day.warga (from observed_npcs when on the page) and an example trait; "
                           "Grandpa criteria texts are not archived; the scene's full NPC list is not archived, so talk_added "
                           "counts only pages whose own observed_npcs held a villager not talked to today")


# ------------------------------------------------------------------ FIX84B (ADR 149): Mines rules fact + gift offer
_HIGHLIGHT = re.compile(r"(.+) x(\d+)")


def bag_rows(body):
    """Bag rows an archived page holds: `facts.inventory` qids ("(O)390 x8", bag order) joined in order with the names in
    `day.inventory_highlights` ("Stone x8", the same bag order without tools) while the stacks agree; a cut list stops it."""
    f = body.get("facts") if isinstance(body.get("facts"), dict) else {}
    day = body.get("day") if isinstance(body.get("day"), dict) else {}
    qids = [m.groups() for m in (_HIGHLIGHT.fullmatch(str(x)) for x in f.get("inventory") or ()) if m]
    qids = [(q, int(n)) for q, n in qids if not q.startswith(("(T)", "(W)"))]
    out = []
    for (qid, n), text in zip(qids, day.get("inventory_highlights") or ()):
        m = _HIGHLIGHT.fullmatch(str(text))
        if not m or int(m[2]) != n:
            break
        out.append(dict(slot=len(out), name=m[1], qualified_item_id=qid, stack=n))
    return out


def with_fix84b(body):
    """(offer, mines_rules_added, gift_args_added, gift_cut): the page as FIX84B's Live7 would have built it. The gift args come
    from the page's own named talk args (the same `talk_args` set) and its bag; birthdays are not archived (upper bound)."""
    import arti_stardew_live7 as L
    from arti_runtime import social_facts as F
    offer = copy.deepcopy(body)
    day = offer.get("day") if isinstance(offer.get("day"), dict) else None
    names = {e.get("name") for e in offer.get("skills") or () if isinstance(e, dict)}
    location = (offer.get("facts") or {}).get("location") if isinstance(offer.get("facts"), dict) else None
    rules = bool(day is not None and (line := L.mines_rules_fact(location, names)))
    if rules:
        day["aturan_mines"] = line
    added, cut = [], 0
    for e in offer.get("skills") or ():
        if isinstance(e, dict) and e.get("name") == "ngobrol_hadiah" and isinstance(e.get("args"), list):
            talk = [a for a in e["args"] if isinstance(a, str) and a != F.ANY_TALK and " ke " not in a]
            added, cut = F.gift_args(talk, bag_rows(offer))
            if added:
                e["args"] = e["args"] + added
                e["does"] = F.TALK_DOES + F.GIFT_DOES
    return offer, rules, added, cut


def fix84b_replay(run):
    """Archived pages re-rendered as they were (before) and with FIX84B's Mines rules fact and gift args (after)."""
    before, after, lost, displaced = [], [], [], Counter()
    rules_pages = rules_shown = gift_pages = gift_offered = gift_kept = gift_cut = 0
    rules_chars = []
    for p in pages(run):
        offer = copy.deepcopy(p["body"])
        offer["goal"] = "x"
        old = render(copy.deepcopy(offer))
        new_offer, rules, added, cut = with_fix84b(offer)
        new = render(new_offer)
        rules_pages += rules
        if rules:
            rules_chars.append(len(json.dumps(new, ensure_ascii=False)) - len(json.dumps(old, ensure_ascii=False)))
            rules_shown += "aturan_mines" in (new.get("day") or {})
        gift_pages += bool(added)
        gift_offered += len(added)
        gift_cut += cut
        shown = pairs(new.get("skills"))
        gift_kept += sum(("ngobrol_hadiah", a) in shown for a in added)
        before.append(_page_row(p["ref"], old))
        after.append(_page_row(p["ref"], new))
        for label, on in before[-1]["facts"].items():
            if on and not after[-1]["facts"].get(label):
                displaced[label] += 1
        if (old.get("day") or {}).get("day_rules") and not (new.get("day") or {}).get("day_rules"):
            displaced["day.day_rules"] += 1
        if missing := pairs(old.get("skills")) - shown:
            lost.append(dict(ref=p["ref"], lost=sorted("%s:%s" % c if c[1] is not None else c[0] for c in missing)))
    b = [r["bytes"] for r in before]
    a = [r["bytes"] for r in after]
    return dict(version=VERSION, report_only=True, run=Path(run).name, mode="fix84b", pages=len(after),
                bytes_median=[_median(b), _median(a)], bytes_max=[max(b, default=0), max(a, default=0)],
                bytes_delta_median=_median([y - x for x, y in zip(b, a)]),
                mines_rules_pages=rules_pages, mines_rules_on_page=rules_shown, mines_rules_chars_added=_median(rules_chars),
                gift_pages=gift_pages, gift_args_offered=gift_offered, gift_args_on_rendered_page=gift_kept,
                gift_pairs_cut=gift_cut, displaced=dict(displaced), choices_lost=sum(len(r["lost"]) for r in lost),
                pages_with_a_lost_choice=lost[:16],
                definition="before = the archived page re-rendered; after = the same with the Mines rules fact on a "
                           "Mines page (as day.aturan_mines) and ngobrol_hadiah gift args (bag x the page's named talk args; birthdays not archived)")


def receipt_line(report):
    if report.get("mode") == "fix84b":
        return ("page weight FIX84B (%s, %d pages): bytes median %s -> %s (delta median %s, max %s -> %s); Mines rules fact on %d "
                "pages (on the rendered page %d, +%s chars median); gift args on %d pages (%d offered, %d on the rendered page, %d pairs cut); displaced %s; "
                "choices lost %d" % (report["run"], report["pages"], *report["bytes_median"], report["bytes_delta_median"],
                                     *report["bytes_max"], report["mines_rules_pages"], report["mines_rules_on_page"], report["mines_rules_chars_added"],
                                     report["gift_pages"], report["gift_args_offered"], report["gift_args_on_rendered_page"],
                                     report["gift_pairs_cut"], report["displaced"] or "none", report["choices_lost"]))
    if report.get("mode") == "social":
        b, a = report["before"]["weight"], report["after"]["weight"]
        return ("page weight FIX79C (social replay %s, %d pages): social chars median %s -> %s, room meaning %s -> %s, bundle "
                "reward %s -> %s, trait %s -> %s, bundle chars %s -> %s, bytes median %s -> %s; bundle lines/page median %s -> %s; "
                "displaced %s; choices lost %d; talk offered %d, added %d"
                % (report["run"], report["pages"], b["social"]["median"], a["social"]["median"], b["meaning"]["median"],
                   a["meaning"]["median"], b["reward"]["median"], a["reward"]["median"], b["trait"]["median"],
                   a["trait"]["median"], b["chars"]["median"], a["chars"]["median"], b["bytes"]["median"], a["bytes"]["median"],
                   report["before"]["bundle_lines_median"], report["after"]["bundle_lines_median"],
                   report["displaced"] or "none", report["choices_lost"], report["talk_offered_before"],
                   report["talk_added_from_observed_npcs"]))
    return _receipt_line(report)


def _receipt_line(report):
    """One line for a receipt, like the capability-regression line."""
    if report.get("mode") == "replay":
        b, a = report["before"], report["after"]
        return ("page weight (replay %s, %d pages): bundle lines/page median %s -> %s (max %s -> %s), chars median %s -> %s, "
                "pages naming it on 2+ lines %s -> %s; choices lost %d; tidur kept %s/%s, pergi_ke kept %s/%s; split %s -> %s"
                % (report["run"], report["pages"], b["bundle_lines_median"], a["bundle_lines_median"], b["bundle_lines_max"],
                   a["bundle_lines_max"], b["bundle_chars_median"], a["bundle_chars_median"], b["pages_two_or_more"],
                   a["pages_two_or_more"], report["choices_lost"], report["kept"].get("tidur_after", 0),
                   report["kept"].get("tidur_before", 0), report["kept"].get("pergi_ke_after", 0),
                   report["kept"].get("pergi_ke_before", 0), report["split"].get("before", 0), report["split"].get("after", 0)))
    facts = "; ".join("%s %d" % (k.split(".")[-1], v) for k, v in report["facts_on_page"].items())
    return ("page weight (%s, %d pages): bundle lines/page median %s (max %s), chars median %s (mean %s, max %s), pages naming it "
            "%s, on 2+ lines %s; on page: %s" % (report["run"], report["pages"], report["bundle_lines_median"],
                                                  report["bundle_lines_max"], report["bundle_chars_median"],
                                                  report["bundle_chars_mean"], report["bundle_chars_max"], report["pages_naming"],
                                                  report["pages_two_or_more"], facts))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("measure", "replay", "social", "fix84b"))
    parser.add_argument("run")
    parser.add_argument("--fixture")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    report = (measure(args.run) if args.mode == "measure" else social_replay(args.run) if args.mode == "social"
              else fix84b_replay(args.run) if args.mode == "fix84b"
              else replay(args.run, args.fixture))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(receipt_line(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

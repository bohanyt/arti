"""Thin presentation of the native world catalog. No Python unlock rules or destination allowlist.

The catalog is knowledge, never an interaction target. Every motor leg still needs fresh scene admission.
"""
from __future__ import annotations

from collections.abc import Mapping
from arti_runtime.native_rows import native_rows
import time
from functools import lru_cache
from pathlib import Path
import hashlib
import json


def _unchunk(value, facts):
    if not isinstance(value, Mapping) or "chunked" not in value:
        return value
    key, count = value.get("chunked"), value.get("parts")
    if not isinstance(key, str) or type(count) is not int or not 0 < count <= len(facts):
        return None
    parts = [facts.get(f"chunk:{key}:{i}") for i in range(count)]
    if not all(isinstance(p, str) for p in parts):
        return None
    raw = "".join(parts)
    try:
        if hashlib.sha256(raw.encode()).hexdigest() != key:
            return None
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None


def current_catalog(runtime, pin):
    return read_catalog(runtime, pin)[0]


def read_catalog(runtime, pin):
    """(catalog, None), or (None, why the frame was not usable). ADR 99: the why is telemetry only."""
    if pin is None or getattr(pin, "player_id", None) is None or not hasattr(runtime, "_obs"):
        return None, "no_pin"
    try:
        record = runtime._obs.get(pin.player_id, "knowledge.world")
        if record is None:
            return None, "no_frame"
        valid = (record.available is True and record.authoritative is True
                 and record.is_complete is True and record.stale is False and record.requires_resync is False
                 and record.screen_id == pin.screen_id and record.session_id == pin.observation_session_id)
        if not valid:
            return None, "frame_unusable"
        if not 0 <= time.monotonic() - record.received_monotonic <= 2:
            return None, "frame_old"
    except (KeyError, AttributeError, TypeError):
        return None, "frame_unreadable"
    value = {k: _unchunk(v, record.facts) for k, v in record.facts.items() if not k.startswith("chunk:")}
    failures = {}
    for key in ("progress", "mail", "quest_log"):
        raw = record.facts.get(key)
        if value.get(key) is None and isinstance(raw, Mapping) and "chunked" in raw:
            count = raw.get("parts")
            missing = ([i for i in range(count) if not isinstance(record.facts.get(f"chunk:{raw['chunked']}:{i}"), str)]
                       if type(count) is int and 0 < count <= len(record.facts) else None)
            failures[key] = dict(reason="chunk_incomplete" if missing else "chunk_invalid",
                                 parts=count, missing=missing, revision=record.domain_revision)
    if failures:
        value["read_failures"] = failures
    graph = value.get("world_graph")
    if not isinstance(graph, Mapping):
        return None, "graph_unreadable"
    if graph.get("current") != pin.location:
        return None, "other_location"
    # A wake can leave the actor in the same room while a young knowledge
    # frame still describes yesterday. Age/location alone cannot validate it.
    state = getattr(runtime, "_state", None)
    motor = state.latest(pin.player_id) if state is not None else None
    date = tuple(getattr(motor, k, None) for k in ("year", "season", "day"))
    if (catalog_date := value.get("date")) is not None and all(v is not None for v in date):
        if catalog_date != f"{date[0]}:{date[1]}:{date[2]}":
            return None, "other_date"
    discoveries = [v for k, v in value.items() if k.startswith("discovery:") and isinstance(v, Mapping)]
    discoveries.sort(key=lambda e: e.get("seq") if type(e.get("seq")) is int else 0)
    total = value.get("discovery_total")
    return dict(value, discoveries=discoveries,
                discovery_pending=max(0, total-len(discoveries)) if type(total) is int else None), None


def places(context):
    catalog = context.get("world_knowledge") or {}
    graph = catalog.get("world_graph") or {}
    # Failed/partial capture stays visible as knowledge, but cannot admit travel.
    if not isinstance(graph.get("places"), (list, tuple)):
        return ()
    return tuple(p for p in native_rows(graph.get("places", ())) if isinstance(p, Mapping)
                 and isinstance(p.get("id"), str) and type(p.get("reachable_now")) is bool)


def rest_facts(context):
    """The native rest/ownership capture (NativeRestFacts) inside the validated world catalog, or None."""
    rest = (context.get("world_knowledge") or {}).get("rest")
    return rest if isinstance(rest, Mapping) else None


def at_own_home(context):
    """fix42 (a33 d7:J672-J848): whether she stands in her own house by native ownership, or None when that capture
    is unavailable. A location-name prefix also matched the host's FarmHouse for a farmhand, so the host's bed was
    her only night offer (bed_not_offered x4, then the 600 s stop)."""
    rest = rest_facts(context)
    if rest is None or not isinstance(rest.get("home"), str) or not isinstance(rest.get("current_location"), str):
        return None
    return rest["current_location"] == rest["home"]


def other_homes(context):
    """Native ids of other players' houses (same ownership capture); never her home."""
    rest = rest_facts(context) or {}
    homes = rest.get("other_homes")
    return {h for h in homes if isinstance(h, str) and h != rest.get("home")} if isinstance(homes, (list, tuple)) else set()


def location_arg(location):
    # Exact native identity, including case. The @ grammar bypasses human aliases, never admission.
    from arti_stardew_interact import _LOCATION_ALIASES
    return next((arg for arg, target in _LOCATION_ALIASES.items() if target == location), "@" + location)


def route(context, destination):
    return next((list(p.get("motor_route", p.get("route")) or ()) for p in places(context)
                 if p["id"] == destination and p["reachable_now"] is True and p.get("go_to_available", True) is True), None)


def travel_options(context, here, *, now=None):
    from arti_runtime.travel_facts import arrival_facts
    from arti_runtime.experience_facts import view
    context = dict(context, world_knowledge=view(context.get("world_knowledge")))
    home = ((context.get("world_knowledge") or {}).get("world_graph") or {}).get("home")
    return [dict(arg="home" if p["id"] == home else location_arg(p["id"]), location=p["id"], label=p.get("label", p["id"]),
                 activities=[("fact", f) for f in p.get("facts", ())] +
                    [("fact", f) for f in commerce_purpose(p, context)], hours=None,
                 route=list(p.get("motor_route", p.get("route")) or ()),
                 arrival=arrival_facts(p, here, now, list(p.get("motor_route", p.get("route")) or ())))
            for p in places(context) if p["id"] != here and p["reachable_now"] is True
            and p.get("go_to_available", True) is True and p.get("route")]


def commerce_purpose(place, context):
    """ADR 186: purpose from experienced native facilities; never shop/item tables or advice."""
    if place.get("experience_known") is False:
        return []
    facts = place.get("facts", ())
    out = []
    if any(str(f).startswith("shop counter ") for f in facts):
        out.append("Konter: lihat stok/harga, beli atau jual; penerimaan barang dibaca dari menu toko.")
    if any(str(f).startswith("shipping bin:") for f in facts):
        out.append("Kotak pengiriman: pilih barang/jumlah; dibayar sesudah tidur.")
    if place.get("id") in context.get("seen_chest_places", ()):
        out.append("Peti yang pernah dilihat: simpan atau ambil barang lewat menu peti.")
    return out


def _render_catalog(catalog):
    """Every place/lock stays on the decision page; full edges/routes are losslessly paged on demand."""
    if not isinstance(catalog, Mapping):
        return dict(status="unknown: native world catalog unavailable")
    graph = catalog.get("world_graph") or {}
    unknown_places = sum(p.get("experience_known") is False for p in native_rows(graph.get("places", ())))
    graph = dict(graph, places=[p for p in native_rows(graph.get("places", ())) if p.get("experience_known") is not False])
    known_ids = {p["id"] for p in graph["places"]}
    graph["places"] = [dict(p, **{key: [hop if hop in known_ids else "area belum dijelajahi" for hop in p[key]]
                                  for key in ("route", "motor_route") if isinstance(p.get(key), (list, tuple))})
                       for p in graph["places"]]
    locks, rows = [], []
    for p in native_rows(graph.get("places", ())):
        if not isinstance(p, Mapping) or not isinstance(p.get("id"), str):
            continue
        if p.get("reachable_now") is True:
            rows.append([p["id"], True])
        else:
            reason = str(p.get("locked") or "unknown")
            if reason not in locks:
                locks.append(reason)
            rows.append([p["id"], False, locks.index(reason)])
    return dict(legend="places=[id,reachable_now,lock_index?]; locks[index]=current reason; @id=travel arg",
                places=rows, locks=locks, complete=graph.get("complete") is True,
                discoveries_pending=catalog.get("discovery_pending"),
                generic_action_needed=[p["id"] for p in native_rows(graph.get("places", ())) if p.get("reachable_now") is True
                                       and p.get("go_to_available") is False],
                unknown_count=unknown_places + len(graph.get("unknown") or ()),
                world_details=dict(places=native_rows(graph.get("places", ())), connections=native_rows(graph.get("connections", ())),
                                   calendar=catalog.get("calendar", ()), unknown=list(graph.get("unknown") or ())))


def render(catalog, *, max_bytes=4096):
    from arti_runtime.experience_facts import view
    catalog = view(catalog)
    result = _render_catalog(catalog)
    details = result.get("world_details")
    compact = {k: v for k, v in result.items() if k != "world_details"}
    if details is None or len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode()) <= max_bytes:
        return result
    # Keep the complete catalog in the existing lossless detail pager. Large worlds never
    # become an arbitrary subset, or crowd every UI/choice page past the provider guard.
    rows = result["places"]
    details = dict(details,
                   generic_action_needed=result["generic_action_needed"])
    return dict(catalog_paged=True, place_count=len(rows),
                reachable_count=sum(row[1] is True for row in rows),
                locked_count=sum(row[1] is False for row in rows),
                generic_action_count=len(result["generic_action_needed"]),
                unknown_count=result["unknown_count"], complete=result["complete"],
                discoveries_pending=result["discoveries_pending"], world_details=details,
                legend="Complete game map, locks, labels and routes are on the existing read-only detail pages.")


def fact_lines(catalog):
    from arti_runtime.quest_facts import indicator_line
    if not isinstance(catalog, Mapping):
        return [indicator_line(None)]
    from arti_runtime.experience_facts import view
    catalog = view(catalog)
    result = [str(s) for s in native_rows(catalog.get("game_facts", ())) if isinstance(s, str)]
    result.append(indicator_line(catalog))
    result.extend("Sumber %s: %s" % kv for kv in (catalog.get("fact_sources") or {}).items())
    result.append('Ingatan farm berasal dari catatan yang tersedia; pertama tercatat bukan bukti pertama sepanjang permainan. Riwayat sebelum catatan ini tidak diketahui.')
    rest = catalog.get("rest")
    if isinstance(rest, Mapping):
        result.append("Native rest facts (ownership eligibility, not input authorization): "
                      + json.dumps(rest, ensure_ascii=False, separators=(",", ":")))
    return result


@lru_cache(maxsize=1)
def _life_map():
    path = Path(__file__).parent / "docs/stardew/primitives-v2/COVERAGE.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return {line.split()[1]: line.split("|")[1].strip() for line in lines
            if line.startswith("| L") and len(line.split()) > 1}


def discovery_references(event, *, wiki_cache=None):
    """Game descriptions win. Secondary reference data never becomes an observed effect.

    Reuse the existing bounded wiki reader with its cache. This boundary never starts network work on
    the voice/decision path; unavailable reference data remains explicit and can be inspected later.
    """
    from arti_stardew_wiki import lookup_stardew_wiki, resolve_stardew_wiki_pages
    subject = str(event.get("subject", "")).casefold()
    ids, question = (), ""
    if any(word in subject for word in ("rod", "pole", "willy", "beach")):
        ids, question = ("L048", "L049", "L050", "L055"), "fishing"
    elif "mine" in subject:
        ids, question = ("L060", "L061"), "mining"
    elif event.get("kind") == "unknown" and "event" in subject:
        ids = ("L237", "L238", "L239", "L240", "L241")
    references = []
    if question:
        urls, reason = resolve_stardew_wiki_pages(question)
        def no_network(_url):
            raise OSError("reference_not_cached")
        result = lookup_stardew_wiki(question, urls, fetcher=no_network, cache=wiki_cache)
        if result.available:
            references.append(dict(source="wiki_reference", observed=False, excerpt=result.excerpt,
                                   urls=[p.canonical_url for p in result.pages]))
        else:
            references.append(dict(source="wiki_reference", available=False,
                                   reason=reason or result.unavailable_reason))
    life = _life_map()
    if ids:
        references.append(dict(source="docs/stardew/primitives-v2/COVERAGE.md", observed=False,
                               related=[life[i] for i in ids if i in life]))
    result = dict(event, references=references)
    try:
        year, season, date = str(event.get("date", "")).split(":")
        ordinal = (int(year)-1)*112 + ("spring","summer","fall","winter").index(season)*28 + int(date)
        if ordinal > 0:
            result.update(day=ordinal, day_label=f"Y{year} {season} {date}")
    except (ValueError, TypeError):
        pass  # unknown dates stay explicitly observed in the current day
    return result


# --------------------------------------------------------------------------- fix13c: progress, letters, quests
# PROGRESS_GAP_AUDIT_2026-10-29 ranked 4/5: the native knowledge facts "progress", "mail" and "quest_log"
# (NativeProgressFacts.cs) as one compact, persistent decision block. Facts only, the game's own words and numbers;
# no priority, order or suggestion. The full lists ride on the existing read-only detail page (progress_details).
SKILLS = ("farming", "fishing", "foraging", "mining", "combat")
QUEST_LINE_MAX = 120
LETTER_LINE_MAX = 140


def _known(value):
    return value if isinstance(value, Mapping) and value and "unknown" not in value else None


def progress_snapshot(catalog, *, crops=None):
    """The evaluator's progress_snapshot shape (scripts/free_play/journey.py) from the native facts, or None."""
    if not isinstance(catalog, Mapping):
        return None
    p, mail, quests = (_known(catalog.get(k)) for k in ("progress", "mail", "quest_log"))
    if p is None and mail is None and quests is None:
        return None
    p = p or {}
    stats = p.get("stats") if isinstance(p.get("stats"), Mapping) else {}
    cc = p.get("cc") if isinstance(p.get("cc"), Mapping) else None
    grandpa = p.get("grandpa") if isinstance(p.get("grandpa"), Mapping) else None
    graph = catalog.get("world_graph") if isinstance(catalog.get("world_graph"), Mapping) else {}
    rows = [r for r in native_rows(graph.get("places", ())) if isinstance(r, Mapping)]
    discoveries = catalog.get("discoveries") if isinstance(catalog.get("discoveries"), list) else [
        v for k, v in catalog.items() if k.startswith("discovery:") and isinstance(v, Mapping)]
    entered = sum(1 for v in discoveries if isinstance(v, Mapping) and v.get("kind") == "location_entered")
    return dict(
        money_earned=p.get("money_earned"), money_earned_self=p.get("money_earned_self"),
        skills={k: v for k, v in (p.get("skills") or {}).items() if type(v) is int} or None,
        tools={k: v[1] for k, v in (p.get("tools") or {}).items()
               if isinstance(v, list) and len(v) == 2 and type(v[1]) is int},
        # FIX72D: native already sends [display name, level]; the row that compares two snapshots needs the name too.
        tool_names={k: v[0] for k, v in (p.get("tools") or {}).items() if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)},
        # ADR 93: the tool at the blacksmith (not in the bag, so absent from `tools`) and the deepest mine floor
        tool_upgrading=p.get("tool_upgrading") if isinstance(p.get("tool_upgrading"), str) and p.get("tool_upgrading") else None,
        deepest_mine_level=p.get("deepest_mine_level") if type(p.get("deepest_mine_level")) is int else None,
        places=dict(reachable=sum(r.get("reachable_now") is True for r in rows), total=len(rows),
                    entered=entered or None) if rows else None,
        friends={k: v for k, v in (p.get("friends") or {}).items() if type(v) is int},
        # individual_money_earned in stats too: the evaluator's save-diff key (scripts/free_play/journey.py).
        stats={**{k: v for k, v in stats.items() if type(v) is int},
               **({"individual_money_earned": p["money_earned_self"]} if type(p.get("money_earned_self")) is int else {})}
              or None,
        crops=dict(crops) if isinstance(crops, Mapping) else None,
        cc=None if cc is None else dict(bundles_done=cc.get("bundles_done"), bundles_total=cc.get("bundles_total"),
                                        rooms_done=cc.get("rooms_done"), rooms_total=cc.get("rooms_total"),
                                        joja=cc.get("joja")),
        grandpa=None if grandpa is None else dict(
            points=grandpa.get("points"), max=grandpa.get("max", 21), candles=grandpa.get("candles"),
            criteria=[[c[0], c[1] > 0] for c in native_rows(grandpa.get("criteria", ()))
                      if isinstance(c, list) and len(c) >= 2 and type(c[1]) is int]),
        quests=None if quests is None else dict(active=quests.get("active"), completed_total=quests.get("completed_total")),
        mail=None if mail is None else dict(unread=mail.get("unread_count"), read=mail.get("read_count")))


def letter_records(catalog):
    """Mailbox letters as mind records: unread (still in the mailbox), read this session (with text), read ids."""
    mail = _known((catalog or {}).get("mail"))
    if mail is None:
        return []
    out = []
    rows = [(r, "unread") for r in native_rows(mail.get("unread", ()))] + [(r, "read") for r in native_rows(mail.get("read_this_session", ()))]
    for row, status in rows:
        if isinstance(row, Mapping) and isinstance(row.get("id"), str):
            out.append(dict(id=row["id"], status=status, title=row.get("title") or "", text=row.get("summary") or "",
                            facts=dict(attachment=bool(row.get("attachment")), **{k: row[k] for k in (
                                "read_place", "read_place_name", "read_time") if k in row})))
    seen = {r["id"] for r in out}
    out += [dict(id=i, status="read") for i in native_rows(mail.get("read_ids", ())) if isinstance(i, str) and i not in seen]
    return out


def quest_log_read(catalog):
    """Whether this frame holds the native quest log. ADR 102: without it nothing can be said about a quest being gone."""
    log = _known((catalog or {}).get("quest_log"))
    return log is not None and log.get("complete", True) is not False


def quest_records(catalog):
    log = _known((catalog or {}).get("quest_log"))
    if log is None:
        return []
    out = []
    for q in list(native_rows(log.get("quests", ())))[:12]:
        if not isinstance(q, Mapping) or not isinstance(q.get("id"), str) or not q["id"]:
            continue
        from arti_runtime.quest_facts import capture
        facts = capture(q, catalog)
        facts.update(source="native_quest_log", read_time=log.get("read_time", "unknown"))
        out.append(dict(id=q["id"], status="completed" if q.get("completed") else "active", title=q.get("title") or "",
                        text=(q.get("objective") if str(q.get("objective") or "").strip(" .") else q.get("description")) or "", facts=facts))
    return out


def bundle_line(b):
    """One unfinished bundle: name done/slots (what it still needs; xN quantity, qN quality)."""
    needs = ", ".join("%s%s%s" % (n[0], " x%d" % n[1] if type(n[1]) is int and n[1] > 1 else "",
                                  " q%d" % n[2] if type(n[2]) is int and n[2] > 0 else "")
                      for n in native_rows(b.get("needs", ())) if isinstance(n, list) and len(n) == 3)
    from arti_runtime.bundle_knowledge import reward_suffix
    return ("%s %s/%s (%s)%s" % (b.get("name"), b.get("done"), b.get("slots"), needs, reward_suffix(b)))[:140]


def cc_rewards_line(cc, grandpa=None):
    """FIX79C (ADR 141): what each Community Center room opens when finished (native ``Strings/UI:JunimoNote_Reward*`` via
    the mod's ``room_rewards``), finished rooms marked, then Grandpa's own Community Center criterion. None without a read,
    after the Joja route or once the Center is complete. Names no bundle (ADR 129 counts bundle lines)."""
    if not isinstance(cc, Mapping) or cc.get("joja") or cc.get("cc_complete"):
        return None
    done = {r[0] for r in cc.get("rooms") or () if isinstance(r, list) and len(r) == 2 and r[1]}
    from arti_runtime.bundle_knowledge import reward_label
    parts = ["%s: %s%s" % (r["room"], reward_label(r["reward"]), " (sudah)" if r["room"] in done else "")
             for r in cc.get("room_rewards") or () if isinstance(r, Mapping) and isinstance(r.get("room"), str)
             and reward_label(r.get("reward")) and not r["reward"].startswith("unknown")]
    if not parts:
        return None
    line = "ruang selesai membuka: " + "; ".join(parts)
    row = next((c for c in (grandpa or {}).get("criteria") or () if isinstance(c, list) and len(c) >= 4
                and c[0] == "community_center"), None) if isinstance(grandpa, Mapping) else None
    if row is not None:
        line += "; evaluasi Kakek: %s (%s/%s poin)" % (row[3] if isinstance(row[3], str) and row[3] else row[0], row[1], row[2])
    return line


def bundle_rooms(bundles):
    """Unfinished bundles grouped by room: {room: [bundle_line, ...]} (the room name is stated once)."""
    rooms = {}
    for b in bundles:
        if isinstance(b, Mapping) and not b.get("complete"):
            rooms.setdefault(str(b.get("room")), []).append(bundle_line(b))
    return rooms


def quest_line(record, limit=QUEST_LINE_MAX):
    from arti_runtime.quest_facts import line
    return line(record, limit)


def letter_line(record):
    text = (record["title"] + ": " if record.get("title") else "") + (record.get("text") or "")
    text = text or "surat (judul belum diketahui)"
    facts = record.get("facts") or {}
    if record.get("status") == "read":
        place = facts.get("read_place_name")
        prefix = "dibaca di " + place if place else "tempat membaca belum diketahui"
        if facts.get("read_time"):
            prefix += " pada " + facts["read_time"]
        text = prefix + "; " + text
    if (record.get("facts") or {}).get("attachment") is True:
        text += " (+barang terlampir)"  # ADR 93: the native attachment flag was captured and never shown
    return text[:LETTER_LINE_MAX]


def tool_label(qualified_id, names=None):
    from arti_runtime.fact_names import item_name
    return item_name(qualified_id, names)


def _delta(now, before):
    return now - before if type(now) is int and type(before) is int else None


def since_start(snapshot, start):
    """Plain facts of what changed since ``start`` (the day's first snapshot): numbers, never judgement."""
    if not snapshot or not start:
        return []
    out = []
    d = _delta(snapshot.get("money_earned_self"), start.get("money_earned_self"))
    if d:
        out.append("uang yang kamu hasilkan +%dg" % d)
    for skill in SKILLS:
        d = _delta((snapshot.get("skills") or {}).get(skill), (start.get("skills") or {}).get(skill))
        if d:
            out.append("level %s +%d" % (skill, d))
    for tool, level in (snapshot.get("tools") or {}).items():
        before = (start.get("tools") or {}).get(tool)
        if type(level) is int and type(before) is int and level > before:
            out.append("%s naik ke level %d" % (tool_label(tool, snapshot.get("tool_names")), level))
    for name, points in (snapshot.get("friends") or {}).items():
        d = _delta(points, (start.get("friends") or {}).get(name, 0))
        if d and d >= 20:
            out.append("teman %s +%d poin" % (name, d))
    for key, label in (("crops_shipped", "tanaman dikirim"), ("fish_caught", "ikan ditangkap"),
                       ("quests_completed", "quest selesai"), ("items_crafted", "barang dibuat"),
                       ("seeds_sown", "benih ditanam"), ("items_foraged", "barang dipungut")):
        d = _delta((snapshot.get("stats") or {}).get(key), (start.get("stats") or {}).get(key))
        if d:
            out.append("%s +%d" % (label, d))
    d = _delta((snapshot.get("cc") or {}).get("bundles_done"), (start.get("cc") or {}).get("bundles_done"))
    if d:
        out.append("bundel Community Center selesai +%d" % d)
    d = _delta((snapshot.get("grandpa") or {}).get("points"), (start.get("grandpa") or {}).get("points"))
    if d:
        out.append("poin evaluasi Kakek %+d" % d)
    return out


def donation_facts(catalog):
    """Present native eligibility separately from fresh motor/menu admission (ADR 49)."""
    progress = _known(catalog.get("progress")) or {}
    cc = progress.get("cc") or {}
    museum = progress.get("museum") or {}
    compact, details = {}, {}
    if "can_read_junimo_text" in cc:
        compact["cc_access"] = dict(door_unlocked=cc.get("unlocked"),
                                    junimo_readable=cc["can_read_junimo_text"])
        if "joja" in cc:
            compact["cc_access"]["joja"] = cc["joja"]
        if "note_seen" in cc:
            compact["cc_access"]["note_seen"] = cc["note_seen"]
        if cc.get("unlocked") is False and any("H" in key.split("/") for key in cc.get("unlock_conditions", ())):
            compact["cc_access"]["door_unlock_actor"] = "host (native event H)"
        from arti_runtime.bundle_knowledge import present
        bundle_summary, bundle_details = present(cc)
        compact.update(bundle_summary)
        details["cc"] = {k: cc[k] for k in ("unlocked", "joja", "cc_complete", "note_seen", "can_read_junimo_text",
            "unlock_location", "unlock_conditions", "translation_location", "translation_conditions",
            "translation", "bag_matches", "bag_matches_total", "eligibility_scope") if k in cc}
        details["cc"].update(bundle_details)
        if "entry_hours" in cc:
            compact["cc_hours"] = cc["entry_hours"]
    if museum:
        matches = museum.get("bag_matches")
        compact["museum_bag"] = matches[:3] if isinstance(matches, list) else "unknown: " + str(museum.get("unknown", "unavailable"))
        if isinstance(matches, list):
            compact["museum_matches"] = len(matches)
        details["museum"] = museum
        place = next((p for p in native_rows((catalog.get("world_graph") or {}).get("places", ()))
                      if p.get("id") == "ArchaeologyHouse"), {})
        # The existing map-derived LockedDoorWarp facts include native hours and current access.
        hours = [s for s in place.get("facts", ()) if isinstance(s, str) and s.startswith("entry hours ")]
        compact["museum_hours"] = hours or ["unknown: native entry hours unavailable"]
        details["museum_entry"] = {k: place[k] for k in ("facts", "locked", "reachable_now") if k in place}
    return compact, details


def progress_block(catalog, *, start=None, letters=(), quests=(), crops=None, names=None, include_details=True):
    """The compact ``progress`` decision block (facts, about 450 B) + ``progress_details`` (DETAIL_KEYS: one
    read_boundary away: every unfinished bundle with its needs, Grandpa criteria, all quests, letter texts)."""
    from arti_runtime.quest_facts import detail
    from arti_runtime.experience_facts import view
    catalog = view(catalog)
    snap = progress_snapshot(catalog, crops=crops)
    if snap is None:
        # Preserve the public mapping contract for an explicitly empty native
        # block, without treating it as a witnessed numeric/log snapshot.
        if isinstance(catalog, Mapping) and any(isinstance(catalog.get(k), Mapping)
                and not catalog[k] for k in ('progress', 'mail', 'quest_log')):
            return {}
        return None
    p = _known(catalog.get("progress")) or {}
    block = {}
    own, team = snap.get("money_earned_self"), snap.get("money_earned")
    if type(own) is int:
        block["uang_dihasilkan"] = "%dg" % own + ("" if team in (None, own) else " (tim %sg)" % team)
    skills = snap.get("skills") or {}
    if skills:
        raised = ["%s %d" % (k, skills[k]) for k in SKILLS if skills.get(k)]
        block["skill"] = (", ".join(raised) + "; lainnya 0") if raised else "semua 0"
    tools = snap.get("tools") or {}
    from arti_runtime.fact_names import source_names
    names = {**(names or {}), **{k: v[0] for k, v in (p.get("tools") or {}).items() if isinstance(v, list) and v},
             **source_names(dict(world_knowledge=catalog))}
    upgraded = ["%s %d" % (tool_label(k, names), v) for k, v in tools.items() if v > 0]
    block["alat"] = ("naik level: " + ", ".join(upgraded)) if upgraded else "%d alat di tas, semua level dasar" % len(tools)
    if snap.get("tool_upgrading"):  # ADR 93: a tool at the blacksmith is neither lost nor "level dasar"
        block["alat"] += "; sedang diupgrade, belum di tas: " + tool_label(snap["tool_upgrading"], names)
    if type(snap.get("deepest_mine_level")) is int:
        block["tambang"] = "lantai terdalam %d" % snap["deepest_mine_level"] + (
            " (belum pernah turun)" if snap["deepest_mine_level"] == 0 else "")
    friends = sorted((snap.get("friends") or {}).items(), key=lambda kv: -kv[1])
    from arti_runtime import social_facts
    cc_full = p.get("cc") if isinstance(p.get("cc"), Mapping) else {}
    rooms_done = [r[0] for r in cc_full.get("rooms") or () if isinstance(r, list) and len(r) == 2 and r[1]]
    if friends:
        block["teman"] = "%d kenal; tertinggi %s %d hati" % (len(friends), friends[0][0], friends[0][1] // 250)
        # FIX79C (ADR 141): the closest villagers with hearts and points, the game's rules, what friendship opens. These
        # ride skills.MEANING_STAGE: they leave a full page first and come back after what she is doing.
        block["teman_hati"] = social_facts.friends_line(snap.get("friends"))
    if friends:
        block["teman_aturan"] = social_facts.RULES
    if opens := social_facts.unlock_line(p.get("grandpa"), cc_full.get("room_rewards") or (), rooms_done):
        block["teman_membuka"] = opens
    if room_line := cc_rewards_line(cc_full, p.get("grandpa")):
        block["cc_hadiah"] = room_line  # FIX79C (ADR 141): what each finished room opens, from the game's own strings
    cc, grandpa = snap.get("cc"), snap.get("grandpa")
    yard = []
    if cc:
        yard.append("Community Center bundel %s/%s, ruang %s/%s%s" % (
            cc.get("bundles_done"), cc.get("bundles_total"), cc.get("rooms_done"), cc.get("rooms_total"),
            ", jalur Joja" if cc.get("joja") else ""))
    if grandpa:
        yard.append("evaluasi Kakek %s/%s poin" % (grandpa.get("points"), grandpa.get("max")))
    if yard:
        block["tamat"] = "; ".join(yard)
    active = [q for q in quests if q.get("status") == "active"]
    if active:
        block["quest"] = [quest_line(q, limit=80) for q in active[:2]] + (
            ["+%d lagi" % (len(active) - 2)] if len(active) > 2 else [])
    if quests:
        block["quest_read"] = dict(source="native_quest_log", read_time=quests[0].get("facts", {}).get("read_time", "unknown"), cap=12)
    unread = [r for r in letters if r.get("status") == "unread"]
    recent = [r for r in letters if r.get("status") == "read" and (r.get("text") or r.get("title"))]
    mail = []
    if unread:
        mail.append("%d belum dibaca di kotak surat: %s" % (len(unread), ", ".join(
            (r.get("title") or "surat (judul belum diketahui)")[:30] for r in unread[:3])))
    if recent:
        mail.append("terakhir dibaca: " + letter_line(recent[-1])[:90])
    if mail:
        block["surat"] = mail
    changed = since_start(snap, start)
    if changed:
        block["sejak_pagi"] = "; ".join(changed)[:160]
    cc_full = p.get("cc") if isinstance(p.get("cc"), Mapping) else {}
    if not include_details:
        donations, _ = donation_facts(catalog)
        if donations:
            block['donation_facts'] = donations
        return block
    block["progress_details"] = dict(
        bundles=bundle_rooms(cc_full.get("bundles", ())),
        rooms_done=[r[0] for r in cc_full.get("rooms") or () if isinstance(r, list) and len(r) == 2 and r[1]],
        grandpa=["%s %s/%s" % (c[0], c[1], c[2]) for c in (p.get("grandpa") or {}).get("criteria", ())
                 if isinstance(c, list) and len(c) >= 3],
        quests=[("%s: " % q.get("status")) + quest_line(q) for q in quests][:12],
        # ADR 69/74: rows hold only fields their detail line cannot carry whole, never the clock sample
        # (the read handle hashes details, so clock ticks alone must not invalidate an offered page).
        quest_facts=[dict(id=q.get("id"), **detail(q.get("facts") or {}, quest_line(q))) for q in quests][:12],
        letters=[("%s: " % r.get("status")) + letter_line(r)[:100] for r in letters if r.get("text")][-4:],
        friends=["%s %d" % kv for kv in friends[:8]], teman_aturan=social_facts.RULES_DETAIL,
        tools=["%s %s" % (k, v) for k, v in tools.items()],
        stats=snap.get("stats"))
    if not friends:
        block["progress_details"].pop("teman_aturan", None)
    donations, donation_details = donation_facts(catalog)
    if donations:
        block["donation_facts"] = donations
        block["progress_details"]["donations"] = donation_details
    return block


def reflection_facts(snapshot, start, *, letters=(), quests=(), wallet=None):
    """fix13c bedtime evidence: citable game facts ``{ref, text}`` for the reflection (refs are stable per fact kind,
    so a goal marked done names the fact that proves it). Today's changes first, then standing yardsticks."""
    facts = []

    def add(ref, text):
        if text:
            from arti_runtime.fact_names import text as named_text
            text = named_text(text)
            facts.append(dict(ref=ref, text=str(text)[:140]))
    if type(wallet) is int:
        add("p:wallet", "uang di dompet sekarang %dg" % wallet)
    for i, line in enumerate(since_start(snapshot, start), 1):
        add("p:today%d" % i, "hari ini: " + line)
    snap = snapshot or {}
    if type(snap.get("money_earned_self")) is int:
        add("p:earned", "total uang yang pernah kamu hasilkan %dg" % snap["money_earned_self"])
    if snap.get("skills"):
        add("p:skills", "level skill: " + ", ".join("%s %d" % (k, snap["skills"].get(k, 0)) for k in SKILLS))
    upgraded = [k for k, v in (snap.get("tools") or {}).items() if v > 0]
    if snap.get("tools") is not None:
        add("p:tools", "alat naik level: " + ", ".join(upgraded) if upgraded else "semua alat masih level dasar")
    cc = snap.get("cc") or {}
    if cc:
        add("p:cc", "Community Center: bundel %s/%s, ruang %s/%s" % (cc.get("bundles_done"), cc.get("bundles_total"),
                                                                    cc.get("rooms_done"), cc.get("rooms_total")))
    grandpa = snap.get("grandpa") or {}
    if grandpa:
        add("p:grandpa", "evaluasi Kakek: %s/%s poin" % (grandpa.get("points"), grandpa.get("max")))
    for record in quests:
        if record.get("status") == "completed":
            add("q:" + record["id"], "quest selesai: " + quest_line(record))
        elif record.get("status") == "active":
            add("q:" + record["id"], "quest aktif: " + quest_line(record))
        elif record.get("status") == "not_in_quest_log":
            add("q:" + record["id"], "quest tidak ada lagi di jurnal: " + (record.get("title") or record["id"]))
    for record in letters[-3:]:
        add("m:" + record["id"], ("surat belum dibaca: " if record.get("status") == "unread" else "surat sudah dibaca: ")
            + letter_line(record))
    return facts

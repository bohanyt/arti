"""ADR 168: presentation knowledge follows scoped native experience, never graph reachability.

The unfiltered catalogue remains the motor's admission source. This copy is for
pages and speech; destinations remain explorable even before their facts are known.
"""
from collections.abc import Mapping
from arti_runtime.native_rows import native_rows
from arti_runtime.offer_cache import memoized as _offer_memoized, ENABLED as _OFFER_CACHE_ENABLED
import re


def events(catalog):
    rows = catalog.get("discoveries")
    return [r for r in rows if isinstance(r,Mapping)] if isinstance(rows, (list, tuple)) else [
        v for k, v in catalog.items() if k.startswith("discovery:") and isinstance(v, Mapping)]


def entered(catalog):
    known = {r.get("subject") for r in experiences(catalog) if r.get("kind") == "location_entered"}
    current = (catalog.get("world_graph") or {}).get("current")
    if current:
        known.add(current)
    return known


def experiences(catalog):
    return list(events(catalog)) + [r for r in native_rows(catalog.get("experience_records")) if isinstance(r,Mapping)]


def text_evidence(catalog):
    """Only delivered/read text, with a ref. Loaded mail/graph/unlock data is not experience (ADR 178)."""
    for row in experiences(catalog):
        if row.get("kind") in ("place_learned", "door_seen", "sign_read", "text_read", "dialogue_read", "said_to_you", "system_read", "board_read"):
            ref = row.get("observation_ref") or row.get("source") or row.get("id")
            if ref:
                yield row.get("text", ""), str(ref), row
    for row in native_rows((catalog.get("mail") or {}).get("read_this_session", ())):
        if isinstance(row, Mapping) and row.get("id"):
            yield " ".join(str(row.get(k) or "") for k in ("title", "summary")), "letter:" + row["id"], row
    for row in native_rows((catalog.get("quest_log") or {}).get("quests", ())):
        if isinstance(row, Mapping) and row.get("id"):
            yield " ".join(str(row.get(k) or "") for k in ("title", "description", "objective")), "native_quest_log:" + row["id"], row


def place_sources(catalog):
    if not _OFFER_CACHE_ENABLED.get():
        return _place_sources(catalog)
    # Only source derivation inputs: rotating progress/tick rows cannot invalidate
    # this expensive text/name index. Scope and every occurrence remain in key.
    graph = catalog.get('world_graph') or {}
    return _place_sources(dict(scope=catalog.get('scope'),
        world_graph=dict(current=graph.get('current'), places=[{k:p[k] for k in
            ('id','label','experience_known','fact_source') if k in p} for p in native_rows(graph.get('places', ())) if isinstance(p, Mapping)]),
        discoveries=events(catalog), experience_records=catalog.get('experience_records'),
        mail=catalog.get('mail'), quest_log=catalog.get('quest_log')))


@_offer_memoized
def _place_sources(catalog):
    sources = {p: "location_entered:" + p for p in entered(catalog) if isinstance(p, str)}
    for p in native_rows((catalog.get("world_graph") or {}).get("places", ())):
        if isinstance(p, Mapping) and p.get("id") and p.get("experience_known") is True:
            sources.setdefault(p["id"], p.get("fact_source", "native_experience"))
    # ADR 225: one scan/index per invocation. No argument serialization, copies
    # or cross-boundary cache; first evidence and native place order stay intact.
    candidates = [(p.get('id'), p.get('label')) for p in
                  native_rows((catalog.get('world_graph') or {}).get('places', ()))
                  if isinstance(p, Mapping) and p.get('id') not in sources]
    from arti_runtime.fact_line import fold
    patterns, folds = {}, {}
    def mentioned(name, text, folded):
        if not isinstance(name, str) or not name:
            return False
        if name not in folds:
            folds[name] = fold(name)
        if folds[name] not in folded:
            return False  # the case-blind search below cannot match
        if name not in patterns:
            patterns[name] = re.compile(r'(?<!\w)' + re.escape(name) + r'(?!\w)', re.I)
        return patterns[name].search(text) is not None
    for text, ref, evidence in text_evidence(catalog):
        text = str(text)
        folded = fold(text)
        for identity, label in candidates:
            if identity in sources:
                continue
            explicit = evidence.get("subject") == identity and evidence.get("kind") in ("place_learned", "door_seen", "sign_read")
            if identity and (explicit or any(mentioned(name, text, folded) for name in (identity, label))):
                sources.setdefault(identity, ref)
    return sources


def explore_arg(arg):
    """Opaque travel handle; never encodes a native place name. Execution still uses the original call."""
    import hashlib
    return "jelajah-" + hashlib.sha256(str(arg).encode()).hexdigest()[:12]


def travel_page(offer, catalog):
    """Remove unseen names/routes/estimates from travel presentation, keeping every offered motor call."""
    from arti_stardew_world import location_arg
    graph = (catalog or {}).get("world_graph") or {}
    public_names = {("home" if p.get("id") == graph.get("home") else location_arg(p["id"])):
                    p.get("label") or p["id"] for p in native_rows(graph.get("places", ()))
                    if isinstance(p, Mapping) and p.get("id")}
    known = place_sources(catalog or {})
    unknown_ids = {p["id"] for p in native_rows(graph.get("places", ()))
                   if isinstance(p, Mapping) and p.get("id") and p["id"] not in known}
    unknown = {("home" if p.get("id") == graph.get("home") else location_arg(p["id"]))
               for p in native_rows(graph.get("places", ())) if isinstance(p, Mapping) and p.get("id") and p["id"] not in known}
    def call_view(value):
        if isinstance(value, Mapping):
            row = {k: call_view(v) for k, v in value.items()}
            arg = row.get("arg")
            if (row.get("skill") or row.get("name")) == "pergi_ke" and isinstance(arg, str) and arg in unknown:
                row["arg"] = explore_arg(arg)
                row.pop("label", None)
            if isinstance(row.get("destination"), str) and row["destination"] in unknown:
                row["destination"] = explore_arg(row["destination"])
            return row
        if isinstance(value, (list, tuple)):
            rows = [call_view(v) for v in value]
            if len(rows) >= 2 and rows[0] == "pergi_ke" and isinstance(rows[1], str) and rows[1] in unknown:
                rows[1] = explore_arg(rows[1])
            return tuple(rows) if isinstance(value, tuple) else rows
        return value
    for key in ("recent_actions", "outcomes", "interrupted_travel", "unproductive_travel_loop", "travel_shuttle", "recovery"):
        if key in offer:
            offer[key] = call_view(offer[key])  # copies: native history/admission retain their original identity
    for entry in offer.get("skills", ()):
        if entry.get("name") != "pergi_ke":
            continue
        # Native map names are public geography; visits still gate their contents.
        entry["args"] = list(entry.get("args", ()))
        after = entry.get("after_travel")
        if isinstance(after, Mapping):
            entry["after_travel"] = {a:
                dict(label=public_names.get(a, "area belum dijelajahi"), can_do=[]) if a in unknown else row for a, row in after.items()}
            for row in entry["after_travel"].values():
                if isinstance(row, dict) and isinstance(row.get("route"), (list, tuple)):
                    row["route"] = ["area belum dijelajahi" if hop in unknown_ids else hop for hop in row["route"]]
        arrival = entry.get("arrival_facts")
        if isinstance(arrival, Mapping):
            rows = arrival.get("rows", ())
            rows = ({a: r for a, r in rows.items() if a not in unknown} if isinstance(rows, Mapping)
                    else [r for r in rows if r[0] not in unknown])
            entry["arrival_facts"] = dict(arrival, rows=rows,
                sources={a: s for a, s in arrival.get("sources", {}).items() if a not in unknown})
        named = {a:n for a,n in (entry.get("nama") or {}).items() if a in entry.get("args", ())}
        for a in entry.get("args", ()):
            if a in public_names and named.get(a) in (None, "tempat (nama belum diketahui)", "area belum dijelajahi"):
                named[a] = public_names[a]
        after_rows = entry.get("after_travel") if isinstance(entry.get("after_travel"), Mapping) else {}
        for a in entry.get("args", ()):
            if not isinstance(a, str):
                continue
            if a.startswith("jelajah-"):
                if named.get(a) in (None,"tempat (nama belum diketahui)","area belum dijelajahi"):
                    named[a] = next((label for arg, label in public_names.items() if explore_arg(arg) == a),
                                    "tempat (nama belum diketahui)")
            elif a not in named:
                label = after_rows.get(a, {}).get("label") if isinstance(after_rows.get(a), Mapping) else None
                if isinstance(label, str) and label and label != "area belum dijelajahi":
                    named[a] = label
        if named:
            entry["nama"] = named
        if not entry.get("does"):
            entry["does"] = "Pergi lewat jalan keluar; nama dari peta game, isi tempat diamati saat tiba."
    # The native-door label summary is built earlier than this presentation pass.
    # Rebuild it from known destinations so an @ID cannot leak beside its opaque handle.
    day = offer.get("day")
    if isinstance(day, dict) and isinstance(day.get("perjalanan_jeda"), str):
        for arg in sorted(unknown, key=len, reverse=True):
            day["perjalanan_jeda"] = re.sub(r"(?<![\w@])" + re.escape(arg) + r"(?!\w)",
                                            explore_arg(arg), day["perjalanan_jeda"])
    if isinstance(day, dict) and "pintu_nama" in day:
        from arti_runtime.travel_facts import door_names
        names = door_names({a: row for entry in offer.get("skills", ()) if entry.get("name") == "pergi_ke"
                            for a, row in (entry.get("after_travel") or {}).items() if not a.startswith("jelajah-")})
        if names:
            day["pintu_nama"] = "; ".join("%s = %s" % kv for kv in names.items())
        else:
            day.pop("pintu_nama", None)


# ADR 230 N2: the native npc_met sources that mean the game showed her that person: a dialogue or a scene
# line with the speaker's name on screen, or a name the social page drew (NativeWorldKnowledge.ObservePeopleShown).
PERSON_SHOWN_SOURCES = ("native_dialogue", "native_event", "native_social_page")


def person_shown(row):
    """False for a person row from friendship data alone (native source ``game_state``, written before ADR 230).

    Such rows stay in her store (never rewritten); they only stop counting as "met" until a shown receipt exists.
    """
    kind = row.get("kind") if isinstance(row, Mapping) else None
    if kind not in ("npc_met", "villager_met"):
        return True
    return str(row.get("source") or "") in PERSON_SHOWN_SOURCES


def witnessed_event(row):
    if row.get("kind") == "unknown":
        return row.get("text") in ("discovery: unknown", "fakta game (nama atau makna belum diketahui)")
    if not person_shown(row):
        return False  # ADR 230 N2: friendship data alone is not meeting her
    return row.get("kind") in ("location_entered", "npc_met", "item_seen", "item_obtained", "story_event",
                               "rare_event", "calendar_read", "board_read", "grandpa_evaluation", "system_read",
                               "place_learned", "door_seen", "sign_read", "text_read", "dialogue_read")


def experienced(catalog, feature):
    """Resolve a public feature through the existing native travel aliases."""
    from arti_stardew_interact import _LOCATION_ALIASES
    return _LOCATION_ALIASES.get(feature, feature) in place_sources(catalog)


def view(catalog):
    if not isinstance(catalog, Mapping) or catalog.get('experience_view') and not catalog.get('fact_line'):
        return catalog
    return _view(catalog)


def _view(catalog):
    """One generic witness rule; never mutate the catalogue used for execution.

    Scoped personal read/visit receipts remain evidence from an older save even
    when its first-seen rows have not arrived in this rotating frame yet.
    Team completion/unlock flags alone do not prove this actor saw the Center.
    """
    if isinstance(catalog, Mapping) and catalog.get("fact_line"):
        from arti_runtime.fact_line import witnessed_catalog, SETTING
        return witnessed_catalog(catalog, {SETTING: True})
    if not isinstance(catalog, Mapping) or catalog.get("experience_view"):
        return catalog
    known = place_sources(catalog)
    graph = catalog.get("world_graph") or {}
    places, sources = [], {}
    for p in native_rows(graph.get("places", ())):
        if not isinstance(p, Mapping):
            continue
        facts, witnessed = [], []
        for fact in native_rows(p.get("facts", ())):
            # Entering the map containing a door does not mean that door was seen.
            if p.get("id") in known:
                facts.append(fact)
                witnessed.append(known[p["id"]])
        row = dict(p, facts=facts, experience_known=p.get("id") in known)
        if witnessed:
            row["fact_source"] = "; ".join(dict.fromkeys(witnessed))
            sources[p["id"]] = row["fact_source"]
        if p.get("id") not in known:
            row.pop("locked", None)  # admission still reads the original graph
            row["label"] = "area belum dijelajahi"
        places.append(row)
    progress = catalog.get("progress")
    if isinstance(progress, Mapping):
        progress = dict(progress)
        cc_seen = any(r.get("kind") == "system_read" and r.get("subject") == "cc" for r in experiences(catalog))
        if not cc_seen:
            progress.pop("cc", None)
        else:
            sources["cc"] = "dari catatan pribadi: buku/tulisan Junimo sudah dibaca"
        from arti_stardew_interact import _LOCATION_ALIASES
        if _LOCATION_ALIASES.get("museum", "museum") not in entered(catalog):
            progress.pop("museum", None)
        else:
            sources["museum"] = "dari kunjungan ke museum"
        # Personal friendship entries are the native met record. No locations
        # or schedules of villagers elsewhere are inferred from them.
        if progress.get("friends"):
            sources["friends"] = "dari pertemuan yang tercatat di pertemanan pribadi"
        if not any(r.get("kind") == "grandpa_evaluation" for r in experiences(catalog)):
            progress.pop("grandpa", None)
    board_seen = any(r.get("kind") in ("calendar_read", "board_read") for r in experiences(catalog))
    visible = [dict(r, references=[]) for r in events(catalog) if witnessed_event(r)]
    result = {k: v for k, v in catalog.items() if not k.startswith("discovery:")}
    result.update(experience_view=True, fact_sources=sources, discoveries=visible,
                  world_graph=dict(graph, places=places,
                                   connections=[e for e in native_rows(graph.get("connections", ())) if e and all(n in known for n in e[:2])],
                                   unknown=[]),
                  calendar=catalog.get("calendar", ()) if board_seen else [])
    if catalog.get("fact_line") is True:
        # ADR 230 N4: the calendar loaded with the save is not an observation. On a fact-line page only the
        # entries she read on the board (a calendar_day_read receipt naming them) count; nothing else follows.
        from arti_runtime.fact_line import Witnesses
        result["calendar"] = Witnesses(catalog).calendar()
    if isinstance(catalog.get("mail"), Mapping):
        result["mail"] = dict(catalog["mail"], unread=[dict(r, title="", summary="")
            for r in native_rows(catalog["mail"].get("unread", ())) if isinstance(r, Mapping)])
    if isinstance(progress, Mapping):
        result["progress"] = progress
    # Native game facts can describe remote unlocks and rewards. Only the
    # acquired item's own facts are earned; the rest stays for native admission.
    result["game_facts"] = [str(r.get("text")) for r in visible
                            if r.get("kind") in ("item_seen", "item_obtained")]
    return result


def place_line(catalog):
    catalog = view(catalog) or {}
    parts = ["%s: %s (sumber: %s)" % (p.get("label") or p["id"], "; ".join(meaning), p["fact_source"])
             for p in native_rows((catalog.get("world_graph") or {}).get("places", ()))
             if (meaning := [f for f in native_rows(p.get("facts", ())) if str(f).startswith(("shop counter ", "fishing:"))])]
    shown = []
    for part in parts:
        if len("; ".join(shown + [part])) > 650:
            break
        shown.append(part)
    if len(shown) < len(parts):
        shown.append("+%d tempat pernah dilihat; fakta lengkap di world_details" % (len(parts) - len(shown)))
    return "; ".join(shown)


def progress_view(progress, catalog):
    """Apply the witness rule *after* the rotating-frame match cache is read."""
    return (view(dict(catalog or {}, experience_view=False, progress=progress)) or {}).get("progress")


def with_memory(catalog, base, cache):
    """Rejoin the existing first-seen ledger when the native wire rotates rows.

    No TTL or current-day filter: an old visit in a carried save stays earned.
    The cache is keyed by file revision *and* native save/actor scope.
    """
    if not isinstance(catalog, Mapping) or base is None or not catalog.get("scope"):
        return catalog
    import hashlib
    import arti_mind
    import arti_state
    path = arti_mind.game_dir(base, "stardew") / "discoveries.jsonl"
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size, catalog["scope"])
        if cache.get("key") != key:
            scope = hashlib.sha256(catalog["scope"].encode()).hexdigest()
            from arti_runtime import jsonl_snapshot  # ADR 231 part C: parsed once per file revision
            # ADR 232 part A: speech rows (anyone's, her own lines included) are never her experience of the game.
            cache.update(key=key, rows=[r for r in jsonl_snapshot.rows(path) if r.get("scope_hash") == scope
                                        and not str(r.get("source") or "").startswith("stream:")])
        prior = cache.get("rows", ())
    except (OSError, ValueError):
        prior = []
    # A rotating-frame cache supplements current sourced evidence; it must not
    # erase an inline read receipt just because no ledger row has arrived yet.
    joined = {}
    for row in list(catalog.get("experience_records") or ()) + list(prior):
        if isinstance(row, Mapping):
            key = row.get("id") or (str(row.get("kind")), str(row.get("subject")),
                                     str(row.get("source")), str(row.get("text")))
            joined[key] = row
    return dict(catalog, experience_view=False, experience_records=list(joined.values()))

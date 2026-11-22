"""ARTI's bedtime reflection (mind slice 1): one model call per verified sleep. Game-agnostic.

Design: runbooks/MIND_LAYER_DESIGN_2026-10-28.md §2.4. The caller passes a ``complete(messages, schema) -> str``
callable bound to its own provider lane; this module builds the ≤3 kB input, validates the JSON reply, drops
every claim whose ref is not in the day's verified set, and writes goals + the journey row through arti_mind.

Failure (provider error, malformed reply) invents nothing: today's open goals carry to the next day with
``stale: true`` and the caller journals ``mind_reflection_failed`` (the Live7 watcher turns it into an alert).

fix13c (PROGRESS_GAP_AUDIT_2026-10-29: 0 goals done in 28 reflections): ``progress`` carries game facts with refs (the
adapter's progress surface, letters, quests); a goal is done only with a cited verified or progress ref, which is
stored as its evidence. Letters/quests are facts; she adopts them as goals or not.

FIX75C (ADR 133; a54 t20-1 carried 7 nights, one from the old cap): a goal she neither finished nor dropped carries
forward every night, any horizon, with no cap on days; ``long`` is a horizon; her ``fun`` / ``not_fun`` claims are
kept as dated memory (``likes.jsonl``) and shown back at bedtime; a new goal over its slot cap is refused, logged, and
shown to her the next night so the choice of what to drop stays hers. ``pref_updates`` is not in the schema.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import arti_mind as M

# fix13c: + progress evidence facts (design 3 kB before game facts had refs). FIX71D (a52: 4096 B left `whys` empty on 29 of 29
# requests and `progress` on 9 of 29; verified kept up to 2958 B): the bedtime lane is not the speech prompt (ADR 119).
INPUT_MAX_BYTES = 6144
TODAY_MAX = 200
TOMORROW_MAX = 160
CLAIM_MAX = 120
CLAIMS_MAX = 3
VERIFIED_MAX = 24
WHYS_MAX = 8
PROGRESS_MAX = 20  # ADR 58: bounded native bundle rewards/matches alongside the existing citable facts
DONE_WHEN_MAX = 80
EVIDENCE_KEEP = 3
NOT_KEPT_SHOWN = 2  # refused new goals from last night, shown once

SYSTEM = (
    "Goal date: t/w/lD-N means game day D; set overrides the authored day. "
    "calendar = tanggal native, bukan perintah. calendar_links menghubungkan id tujuan (atau new:0, new:1 untuk "
    "tujuan baru; today/tomorrow untuk rencana) ke ref calendar saat tulisan menamai acara, termasuk nama terjemahannya. Tanggal tidak membuktikan "
    "kehadiran atau penyelesaian. whys dan today adalah tulisanmu, bukan bukti native. "
    "Kamu ARTI. Kamu baru tidur di akhir satu hari main game. Tulis refleksi singkat dengan suaramu sendiri "
    "(bahasa Indonesia santai). Balas JSON sesuai schema saja. "
    "today: ringkasan harimu (≤200). fun / not_fun: hal yang menurutmu seru / nggak seru, masing-masing dengan ref "
    "dari verified; klaim tanpa ref verified akan dibuang; yang tersimpan muncul lagi di yang_kamu_suka. "
    "goals: keep / drop berisi id dari goals_open; done berisi "
    "id + ref verified; new = tujuan barumu (horizon today = besok, week = beberapa hari ke depan, long = sampai "
    "kejadiannya terjadi, berapa lama pun; text ≤100), boleh kosong. "
    "tomorrow: niatmu untuk besok (≤160). Tujuan itu pilihanmu sendiri; verified dan progress cuma fakta. "
    "progress = fakta game dengan ref (uang, skill, alat, teman, Community Center, evaluasi Kakek, surat, quest). "
    "goals.done hanya untuk tujuan yang terbukti selesai: ref-nya dari verified atau progress jadi buktinya; tanpa "
    "bukti tujuan tetap terbuka. goals.progress = tujuan yang maju tapi belum selesai, dengan ref buktinya (boleh "
    "kosong). Tujuan yang tidak kamu drop dan belum selesai terbawa terus, malam demi malam, sampai selesai atau kamu "
    "drop; umur_hari dan maju_terakhir di goals_open cuma fakta. slots = isi tiap horizon; kalau penuh, tujuan baru di "
    "horizon itu tidak tersimpan (lihat not_kept_last_night) kecuali ada yang kamu drop. new.done_when = fakta game "
    "yang menandai tujuan itu selesai (≤80, boleh kosong). Surat dan quest cuma fakta: boleh kamu jadikan tujuan, "
    "boleh juga tidak."
)

_CLAIM = {"type": "object", "additionalProperties": False,
          "properties": {"text": {"type": "string", "maxLength": CLAIM_MAX}, "ref": {"type": "string"}},
          "required": ["text", "ref"]}
SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "today": {"type": "string", "maxLength": TODAY_MAX},
        "fun": {"type": "array", "maxItems": CLAIMS_MAX, "items": _CLAIM},
        "not_fun": {"type": "array", "maxItems": CLAIMS_MAX, "items": _CLAIM},
        "goals": {"type": "object", "additionalProperties": False, "properties": {
            "keep": {"type": "array", "items": {"type": "string"}},
            "done": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "properties": {"id": {"type": "string"}, "ref": {"type": "string"}}, "required": ["id", "ref"]}},
            "drop": {"type": "array", "items": {"type": "string"}},
            "progress": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                         "properties": {"id": {"type": "string"}, "ref": {"type": "string"}}, "required": ["id", "ref"]}},
            "calendar_links": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "ref": {"type": "string"}}, "required": ["id", "ref"]}},
            "new": {"type": "array", "maxItems": 4, "items": {"type": "object", "additionalProperties": False,
                    "properties": {"horizon": {"type": "string", "enum": list(M.HORIZONS)},
                                   "text": {"type": "string", "maxLength": M.GOAL_TEXT_MAX},
                                   "done_when": {"type": "string", "maxLength": DONE_WHEN_MAX}},
                    "required": ["horizon", "text", "done_when"]}},
        }, "required": ["keep", "done", "drop", "progress", "new", "calendar_links"]},
        "tomorrow": {"type": "string", "maxLength": TOMORROW_MAX},
    },
    "required": ["today", "fun", "not_fun", "goals", "tomorrow"],
}


class ReflectionError(ValueError):
    """The reply cannot be used; nothing is written from it."""


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def progress_facts(progress: Iterable[Any]) -> list[dict]:
    """Game facts as ``{ref, text}``: a mapping keeps its ref; a plain string gets ``p<n>``."""
    out = []
    for i, item in enumerate(progress, 1):
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            out.append({"ref": str(item.get("ref") or "p%d" % i), "text": item["text"][:140]})
        elif isinstance(item, str) and item:
            out.append({"ref": "p%d" % i, "text": item[:140]})
    return out[:PROGRESS_MAX]


def _goal_view(g: Mapping, *, game: str = "", day: int | None = None, calendar=(), native_targets=()) -> dict:
    view = dict(M.goal_view(g, game=game, day=day, calendar=calendar, native_targets=native_targets), horizon=g["horizon"])
    if g.get("done_when"):
        view["done_when"] = M.plan_dates.annotate(g["done_when"], M.dated_goal(g, game, calendar), game=game, day=day)
    if g.get("carried_days"):
        view["carried_days"] = g["carried_days"]
    return view  # FIX75C: the last cited evidence rides as ``maju_terakhir`` (dated) from M.goal_view, not twice


def slot_facts(goals_open: Iterable[Mapping]) -> dict[str, str]:
    """``{"today": "1/3", ...}``: how full each horizon is, so the choice of what to drop stays hers (FIX75C)."""
    rows = list(goals_open)
    return {h: "%d/%d" % (sum(1 for g in rows if g.get("horizon") == h), cap) for h, cap in M.GOAL_CAPS.items()}


def build_input(*, day_label: str, verified: Iterable[Mapping], whys: Iterable[str], goals_open: Iterable[Mapping],
                prefs: Iterable[Mapping], progress: Iterable[Any] = (), max_bytes: int = INPUT_MAX_BYTES,
                game: str = "", day: int | None = None, goals_expired: Iterable[Mapping] = (), calendar=(), native_targets=(),
                likes: Mapping[str, list[str]] | None = None, not_kept: Iterable[Mapping] = (), seen_calendar=False) -> dict:
    """The user payload (≤ ``max_bytes``). The largest of whys / progress / verified gives up its oldest row; never goals,
    prefs, slots, likes or last night's refused goals (each bounded by construction; FIX75C reports what they displace)."""
    from arti_runtime.goal_facts import annotate
    goals_open = list(goals_open)
    if seen_calendar:
        from arti_runtime.fact_line import plan_views
        calendar = [dict(e, _receipt_only=True) for e in calendar]
        goals_open = plan_views(goals_open, calendar)
        goals_expired = plan_views(goals_expired, calendar)
    payload = {
        "day": str(day_label),
        "verified": [{"ref": str(v["ref"]), "text": str(v["text"])[:140]} for v in verified][-VERIFIED_MAX:],
        "whys": [annotate(str(w)[:M.WHY_MAX_CHARS], native_targets) for w in whys if w][-WHYS_MAX:],
        "whys_basis": "authored intent, not native completion evidence",
        "progress": progress_facts(progress),
        "goals_open": [_goal_view(g, game=game, day=day, calendar=calendar, native_targets=native_targets) for g in goals_open],
        "slots": slot_facts(goals_open),
        "prefs": [p["text"] for p in prefs][:M.TRAIT_LINES + M.GAME_PREF_LINES],
    }
    if likes and (likes.get("suka") or likes.get("nggak_suka")):
        payload["yang_kamu_suka"] = {k: list(v)[-M.LIKES_SHOWN:] for k, v in likes.items() if v}
    refused = [dict(horizon=str(r.get("horizon")), text=str(r.get("text"))[:M.GOAL_TEXT_MAX], reason="slot_full")
               for r in not_kept if isinstance(r, Mapping) and r.get("text")][:NOT_KEPT_SHOWN]
    if refused:
        payload["not_kept_last_night"] = refused
    if calendar:
        season = (day - 1) // 28 % 4 if type(day) is int else None
        text = " ".join(g["text"] for g in goals_open)
        payload["calendar"] = [dict(id=e["id"], name=e["name"], dates=e["dates"],
            **({"sumber": e["sumber"]} if seen_calendar and e.get("sumber") else {})) for e in calendar
            if any((d := M.plan_dates.ordinal(game, date)) is not None and (d - 1) // 28 % 4 == season
                   for date in e.get("dates", ())) or M.plan_dates.event_references(text, game=game, authored_day=day, calendar=[e])]

    expired = list(goals_expired)
    if expired:
        payload["goals_expired"] = [M.goal_view(g, game=game, day=day, calendar=calendar, native_targets=native_targets) for g in expired]
    while _size(payload) > max_bytes:
        largest = max(("whys", "progress", "verified"), key=lambda key: _size(payload[key]))
        if not payload[largest]:
            break
        payload[largest].pop(0)
    return payload


def messages_for(payload: Mapping) -> list[dict[str, str]]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]


def _text(value, max_len, field) -> str:
    if not isinstance(value, str):
        raise ReflectionError("wrong_type:" + field)
    value = " ".join(value.split())
    if len(value) > max_len:
        raise ReflectionError("too_long:" + field)
    return value


def field_reason(value, max_len, field) -> str | None:
    """fix131A: why one text field is refused, with its measured length (``too_long:today:213/200``), else None."""
    if not isinstance(value, str):
        return "wrong_type:" + field
    length = len(" ".join(value.split()))
    return "too_long:%s:%d/%d" % (field, length, max_len) if length > max_len else None


TEXT_FIELDS = (("today", TODAY_MAX), ("tomorrow", TOMORROW_MAX))


def parse(raw: str, *, verified_refs: Iterable[str], open_goal_ids: Iterable[str],
          fact_text: Mapping[str, str] | None = None, horizons=M.HORIZONS,
          per_field: bool = False) -> tuple[dict, list[dict]]:
    """(result, dropped). Strict JSON object; claims/done without a verified/progress ref and unknown ids are dropped.

    ``fact_text`` maps every citable ref (verified + progress) to its text; a done item stores that text as evidence.

    ``per_field`` (fix131A, goal ladder only; default False keeps every existing caller's behaviour): an invalid
    ``today``/``tomorrow``/``fun``/``not_fun``/``goals`` is refused alone and left empty, its reason in ``dropped``
    (``kind: field``); her words are never cut or rewritten. Without it, the first invalid field raises as before."""
    if per_field:
        return _parse_per_field(raw, verified_refs=verified_refs, open_goal_ids=open_goal_ids,
                                fact_text=fact_text, horizons=horizons)
    text = raw.strip() if isinstance(raw, str) else raw
    if isinstance(text, str) and text.startswith("```") and text.endswith("```"):
        text = text.strip("`").removeprefix("json").strip()  # a bare code fence only; no prose extraction
    try:
        obj = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ReflectionError("not_json") from exc
    if not isinstance(obj, dict):
        raise ReflectionError("not_object")
    fact_text = dict(fact_text or {})
    refs, ids = set(verified_refs) | set(fact_text), set(open_goal_ids)
    dropped: list[dict] = []
    result: dict[str, Any] = {"today": _text(obj.get("today", ""), TODAY_MAX, "today"),
                              "tomorrow": _text(obj.get("tomorrow", ""), TOMORROW_MAX, "tomorrow")}
    for key in ("fun", "not_fun"):
        items = obj.get(key) or []
        if not isinstance(items, list):
            raise ReflectionError("wrong_type:" + key)
        kept = []
        for item in items[:CLAIMS_MAX * 2]:
            if not isinstance(item, dict):
                dropped.append(dict(kind=key, reason="wrong_type"))
                continue
            text, ref = item.get("text"), item.get("ref")
            if ref not in refs:
                dropped.append(dict(kind=key, reason="unverified_ref", ref=str(ref)[:40]))
            elif M.text_reason(text, CLAIM_MAX):
                dropped.append(dict(kind=key, reason=M.text_reason(text, CLAIM_MAX), ref=ref))
            elif len(kept) < CLAIMS_MAX:
                kept.append(dict(text=" ".join(text.split()), ref=ref))
        result[key] = kept
    goals = obj.get("goals") or {}
    if not isinstance(goals, dict):
        raise ReflectionError("wrong_type:goals")
    def known(values, kind):
        out = []
        for value in values if isinstance(values, list) else ():
            if isinstance(value, str) and value in ids:
                out.append(value)
            else:
                dropped.append(dict(kind=kind, reason="unknown_goal", id=str(value)[:40]))
        return out
    done = []
    for item in goals.get("done") or []:
        if not isinstance(item, dict) or item.get("id") not in ids:
            dropped.append(dict(kind="done", reason="unknown_goal", id=str((item or {}).get("id"))[:40]
                                if isinstance(item, dict) else None))
        elif item.get("ref") not in refs:
            dropped.append(dict(kind="done", reason="unverified_ref", id=item["id"], ref=str(item.get("ref"))[:40]))
        else:
            done.append(dict(id=item["id"], ref=item["ref"],
                             **({"evidence": fact_text[item["ref"]]} if item["ref"] in fact_text else {})))
    progressed = []
    for item in goals.get("progress") or []:
        if not isinstance(item, dict) or item.get("id") not in ids:
            dropped.append(dict(kind="progress", reason="unknown_goal"))
        elif item.get("ref") not in refs:
            dropped.append(dict(kind="progress", reason="unverified_ref", id=item["id"], ref=str(item.get("ref"))[:40]))
        else:
            progressed.append(dict(id=item["id"], ref=item["ref"], evidence=fact_text.get(item["ref"], "")))
    new = []
    for item in goals.get("new") or []:
        horizon = item.get("horizon") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if horizon not in horizons:
            dropped.append(dict(kind="new", reason="horizon_out_of_scope", horizon=str(horizon)[:12]))
        elif M.text_reason(text, M.GOAL_TEXT_MAX):
            dropped.append(dict(kind="new", reason=M.text_reason(text, M.GOAL_TEXT_MAX)))
        else:
            when = item.get("done_when") if isinstance(item.get("done_when"), str) else ""
            new.append(dict(horizon=horizon, text=" ".join(text.split()),
                            **({"done_when": " ".join(when.split())[:DONE_WHEN_MAX]} if when.strip() else {})))
    done_ids = {d["id"] for d in done}
    result["goals"] = dict(keep=[i for i in known(goals.get("keep") or [], "keep") if i not in done_ids],
                           done=done, drop=[i for i in known(goals.get("drop") or [], "drop") if i not in done_ids],
                           progress=[p for p in progressed if p["id"] not in done_ids], new=new)
    if obj.get("pref_updates"):  # FIX75C: her likes are the fun / not_fun claims above; this key is not in the schema
        dropped.append(dict(kind="pref_updates", reason="not_in_schema_use_fun_not_fun", n=len(obj["pref_updates"])))
    return result, dropped


def refused_fields(obj: Mapping) -> list[tuple[str, str]]:
    """fix131A: [(field, reason)] for the reply's invalid top-level fields, without touching their words."""
    out = [(field, reason) for field, limit in TEXT_FIELDS
           if field in obj and (reason := field_reason(obj[field], limit, field))]
    out += [(key, "wrong_type:" + key) for key in ("fun", "not_fun") if obj.get(key) and not isinstance(obj[key], list)]
    if obj.get("goals") and not isinstance(obj["goals"], dict):
        out.append(("goals", "wrong_type:goals"))
    return out


def _parse_per_field(raw, **kwargs):
    text = raw.strip() if isinstance(raw, str) else raw
    if isinstance(text, str) and text.startswith("```") and text.endswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        obj = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ReflectionError("not_json") from exc
    if not isinstance(obj, dict):
        raise ReflectionError("not_object")
    refused = refused_fields(obj)
    empty = {"today": "", "tomorrow": "", "fun": [], "not_fun": [], "goals": {}}
    clean = dict(obj, **{field: empty[field] for field, _ in refused})
    result, dropped = parse(json.dumps(clean, ensure_ascii=False), per_field=False, **kwargs)
    return result, [dict(kind="field", field=field, reason=reason) for field, reason in refused] + dropped


def apply(goals: Iterable[Mapping], result: Mapping, *, day: int, game: str = "", calendar=(),
          effective_day: int | None = None, horizons=M.HORIZONS, caps=None) -> tuple[list[dict], list[dict]]:
    """(rows, dropped): goal rows after the reflection on game day ``day``. New today goals are for ``day + 1``.

    fix13c: an open today goal she neither finished nor dropped carries into tomorrow (``carried_days`` + 1) with its
    cited evidence. FIX75C (ADR 133): no carry cap and no TTL on any horizon; a goal ends by her ``done`` with a ref,
    her ``drop``, or the logged safety cap. Caps 3/3/3 open per horizon: a new goal over its cap is refused and
    reported (``kind: new, reason: cap``, with her text, shown to her the next night), never an older one silently."""
    rows = [M.dated_goal(g, game) for g in goals]
    by_id = {g["id"]: g for g in rows}
    dropped = []
    for item in result["goals"]["done"]:
        by_id[item["id"]].update(status="done", done_ref=item["ref"], revised_on_day=day,
                                 **({"done_evidence": item["evidence"]} if item.get("evidence") else {}))
    for goal_id in result["goals"]["drop"]:
        by_id[goal_id].update(status="dropped", revised_on_day=day)
    for item in result["goals"].get("progress", ()):
        row = by_id[item["id"]]
        row["evidence"] = (list(row.get("evidence") or []) + [[day, item["ref"], item.get("evidence", "")]])[-EVIDENCE_KEEP:]
    kept = set(result["goals"]["keep"])
    for row in rows:
        if (row.get("horizon") == "today" and row.get("status") == "open"
                and type(row.get("for_day")) is int and row["for_day"] <= day):  # a missed night still carries
            carry_to = day + 1 if effective_day is None else effective_day
            row.update(for_day=max(row['for_day'], carry_to),
                       carried_days=int(row.get("carried_days") or 0) + int(carry_to > row['for_day']))
        if row["id"] in kept:
            row["revised_on_day"] = day
            row.pop("stale", None)
    tomorrow = day + 1 if effective_day is None else effective_day
    caps = M.GOAL_CAPS if caps is None else caps
    counts = {h: sum(1 for g in rows if M.goal_alive(g, tomorrow) and g["horizon"] == h) for h in horizons}
    n = 0
    for item in result["goals"]["new"]:
        horizon = item["horizon"]
        same = next((g for g in rows if M.goal_alive(g, tomorrow) and g["horizon"] == horizon
                     and g["text"].casefold() == item["text"].casefold()), None)
        if same is not None:  # her carried goal restated: one goal, not a duplicate row
            same["revised_on_day"] = day
            dropped.append(dict(kind="new", reason="same_as_open_goal", id=same["id"]))
            continue
        if counts[horizon] >= caps[horizon]:
            dropped.append(dict(kind="new", reason="cap", horizon=horizon, text=item["text"]))
            continue
        n += 1
        goal_id = "%s%d-%d" % (horizon[0], tomorrow, n)
        while goal_id in by_id:
            n += 1
            goal_id = "%s%d-%d" % (horizon[0], tomorrow, n)
        row = dict(v=M.MIND_VERSION, id=goal_id, horizon=horizon, text=item["text"], status="open",
                   set_on_day=tomorrow, for_day=tomorrow if horizon == "today" else None, why_set="reflection",
                   **({"done_when": item["done_when"]} if item.get("done_when") else {}))
        row = M.plan_dates.ground(row, game=game, authored_day=day, calendar=calendar)
        row["new_index"] = result["goals"]["new"].index(item)
        rows.append(row)
        by_id[goal_id] = row
        counts[horizon] += 1
    events = {e["id"]: e for e in calendar if isinstance(e, dict) and e.get("id")}
    for link in result.get("calendar_links", ()):
        target = next((g for g in rows if g["id"] == link.get("id") or
                       "new:" + str(g.get("new_index", "")) == link.get("id")), None)
        event = events.get(link.get("ref"))
        if target is not None and event is not None:
            named = M.plan_dates.event_references(target["text"], game=game, authored_day=day,
                                                 calendar=calendar, named_only=True)
            if named and not any(M.plan_dates.ordinal(game, d) in named for d in event.get("dates", ())):
                continue
        if target is not None and event is not None:
            target["referenced_days"] = sorted(set(list(target.get("referenced_days", ())) +
                M.plan_dates.linked_days([link], link["id"], game=game, calendar=calendar)))
            target["calendar_refs"] = sorted(set(list(target.get("calendar_refs", ())) + [event["id"]]))
    for g in rows:
        g.pop("new_index", None)
    rows, capped = M.cap_overflow(rows, tomorrow)
    dropped.extend(dict(kind="goal", reason="safety_cap", id=g["id"], horizon=g["horizon"]) for g in capped)
    return rows, dropped


def carry_stale(goals: Iterable[Mapping], *, day: int) -> list[dict]:
    """Failure path: today's open goals carry to ``day + 1`` with ``stale: true``; nothing new is invented."""
    rows = []
    for g in goals:
        g = dict(g)
        if (g.get("status") == "open" and g.get("horizon") == "today"
                and type(g.get("for_day")) is int and g["for_day"] <= day):
            g.update(for_day=day + 1, stale=True)
        rows.append(g)
    return rows


def reflect(complete: Callable[[list, dict], str], base: Path, *, game: str, day: int, day_label: str,
            verified: list[Mapping], whys: list[str], progress: list[Any] = (), places: list[str] = (),
            effects: Mapping | None = None, now: datetime | None = None, snapshot: Mapping | None = None,
            records: Mapping | None = None, calendar=(), native_targets=(), seen_calendar=False) -> dict:
    """Run one reflection and write its effects. Returns the journal row (``mind_reflection`` or
    ``mind_reflection_failed``); never raises and never writes soul or prompt text into the row."""
    base = Path(base)
    goals = [M.dated_goal(g, game, calendar) for g in M.load_goals(base, game)]
    live = M.open_goals(goals, day)
    open_rows = [g for h in M.HORIZONS for g in live[h]]
    traits, _ = M.pick_prefs(M.load_prefs(base), game=game, tags=M.REFLECTION_TAGS)
    expired = [g for g in goals if g.get('horizon') in M.HORIZONS and g.get("status") in ("open", "expired")
               and not M.goal_alive(g, day) and g["set_on_day"] <= day and M.expired_recent(g, day)][-M.GOAL_CAPS["today"]:]
    last_night = next((r for r in reversed(M.load_journey(base, game)) if r.get("day") == day - 1), {})
    payload = build_input(day_label=day_label, verified=verified, whys=whys, goals_open=open_rows,
                          prefs=traits, progress=progress, game=game, day=day, goals_expired=expired, calendar=calendar,
                          native_targets=native_targets, likes=M.likes_lines(base, game, before_day=day),
                          not_kept=(last_night.get("reflection") or {}).get("not_kept") or (), seen_calendar=seen_calendar)
    refs = [v["ref"] for v in payload["verified"]]
    fact_text = {v["ref"]: v["text"] for v in payload["verified"] + payload["progress"]}
    try:
        raw = complete(messages_for(payload), SCHEMA)
        result, dropped = parse(raw, verified_refs=refs, open_goal_ids=[g["id"] for g in open_rows], fact_text=fact_text)
    except Exception as exc:  # noqa: BLE001 - provider or parse failure: carry, alert, invent nothing
        code = str(exc)[:60] if isinstance(exc, ReflectionError) else type(exc).__name__
        try:
            M.save_goals(base, game, carry_stale(goals, day=day))
        except (OSError, ValueError):
            code += "|goals_unwritten"
        return dict(ev="mind_reflection_failed", game=game, day=day, date=day_label, code=code,
                    carried=[g["id"] for g in open_rows], input_bytes=_size(payload), alert=True)
    # Semantic name matching belongs to the existing reflection call. Only a native
    # catalog ref can contribute a date; translated event names need no Python alias table.
    try:
        links = json.loads(raw).get("goals", {}).get("calendar_links", [])
    except (ValueError, AttributeError):
        links = []
    if not isinstance(links, list):
        links = []
    result["calendar_links"] = [l for l in links if isinstance(l, dict)
                                and isinstance(l.get("id"), str) and isinstance(l.get("ref"), str)]
    safe_links = []
    raw_new = json.loads(raw).get("goals", {}).get("new", [])
    if not isinstance(raw_new, list):
        raw_new = []
    for link in result["calendar_links"]:
        if link["id"].startswith("new:"):
            index = link["id"][4:]
            source = raw_new[int(index)] if index.isdigit() and int(index) < len(raw_new) else None
            match = next((i for i, g in enumerate(result["goals"]["new"]) if isinstance(source, dict)
                          and g["horizon"] == source.get("horizon")
                          and g["text"] == " ".join(str(source.get("text", "")).split())), None)
            if match is None:
                continue
            link = dict(link, id="new:" + str(match))
        safe_links.append(link)
    result["calendar_links"] = safe_links
    rows, cap_dropped = apply(goals, result, day=day, game=game, calendar=calendar)
    dropped += cap_dropped
    not_kept = [dict(horizon=d["horizon"], text=d["text"]) for d in cap_dropped if d.get("reason") == "cap" and d.get("text")]
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    journey = dict(v=M.MIND_VERSION, schema=M.JOURNEY_SCHEMA, id=str(day_label), day=day, date=str(day_label),
                   effects=dict(effects or {}), places=list(places)[:12], discoveries=[],
                   unfinished_intents=[g["text"] for g in live["today"]
                                       if g["id"] not in {d["id"] for d in result["goals"]["done"]}],
                   whys_sample=[str(w)[:M.WHY_MAX_CHARS] for w in whys if w][-3:], reflected_at=stamp,
                   **({"progress": dict(snapshot)} if isinstance(snapshot, Mapping) else {}),
                   **({"records": dict(records)} if isinstance(records, Mapping) else {}),
                   reflection=dict(today=result["today"], fun=result["fun"], not_fun=result["not_fun"],
                                   **({"not_kept": not_kept} if not_kept else {}),
                                   today_dates=sorted(set(M.plan_dates.references(result["today"], game=game, authored_day=day, calendar=calendar)
                                       + M.plan_dates.linked_days(result["calendar_links"], "today", game=game, calendar=calendar))),
                                   tomorrow=result["tomorrow"],
                                   tomorrow_dates=sorted(set(M.plan_dates.references(result["tomorrow"], game=game, authored_day=day, calendar=calendar)
                                       + M.plan_dates.linked_days(result["calendar_links"], "tomorrow", game=game, calendar=calendar)))))
    history_trimmed = M.save_goals(base, game, rows)
    M.save_journey_row(base, game, journey)
    try:  # FIX75C: her fun / not_fun claims become dated memory; the goals and journey never wait on this file
        likes = M.save_likes(base, game, day=day, day_label=day_label, fun=result["fun"], not_fun=result["not_fun"],
                             evidence=fact_text)
    except (OSError, ValueError) as exc:
        likes = dict(saved=0, trimmed=0, error=type(exc).__name__)
    new_ids = [g["id"] for g in rows if g.get("set_on_day") == day + 1 and g.get("why_set") == "reflection"]
    tomorrow = day + 1
    alive = [g for g in rows if M.goal_alive(g, tomorrow)]
    carried = [dict(id=g["id"], horizon=g["horizon"], umur_hari=M.goal_age_days(g, tomorrow),
                    **({"carried_days": g["carried_days"]} if g.get("carried_days") else {}),
                    **({"evidence": g["evidence"][-1][2]} if g.get("evidence") else {}))
               for g in alive if M.goal_age_days(g, tomorrow) >= 1]
    closed = [d["id"] for d in result["goals"]["done"] if str(d.get("evidence") or "").strip()]
    return dict(ev="mind_reflection", game=game, day=day, date=day_label, input_bytes=_size(payload),
                verified_refs=len(refs), progress_refs=len(payload["progress"]), carried=carried,
                progressed=result["goals"]["progress"], kept_fun=len(result["fun"]), kept_not_fun=len(result["not_fun"]),
                fun_refs=[f["ref"] for f in result["fun"]], done=result["goals"]["done"],
                kept=result["goals"]["keep"], dropped_goals=result["goals"]["drop"], new_goals=new_ids,
                dropped=dropped, today=result["today"], tomorrow=result["tomorrow"],
                # FIX75C receipt facts: what is alive after this night, how old the oldest is, what she closed or dropped
                alive={h: sum(1 for g in alive if g["horizon"] == h) for h in M.HORIZONS},
                oldest_age_days=max((M.goal_age_days(g, tomorrow) for g in alive), default=None),
                dropped_by_her=len(result["goals"]["drop"]), closed_with_evidence=closed,
                capped=[d["id"] for d in cap_dropped if d.get("reason") == "safety_cap"],
                history_trimmed=history_trimmed, likes=likes)

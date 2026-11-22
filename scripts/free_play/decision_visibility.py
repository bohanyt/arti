"""What ARTI actually saw on the page she decided from, not only what was offered (evaluator 1.5.0, ADR 91).

Report-only. Never a goal_gate, ranking or tagging input. Reads the journal and the run's archived provider requests
(`<journal>/provider_archive/NNN-request.json`, packed into the run's `objects/` store after a verified day and read
back only through `scripts.free_play.storage`). The link between a decision and its real rendered prompt is the
provider request index: the `trial_reservation` (kind provider) row written when the physical call starts is the
archive file number (`arti_stardew_trial_archive.TrialProviderArchive.step`). Nothing is guessed: a decision whose
archived request is missing, truncated or not a decision page is UNKNOWN with its missing_signal.

Definitions are frozen here, as the evaluator freezes every other definition, so a later renderer change cannot
silently move an existing evaluator version. Sources: `arti_stardew_skills.boundary_offer_page` (0-based
`presentation.page`/`pages`/`handle`, `read_boundary` navigation `<handle>:<n>`), `READ_CHANGED` ("dunia berubah"),
`PAGE_DAY`/`MINIMAL_PAGE_FACTS` (ADR 85/88), and `skill_offer` (`skills`/`offered` parallel lists, args cut to 64).
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import statistics

from scripts.free_play import storage

VERSION = "1.5.1"
READ_NAV = "read_boundary"
TRAVEL = "pergi_ke"
STALE_READ_MARK = "dunia berubah"  # arti_stardew_skills.READ_CHANGED (fix62, ADR 89)
OFFER_ARG_CHARS = 64  # skill_offer.offered = str(arg)[:64]
HIDDEN_OFFER_MIN = 10  # highlight: offered on >= 10 decisions and visible on none
# Key facts (ADR 85/86/88): block paths on the decision page. Presence is measured on the page she decided from.
FACTS = {"day": ("day",), "day.bag": ("day", "bag"), "day.tas_kegunaan": ("day", "tas_kegunaan"),
         "day.tas_info": ("day", "tas_info"), "facts.mancing": ("facts", "mancing"),
         "facts.tanpa_efek": ("facts", "tanpa_efek"), "facts.barang_jatuh": ("facts", "barang_jatuh"),
         "quest_lines": ("progress", "quest")}
REQUEST_FILE = re.compile(r"^(\d+)-request\.json$")
DEFINITION = ("per skill_model_decision row: the archived provider request of the last provider reservation before it "
              "(decision-shaped body = user JSON with a skills list); paged = presentation present; visible choices = "
              "(name, arg) pairs in that page's skills (read_boundary excluded) vs the skill_offer pairs; a read-on is "
              "a skill_boundary_page row paired with the next archived request (served = same handle and page); fact "
              "presence = key on the decided page (absence includes not-applicable facts, so rates are floors of "
              "visibility, never counts of hidden facts); prompt bytes = skill_boundary_render_bytes.bytes of the "
              "decided request; menu (unknown_ui) decisions are counted apart; report-only, never a goal_gate, "
              "ranking or tagging input")


def prompt_body(record):
    """The user JSON of an archived provider request, or (None, reason). Codex prompt or api messages (ADR 61)."""
    if not isinstance(record, dict):
        return None, "archived request unreadable"
    content = record.get("content")
    if not isinstance(content, dict):
        return None, "archived request truncated" if record.get("truncated") else "archived request has no content"
    text = None
    prompt = content.get("prompt")
    if isinstance(prompt, str):
        i = prompt.find("\nSchema: ")
        j = prompt.find("\n\n", i) if i >= 0 else -1
        text = prompt[j + 2:] if j >= 0 else None
    elif isinstance(content.get("messages"), list) and len(content["messages"]) >= 2:
        text = (content["messages"][1] or {}).get("content") if isinstance(content["messages"][1], dict) else None
    try:
        body = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        body = None
    return (body, None) if isinstance(body, dict) else (None, "archived request prompt unparsed")


def decision_shaped(body):
    return isinstance(body, dict) and isinstance(body.get("skills"), list)


def pairs(skills):
    """(name, arg) choices on a page or in an offer, read navigation excluded; args cut like skill_offer.offered."""
    result = set()
    for entry in skills or ():
        if not isinstance(entry, dict) or entry.get("name") == READ_NAV or not isinstance(entry.get("name"), str):
            continue
        args = entry.get("args")
        for arg in (args if isinstance(args, list) and args else [None]):
            result.add((entry["name"], None if arg is None else str(arg)[:OFFER_ARG_CHARS]))
    return result


def offered_pairs(offer):
    names, args = offer.get("skills") or [], offer.get("offered") or []
    result = set()
    for i, name in enumerate(names):
        if not isinstance(name, str) or name == READ_NAV:
            continue
        arg = args[i] if i < len(args) else None
        result.add((name, None if arg in (None, "None") else str(arg)[:OFFER_ARG_CHARS]))
    return result


def present(body, path):
    value = body
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return bool(value) if isinstance(value, (list, dict, str)) else value is not None


def archived_requests(evidence, journal_dir):
    """{index: record} for this journal's provider_archive, through the storage layer (raw or packed)."""
    folder = Path(journal_dir) / "provider_archive"
    found = {}
    if not folder.is_dir():
        return found
    try:
        paths = storage.files(folder, "*-request.json")
    except ValueError as exc:
        evidence.warnings.append(f"{evidence.ref(folder)}: {exc}")
        return found
    for path in paths:
        match = REQUEST_FILE.match(path.name)
        if not match:
            continue
        evidence.track(path)
        try:
            found[int(match.group(1))] = json.loads(storage.read_bytes(path).decode("utf-8-sig"))
        except (ValueError, OSError):
            found[int(match.group(1))] = None
    return found


def _chains(rows):
    """Per skill_model_decision row: the provider reservations since the previous decision, each with the latest
    offer and render rows before it, and the read-on rows (skill_boundary_page) in that span."""
    chain, reads, offer, render = [], [], None, None
    for n, row in rows:
        ev = row.get("ev")
        if ev == "skill_offer":
            offer = row
        elif ev == "skill_boundary_render_bytes":
            render = row
        elif ev == "skill_boundary_page":
            reads.append(dict(line=n, handle=row.get("handle"), page=row.get("page")))
        elif ev == "trial_reservation" and row.get("kind") == "provider" and type(row.get("index")) is int:
            chain.append(dict(index=row["index"], line=n, offer=offer, render=render))
        elif ev == "skill_model_decision":
            yield n, row, chain, reads
            chain, reads = [], []


def _unknown(ref, row, signal):
    return dict(ref=ref, trigger=row.get("trigger"), status="UNKNOWN", missing_signal=signal)


def _read_outcome(read, requests):
    """How a read-on came back: the next archived decision-shaped request after the read row."""
    following = [r for r in requests if r["line"] > read["line"]]
    if not following:
        return dict(requested_page=read["page"], returned_page=None, served=None, outcome="unknown",
                    missing_signal="no provider request after the read")
    nxt = following[0]
    if nxt["body"] is None:
        return dict(requested_page=read["page"], returned_page=None, served=None, outcome="unknown",
                    request_index=nxt["index"], missing_signal=nxt["reason"])
    page = nxt["body"].get("presentation") if isinstance(nxt["body"].get("presentation"), dict) else {}
    returned, handle = page.get("page", 0), page.get("handle")
    served = handle == read["handle"] and returned == read["page"]
    stale = STALE_READ_MARK in str(nxt["body"].get("halaman_baca") or "")
    outcome = ("served" if served else "stale_notice" if stale else
               "page0_other_handle" if returned == 0 and read["page"] not in (0, None) else "other_page")
    return dict(requested_page=read["page"], returned_page=returned, served=served, outcome=outcome,
                request_index=nxt["index"], same_handle=handle == read["handle"])


def decision_visibility(evidence, journals):
    per, reasons = [], Counter()
    skill_stats = defaultdict(lambda: dict(offered=0, visible=0, chosen=0))
    place_stats = defaultdict(lambda: dict(offered=0, visible=0, chosen=0))
    facts = {name: 0 for name in FACTS}
    reads_total = Counter()
    bytes_seen, pages_paged, pages_all = [], [], []
    counts = Counter()
    any_archive = any_reservation = False
    for path, rows in journals:
        day = path.parent.parent.name
        archive = archived_requests(evidence, path.parent)
        any_archive |= bool(archive)
        for n, row, chain, reads in _chains(rows):
            ref = f"{day}:J{n}"
            counts["decisions"] += 1
            if row.get("choice") == "invalid":
                counts["invalid_choices"] += 1
            if not chain:
                reasons["no provider reservation before the decision"] += 1
                per.append(_unknown(ref, row, "no provider reservation before the decision"))
                continue
            any_reservation = True
            requests = []
            for item in chain:
                record = archive.get(item["index"]) if item["index"] in archive else None
                if item["index"] not in archive:
                    body, reason = None, f"archived request {item['index']:03d} missing"
                else:
                    body, reason = prompt_body(record)
                if body is not None and not decision_shaped(body):
                    continue  # another lane's call (reflection, background): not a page she decided from
                requests.append(dict(item, body=body, reason=reason))
            decided = requests[-1] if requests else None
            if decided is None or decided["body"] is None:
                signal = decided["reason"] if decided else "no decision request in the archive"
                reasons[signal] += 1
                per.append(_unknown(ref, row, signal))
                continue
            body, offer = decided["body"], decided["offer"] or {}
            if "unknown_ui" in body:
                counts["menu_decisions"] += 1
                per.append(dict(ref=ref, trigger=row.get("trigger"), kind="menu", request_index=decided["index"],
                                status="menu", missing_signal="open menu: its own pager, counted apart"))
                continue
            counts["with_prompt"] += 1
            page = body.get("presentation") if isinstance(body.get("presentation"), dict) else None
            pages = page.get("pages") if page else 1
            shown = page.get("page") if page else 0
            if page:
                counts["paged"] += 1
                counts["paged_without_day"] += not isinstance(body.get("day"), dict)
                if type(pages) is int:
                    pages_paged.append(pages)
            if type(pages) is int:
                pages_all.append(pages)
            visible, offered = pairs(body.get("skills")), offered_pairs(offer)
            choice = row.get("choice")
            chosen = ((choice[0], None if choice[1] is None else str(choice[1])[:OFFER_ARG_CHARS])
                      if isinstance(choice, list) and len(choice) == 2 and isinstance(choice[0], str) else None)
            for name in {p[0] for p in offered}:
                skill_stats[name]["offered"] += 1
                skill_stats[name]["visible"] += any(p[0] == name for p in visible)
            for name in {p[0] for p in visible} - {p[0] for p in offered}:
                skill_stats[name]["visible"] += 1  # on the page but not in the offer row (counted, never hidden)
            if chosen:
                skill_stats[chosen[0]]["chosen"] += 1
            for name, arg in offered | visible:
                if name == TRAVEL and arg is not None:
                    place_stats[arg]["offered"] += (name, arg) in offered
                    place_stats[arg]["visible"] += (name, arg) in visible
            if chosen and chosen[0] == TRAVEL and chosen[1] is not None:
                place_stats[chosen[1]]["chosen"] += 1
            all_visible = bool(offered) and offered <= visible
            counts["all_choices_visible"] += all_visible
            counts["offer_row_missing"] += not offer
            seen = [name for name, keys in FACTS.items() if present(body, keys)]
            for name in seen:
                facts[name] += 1
            size = (decided["render"] or {}).get("bytes")
            if type(size) is int:
                bytes_seen.append(size)
            outcomes = [_read_outcome(read, requests) for read in reads]
            for outcome in outcomes:
                reads_total["attempted"] += 1
                reads_total[outcome["outcome"]] += 1
            counts["decisions_with_reads"] += bool(reads)
            per.append(dict(ref=ref, trigger=row.get("trigger"), kind="skill", status="ok", request_index=decided["index"],
                            paged=bool(page), pages=pages, page=shown, handle=page.get("handle") if page else None,
                            stale_read_notice=STALE_READ_MARK in str(body.get("halaman_baca") or ""),
                            reads=outcomes, offered=len(offered), visible=len(offered & visible),
                            off_page=sorted(f"{n}:{a}" if a is not None else n for n, a in offered - visible)[:16],
                            all_choices_visible=all_visible, chosen=list(chosen) if chosen else choice,
                            prompt_bytes=size, facts_on_page=seen))
    known = counts["with_prompt"]
    if not counts["decisions"]:
        status, signal = "UNKNOWN", "no skill_model_decision rows"
    elif not any_archive:
        status, signal = "UNKNOWN", "no provider_archive request files (raw or objects store)"
    elif not any_reservation:
        status, signal = "UNKNOWN", "no provider reservation rows: decisions cannot be linked to archived requests"
    elif not known:
        detail = "; ".join(f"{k} x{n}" for k, n in sorted(reasons.items())) or f"menu decisions only ({counts['menu_decisions']})"
        status, signal = "UNKNOWN", "no skill decision with a readable archived request: " + detail
    else:
        status, signal = "MEASURED", None
    pct = lambda part, whole: round(100 * part / whole, 1) if whole else None  # noqa: E731
    hidden = sorted([dict(kind="destination", name=k, **v) for k, v in place_stats.items()
                     if v["offered"] >= HIDDEN_OFFER_MIN and v["visible"] == 0]
                    + [dict(kind="skill", name=k, **v) for k, v in skill_stats.items()
                       if v["offered"] >= HIDDEN_OFFER_MIN and v["visible"] == 0],
                    key=lambda h: (-h["offered"], h["kind"], h["name"]))
    return dict(
        version=VERSION, report_only=True, status=status, missing_signal=signal,
        coverage=dict(decisions=counts["decisions"], skill_decisions_with_prompt=known,
                      menu_decisions=counts["menu_decisions"], invalid_choices=counts["invalid_choices"],
                      unknown=sum(reasons.values()), unknown_reasons=dict(sorted(reasons.items())),
                      offer_row_missing=counts["offer_row_missing"]),
        summary=dict(pct_decisions_paged=pct(counts["paged"], known), paged=counts["paged"],
                     median_pages_paged=_median(pages_paged),
                     median_pages_all=_median(pages_all),
                     pct_all_choices_visible=pct(counts["all_choices_visible"], known),
                     all_choices_visible=counts["all_choices_visible"],
                     median_prompt_bytes=_median(bytes_seen),
                     prompt_bytes_basis="skill_boundary_render_bytes.bytes of the decided request (encoded provider request)",
                     paged_without_day=counts["paged_without_day"], decisions_with_reads=counts["decisions_with_reads"]),
        reads=dict(attempted=reads_total["attempted"], served=reads_total["served"],
                   page0_other_handle=reads_total["page0_other_handle"], stale_notice=reads_total["stale_notice"],
                   other_page=reads_total["other_page"], unknown=reads_total["unknown"],
                   definition="page0_other_handle = a read of page N>0 answered by page 0 under another handle "
                              "(pre-fix62 fault); stale_notice = a fresh decision page saying the world changed (ADR 89)"),
        skills={k: skill_stats[k] for k in sorted(skill_stats)},
        destinations={k: place_stats[k] for k in sorted(place_stats)},
        hidden_offers=hidden, hidden_offer_rule=f"offered on >= {HIDDEN_OFFER_MIN} decisions, visible on 0",
        facts={name: dict(on_page=facts[name], decisions=known, rate=pct(facts[name], known) if known else None)
               for name in FACTS},
        per_decision=per, definition=DEFINITION)


def _median(values):
    """Median as an int when it is whole (a page count), else a float."""
    if not values:
        return None
    m = statistics.median(values)
    return int(m) if float(m).is_integer() else m


def _n(value):
    return "unknown" if value is None else value


def visibility_markdown(v):
    """EVALUATION.md section. Report-only; never a verdict."""
    if not v:
        return []
    c, s, r = v["coverage"], v["summary"], v["reads"]
    lines = ["", "## Decision visibility (report-only)", "",
             f"**{v['status']}**" + (f": {v['missing_signal']}." if v["missing_signal"] else ".")
             + f" Skill decisions with their real page {c['skill_decisions_with_prompt']}/{c['decisions']} "
             f"(menu {c['menu_decisions']}, unknown {c['unknown']}, invalid choices {c['invalid_choices']}).",
             f"Paged {_n(s['pct_decisions_paged'])}% ({s['paged']}); median pages (paged / all) "
             f"{_n(s['median_pages_paged'])} / {_n(s['median_pages_all'])}; all choices visible {_n(s['pct_all_choices_visible'])}% "
             f"({s['all_choices_visible']}); median prompt bytes {_n(s['median_prompt_bytes'])}; paged pages without `day` "
             f"{s['paged_without_day']}.",
             f"Read-ons {r['attempted']}: served {r['served']}, page 0 under another handle {r['page0_other_handle']}, "
             f"stale notice {r['stale_notice']}, other page {r['other_page']}, unknown {r['unknown']}. {r['definition']}."]
    if v["hidden_offers"]:
        lines += ["", f"Offered but never on her page ({v['hidden_offer_rule']}):", "",
                  "| Kind | Name | Offered | Visible | Chosen |", "|---|---|---:|---:|---:|"]
        lines += [f"| {h['kind']} | {h['name']} | {h['offered']} | {h['visible']} | {h['chosen']} |" for h in v["hidden_offers"]]
    else:
        lines += ["", f"Offered but never on her page ({v['hidden_offer_rule']}): none."]
    lines += ["", "| Fact on the decided page | On page | Decisions | Rate % |", "|---|---:|---:|---:|"]
    lines += [f"| {name} | {f['on_page']} | {f['decisions']} | {_n(f['rate'])} |" for name, f in v["facts"].items()]
    lines += ["", f"Per destination/skill offered, visible and chosen, and each decision's page and reads, are in "
                  f"EVALUATION.json `decision_visibility`. {v['definition']}."]
    return lines


def scoreboard_line(e):
    """One scoreboard line per run (separate section; no existing column changes)."""
    v = e.get("decision_visibility")
    if not v:
        return f"- {e['run']}: unknown (evaluation predates decision visibility)."
    c, s, r = v["coverage"], v["summary"], v["reads"]
    hidden = ", ".join(f"{h['name']} {h['offered']}/0" for h in v["hidden_offers"][:6]) or "none"
    more = f" (+{len(v['hidden_offers']) - 6} more)" if len(v["hidden_offers"]) > 6 else ""
    if v["status"] != "MEASURED":
        return f"- {e['run']}: {v['status']} ({v['missing_signal']}); decisions {c['decisions']}."
    return (f"- {e['run']}: decisions {c['skill_decisions_with_prompt']}/{c['decisions']} with their page; paged "
            f"{_n(s['pct_decisions_paged'])}% (median {_n(s['median_pages_paged'])} pages); all choices visible "
            f"{_n(s['pct_all_choices_visible'])}%; median prompt {_n(s['median_prompt_bytes'])} B; reads {r['attempted']} "
            f"(served {r['served']}, page 0 under another handle {r['page0_other_handle']}); `day` on page "
            f"{_n(v['facts']['day']['rate'])}%; offered >= {HIDDEN_OFFER_MIN}x, never visible: {hidden}{more}.")

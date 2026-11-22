"""Mabar → her long-term memory: verified facts only, as a vault session note and Discord diary drafts.

`build(run, mind)` reads ONLY verified sources from a finished mabar run and writes two artifacts into the run folder:
  vault-export/<session>.md     vault session note (live ARTI indexes vault/**/*.md on her next start)
  diary-export.jsonl            DIARY_FORMAT v1 lines, status "draft" (Discord reads only entries the owner marks ok)
Sources: the HOST-witnessed story rows (relay, native lines), her own look read back from the native profile, her
first-seen discoveries (people met, items obtained, places entered), letters and quests in her mind ledger, and the
count of owner speech lines. No model writes anything here; no viewer names; nothing from the soul or the live vault.

`export_to_live(run, stream_root)` (owner OK only): creates vault/sessions/<session>.md only if it does not exist and
appends the diary drafts whose ids are not in the file yet. It touches nothing else.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

WIB = dt.timezone(dt.timedelta(hours=7))
KEEP_DISCOVERIES = ("npc_met", "item_obtained", "location_entered")
# Her status line speaks to her ("Namamu"); her own memory speaks as her ("Namaku").
FIRST_PERSON = (("Namamu", "Namaku"), ("Gendermu", "Genderku"), ("Hal favoritmu", "Hal favoritku"),
                ("Rambutmu", "Rambutku"), ("Bajumu", "Bajuku"), ("Bawahanmu", "Bawahanku"), ("Matamu", "Mataku"),
                ("Karaktermu", "Karakterku"))


def first_person(text):
    for second, first in FIRST_PERSON:
        text = text.replace(second, first)
    return text
LINES_PER_EPISODE = 6
_CODE = re.compile(r"\s*\((?:kode[^)]*)\)")


def made_farmer(look):
    """Her farmer creation as something that HAPPENED that evening, in her words (owner 2026-11-11: the old
    "[recorded remark omitted]" list read as a character sheet and she later announced it as "lore"). Facts only
    from the verified native readback lines; colour codes dropped; no trait vocabulary."""
    if not look or not look.get("verified"):
        return ""
    parts, name, fav, farm = [], None, None, None
    for raw in (str(l.get("text") or "") for l in look.get("lines") or []):
        text = _CODE.sub("", raw).strip().rstrip(".")
        m = re.match(r"Namamu (.+)", text)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"Hal favoritmu: (.+)", text)
        if m:
            fav = m.group(1)
            continue
        m = re.match(r"Farm ini bernama (.+)", text)
        if m:
            farm = m.group(1)
            continue
        for second, plain in (("Rambutmu ", "rambut "), ("Bajumu ", "baju "), ("Bawahanmu ", "bawahan "),
                              ("Matamu ", "mata ")):
            if text.startswith(second):
                parts.append(plain + text[len(second):].replace(", warna ", " ").replace(", model rambut nomor ", " model "))
                break
        else:
            if text.startswith("Tanpa aksesori"):
                parts.append("tanpa aksesori")
    if not (name or parts):
        return ""
    where = " di %s" % farm if farm else ""
    sentence = "Waktu masuk%s, aku bikin farmer-ku sendiri di game" % where
    if name:
        sentence += ", namanya %s" % name
    if parts:
        sentence += ": " + ", ".join(parts)
    if fav:
        sentence += "; di kolom hal favorit aku tulis %s" % fav
    return sentence + "."


def _jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def session_id(run):
    """`<YYYY-MM-DD>-mabar`, the WIB date the run started (a session crossing midnight keeps its start date)."""
    mode = Path(run) / "MODE.json"
    stamp = json.loads(mode.read_text(encoding="utf-8")).get("utc") if mode.is_file() else None
    try:
        start = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        start = dt.datetime.now(dt.timezone.utc)
    return start.astimezone(WIB).strftime("%Y-%m-%d") + "-mabar"


def gather(run, mind, scope_hashes=None):
    """Verified facts of one run, grouped; every item keeps its source."""
    stardew = Path(mind) / "stardew"
    ledger = _jsonl(stardew / "ledger.jsonl")
    story = [r for r in ledger if r.get("kind") == "native_story"]
    opening = [r["facts"] for r in story if (r.get("facts") or {}).get("witness") == "host"]
    looks = [r["facts"] for r in ledger if r.get("kind") == "own_look"]
    look = next((f for f in reversed(looks) if f.get("verified")), looks[-1] if looks else None)
    letters = [r for r in ledger if r.get("kind") == "letter"]
    quests = [r for r in ledger if r.get("kind") == "quest"]
    days, kept = {}, []
    for r in _jsonl(stardew / "discoveries.jsonl"):
        # The mind is shared by every mabar run; only this farm's rows belong in this note (11-10 mixed farms).
        if scope_hashes and r.get("scope_hash") not in scope_hashes:
            continue
        if r.get("kind") in KEEP_DISCOVERIES and r.get("text"):
            text = r.get("place_name") if r.get("kind") == "location_entered" and r.get("place_name") else r["text"]
            days.setdefault(r.get("day_label") or r.get("date") or "?", []).append((r["kind"], str(text)[:120]))
            kept.append({k: r[k] for k in ("kind", "text", "place_name", "day_label", "scope_hash") if r.get(k)})
    owner_lines = len(_jsonl(Path(run) / "owner_reactions.jsonl"))
    cont_path = Path(run) / "continued.json"
    try:
        continued = json.loads(cont_path.read_text(encoding="utf-8")) if cont_path.is_file() else None
    except ValueError:
        continued = None
    return dict(opening=opening, look=look, letters=len(letters), quests=len(quests), days=days,
                owner_lines=owner_lines, continued=continued, discoveries=kept)


def _title(r):
    return (r.get("title") or (r.get("facts") or {}).get("title") or r.get("item_id") or "").strip()


def note(session, facts, *, generated_at):
    out = ["# Mabar Session: %s" % session, "",
           "Main Stardew Valley bareng Bohan (owner jadi HOST, aku farmhand), " + (
               "lanjutan farm %s Farm (malam main bareng ke-%s)." % (facts["continued"]["farm"], facts["continued"]["evening"])
               if facts.get("continued") else "farm baru dari awal."),
           "Sumber: catatan terverifikasi run mabar (relay HOST, profil native, penemuan pertama). Dibuat %s." % generated_at,
           ""]
    if facts["opening"]:
        out += ["## Pembukaan (aku nonton bareng owner di layar HOST, aku belum ada di adegan itu)", ""]
        for ep in facts["opening"]:
            out.append("- %s %s di %s (tanggal native %s):" % (ep.get("event_type", "?"), ep.get("event_id", "?"),
                                                              ep.get("location", "?"), ep.get("native_date") or ep.get("date")))
            for line in (ep.get("lines") or [])[:LINES_PER_EPISODE]:
                who = line.get("speaker") if line.get("speaker") not in (None, "", "UNKNOWN") else "tanpa nama pembicara"
                out.append('  - %s: "%s"' % (who, line.get("text", "")))
        out.append("")
    # A continued evening did not make the farmer; only the evening that did gets the moment.
    if not facts.get("continued"):
        made = made_farmer(facts["look"])
        out += ["## Waktu aku bikin farmer (dibaca dari profil game setelah OK)", "",
                "- " + (made or "Belum terverifikasi dari profil game; rinciannya belum diketahui."), ""]
    if facts["days"]:
        out += ["## Hari-hari pertama (penemuan pertama yang tercatat)", ""]
        for day in sorted(facts["days"], key=lambda d: (len(d), d)):
            items = facts["days"][day]
            met = [t.split(":")[0] for k, t in items if k == "npc_met"]
            got = [t.split(":")[0] for k, t in items if k == "item_obtained"]
            went = [t for k, t in items if k == "location_entered"]
            parts = []
            if met:
                parts.append("kenal (catatan pertemanan native): " + ", ".join(met))
            if got:
                parts.append("dapat: " + ", ".join(got))
            if went:
                parts.append("masuk: " + ", ".join(went))
            out.append("- %s: %s" % (day, "; ".join(parts) if parts else "tidak ada penemuan baru tercatat"))
        out.append("")
    out += ["## Catatan", "", "- Surat tercatat: %d. Quest tercatat: %d. Owner ngomong ke aku: %d kali." % (
        facts["letters"], facts["quests"], facts["owner_lines"]), ""]
    return "\n".join(out)


def diary(session, facts, *, generated_at):
    """DIARY_FORMAT v1 drafts: factual first-person sentences from the same verified facts."""
    rows = []

    def add(kind, text):
        rows.append(dict(v=1, id="%s-%02d" % (session, len(rows)), session=session, generated_at=generated_at,
                         at=None, game="Stardew Valley", kind=kind, status="draft", text=text[:400 if kind == "summary" else 280],
                         mood=None, names=[]))

    met = sorted({t.split(":")[0] for items in facts["days"].values() for k, t in items if k == "npc_met"})
    if facts.get("continued"):
        add("summary", "Aku lanjut main Stardew Valley bareng Bohan di %s Farm: dia HOST, aku pemain kedua"
            % facts["continued"]["farm"] + (", dan tercatat kenal sama " + ", ".join(met) if met else "") + ".")
    else:
        add("summary", "Aku mulai main Stardew Valley bareng Bohan di farm baru: dia HOST, aku pemain kedua. "
            "Aku bikin farmer-ku sendiri di game" + (" dan tercatat kenal sama " + ", ".join(met) if met else "") + ".")
    if facts["opening"]:
        quote = next((l.get("text") for ep in facts["opening"] for l in (ep.get("lines") or []) if l.get("text")), "")
        add("moment", "Aku nonton pembukaannya bareng Bohan dari layarnya" + (', ada kalimat "%s".' % quote[:120] if quote else "."))
    made = "" if facts.get("continued") else made_farmer(facts["look"])
    if made:
        add("moment", made)
    return rows


def build(run, mind, *, now=None, scope_hashes=None, farm_tag=None):
    """``farm_tag``: the note's farm tag line (ADR 238, ``arti_runtime.vault_farm.tag_line``); None = no tag."""
    run, mind = Path(run), Path(mind)
    generated_at = (now or dt.datetime.now(WIB)).isoformat(timespec="seconds")
    session = session_id(run)
    facts = gather(run, mind, scope_hashes)
    out = run / "vault-export"
    out.mkdir(exist_ok=True)
    md = out / ("%s.md" % session)
    text = note(session, facts, generated_at=generated_at)
    if farm_tag:
        from arti_runtime import vault_farm
        text = vault_farm.add_tag(text, farm_tag)
    md.write_text(text, encoding="utf-8")
    # This farm's discovery rows for ARTI-DiaryAuto, which reads the run folder and never opens the mind dir.
    disc = run / "discoveries.jsonl"
    disc.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in facts["discoveries"]), encoding="utf-8")
    jl = run / "diary-export.jsonl"
    jl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in diary(session, facts, generated_at=generated_at)),
                  encoding="utf-8")
    return dict(session=session, note=str(md), note_sha256=_sha(md), diary=str(jl), diary_sha256=_sha(jl),
                opening_episodes=len(facts["opening"]), look_verified=bool(facts["look"] and facts["look"].get("verified")),
                days=len(facts["days"]), owner_lines=facts["owner_lines"])


def export_to_live(run, stream_root, *, diary=True):
    """Create the vault note only if absent; append diary drafts with new ids (diary=False: the note only; the
    Discord side is ARTI-DiaryAuto's). Returns what was written."""
    run, stream_root = Path(run), Path(stream_root)
    notes = sorted((run / "vault-export").glob("*.md"))
    if len(notes) != 1:
        raise RuntimeError("run `remember` first (exactly one vault note expected)")
    target = stream_root / "vault" / "sessions" / notes[0].name
    if not (stream_root / "vault" / "sessions").is_dir():
        raise RuntimeError("live vault/sessions folder not found")
    result = dict(note_target=str(target))
    if target.exists():
        result["note"] = "exists, untouched"
    else:
        with open(target, "x", encoding="utf-8") as f:
            f.write(notes[0].read_text(encoding="utf-8"))
        result.update(note="created", note_sha256=_sha(target))
    if not diary:
        return result
    diary_path = stream_root / "data" / "diary" / "arti_diary.jsonl"
    have = {r.get("id") for r in _jsonl(diary_path)}
    fresh = [r for r in _jsonl(run / "diary-export.jsonl") if r.get("id") not in have and r.get("status") == "draft"]
    if fresh:
        diary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(diary_path, "a", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in fresh)
    result.update(diary_appended=len(fresh), diary_sha256=_sha(diary_path) if diary_path.is_file() else None)
    return result

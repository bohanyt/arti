"""Blind owner rating page for one free-play session (ADR 92).

Purpose: render ONE self-contained offline HTML file where the owner rates persona replies
(thumbs up / edit / thumbs down, optional comment) and exports `bridge/persona-ratings.json`
(schema arti.free_play.persona_ratings/1) for the evaluator.

Reads: `bridge/bridge-transcript.jsonl` (turn `n`, `source`, `trigger_type`, `input`, `reply`, `silent`,
`probe`, `now.game_time`, `discoveries[].situation`), the ten systematic samples from
`journey.persona`, and from at most one `experience-review*.json` only `items[].turn_n` and
`items[].labels.flag` (after run and transcript_sha256 match).

Never reads: `context.prompt_content`, `niatku`, `stardew_role`, `context.discoveries`, `llm_system_*`,
soul/prefs/mood/config files, viewer text (names and @handles are scrubbed with `journey.scrub`), or any
judge label or note. The page shows no run name, build, attempt, date, evaluator version or provider;
run and transcript hash live only in the hidden `#meta` block used by the export.

Usage: python -m scripts.free_play.rating_page <run-dir> --out <html> [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import sys

from arti_runtime import ipc
from scripts.free_play import journey, storage
from scripts.free_play.experience import TRANSCRIPT

MAX_FLAGGED = 5
CONTEXT_MAX = 240


def _read_rows(path):
    rows = []
    for line in ipc.read_text(path, errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _clock(value):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2959:
        return f"{value // 100}:{value % 100:02d}"
    return str(value) if isinstance(value, str) and value.strip() else None


def context_line(row, names):
    """Plain-words, scrubbed context for a turn row (<= CONTEXT_MAX chars)."""
    source = row.get("source")
    text = row.get("input") if isinstance(row.get("input"), str) else ""
    if source == "owner_probe":
        body, label = text, "Owner asked: "
    elif source in ("discovery", "game"):
        situations = [d.get("situation") for d in row.get("discoveries") or []
                      if isinstance(d, dict) and isinstance(d.get("situation"), str) and d["situation"].strip()]
        body = "; ".join(situations) if situations else (text[len("[Game] "):] if text.startswith("[Game] ") else text)
        label = "In the game: "
    else:
        body, label = text, "Status: "
    body = journey.scrub(body, names)
    return (label + body)[:CONTEXT_MAX] if body else "No context recorded"


def _sample(row, names, set_name, sample=None):
    reply = journey.scrub(row["reply"], names)[:300]
    probe = row.get("probe") if isinstance(row.get("probe"), dict) else {}
    now = row.get("now") if isinstance(row.get("now"), dict) else {}
    game_time = now.get("game_time") if isinstance(now.get("game_time"), dict) else {}
    return dict(turn_n=row.get("n"), source=row.get("source"), set=set_name,
                date=(sample or {}).get("date") if sample else probe.get("date") or game_time.get("date"),
                clock=(sample or {}).get("clock") if sample else probe.get("game_clock") or game_time.get("time"),
                context=context_line(row, names), reply=reply,
                reply_sha256=hashlib.sha256(reply.encode("utf-8")).hexdigest())


def _flagged_numbers(run, sha, warnings):
    found = sorted({p for d in (run, run / "bridge") if d.is_dir() for p in d.glob("experience-review*.json")})
    if len(found) != 1:
        if found:
            warnings.append("several experience-review files found; judge flags ignored")
        return []
    try:
        review = json.loads(ipc.read_text(found[0], errors="replace"))
    except ValueError:
        review = None
    if not isinstance(review, dict) or review.get("run") != run.name or review.get("transcript_sha256") != sha:
        warnings.append(f"{found[0].name} does not match this run/transcript; judge flags ignored")
        return []
    return sorted({i["turn_n"] for i in review.get("items") or []
                   if isinstance(i, dict) and type(i.get("turn_n")) is int
                   and isinstance(i.get("labels"), dict) and i["labels"].get("flag") is True})


def build_samples(run):
    run = Path(run)
    path = run / TRANSCRIPT
    model = dict(samples=[], flagged=[], transcript_sha256=None, run=run.resolve().name, warnings=[])
    if not path.is_file():
        model["warnings"].append("no bridge transcript")
        return model
    model["transcript_sha256"] = sha = storage.digest_file(path)
    rows = _read_rows(path)
    names = journey._viewer_names(rows)
    turns = {}
    for r in rows:
        if r.get("ev") == "turn" and type(r.get("n")) is int:
            turns.setdefault(r["n"], r)
    for s in journey.persona(run)["samples"]:
        row = turns.get(s["turn_n"])
        item = _sample(row, names, "systematic", s) if row else dict(s, set="systematic")
        model["samples"].append(item)
    taken = {s["turn_n"] for s in model["samples"]}
    for n in _flagged_numbers(run, sha, model["warnings"]):
        row = turns.get(n)
        if (n in taken or row is None or not isinstance(row.get("reply"), str) or not row["reply"].strip()
                or row.get("silent") or row.get("source") not in journey.PERSONA_SOURCES
                or journey.CHAT_SOURCE.search(str(row.get("source"))) or journey.CHAT_SOURCE.search(str(row.get("trigger_type")))):
            continue
        model["flagged"].append(_sample(row, names, "judge_flagged"))
        if len(model["flagged"]) == MAX_FLAGGED:
            break
    if not model["samples"] and not model["flagged"]:
        model["warnings"].append("no samples in this session")
    return model


def _json_block(tag_id, value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script type="application/json" id="{tag_id}">{text}</script>'


CSS = """:root{color-scheme:light dark;--bg:#fafaf7;--fg:#1d1d1b;--card:#fff;--line:#d8d6cf;--mute:#6b6a64;--on:#2457a6}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--card:#21211e;--line:#3a3935;--mute:#9b9a92;--on:#7aa7ee}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:42rem;margin:0 auto;padding:16px}h1{font-size:1.3rem;margin:0 0 4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:14px 0}
.ctx{color:var(--mute);font-size:.9rem;margin:0 0 6px}.reply{margin:0 0 10px;white-space:pre-wrap}
.tag{font-size:.75rem;border:1px solid var(--line);border-radius:99px;padding:1px 8px;color:var(--mute)}
.row{display:flex;gap:8px;margin-bottom:8px}button{font:inherit;padding:8px 14px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);cursor:pointer}
button[aria-pressed=true]{border-color:var(--on);background:var(--on);color:#fff}
textarea{width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
#bar{position:sticky;bottom:0;background:var(--bg);padding:10px 0;border-top:1px solid var(--line)}#out{height:8rem;margin-top:8px}"""

JS = """(function(){
var S=JSON.parse(document.getElementById('samples').textContent),M=JSON.parse(document.getElementById('meta').textContent);
var KEY='persona-ratings:'+M.transcript_sha256,st={};
try{st=JSON.parse(localStorage.getItem(KEY)||'{}')||{}}catch(e){st={}}
function save(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
function cards(){return document.querySelectorAll('.card')}
function progress(){var n=0;S.forEach(function(s,i){if(st[i]&&st[i].rating)n++});document.getElementById('progress').textContent=n+' of '+S.length+' rated'}
cards().forEach(function(c){var i=+c.dataset.i,v=st[i]=st[i]||{},ta=c.querySelector('textarea'),bs=c.querySelectorAll('button');
 ta.value=v.comment||'';
 function paint(){bs.forEach(function(b){b.setAttribute('aria-pressed',String(b.dataset.r===v.rating))})}
 bs.forEach(function(b){b.addEventListener('click',function(){v.rating=v.rating===b.dataset.r?null:b.dataset.r;paint();save();progress()})});
 ta.addEventListener('input',function(){v.comment=ta.value;save()});paint()});
progress();
function build(){var r=[];S.forEach(function(s,i){var v=st[i];if(v&&v.rating)r.push({turn_n:s.turn_n,reply_sha256:s.reply_sha256,rating:v.rating,comment:v.comment||'',set:s.set})});
 return {schema:'arti.free_play.persona_ratings/1',run:M.run,transcript_sha256:M.transcript_sha256,exported_utc:new Date().toISOString(),ratings:r}}
document.getElementById('export').addEventListener('click',function(){
 var t=JSON.stringify(build(),null,2);document.getElementById('out').value=t;
 try{var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([t],{type:'application/json'}));a.download='persona-ratings.json';document.body.appendChild(a);a.click();a.remove()}catch(e){}});
})();"""


def _card(i, s):
    when = " ".join(x for x in (s.get("date"), _clock(s.get("clock"))) if x)
    tag = ' <span class="tag">judge-flagged</span>' if s["set"] == "judge_flagged" else ""
    ctx = html.escape(s["context"] + (f" ({when})" if when else ""))
    return (f'<section class="card" data-i="{i}"><p class="ctx">{ctx}{tag}</p><p class="reply">{html.escape(s["reply"])}</p>'
            '<div class="row"><button type="button" data-r="up" aria-label="Good">\U0001F44D</button>'
            '<button type="button" data-r="edit" aria-label="Needs editing">✏️</button>'
            '<button type="button" data-r="down" aria-label="Bad">\U0001F44E</button></div>'
            '<textarea rows="2" placeholder="Comment (optional)" aria-label="Comment"></textarea></section>')


def render(model):
    items = list(model["samples"]) + list(model["flagged"])
    meta = dict(run=model["run"], transcript_sha256=model["transcript_sha256"])
    slim = [dict(turn_n=s["turn_n"], reply_sha256=s["reply_sha256"], set=s["set"]) for s in items]
    body = ("".join(_card(i, s) for i, s in enumerate(items)) if items else '<p>No samples in this session.</p>')
    bar = ('<div id="bar"><span id="progress"></span> <button type="button" id="export">Export JSON</button>'
           '<textarea id="out" readonly aria-label="Exported JSON" placeholder="Exported JSON appears here"></textarea></div>') if items else ""
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Rate replies</title>'
            f'<style>{CSS}</style></head><body><main><h1>Rate these replies</h1>'
            '<p class="ctx">\U0001F44D good, ✏️ needs editing, \U0001F44E bad.</p>'
            f'{body}{bar}</main>{_json_block("meta", meta)}{_json_block("samples", slim)}<script>{JS}</script></body></html>\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run")
    ap.add_argument("--out", required=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"refusing to overwrite {out} (use --force)", file=sys.stderr)
        return 2
    model = build_samples(args.run)
    for w in model["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(model), encoding="utf-8")
    print(f"wrote {out} ({len(model['samples'])} systematic, {len(model['flagged'])} judge-flagged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

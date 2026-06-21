"""Lightweight HTML dashboard for Arti API telemetry (per model / provider / subsystem)."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import arti_api_telemetry as tel

_ROOT = Path(__file__).resolve().parent
_AA_MODELS_URL = "https://artificialanalysis.ai/models"


def _benchmarks() -> dict[str, Any]:
    path = _ROOT / "data" / "model_benchmarks.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("models") or {}
    except Exception:
        return {}


def _bench_for_model(model: str) -> dict[str, Any]:
    b = _benchmarks()
    if model in b:
        return b[model]
    short = model.split("/")[-1] if "/" in model else model
    return b.get(short) or {}


def _aa_link(model: str) -> str:
    bench = _bench_for_model(model)
    q = bench.get("aa_query") or model.split("/")[-1]
    return f"{_AA_MODELS_URL}?q={q}" if q else _AA_MODELS_URL


def _recompute_ref(event: dict[str, Any]) -> float:
    if event.get("reference_cost_usd"):
        return float(event["reference_cost_usd"])
    usage = tel.UsageInfo(
        prompt_tokens=int(event.get("prompt_tokens") or 0),
        completion_tokens=int(event.get("completion_tokens") or 0),
        total_tokens=int(event.get("total_tokens") or 0),
    )
    bench = _bench_for_model(str(event.get("model") or ""))
    entry = tel._merged_price_entry(
        str(event.get("provider") or ""),
        str(event.get("model") or ""),
        tel.DEFAULT_CONFIG,
    )
    ref, _ = tel.reference_cost_usd(usage, entry)
    return ref


def _extra_note(extra: dict[str, Any] | None) -> str:
    if not extra:
        return ""
    parts: list[str] = []
    if extra.get("audio_sec") is not None:
        parts.append(f"{extra['audio_sec']}s audio")
    if extra.get("batch_size"):
        parts.append(f"batch×{extra['batch_size']}")
    if extra.get("purpose"):
        parts.append(str(extra["purpose"]))
    if extra.get("lane"):
        parts.append(str(extra["lane"]))
    if extra.get("backend"):
        parts.append(str(extra["backend"]))
    return ", ".join(parts)


def load_dashboard_data(config: dict | None = None) -> dict[str, Any]:
    cfg = {**tel.DEFAULT_CONFIG, **(config or {})}
    tdir = tel._telemetry_dir(cfg)
    sessions: list[str] = []
    events: list[dict[str, Any]] = []
    for path in sorted(tdir.glob("*.jsonl")):
        sessions.append(path.stem)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    by_model: dict[str, dict[str, Any]] = {}
    by_provider: dict[str, dict[str, Any]] = defaultdict(lambda: _empty_agg())
    by_subsystem: dict[str, dict[str, Any]] = defaultdict(lambda: _empty_agg())

    for e in events:
        model = str(e.get("model") or "unknown")
        provider = str(e.get("provider") or "unknown")
        subsystem = str(e.get("subsystem") or "unknown")
        key = f"{subsystem}|{provider}|{model}"
        bench = _bench_for_model(model)
        ref = _recompute_ref(e)
        tokens = int(e.get("total_tokens") or 0)
        lat = int(e.get("latency_ms") or 0)

        row = by_model.setdefault(
            key,
            {
                "subsystem": subsystem,
                "provider": provider,
                "model": model,
                "vendor": bench.get("vendor", provider),
                "display": bench.get("display", model),
                "weight_tier": bench.get("weight_tier", "?"),
                "calls": 0,
                "tokens": 0,
                "ref_usd": 0.0,
                "actual_usd": 0.0,
                "latency_sum": 0,
                "fail": 0,
                "aa_url": _aa_link(model),
                "info_samples": [],
            },
        )
        row["calls"] += 1
        if e.get("ok", True):
            pass
        else:
            row["fail"] += 1
        note = _extra_note(e.get("extra") if isinstance(e.get("extra"), dict) else None)
        if note and note not in row["info_samples"] and len(row["info_samples"]) < 2:
            row["info_samples"].append(note)
        row["tokens"] += tokens
        row["ref_usd"] += ref
        row["actual_usd"] += float(e.get("cost_usd") or 0)
        row["latency_sum"] += lat

        for bucket, name in ((by_provider, provider), (by_subsystem, subsystem)):
            b = bucket[name]
            b["calls"] += 1
            if not e.get("ok", True):
                b["fail"] = b.get("fail", 0) + 1
            b["tokens"] += tokens
            b["ref_usd"] += ref
            b["actual_usd"] += float(e.get("cost_usd") or 0)
            b["latency_sum"] += lat

    models = list(by_model.values())
    for m in models:
        m["ref_per_1m"] = (m["ref_usd"] / m["tokens"] * 1_000_000) if m["tokens"] else 0
        m["avg_latency_ms"] = int(m["latency_sum"] / m["calls"]) if m["calls"] else 0
        m["info"] = "; ".join(m.get("info_samples") or [])
    models.sort(key=lambda x: -x["ref_usd"])

    provider_detail: list[dict[str, Any]] = []
    by_prov_models: dict[str, list[dict]] = defaultdict(list)
    for m in models:
        by_prov_models[m["provider"]].append(m)
    for pname, mlist in by_prov_models.items():
        mlist.sort(key=lambda x: -x["calls"])
        provider_detail.append(
            {
                "name": pname,
                "calls": sum(x["calls"] for x in mlist),
                "tokens": sum(x["tokens"] for x in mlist),
                "ref_usd": sum(x["ref_usd"] for x in mlist),
                "models": mlist,
            }
        )
    provider_detail.sort(key=lambda x: -x["ref_usd"])

    groq_rows = [m for m in models if m["provider"] == "groq"]
    local_rows = [m for m in models if m["provider"] in ("local", "lmstudio")]

    def _finalize(groups: dict[str, dict]) -> list[dict]:
        out = []
        for name, g in groups.items():
            out.append(
                {
                    "name": name,
                    "calls": g["calls"],
                    "fail": g.get("fail", 0),
                    "tokens": g["tokens"],
                    "ref_usd": g["ref_usd"],
                    "actual_usd": g["actual_usd"],
                    "avg_latency_ms": int(g["latency_sum"] / g["calls"]) if g["calls"] else 0,
                }
            )
        out.sort(key=lambda x: -x["ref_usd"])
        return out

    total_ref = sum(m["ref_usd"] for m in models)
    total_actual = sum(m["actual_usd"] for m in models)
    total_tokens = sum(m["tokens"] for m in models)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sessions": sessions,
        "event_count": len(events),
        "total_tokens": total_tokens,
        "total_ref_usd": total_ref,
        "total_actual_usd": total_actual,
        "models": models,
        "by_provider": _finalize(by_provider),
        "by_subsystem": _finalize(by_subsystem),
        "provider_detail": provider_detail,
        "groq_rows": groq_rows,
        "local_rows": local_rows,
        "aa_url": _AA_MODELS_URL,
    }


def _empty_agg() -> dict[str, Any]:
    return {"calls": 0, "fail": 0, "tokens": 0, "ref_usd": 0.0, "actual_usd": 0.0, "latency_sum": 0}


_SCRIBBLE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Comic+Relief:wght@400;700&display=swap');
:root{
  --paper:#ffffff;--paper-dark:#f0f0f0;--ink:#1a1a1a;--ink-light:#3a3a3a;
  --ink-faint:#6a6a6a;--pencil:#4a4a4a;
  --crayon-pink:#e8a0b0;--crayon-blue:#8ab4d8;--crayon-yellow:#e8d88a;
  --crayon-green:#8ad8a0;--crayon-red:#d88a8a;--crayon-purple:#b08ad8;
  --shadow:rgba(0,0,0,0.10);--border:#2a2a2a;
}
*{margin:0;padding:0;box-sizing:border-box}
html{background:var(--paper)}
body{
  background:var(--paper);color:var(--ink);
  font-family:'Comic Relief','Comic Sans MS',cursive,sans-serif;
  min-height:100vh;font-size:1.05em;line-height:1.6;position:relative;overflow-x:hidden;
}
body::before{
  content:'';position:fixed;inset:0;
  background-image:
    repeating-linear-gradient(0deg,transparent,transparent 28px,rgba(0,0,0,0.03) 28px,rgba(0,0,0,0.03) 29px),
    repeating-linear-gradient(90deg,transparent,transparent 28px,rgba(0,0,0,0.02) 28px,rgba(0,0,0,0.02) 29px);
  pointer-events:none;z-index:0;background-size:29px 29px;
}
.container{position:relative;z-index:1;max-width:1100px;margin:0 auto;padding:24px 20px 40px}
.header{
  text-align:center;margin-bottom:24px;padding:36px 20px 24px;
  border:3px solid var(--border);position:relative;background:var(--paper);
  box-shadow:6px 6px 0 var(--border);
}
.header::before{
  content:'';position:absolute;top:8px;left:8px;right:8px;bottom:8px;
  border:2px dashed var(--ink-faint);pointer-events:none;
}
.header h1{
  font-size:2.4em;font-weight:700;color:var(--ink);margin-bottom:8px;
  text-shadow:2px 2px 0 var(--paper-dark);
}
.header .subtitle{color:var(--pencil);font-size:1em;margin-bottom:14px}
.badges{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.badges span{
  padding:4px 12px;font-weight:700;font-size:0.75em;
  border:2px solid var(--border);box-shadow:2px 2px 0 var(--border);
}
.badges .live{background:var(--crayon-green)}
.badges .ref{background:var(--crayon-blue)}
.badges .cost{background:var(--crayon-pink)}
.stats-row{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:12px;margin-bottom:24px;
}
.stat-card{
  background:var(--paper);border:2px solid var(--border);padding:16px;
  text-align:center;box-shadow:3px 3px 0 var(--shadow);transition:all 0.2s;
}
.stat-card:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--border)}
.stat-card .icon{font-size:1.8em;margin-bottom:4px}
.stat-card .value{font-size:1.6em;font-weight:800;color:var(--ink)}
.stat-card .label{color:var(--pencil);font-size:0.78em}
.section{margin-bottom:32px;scroll-margin-top:20px}
h2{
  font-size:1.5em;font-weight:700;margin:0 0 12px;padding-bottom:6px;
  border-bottom:2px dashed var(--border);position:relative;
}
h2::after{
  content:'';position:absolute;bottom:-4px;left:0;width:48px;height:4px;
  background:var(--crayon-pink);border-radius:2px;
}
.note{
  background:var(--crayon-blue)22;border-left:4px solid var(--crayon-blue);
  padding:12px 16px;margin:0 0 20px;color:var(--ink-light);font-size:0.92em;
}
.note code{
  background:var(--paper-dark);padding:1px 5px;border:1px solid var(--ink-faint);
  font-family:Consolas,monospace;font-size:0.85em;
}
table{width:100%;border-collapse:collapse;margin:8px 0;font-size:0.86em}
th{
  text-align:left;padding:10px 10px;background:var(--paper-dark);color:var(--ink);
  font-size:0.78em;font-weight:700;text-transform:uppercase;
  border-bottom:2px solid var(--border);
}
td{padding:9px 10px;border-bottom:1px solid var(--paper-dark);color:var(--ink-light)}
tr:hover td{background:var(--paper-dark)}
a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--crayon-pink)}
a:hover{color:var(--crayon-pink)}
.mood-tag{
  display:inline-block;padding:2px 8px;font-weight:700;font-size:0.72em;
  border:2px solid var(--border);box-shadow:1px 1px 0 var(--shadow);
}
.mood-tag.light{background:var(--crayon-green)}
.mood-tag.medium{background:var(--crayon-yellow)}
.mood-tag.heavy{background:var(--crayon-red)}
.mood-tag.unknown{background:var(--paper-dark)}
.barwrap{
  background:var(--paper-dark);border:2px solid var(--border);
  height:10px;min-width:50px;box-shadow:inset 1px 1px 0 var(--shadow);
}
.bar{height:100%;background:var(--crayon-pink);border-right:2px solid var(--border)}
.table-wrap{
  background:var(--paper);border:2px solid var(--border);
  padding:4px;box-shadow:3px 3px 0 var(--shadow);overflow-x:auto;
}
.footer{
  text-align:center;padding:24px 16px;color:var(--pencil);font-size:0.85em;
  border-top:2px dashed var(--border);margin-top:16px;
}
.provider-card{
  background:var(--paper);border:2px solid var(--border);padding:14px;margin:0 0 14px;
  box-shadow:3px 3px 0 var(--shadow);
}
.provider-card h3{margin:0 0 8px;font-size:1.1em}
.provider-card .meta{color:var(--pencil);font-size:0.82em;margin-bottom:8px}
@media(max-width:768px){
  .header h1{font-size:1.8em}
  .stats-row{grid-template-columns:repeat(2,1fr)}
  table{font-size:0.75em}
}
"""


def _tier_tag(tier: str) -> str:
    t = html.escape(tier)
    cls = t if t in ("light", "medium", "heavy") else "unknown"
    return f'<span class="mood-tag {cls}">{t}</span>'


def _bar(width_pct: float) -> str:
    w = max(4, min(100, int(width_pct)))
    return f'<div class="barwrap"><div class="bar" style="width:{w}%"></div></div>'


def render_html(data: dict[str, Any], *, refresh_seconds: int = 0) -> str:
    max_ref = max((m["ref_usd"] for m in data["models"]), default=1.0) or 1.0
    model_rows = []
    for m in data["models"]:
        pct = 100 * m["ref_usd"] / max_ref
        info = html.escape(str(m.get("info") or ""))
        fail = int(m.get("fail") or 0)
        model_rows.append(
            f"<tr>"
            f"<td>{html.escape(m['subsystem'])}</td>"
            f"<td>{html.escape(m['provider'])}</td>"
            f"<td title=\"{html.escape(m['model'])}\">{html.escape(m['display'][:32])}</td>"
            f"<td>{_tier_tag(str(m.get('weight_tier', '?')))}</td>"
            f"<td>{m['calls']}" + (f" <span class=\"mood-tag heavy\">{fail}✗</span>" if fail else "") + "</td>"
            f"<td>{m['tokens']:,}</td>"
            f"<td>${m['ref_per_1m']:.3f}</td>"
            f"<td>${m['ref_usd']:.4f}</td>"
            f"<td>${m['actual_usd']:.4f}</td>"
            f"<td>{m['avg_latency_ms']}ms</td>"
            f"<td class=\"info\">{info}</td>"
            f"<td>{_bar(pct)}</td>"
            f"<td><a href=\"{html.escape(m['aa_url'])}\" target=\"_blank\" rel=\"noopener\">AA ↗</a></td>"
            f"</tr>"
        )

    prov_rows = "".join(
        f"<tr><td><strong>{html.escape(p['name'])}</strong></td><td>{p['calls']}</td>"
        f"<td>{p.get('fail', 0)}</td><td>{p['tokens']:,}</td>"
        f"<td>${p['ref_usd']:.4f}</td><td>${p['actual_usd']:.4f}</td><td>{p['avg_latency_ms']}ms</td></tr>"
        for p in data["by_provider"]
    )
    sub_rows = "".join(
        f"<tr><td><strong>{html.escape(s['name'])}</strong></td><td>{s['calls']}</td>"
        f"<td>{s.get('fail', 0)}</td><td>{s['tokens']:,}</td>"
        f"<td>${s['ref_usd']:.4f}</td><td>${s['actual_usd']:.4f}</td><td>{s['avg_latency_ms']}ms</td></tr>"
        for s in data["by_subsystem"]
    )

    provider_cards = []
    for p in data.get("provider_detail") or []:
        mini = "".join(
            f"<tr><td>{html.escape(m['subsystem'])}</td><td title=\"{html.escape(m['model'])}\">"
            f"{html.escape(m['display'][:28])}</td><td>{m['calls']}</td>"
            f"<td>{m['avg_latency_ms']}ms</td></tr>"
            for m in p.get("models") or []
        )
        provider_cards.append(
            f'<div class="provider-card"><h3>🏷️ {html.escape(p["name"])}</h3>'
            f'<p class="meta">{p["calls"]} calls · {p["tokens"]:,} tokens · ref ${p["ref_usd"]:.4f}</p>'
            f'<div class="table-wrap"><table><tr><th>Subsystem</th><th>Model</th><th>Calls</th><th>Avg ms</th></tr>'
            f'{mini or "<tr><td colspan=4>—</td></tr>"}</table></div></div>'
        )

    groq_section = ""
    if data.get("groq_rows"):
        groq_lines = "".join(
            f"<tr><td>{html.escape(g['subsystem'])}</td><td>{html.escape(g['model'])}</td>"
            f"<td>{g['calls']}</td><td>{html.escape(str(g.get('info','')))}</td><td>{g['avg_latency_ms']}ms</td></tr>"
            for g in data["groq_rows"]
        )
        groq_section = f"""
<div class="section" id="groq">
  <h2>✎ Groq (Whisper + voice LLM)</h2>
  <div class="table-wrap"><table>
    <tr><th>Subsystem</th><th>Model</th><th>Calls</th><th>Info</th><th>Avg ms</th></tr>
    {groq_lines}
  </table></div>
</div>"""

    local_section = ""
    if data.get("local_rows"):
        local_lines = "".join(
            f"<tr><td>{html.escape(g['subsystem'])}</td><td>{html.escape(g['model'])}</td>"
            f"<td>{g['calls']}</td><td>{html.escape(g.get('info',''))}</td><td>{g['avg_latency_ms']}ms</td></tr>"
            for g in data["local_rows"]
        )
        local_section = f"""
<div class="section" id="local">
  <h2>✎ Lokal (Whisper / LM Studio embed)</h2>
  <div class="table-wrap"><table>
    <tr><th>Subsystem</th><th>Model</th><th>Calls</th><th>Info</th><th>Avg ms</th></tr>
    {local_lines}
  </table></div>
</div>"""
    sessions = ", ".join(html.escape(s) for s in data["sessions"]) or "(belum ada)"
    refresh_meta = (
        f'  <meta http-equiv="refresh" content="{int(refresh_seconds)}">\n'
        if refresh_seconds > 0
        else ""
    )
    watch_badge = (
        f'<span class="live">⟳ auto {int(refresh_seconds)}s</span>' if refresh_seconds > 0 else ""
    )

    empty_models = (
        '<tr class="empty"><td colspan="13">Belum ada telemetry — jalankan bridge atau bridge_health --deep</td></tr>'
    )

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
{refresh_meta}<title>📊 Arti API Telemetry [Scribble Edition]</title>
<style>{_SCRIBBLE_CSS}</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1>📊 Arti API Telemetry</h1>
  <p class="subtitle">Pemakaian model per provider &amp; subsystem — live saat stream</p>
  <div class="badges">
    <span class="ref">v0.6 telemetry</span>
    <span class="cost">{html.escape(data['generated_at'])}</span>
    {watch_badge}
  </div>
</div>

<div class="stats-row">
  <div class="stat-card"><div class="icon">📞</div><div class="value">{data['event_count']:,}</div><div class="label">API calls</div></div>
  <div class="stat-card"><div class="icon">🔤</div><div class="value">{data['total_tokens']:,}</div><div class="label">Total tokens</div></div>
  <div class="stat-card"><div class="icon">📐</div><div class="value">${data['total_ref_usd']:.4f}</div><div class="label">Reference $</div></div>
  <div class="stat-card"><div class="icon">💸</div><div class="value">${data['total_actual_usd']:.4f}</div><div class="label">Actual $</div></div>
</div>

<div class="note">
  <strong>Reference $</strong> = perkiraan beban kalau ditagih (dari <code>api_cost_table.json</code> + <code>model_benchmarks.json</code>).
  <strong>Actual $</strong> = yang keluar dompet ($0 untuk free tier).
  Sessions: <strong>{sessions}</strong> —
  banding kualitas/kecepatan: <a href="{html.escape(data['aa_url'])}" target="_blank" rel="noopener">Artificial Analysis ↗</a>
</div>

<div class="section" id="models">
  <h2>✎ Per model</h2>
  <div class="table-wrap">
  <table>
    <tr>
      <th>Subsystem</th><th>Provider</th><th>Model</th><th>Tier</th><th>Calls</th><th>Tokens</th>
      <th>Ref $/1M</th><th>Ref $</th><th>Actual $</th><th>Avg ms</th><th>Info</th><th>Load</th><th>AA</th>
    </tr>
    {''.join(model_rows) or empty_models}
  </table>
  </div>
</div>

{groq_section}
{local_section}

<div class="section" id="provider-detail">
  <h2>✎ Per provider → model</h2>
  {''.join(provider_cards) or '<p class="note">Belum ada data provider.</p>'}
</div>

<div class="section" id="providers">
  <h2>✎ Ringkasan per provider</h2>
  <div class="table-wrap">
  <table>
    <tr><th>Provider</th><th>Calls</th><th>Fail</th><th>Tokens</th><th>Ref $</th><th>Actual $</th><th>Avg ms</th></tr>
    {prov_rows or '<tr class="empty"><td colspan="7">—</td></tr>'}
  </table>
  </div>
</div>

<div class="section" id="subsystems">
  <h2>✎ Per subsystem (fitur Arti)</h2>
  <div class="table-wrap">
  <table>
    <tr><th>Subsystem</th><th>Calls</th><th>Fail</th><th>Tokens</th><th>Ref $</th><th>Actual $</th><th>Avg ms</th></tr>
    {sub_rows or '<tr class="empty"><td colspan="7">—</td></tr>'}
  </table>
  </div>
</div>

<div class="footer">
  <p>🎭 Arti VTuber — Scribble Edition telemetry</p>
  <p>Style: OMORI / handwritten / 90% B&amp;W + krayon ✎</p>
</div>

</div>
</body>
</html>"""


def generate_dashboard(
    config: dict | None = None,
    *,
    output: Path | str | None = None,
    refresh_seconds: int = 0,
) -> Path:
    cfg = {**tel.DEFAULT_CONFIG, **(config or {})}
    data = load_dashboard_data(cfg)
    out = Path(output) if output else tel._telemetry_dir(cfg) / "dashboard.html"
    if not out.is_absolute():
        out = _ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data, refresh_seconds=refresh_seconds), encoding="utf-8")
    return out


def watch_dashboard(
    config: dict | None = None,
    *,
    output: Path | str | None = None,
    interval_sec: int = 15,
    open_browser: bool = False,
) -> None:
    """Regenerate dashboard HTML every *interval_sec* while bridge runs."""
    import time
    import webbrowser

    cfg = {**tel.DEFAULT_CONFIG, **(config or {})}
    opened = False
    print(f"[Telemetry] Watch mode — refresh every {interval_sec}s (Ctrl+C to stop)")
    try:
        while True:
            out = generate_dashboard(cfg, output=output, refresh_seconds=interval_sec)
            print(f"[Telemetry] Dashboard -> {out}")
            if open_browser and not opened:
                webbrowser.open(out.as_uri())
                opened = True
            time.sleep(max(3, interval_sec))
    except KeyboardInterrupt:
        print("[Telemetry] Watch stopped")


def main() -> None:
    import argparse
    import webbrowser

    p = argparse.ArgumentParser(description="Generate Arti telemetry HTML dashboard")
    p.add_argument("--open", action="store_true", help="Open dashboard in browser")
    p.add_argument("--watch", action="store_true", help="Keep regenerating while bridge runs")
    p.add_argument("--interval", type=int, default=15, help="Watch refresh seconds (default 15)")
    p.add_argument("-o", "--output", default="", help="Output HTML path")
    args = p.parse_args()
    if args.watch:
        watch_dashboard(
            output=args.output or None,
            interval_sec=args.interval,
            open_browser=args.open,
        )
        return
    out = generate_dashboard(output=args.output or None)
    print(f"[Telemetry] Dashboard -> {out}")
    if args.open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()

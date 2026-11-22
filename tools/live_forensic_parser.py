"""Read-only, standard-library analysis of ARTI logs and telemetry JSONL.

Does not import ARTI runtime, call providers, or write input files. Times in
untimestamped terminal records remain unknown; line order is not causality.
Quantiles use linear interpolation at (n - 1) * p. Output goes to stdout.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re


COMPONENTS = ("vts_mikir", "rag", "llm", "tts_synth", "tts_play", "tts_jeda", "lain")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FIELDS = re.compile(r"\b([a-z_]+)=(-?\d+)(?:ms)?\b")
MARKERS = {
    "vision_budget": "[Vision] Budget turn habis",
    "still_screen_messages": "[Vision] Layar diam",
    "agy_timeout": "[Agy] timeout ",
    "agy_fallback": "[Agy] utama gagal",
    "queue_full": "[Queue] Penuh",
    "personal_cooldown": "kena cooldown pribadi",
    "echo_suppress": "[ASR] Echo suppress",
    "noise_filter": "[ASR Noise Filter] Skip noise:",
    "asr_cloud_success": "[ASR - Groq Cloud Whisper] Sukses",
    "curious_culled": "[Queue] Curious dibuang",
    "answer": "Arti menjawab:",
}


def summarize(values):
    values = sorted(values)
    def percentile(p):
        if not values:
            return None
        i = (len(values) - 1) * p
        lo = int(i)
        hi = min(lo + 1, len(values) - 1)
        return round(values[lo] + (values[hi] - values[lo]) * (i - lo), 3)
    return dict(count=len(values), sum=sum(values), median=percentile(.5),
                p90=percentile(.9), p95=percentile(.95),
                maximum=max(values) if values else None)


def parse_log(raw):
    # Progress bars use bare CR in this log. Preserve physical LF line numbers
    # (as shown by rg/editors), while still scanning every CR-delimited event.
    lines = ANSI.sub("", raw).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out = {key: [] for key in (
        "latency", "malformed_latency_lines", "incomplete_latency_lines",
        "vision_attempts", "vision_successes", "vision_failures", "vision_uptime",
        "scouter_attempts", "scouter_successes", "scouter_failures", "scouter_uptime",
        "agy", "codex", "recycles", "rewarm", "web_events", "marker_events")}
    counts = Counter({key: 0 for key in MARKERS})
    tags = Counter()
    last_history_time = None
    for line_no, line in enumerate(lines, 1):
        tags.update(re.findall(r"\[([A-Za-z][A-Za-z /_-]*)\]", line))
        stamp = re.search(r"\[History Recorded\] \[(\d\d:\d\d:\d\d)\]", line)
        if stamp:
            last_history_time = stamp[1]
        for key, marker in MARKERS.items():
            occurrences = line.count(marker)
            counts[key] += occurrences
            if occurrences:
                out["marker_events"].append(dict(kind=key, line=line_no, text=line,
                    preceding_history_time=last_history_time, occurrences=occurrences))
        for match in re.finditer(r"\[Latency\]([^\[]*)", line):
            fields = {k: int(v) for k, v in FIELDS.findall(match[1])}
            if "total" not in fields:
                out["malformed_latency_lines"].append(line_no)
                continue
            fields.setdefault("tts_jeda", 0)
            complete = all(k in fields for k in COMPONENTS)
            if not complete:
                out["incomplete_latency_lines"].append(line_no)
            component_sum = sum(fields[k] for k in COMPONENTS) if complete else None
            out["latency"].append(dict(line=line_no, **fields,
                sum_components_ms=component_sum,
                residual_ms=fields["total"] - component_sum if complete else None,
                preceding_history_time=last_history_time))
        for subsystem in ("Vision", "Scouter"):
            name = subsystem.lower()
            prefix = re.escape(f"[{subsystem}]")
            for m in re.finditer(prefix + r" Trying ([\w.-]+)\.\.\.", line):
                out[name + "_attempts"].append(dict(line=line_no, provider=m[1]))
            for m in re.finditer(prefix + r" OK ([\w.-]+) \((\d+)ms\)", line):
                out[name + "_successes"].append(dict(line=line_no, provider=m[1], latency_ms=int(m[2])))
            for m in re.finditer(prefix + r" ([\w.-]+) (parse fail \((\d+)ms\)|fail: [^\[]*)", line):
                out[name + "_failures"].append(dict(line=line_no, provider=m[1],
                    reason=m[2], latency_ms=int(m[3]) if m[3] else None))
            for m in re.finditer(prefix + r" uptime ([^\[]*)", line):
                out[name + "_uptime"].append(dict(line=line_no,
                    **{k: int(v) for k, v in FIELDS.findall(m[1])}))
        for m in re.finditer(r"\[Agy\] (\S+) init=(\d+)ms.*?total=(\d+)ms status=(\S+) turn=(\d+)", line):
            out["agy"].append(dict(line=line_no, model=m[1], init_ms=int(m[2]),
                latency_ms=int(m[3]), status=m[4], turn=int(m[5])))
        for m in re.finditer(r"\[Codex\] (\S+) (\d+)ms \(turn (\d+)\)", line):
            out["codex"].append(dict(line=line_no, model=m[1], latency_ms=int(m[2]), turn=int(m[3])))
        for m in re.finditer(r"\[Agy\] proses dibuang \(([^)]+)\)", line):
            out["recycles"].append(dict(line=line_no, reason=m[1]))
        if "[Agy]" in line and any(s in line.lower() for s in ("hangat", "warm", "siap")):
            out["rewarm"].append(dict(line=line_no, text=line))
        for m in re.finditer(r"\[WebLookup\]([^\[]*)", line):
            out["web_events"].append(dict(line=line_no, text=m[0].strip(),
                preceding_history_time=last_history_time))
    out.update(line_count=len(lines), counts=dict(counts), tags=dict(tags),
               latency_marker_count=sum(line.count("[Latency]") for line in lines))
    out["latency_stats_ms"] = {key: summarize(r[key] for r in out["latency"] if key in r)
                                for key in (*COMPONENTS, "total")}
    total = out["latency_stats_ms"]["total"]["sum"]
    out["component_shares_pct"] = {key: round(out["latency_stats_ms"][key]["sum"] / total * 100, 4)
                                    if total else None for key in COMPONENTS}
    out["worst10"] = sorted(out["latency"], key=lambda r: -r["total"])[:10]
    for name in ("vision", "scouter"):
        out[name + "_success_stats_ms"] = summarize(r["latency_ms"] for r in out[name + "_successes"])
        out[name + "_by_provider"] = {
            kind: dict(Counter(r["provider"] for r in out[name + "_" + kind]))
            for kind in ("attempts", "successes", "failures")}
    return out


def parse_telemetry(raw, start=None, end=None):
    """Filter [start, end) by ISO local timestamp; never equate session ID with PID."""
    events, invalid, outside = [], [], 0
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not all(k in row for k in ("ts", "subsystem", "provider", "ok", "latency_ms")):
                raise ValueError("missing telemetry fields")
            if not isinstance(row["ts"], str) or not isinstance(row["ok"], bool) or not isinstance(row["latency_ms"], (int, float)):
                raise ValueError("invalid telemetry fields")
            if (start and row["ts"] < start) or (end and row["ts"] >= end):
                outside += 1
            else:
                events.append(dict(row, line=line_no))
        except (ValueError, TypeError):
            invalid.append(line_no)
    groups = defaultdict(list)
    for row in events:
        groups[(row["subsystem"], row["provider"])].append(row)
    return dict(record_count=len(events), invalid_lines=invalid, outside_window=outside,
        first_ts=min((e["ts"] for e in events), default=None),
        last_ts=max((e["ts"] for e in events), default=None),
        groups=[dict(subsystem=s, provider=p, count=len(rows),
                     ok=sum(r["ok"] for r in rows), fail=sum(not r["ok"] for r in rows),
                     latency_ms=summarize(r["latency_ms"] for r in rows))
                for (s, p), rows in sorted(groups.items())], events=events)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--telemetry", action="store_true")
    parser.add_argument("--start", help="inclusive ISO local timestamp (telemetry only)")
    parser.add_argument("--end", help="exclusive ISO local timestamp (telemetry only)")
    args = parser.parse_args()
    data = args.path.read_bytes()
    raw = data.decode("utf-8-sig")
    result = parse_telemetry(raw, args.start, args.end) if args.telemetry else parse_log(raw)
    result["input"] = dict(name=args.path.name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()

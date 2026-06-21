"""API call telemetry — tokens, latency, cost rollup per session."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_lock = threading.Lock()
_buffer: list[dict[str, Any]] = []
_session_id: str = ""


@dataclass
class UsageInfo:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    reference_cost_usd: float = 0.0
    reference_per_1m: float = 0.0
    cost_source: str = "unknown"  # reported | estimated | free | unknown


DEFAULT_CONFIG: dict[str, Any] = {
    "telemetry_enabled": True,
    "telemetry_dir": "data/telemetry",
    "telemetry_log_each_call": True,
    "telemetry_cost_table_path": "data/api_cost_table.json",
    "telemetry_benchmarks_path": "data/model_benchmarks.json",
    "telemetry_flush_every": 20,
}


def set_session_id(session_id: str) -> None:
    global _session_id
    _session_id = session_id or ""


def parse_openai_usage(body: dict[str, Any]) -> UsageInfo:
    usage = body.get("usage") or {}
    info = UsageInfo(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
    )
    if not info.total_tokens and (info.prompt_tokens or info.completion_tokens):
        info.total_tokens = info.prompt_tokens + info.completion_tokens
    cost = usage.get("cost")
    if cost is not None:
        try:
            info.cost_usd = float(cost)
            info.cost_source = "reported"
        except (TypeError, ValueError):
            pass
    return info


def parse_gemini_usage(body: dict[str, Any]) -> UsageInfo:
    meta = body.get("usageMetadata") or {}
    prompt = int(meta.get("promptTokenCount") or 0)
    completion = int(meta.get("candidatesTokenCount") or 0)
    total = int(meta.get("totalTokenCount") or prompt + completion)
    return UsageInfo(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, cost_source="unknown")


def _telemetry_dir(config: dict) -> Path:
    rel = config.get("telemetry_dir", "data/telemetry")
    p = Path(rel)
    if not p.is_absolute():
        p = _ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cost_table(config: dict) -> dict[str, Any]:
    path = config.get("telemetry_cost_table_path", "data/api_cost_table.json")
    p = Path(path)
    if not p.is_absolute():
        p = _ROOT / p
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _lookup_price_entry(provider: str, model: str, table: dict[str, Any]) -> dict[str, Any]:
    keys = [
        model,
        f"{provider}/{model}",
        model.split("/")[-1] if "/" in model else model,
    ]
    for k in keys:
        if k and k in table and isinstance(table[k], dict):
            return table[k]
    return {}


def _benchmark_models(config: dict) -> dict[str, Any]:
    path = config.get("telemetry_benchmarks_path", "data/model_benchmarks.json")
    p = Path(path)
    if not p.is_absolute():
        p = _ROOT / p
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("models") or {}
    except Exception:
        return {}


def _lookup_benchmark(provider: str, model: str, config: dict) -> dict[str, Any]:
    return _lookup_price_entry(provider, model, _benchmark_models(config))


def _merged_price_entry(provider: str, model: str, config: dict) -> dict[str, Any]:
    table_entry = _lookup_price_entry(provider, model, _cost_table(config))
    bench = _lookup_benchmark(provider, model, config)
    if not table_entry:
        return bench
    if not bench:
        return table_entry
    return {**bench, **table_entry}


def reference_cost_usd(usage: UsageInfo, entry: dict[str, Any]) -> tuple[float, float]:
    """
    Hypothetical USD if billed at list/proxy rates (for comparing model weight).
    Formula: (prompt * $/1M_in + completion * $/1M_out) / 1e6, else blended * total / 1e6.
  Returns (reference_usd, effective_per_1m).
    """
    if not usage.total_tokens and not (usage.prompt_tokens or usage.completion_tokens):
        return 0.0, float(entry.get("ref_blended_per_1m") or entry.get("usd_per_1m_tokens") or 0)

    inp = float(entry.get("ref_input_per_1m") or 0)
    out = float(entry.get("ref_output_per_1m") or 0)
    if inp > 0 or out > 0:
        p = usage.prompt_tokens or max(0, usage.total_tokens - usage.completion_tokens)
        c = usage.completion_tokens or max(0, usage.total_tokens - p)
        ref = (p * inp + c * out) / 1_000_000.0
        eff = (ref / usage.total_tokens * 1_000_000.0) if usage.total_tokens else (inp + out) / 2
        return ref, eff

    blended = float(entry.get("ref_blended_per_1m") or entry.get("usd_per_1m_tokens") or 0)
    if blended > 0 and usage.total_tokens > 0:
        ref = (usage.total_tokens / 1_000_000.0) * blended
        return ref, blended
    return 0.0, 0.0


def estimate_cost(provider: str, model: str, usage: UsageInfo, config: dict) -> UsageInfo:
    entry = _merged_price_entry(provider, model, config)

    ref, eff = reference_cost_usd(usage, entry)
    usage.reference_cost_usd = ref
    usage.reference_per_1m = eff

    if usage.cost_source == "reported" and usage.cost_usd > 0:
        return usage

    if entry.get("free"):
        usage.cost_usd = 0.0
        usage.cost_source = "free"
        return usage

    per_1m = float(entry.get("usd_per_1m_tokens") or 0)
    if per_1m > 0 and usage.total_tokens > 0:
        usage.cost_usd = (usage.total_tokens / 1_000_000.0) * per_1m
        usage.cost_source = "estimated"
    return usage


def record_call(
    *,
    subsystem: str,
    provider: str,
    model: str,
    latency_ms: int,
    ok: bool = True,
    usage: UsageInfo | None = None,
    turn_id: str | None = None,
    extra: dict[str, Any] | None = None,
    config: dict | None = None,
) -> None:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if not cfg.get("telemetry_enabled", True):
        return
    u = usage or UsageInfo()
    u = estimate_cost(provider, model, u, cfg)
    bench = _lookup_benchmark(provider, model, cfg)
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session_id": _session_id or "unknown",
        "subsystem": subsystem,
        "provider": provider,
        "model": model,
        "weight_tier": bench.get("weight_tier"),
        "model_display": bench.get("display"),
        "latency_ms": latency_ms,
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "total_tokens": u.total_tokens,
        "cost_usd": round(u.cost_usd, 6),
        "cost_source": u.cost_source,
        "reference_cost_usd": round(u.reference_cost_usd, 6),
        "reference_per_1m": round(u.reference_per_1m, 4),
        "turn_id": turn_id,
        "ok": ok,
        "extra": extra or {},
    }
    with _lock:
        if cfg.get("telemetry_log_each_call", True):
            _append_event(event, cfg)
        else:
            _buffer.append(event)
            flush_every = int(cfg.get("telemetry_flush_every", 20))
            if len(_buffer) >= flush_every:
                for ev in _buffer:
                    _append_event(ev, cfg)
                _buffer.clear()


def record_openai_response(
    *,
    subsystem: str,
    provider: str,
    model: str,
    body: dict[str, Any],
    latency_ms: int,
    ok: bool = True,
    config: dict | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    usage = parse_openai_usage(body)
    record_call(
        subsystem=subsystem,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        ok=ok,
        usage=usage,
        extra=extra,
        config=config,
    )


def record_gemini_response(
    *,
    subsystem: str,
    model: str,
    body: dict[str, Any],
    latency_ms: int,
    ok: bool = True,
    config: dict | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    usage = parse_gemini_usage(body)
    record_call(
        subsystem=subsystem,
        provider="google_gemini",
        model=model,
        latency_ms=latency_ms,
        ok=ok,
        usage=usage,
        extra=extra,
        config=config,
    )


def _append_event(event: dict[str, Any], config: dict) -> None:
    sid = event.get("session_id") or "unknown"
    path = _telemetry_dir(config) / f"{sid}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def flush(config: dict | None = None) -> None:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    with _lock:
        if not _buffer:
            return
        for ev in _buffer:
            _append_event(ev, cfg)
        _buffer.clear()


def load_events(config: dict | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    tdir = _telemetry_dir(cfg)
    if session_id:
        paths = [tdir / f"{session_id}.jsonl"]
    else:
        paths = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    events: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if session_id:
            break
    return events


def session_summary(session_id: str, config: dict | None = None) -> dict[str, Any]:
    events = [e for e in load_events(config, session_id) if e.get("session_id") == session_id]
    if not events:
        events = load_events(config, session_id)
    by_key: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    total_ref = 0.0
    total_tokens = 0
    ok = fail = 0
    for e in events:
        key = f"{e.get('subsystem')}|{e.get('provider')}|{e.get('model')}"
        bucket = by_key.setdefault(
            key,
            {
                "subsystem": e.get("subsystem"),
                "provider": e.get("provider"),
                "model": e.get("model"),
                "calls": 0,
                "ok": 0,
                "fail": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "reference_cost_usd": 0.0,
                "reference_per_1m": 0.0,
                "latency_ms_sum": 0,
            },
        )
        bucket["calls"] += 1
        if e.get("ok", True):
            bucket["ok"] += 1
            ok += 1
        else:
            bucket["fail"] += 1
            fail += 1
        bucket["prompt_tokens"] += int(e.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(e.get("completion_tokens") or 0)
        tt = int(e.get("total_tokens") or 0)
        bucket["total_tokens"] += tt
        total_tokens += tt
        c = float(e.get("cost_usd") or 0)
        bucket["cost_usd"] += c
        total_cost += c
        r = float(e.get("reference_cost_usd") or 0)
        bucket["reference_cost_usd"] += r
        total_ref += r
        if e.get("reference_per_1m"):
            bucket["reference_per_1m"] = float(e.get("reference_per_1m") or 0)
        bucket["latency_ms_sum"] += int(e.get("latency_ms") or 0)

    for bucket in by_key.values():
        if bucket["total_tokens"] and bucket["reference_cost_usd"]:
            bucket["reference_per_1m"] = (
                bucket["reference_cost_usd"] / bucket["total_tokens"]
            ) * 1_000_000

    subs: dict[str, dict[str, int]] = {}
    for e in events:
        sub = str(e.get("subsystem") or "unknown")
        s = subs.setdefault(sub, {"ok": 0, "fail": 0})
        if e.get("ok", True):
            s["ok"] += 1
        else:
            s["fail"] += 1

    def _rate(sub: str) -> float | None:
        s = subs.get(sub, {})
        t = s.get("ok", 0) + s.get("fail", 0)
        return round(s.get("ok", 0) / t, 3) if t else None

    return {
        "session_id": session_id,
        "event_count": len(events),
        "ok": ok,
        "fail": fail,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "total_reference_cost_usd": round(total_ref, 6),
        "by_provider_model": list(by_key.values()),
        "vision_success_rate": _rate("vision"),
        "scouter_success_rate": _rate("scouter"),
        "voice_success_rate": _rate("voice"),
        "observer_success_rate": _rate("observer"),
        "asr_success_rate": _rate("asr"),
        "embed_success_rate": _rate("embed"),
    }


def format_api_usage_markdown(summary: dict[str, Any]) -> str:
    if not summary.get("event_count"):
        return "## API Usage\n\nTidak ada telemetry untuk sesi ini.\n"
    lines = [
        "## API Usage",
        "",
        f"- Events: {summary['event_count']} (ok={summary['ok']}, fail={summary['fail']})",
        f"- Total tokens: {summary['total_tokens']}",
        f"- Actual cost USD: ${summary['total_cost_usd']:.4f} (yang kamu bayar)",
        f"- Reference cost USD: ${summary.get('total_reference_cost_usd', 0):.4f} (beban relatif / list price proxy)",
        "",
        "Reference = perkiraan kalau model ditagih; model gratis tetap punya angka buat banding mana yang lebih \"berat\".",
        "",
        "| Subsystem | Provider | Model | Calls | Tokens | Ref $/1M | Ref USD | Actual $ |",
        "|-----------|----------|-------|-------|--------|----------|---------|----------|",
    ]
    for row in sorted(
        summary.get("by_provider_model") or [],
        key=lambda r: -(r.get("reference_cost_usd") or r.get("total_tokens") or 0),
    ):
        ref_per_1m = row.get("reference_per_1m") or 0
        if not ref_per_1m and row.get("total_tokens"):
            ref_per_1m = (float(row.get("reference_cost_usd") or 0) / row["total_tokens"]) * 1_000_000
        lines.append(
            f"| {row.get('subsystem', '')} | {row.get('provider', '')} | {str(row.get('model', ''))[:24]} | "
            f"{row.get('calls', 0)} | {row.get('total_tokens', 0)} | "
            f"${float(ref_per_1m):.3f} | ${float(row.get('reference_cost_usd', 0)):.4f} | "
            f"${float(row.get('cost_usd', 0)):.4f} |"
        )
    lines.append("")
    return "\n".join(lines)


def format_telemetry_report(config: dict | None = None) -> str:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    tdir = _telemetry_dir(cfg)
    paths = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return "[Telemetry] No data in data/telemetry/"
    latest = paths[0].stem
    summary = session_summary(latest, cfg)
    out = [f"=== Telemetry: {latest} ===", format_api_usage_markdown(summary)]
    return "\n".join(out)


def check_quota_warnings(summary: dict[str, Any], config: dict | None = None) -> list[str]:
    """Simple free-tier burn warnings."""
    warnings: list[str] = []
    events = summary.get("event_count", 0)
    if events > 400:
        warnings.append(f"[Telemetry] WARN: {events} API calls this session — cek quota Gemini/OpenRouter")
    fail_total = int(summary.get("fail", 0))
    if fail_total >= 3:
        by_provider: dict[str, int] = {}
        for row in summary.get("by_provider_model") or []:
            f = int(row.get("fail") or 0)
            if f:
                prov = str(row.get("provider") or "unknown")
                by_provider[prov] = by_provider.get(prov, 0) + f
        if by_provider:
            parts = ", ".join(f"{p}={n}" for p, n in sorted(by_provider.items()))
            warnings.append(
                f"[Telemetry] WARN: {fail_total} failed API calls — breakdown: {parts}"
            )
        else:
            warnings.append("[Telemetry] WARN: 3+ failed API calls — cek keys / rate limits")
    return warnings

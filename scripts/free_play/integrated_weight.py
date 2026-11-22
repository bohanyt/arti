"""Package page-weight replay over committed synthetic inputs (ADR 175).

No run folders, provider calls, private state, or game IO. The package step writes
its report before advancing the committed comparison baseline. If that changes
the baseline, commit it before the package's final clean-source identity check.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "integrated-page-weight.json"
BASELINE = FIXTURE.with_name("integrated-page-weight-baseline.json")


class ReplayInputRefusal(ValueError):
    """A named refusal the package supervisor can report without a traceback."""


def _read(path, label):
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayInputRefusal(f"page_weight_input_unavailable: {label} expected at {path}: {exc}") from exc


def replay(fixture=FIXTURE, *, config=None):
    from tools import page_weight as W

    from arti_runtime.page_budget import PageBudget
    budget = PageBudget.from_config(config or {})
    fixture = Path(fixture)
    data = _read(fixture, "decision-page fixture")
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list) or not pages:
        raise ReplayInputRefusal(f"page_weight_no_samples: decision-page fixture expected at {fixture}; pages=0")
    if data.get("schema") != "arti.integrated-page-weight.fixture/1":
        raise ReplayInputRefusal(f"page_weight_invalid_fixture: expected fixture schema /1 at {fixture}")
    per_page, missing = [], []
    for index, page in enumerate(pages):
        offer = page.get("offer") if isinstance(page, dict) else None
        if (not isinstance(offer, dict) or not offer.get("skills")
                or not isinstance(page.get("id"), str) or not page["id"]):
            raise ReplayInputRefusal(f"page_weight_invalid_fixture: page {index} has no decision skills at {fixture}")
        body = W.render(copy.deepcopy(offer), budget=budget)
        page_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        if page_bytes > budget.page_limit:
            raise ReplayInputRefusal(f"page_weight_limit_exceeded: {page_bytes} > configured {budget.page_limit} B")
        lost = W.pairs(offer["skills"]) - W.pairs(body.get("skills"))
        missing.extend(dict(page=page["id"], skill=name, arg=arg) for name, arg in sorted(lost, key=str))
        # Exactly page_weight._page_row's UTF-8 ASCII JSON metric, excluding the
        # provider envelope, for comparison with the historical pkg88 lines.
        size = len(json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        per_page.append(dict(id=page["id"], bytes=size, page_bytes=page_bytes, choices=len(W.pairs(body.get("skills")))))
    sizes = [page["bytes"] for page in per_page]
    return dict(schema="arti.integrated-page-weight/1", status="OFFLINE_REPLAY_ONLY", report_only=True,
                **budget.telemetry(), fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(), pages=len(sizes),
                after=dict(weight=dict(bytes=dict(median=statistics.median(sizes), max=max(sizes),
                                                  mean=round(statistics.fmean(sizes), 1)))),
                choices_lost=len(missing), input_choice_loss=missing, per_page=per_page,
                definition="Committed synthetic offers through current production renderer and provider-fit limits; "
                           "ASCII JSON UTF-8 bytes; tokens=bytes/4 estimate; provider envelope excluded. "
                           "Synthetic corpus differs from pkg88 a61/a62, so absolute medians are not interchangeable.")


def baseline_for(report):
    return dict(schema="arti.integrated-page-weight.baseline/1", fixture_sha256=report["fixture_sha256"],
                pages=report["pages"], median_bytes=report["after"]["weight"]["bytes"]["median"],
                max_bytes=report["after"]["weight"]["bytes"]["max"])


def compare(report, baseline):
    previous = _read(baseline, "comparison baseline")
    median = previous.get("median_bytes") if isinstance(previous, dict) else None
    if (not isinstance(previous, dict) or previous.get("schema") != "arti.integrated-page-weight.baseline/1"
            or type(median) not in (int, float) or not math.isfinite(median) or median <= 0
            or type(previous.get("pages")) is not int or previous["pages"] <= 0):
        raise ReplayInputRefusal(f"page_weight_no_samples: comparison baseline expected at {baseline}; median={median}")
    if previous.get("fixture_sha256") != report["fixture_sha256"] or previous["pages"] != report["pages"]:
        raise ReplayInputRefusal(f"page_weight_baseline_mismatch: fixture hash/page count differs from baseline at {baseline}")
    current = report["after"]["weight"]["bytes"]["median"]
    report["comparison"] = dict(previous_median_bytes=median, current_median_bytes=current,
                                median_growth_bytes=current - median)
    report["receipt_line"] = (f"integrated page weight: synthetic full-page median {current:g} B "
                              f"(~{round(current / 4)} tokens), previous {median:g} B, growth {current - median:+g} B, "
                              f"{report['pages']} pages, max {report['after']['weight']['bytes']['max']} B, "
                              f"choices lost {report['choices_lost']}; configured page limit {report.get('page_limit_bytes', 12000)} B, "
                              f"guard {report.get('render_guard_bytes', 13000)} B, scale {report.get('page_budget_scale', 1)}. Tokens are bytes/4 estimates.")
    return report


def _write(path, value):
    path = Path(path)
    encoded = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == encoded:
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def package_step(output, *, fixture=FIXTURE, baseline=BASELINE, config=None):
    report = compare(replay(fixture) if config is None else replay(fixture, config=config), baseline)
    if report["choices_lost"]:
        raise ReplayInputRefusal(f"page_weight_choice_loss: {report['choices_lost']} choices lost from fixture at {fixture}")
    report["source_sha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    _write(output / "integrated-page-weight.json", report)
    (output / "integrated-page-weight-line.txt").write_text(report["receipt_line"] + "\n", encoding="utf-8")
    # Advance only after a complete successful replay/report, never on refusal.
    _write(baseline, baseline_for(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--trial-config", type=Path)
    args = parser.parse_args(argv)
    try:
        config = _read(args.trial_config, "trial config") if args.trial_config else None
        report = package_step(args.output, fixture=args.fixture, baseline=args.baseline, config=config)
    except ReplayInputRefusal as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(report["receipt_line"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

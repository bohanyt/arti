"""Read-only source, temporary metadata/size replay of the old archive budget scan.

No provider payload/reply is opened. Packed indexes retain the recorded logical
sizes; raw files become sparse placeholders. The source run is never written.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from arti_stardew_trial_archive import TrialProviderArchive
from scripts.free_play import storage


def replay(run, output):
    run = Path(run).resolve()
    output = Path(output).resolve()
    if output.is_relative_to(run):
        raise ValueError("replay output must be outside the read-only source run")
    output.mkdir(parents=True, exist_ok=True)
    folders = sorted(Path(run).glob("runtime/live7/*/day-*/journal/provider_archive"))
    if not folders:
        raise ValueError("no provider archive folders in source run")
    results = []
    for days in sorted({1, min(10, len(folders)), len(folders)}):
        with tempfile.TemporaryDirectory(prefix="archive-replay-", dir=output) as temp:
            target = Path(temp) / "run"
            copies = []
            for folder in folders[:days]:
                destination = target / folder.relative_to(run)
                destination.mkdir(parents=True)
                if (folder / storage.INDEX).is_file():
                    doc, _ = storage.index(folder)
                    doc["objects"] = os.path.relpath(target / "objects", destination)
                    (destination / storage.INDEX).write_text(json.dumps(doc), encoding="utf-8")
                else:
                    for file in storage.files(folder, "*-*.json"):
                        p = destination / file.relative_to(folder)
                        p.parent.mkdir(parents=True, exist_ok=True)
                        with p.open("wb") as stream:
                            stream.truncate(file.stat().st_size)
                copies.append(destination)
            started = time.perf_counter()
            total = sum(storage.file_size(p) for folder in copies for p in storage.files(folder, "*-*.json"))
            before_ms = (time.perf_counter() - started) * 1000
            sink = TrialProviderArchive(copies[-1].parent)
            started = time.perf_counter()
            sink._budget_allows(0)
            cold_ms = (time.perf_counter() - started) * 1000
            warm = []
            for _ in range(15):
                started = time.perf_counter()
                sink._budget_allows(0)
                warm.append((time.perf_counter() - started) * 1000)
            from arti_runtime.archive_budget import accounting
            with accounting(sink.directory) as (_, counted, _):
                assert counted == total, (counted, total)
            row = dict(interpreter=sys.executable, python=sys.version.split()[0], days=days,
                       files=sum(len(storage.files(f, "*-*.json")) for f in copies), bytes=total,
                       before_ms=round(before_ms, 3), after_cold_ms=round(cold_ms, 3),
                       after_warm_median_ms=round(statistics.median(warm), 3))
            print(json.dumps(row), flush=True)
            results.append(row)
    (output / "archive-replay.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    replay(args.run, args.output)

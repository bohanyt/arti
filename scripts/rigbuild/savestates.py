"""The bench's savestates are produced, not kept (ADR 245).

`bench_savestates.json` records how each savestate a bench check names was made. `plan()` prints one line per
state: PRESENT (hash-verified), MAKE (its source run is on this machine; `--make` creates it with
`savestate add`), BENCH (the bench imports it from a live checkpoint at admission, ADR 242), or MISSING with the
exact situation to reproduce and the command that turns a run reaching it into the savestate. Saves never enter the
repository; the library lives on the rig (`savestates` in the config).
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def recipes(path=None):
    return json.loads(Path(path or HERE / "bench_savestates.json").read_text(encoding="utf8"))["states"]


def add_command(library, run, row):
    refs = " ".join(f'--journal-ref "{r}"' for r in row["journal_refs"])
    checkpoint = f" --checkpoint {row['checkpoint']}" if row.get("checkpoint") else ""
    return (f'python -m scripts.free_play.savestate --library "{library}" add "{run}" --id {row["id"]}{checkpoint} '
            f'--expect <expect.json with the recipe\'s expect list> --broken "{row["broken"]}" {refs}')


def plan(cfg, rows=None, *, savestate=None):
    if savestate is None:
        from scripts.free_play import savestate
    library = Path(cfg.savestates)
    out = []
    for row in rows or recipes():
        entry = savestate.entry_path(row["id"], library)
        if (entry / "manifest.json").is_file():
            try:
                doc = savestate.verify(entry)
                status, line = "PRESENT", f"verified, {doc['native_date']}"
            except (OSError, ValueError, KeyError) as exc:
                status, line = "BROKEN", f"present but fails verification: {exc}"
        elif row["kind"] == "add":
            run = Path(cfg.rig_root) / row["run"]
            if run.is_dir():
                status, line = "MAKE", add_command(library, run, row)
            else:
                status, line = "MISSING", (f"needs {row['situation']}: run a soak on a new test farm until that "
                                           f"save exists, then: {add_command(library, '<that run folder>', row)}")
        elif row["kind"] == "from-checkpoint":
            folder = Path(cfg.rig_root) / row["checkpoint_folder"]
            status, line = (("BENCH", "the bench imports it at admission from " + str(folder)) if folder.is_dir()
                            else ("MISSING", f"needs {row['situation']}: a live checkpoint folder; then "
                                             f"`python -m scripts.free_play.savestate from-checkpoint <folder> --id "
                                             f"{row['id']} --needs ... --journal-ref manifest.json`"))
        else:
            status, line = "MISSING", f"capture once: {row['situation']}; {row['made_by']}"
        out.append((row["id"], status, f"{status} {row['id']} ({row.get('used_by', '')}): {line}"))
    return out


def make(cfg, rows=None, *, savestate=None, say=print):
    """Create every MAKE row with `savestate add`; the native date must equal the recipe's. Returns failures."""
    if savestate is None:
        from scripts.free_play import savestate
    rows = rows or recipes()
    by_id = {r["id"]: r for r in rows}
    failures = 0
    for state_id, status, line in plan(cfg, rows, savestate=savestate):
        if status != "MAKE":
            say(line)
            failures += status == "BROKEN"
            continue
        row = by_id[state_id]
        try:
            entry = savestate.add(Path(cfg.rig_root) / row["run"], state_id, expect=row["expect"], broken=row["broken"],
                                  journal_refs=row["journal_refs"], checkpoint=row.get("checkpoint"),
                                  library=Path(cfg.savestates))
            doc = savestate.verify(entry)
            if doc["native_date"] != row["native_date"]:
                raise ValueError(f"made {doc['native_date']}, recipe says {row['native_date']}")
            say(f"MADE {state_id} {doc['native_date']} {doc['payload_bytes']} bytes")
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            failures += 1
            say(f"FAIL {state_id}: {exc}")
    return failures

#!/usr/bin/env python
"""Live7 WP5: pack / verify / push one finished run folder as a hash-verified archive.

    archive.py pack   <run_dir> [--refresh] [--cycle C] [--day-trial NAME] [--archive-root R] [--exclude GLOB ...]
    archive.py verify <archive_dir> [--source RUN_DIR] [--expect HASHES.json]
    archive.py hash   <dir> [--out HASHES.json]          (read-only file list + sha256, no copy)
    archive.py push   <archive_dir> [--host USER@HOST] [--dest REMOTE_ROOT] [--remote-python PY --remote-repo REPO]

Archive layout: `<archive-root>/<cycle>/<day-trial>/` = a byte-identical copy of the run folder (relative paths kept),
plus `manifest.json` (code SHA, DLL sha256, profile, verdict, game dates, every file with bytes + sha256 + mtime + kind)
and the derived `clock_index.jsonl` (journal line -> wall, game date/time, location).

Live7 run folder (`RUN/live7/<c>/cycle_state.json`; rehearsal 1 packed only the attempt and lost the cycle evidence):
`pack RUN --cycle C` writes one archive per day-trial attempt (`RUN/live7/<c>/day-*-attempt-*`, journal at its top,
as above) plus one `<archive-root>/<cycle>/run-<RUN name>/` archive (manifest kind `live7_run_evidence`) with the
whole rest of the run folder: cycle_state/cycle_events/identity, pad_watch, ui-op-log, staging log, launch/exit
records, SMAPI logs, screenshots, HOST batch files and checkpoint manifests (PLAN §3.1). Live save copies under
`live7/*/checkpoints/*/save/` stay out (save copies live with the savestates). An attempt archive that already exists
is reused only if it verifies against its source. Packing a single attempt folder of a Live7 run still works but
warns that the cycle evidence is not in it.

Rules (PLAN §3.1):
- `pack` refuses while any Stardew/SMAPI process runs on this machine (fail-closed if the process list is unreadable).
- `pack --refresh` adds late evidence after checking every archived source byte is unchanged. Prior snapshots
  are retained under `<cycle>/.history/`; changed or removed original evidence refuses refresh.
- Ordinary `pack` never overwrites an existing archive, hashes the source while copying, re-hashes the copy, and only then
  renames `<dest>.partial` to `<dest>`.
- Raw archives go to the LAB only (scp over ssh), never to GitHub or a cloud service.
Python stdlib only. Exit codes: 0 ok, 1 hash mismatch, 2 usage, 3 game running / process check failed,
4 destination exists, 5 transfer failed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.free_play import storage
import live7_common as C  # noqa: E402

SCHEMA = "live7_archive.v1"
GAME_PROCESS_RE = re.compile(r"stardew|smapi", re.I)
DEFAULT_HOST = "operator@192.0.2.13"
DEFAULT_REMOTE_ROOT = "C:/arti-archive"
DERIVED = ("manifest.json", "clock_index.jsonl")
RUN_EVIDENCE_KIND = "live7_run_evidence"
CHECKPOINT_SAVE_GLOB = "live7/*/checkpoints/*/save/*"
EXIT_OK, EXIT_MISMATCH, EXIT_USAGE, EXIT_GAME, EXIT_EXISTS, EXIT_TRANSFER = 0, 1, 2, 3, 4, 5


class ProcessCheckError(RuntimeError):
    pass


def running_game_processes() -> list[str]:
    """Names of running Stardew/SMAPI processes. Raises ProcessCheckError if the list cannot be read."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                raise ProcessCheckError(f"tasklist rc={out.returncode}")
            names = [row[0] for row in csv.reader(io.StringIO(out.stdout)) if row]
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                raise ProcessCheckError(f"ps rc={out.returncode}")
            names = [x.strip() for x in out.stdout.splitlines() if x.strip()]
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessCheckError(str(exc)) from exc
    return sorted({n for n in names if GAME_PROCESS_RE.search(n)})


def _copy_hash(src: Path, dst: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with storage.open_file(src) as fi, open(dst, "wb") as fo:
        for block in iter(lambda: fi.read(1 << 20), b""):
            h.update(block)
            size += len(block)
            fo.write(block)
    stamp = storage.file_stat(src).st_mtime
    os.utime(dst, (stamp, stamp))
    return h.hexdigest(), size


def file_list(root: Path, exclude=()) -> list[dict]:
    out = []
    for p in C.iter_files(root, exclude):
        rel = p.relative_to(root).as_posix()
        st = storage.file_stat(p)
        out.append({"path": rel, "bytes": st.st_size, "sha256": C.sha256_file(p), "mtime": round(st.st_mtime, 3),
                    "kind": C.file_kind(rel)})
    return out


def files_digest(files: list[dict]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda f: f["path"]):
        h.update(f"{f['path']}\t{f['sha256']}\t{f['bytes']}\n".encode("utf-8"))
    return h.hexdigest()


def _dll_hashes(root: Path) -> list[dict]:
    out = []
    for p in sorted(root.rglob("ArtiStardewBridge.dll")):
        out.append({"path": p.relative_to(root).as_posix(), "sha256": C.sha256_file(p)})
    return out


def build_identity(root: Path) -> dict:
    report = C.load_json(C.find_report(root)) or {}
    ident = report.get("identity") or {}
    approval = None
    for cand in ("approval.json", "evidence/approval-copy.json"):
        approval = C.load_json(root / cand)
        if approval:
            break
    out = {
        "code": {"sha": ident.get("source"), "tree": ident.get("tree"), "dirty": ident.get("dirty")},
        "dll": {"sha256": ident.get("dll"), "copies": _dll_hashes(root)},
        "profile": report.get("profile"),
        "limits": report.get("limits"),
        "verdict": {"status": report.get("status"),
                    "reason": report.get("reason") or ((report.get("skills") or {}).get("session") or {}).get("reason"),
                    "physical_inputs": report.get("physical_inputs"), "elapsed_seconds": report.get("elapsed_seconds")},
        "approval_present": bool(approval),
    }
    copies = {c["sha256"] for c in out["dll"]["copies"]}
    out["dll"]["copies_match_report"] = (not copies) or (ident.get("dll") in copies and len(copies) == 1)
    return out


def cmd_hash(args) -> int:
    root = Path(args.dir)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return EXIT_USAGE
    files = [f for f in file_list(root, args.exclude or ()) if f["path"] not in DERIVED]
    doc = {"root": root.name, "files": files, "files_sha256": files_digest(files), "count": len(files),
           "bytes": sum(f["bytes"] for f in files)}
    text = json.dumps(doc, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{doc['count']} files, {doc['bytes']} bytes, files_sha256 {doc['files_sha256']} -> {args.out}")
    else:
        print(text)
    return EXIT_OK


def live7_cycle_dirs(run: Path) -> list[Path]:
    return sorted(p.parent for p in run.glob("live7/*/cycle_state.json"))


def live7_attempts(run: Path) -> list[Path]:
    return [a for c in live7_cycle_dirs(run) for a in sorted(c.glob("day-*-attempt-*"))
            if a.is_dir() and C.find_journal(a) is not None]


def _label_ok(*labels) -> bool:
    for label in labels:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
            print(f"bad label {label!r} (letters, digits, . _ - only)", file=sys.stderr)
            return False
    return True


def _inside(dest: Path, src: Path) -> bool:
    try:
        dest.resolve().relative_to(src)
        return True
    except ValueError:
        return False


def cmd_pack(args, process_check=running_game_processes) -> int:
    src = Path(args.run_dir).resolve()
    if not src.is_dir():
        print(f"not a directory: {src}", file=sys.stderr)
        return EXIT_USAGE
    try:
        procs = process_check()
    except ProcessCheckError as exc:
        print(f"REFUSED: cannot read the process list ({exc}); not packing (fail-closed)", file=sys.stderr)
        return EXIT_GAME
    if procs:
        print(f"REFUSED: Stardew/SMAPI is running ({', '.join(procs)}); close the games before packing", file=sys.stderr)
        return EXIT_GAME
    root = Path(args.archive_root) if args.archive_root else C.default_archive_root()
    if C.find_journal(src) is None and live7_cycle_dirs(src):
        return pack_live7_run(src, root, args)
    if C.find_journal(src) is None:
        print(f"REFUSED: no journal under {src}/journal; not a run folder", file=sys.stderr)
        return EXIT_USAGE
    cycle = args.cycle or "uncycled"
    day_trial = args.day_trial or src.name
    if not _label_ok(cycle, day_trial):
        return EXIT_USAGE
    dest = root / cycle / day_trial
    if _inside(dest, src):
        print("REFUSED: archive destination is inside the run folder", file=sys.stderr)
        return EXIT_USAGE
    if dest.exists() and not getattr(args, "refresh", False):
        print(f"REFUSED: archive exists: {dest} (archives are never overwritten)", file=sys.stderr)
        return EXIT_EXISTS
    if (src.parent / "cycle_state.json").is_file():
        print(f"WARNING: {src.name} is one attempt of Live7 cycle {src.parent.name}; its cycle evidence (cycle state/"
              f"events, pad_watch, ui-op-log, SMAPI logs, screenshots) is NOT in this archive. Pack the run folder "
              f"{src.parent.parent.parent} instead (PLAN §3.1).", file=sys.stderr)
    return _pack_dir(src, dest, cycle, day_trial, args.exclude or (), refresh=getattr(args, "refresh", False))


def pack_live7_run(src: Path, root: Path, args) -> int:
    """One archive per attempt + one run-evidence archive with the rest of the run folder."""
    cycle = args.cycle or src.name
    run_label = "run-" + src.name
    if not _label_ok(cycle, run_label):
        return EXIT_USAGE
    if _inside(root / cycle, src):
        print("REFUSED: archive destination is inside the run folder", file=sys.stderr)
        return EXIT_USAGE
    cycles = live7_cycle_dirs(src)
    attempts = live7_attempts(src)
    multi = len(cycles) > 1
    plan = []
    for a in attempts:
        label = (a.parent.name + "-" + a.name) if multi else a.name
        if not _label_ok(label):
            return EXIT_USAGE
        plan.append((a, label))
    run_dest = root / cycle / run_label
    if run_dest.exists() and not getattr(args, "refresh", False):
        print(f"REFUSED: archive exists: {run_dest} (archives are never overwritten)", file=sys.stderr)
        return EXIT_EXISTS
    sessions = []
    for a, label in plan:
        dest = root / cycle / label
        problems = []
        if dest.exists() and getattr(args, "refresh", False):
            problems = _refresh_problems(a, dest, args.exclude or ())
            if problems:
                _refresh_refused(dest, problems)
                return EXIT_EXISTS
        if dest.exists():
            problems = verify_archive(dest, a)
            if problems and not getattr(args, "refresh", False):
                print(f"REFUSED: archive exists and differs from its source: {dest} ({problems[:3]})", file=sys.stderr)
                return EXIT_EXISTS
            if not problems:
                print(f"ALREADY ARCHIVED (verified against source) {dest}")
        if not dest.exists() or problems:
            rc = _pack_dir(a, dest, cycle, label, args.exclude or (),
                           extra={"live7_run_evidence": run_label, "live7_cycle_dir": a.parent.name},
                           refresh=getattr(args, "refresh", False))
            if rc != EXIT_OK:
                return rc
        sessions.append({"day_trial": label, "cycle_dir": a.parent.name, "attempt": a.name,
                         "source": a.relative_to(src).as_posix()})
    exclude = [f"{a.relative_to(src).as_posix()}/*" for a, _ in plan] + [CHECKPOINT_SAVE_GLOB] + list(args.exclude or ())
    states = {}
    for c in cycles:
        st = C.load_json(c / "cycle_state.json") or {}
        states[c.name] = {"status": st.get("status"),
                          "days": [{k: d.get(k) for k in ("index", "date", "status", "outcome", "next_date", "checkpoint")}
                                   for d in st.get("days", [])]}
    identity = next((C.load_json(c / "identity.json") for c in cycles if (c / "identity.json").is_file()), None) or {}
    copies = _dll_hashes(src)
    return _pack_dir(src, run_dest, cycle, run_label, exclude, extra={
        "kind": RUN_EVIDENCE_KIND, "sessions": sessions, "cycles": states, "cycle_identity": identity or None,
        "code": {"sha": identity.get("source"), "tree": identity.get("tree"), "dirty": identity.get("dirty")},
        "dll": {"sha256": identity.get("dll"), "copies": copies,
                "copies_match_report": all(c["sha256"] == identity.get("dll") for c in copies)},
        "verdict": {"status": "cycle:" + ",".join(f"{k}={v['status']}" for k, v in states.items()), "reason": None,
                    "physical_inputs": None, "elapsed_seconds": None}}, refresh=getattr(args, "refresh", False))


def _refresh_refused(dest, problems):
    print(f"REFUSED refresh: {dest} ({problems[:3]}). "
          "An evidence file may be still being written (for example by tee). "
          "Finish writers and keep pack/report output outside the run folder. "
          "Refresh adds new files only; archived originals must remain byte-identical. "
          "If an original changed, retain this snapshot and pack to a new archive root.", file=sys.stderr)


def _refresh_problems(src, dest, exclude):
    problems = verify_archive(dest)
    manifest = C.load_json(dest / "manifest.json") or {}
    if manifest.get("source") != str(src) or manifest.get("excluded_globs") != list(exclude):
        problems.append("source or exclusions changed")
    for row in manifest.get("files", []):
        path = src / row["path"]
        if not storage.exists(path) or C.sha256_file(path) != row["sha256"]:
            problems.append("original evidence changed or missing: " + row["path"])
    return problems


def _pack_dir(src: Path, dest: Path, cycle: str, day_trial: str, exclude, extra=None, *, refresh=False) -> int:
    original = C.load_json(dest / "manifest.json") if dest.exists() else None
    if dest.exists():
        problems = _refresh_problems(src, dest, exclude) if refresh else ["archive exists"]
        if problems:
            _refresh_refused(dest, problems)
            return EXIT_EXISTS
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    files = []
    for p in C.iter_files(src, exclude):
        rel = p.relative_to(src).as_posix()
        if rel in DERIVED:
            continue
        digest, size = _copy_hash(p, tmp / rel)
        files.append({"path": rel, "bytes": size, "sha256": digest, "mtime": round(storage.file_stat(p).st_mtime, 3),
                      "kind": C.file_kind(rel)})
    bad = [f["path"] for f in files if C.sha256_file(tmp / f["path"]) != f["sha256"]]
    if bad:
        print(f"MISMATCH after copy: {bad[:5]}", file=sys.stderr)
        return EXIT_MISMATCH
    if original:
        copied = {f["path"]: f["sha256"] for f in files}
        if any(copied.get(f["path"]) != f["sha256"] for f in original["files"]):
            _refresh_refused(dest, ["original evidence changed during copy"])
            return EXIT_MISMATCH
    # Derived clock index (journal line -> wall, game date/time, location).
    session = C.load_session(tmp, cycle, day_trial)
    derived = []
    if session.lines:
        ci = tmp / "clock_index.jsonl"
        with open(ci, "w", encoding="utf-8") as fh:
            for rec in C.clock_index_records(session.clock, session.lines):
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        derived.append({"path": "clock_index.jsonl", "bytes": ci.stat().st_size, "sha256": C.sha256_file(ci),
                        "kind": "clock_index", "rate_game_min_per_s": session.clock.rate,
                        "rate_src": session.clock.rate_src})
    walls = [r.wall for r in session.clock.rows if r.wall is not None]
    manifest = {
        "schema": SCHEMA, "tool": C.TOOL_VERSION, "packed_utc": C.utc_iso(time.time()),
        "packed_on": platform.node(), "source": str(src), "cycle": cycle, "day_trial": day_trial,
        "domain": "stardew", **build_identity(src),
        "journal": session.journal_path.relative_to(tmp).as_posix() if session.journal_path else None,
        "journal_lines": len(session.lines),
        "wall": {"start_utc": C.utc_iso(min(walls)) if walls else None, "end_utc": C.utc_iso(max(walls)) if walls else None},
        "game_dates": [C.date_label(d) for d in C.session_dates(session)],
        "files": files, "files_sha256": files_digest(files), "count": len(files),
        "bytes": sum(f["bytes"] for f in files), "derived": derived,
        "excluded_globs": list(exclude),
        **(extra or {}),
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    prior = None
    if dest.exists():
        prior = dest.parent / ".history" / dest.name / str(time.time_ns())
        prior.parent.mkdir(parents=True, exist_ok=True)
        os.replace(dest, prior)
    try:
        os.replace(tmp, dest)
    except OSError:
        if prior is not None:
            os.replace(prior, dest)
        raise
    print(f"PACKED {dest}\n  {manifest['count']} files, {manifest['bytes']} bytes, files_sha256 {manifest['files_sha256']}\n"
          f"  code {manifest['code']['sha']} dll {manifest['dll']['sha256']} verdict {manifest['verdict']['status']}"
          f"/{manifest['verdict']['reason']} dates {manifest['game_dates']}")
    return EXIT_OK


def verify_archive(adir: Path, source: Path | None = None, expect: dict | None = None) -> list[str]:
    problems = []
    manifest = C.load_json(adir / "manifest.json")
    if not manifest or manifest.get("schema") != SCHEMA:
        return [f"no {SCHEMA} manifest in {adir}"]
    listed = {f["path"]: f for f in manifest["files"]}
    for f in manifest["files"] + manifest.get("derived", []):
        p = adir / f["path"]
        if not p.is_file():
            problems.append(f"missing {f['path']}")
        elif p.stat().st_size != f["bytes"] or C.sha256_file(p) != f["sha256"]:
            problems.append(f"hash/size mismatch {f['path']}")
    known = set(listed) | {d["path"] for d in manifest.get("derived", [])} | {"manifest.json"}
    for p in C.iter_files(adir):
        rel = p.relative_to(adir).as_posix()
        if rel not in known:
            problems.append(f"unlisted file {rel}")
    if files_digest(manifest["files"]) != manifest.get("files_sha256"):
        problems.append("files_sha256 does not match the file list")
    if source is not None:
        src = {f["path"]: f for f in file_list(source, manifest.get("excluded_globs") or ()) if f["path"] not in DERIVED}
        for rel in sorted(set(src) | set(listed)):
            if rel not in listed:
                problems.append(f"source file not archived {rel}")
            elif rel not in src:
                problems.append(f"archived file missing in source {rel}")
            elif src[rel]["sha256"] != listed[rel]["sha256"]:
                problems.append(f"source differs {rel}")
    if expect is not None:
        exp = {f["path"]: f["sha256"] for f in expect.get("files", [])}
        for rel in sorted(set(exp) | set(listed)):
            if exp.get(rel) != (listed.get(rel) or {}).get("sha256"):
                problems.append(f"expected hash differs {rel}")
        if expect.get("files_sha256") and expect["files_sha256"] != manifest["files_sha256"]:
            problems.append("expected files_sha256 differs")
    return problems


def cmd_verify(args) -> int:
    adir = Path(args.archive_dir)
    expect = C.load_json(Path(args.expect)) if args.expect else None
    if args.expect and expect is None:
        print(f"cannot read {args.expect}", file=sys.stderr)
        return EXIT_USAGE
    problems = verify_archive(adir, Path(args.source) if args.source else None, expect)
    manifest = C.load_json(adir / "manifest.json") or {}
    if problems:
        print(f"VERIFY FAIL {adir}: {len(problems)} problem(s)")
        for p in problems[:50]:
            print("  " + p)
        return EXIT_MISMATCH
    print(f"VERIFY OK {adir}: {manifest.get('count')} files, files_sha256 {manifest.get('files_sha256')}"
          + (" (source matches)" if args.source else "") + (" (expected hashes match)" if expect else ""))
    return EXIT_OK


def _run(cmd: list[str], runner=subprocess.run) -> subprocess.CompletedProcess:
    print("+ " + " ".join(cmd))
    return runner(cmd, capture_output=True, text=True, timeout=3600)


def _win(path: str) -> str:
    return path.replace("/", "\\")


def cmd_push(args, runner=subprocess.run) -> int:
    adir = Path(args.archive_dir).resolve()
    problems = verify_archive(adir)
    if problems:
        print(f"REFUSED: local archive does not verify ({problems[:3]})", file=sys.stderr)
        return EXIT_MISMATCH
    manifest = C.load_json(adir / "manifest.json")
    cycle, day_trial = manifest["cycle"], manifest["day_trial"]
    dest_root = args.dest.rstrip("/\\")
    if args.host in ("", "local"):
        target = Path(dest_root) / cycle / day_trial
        if target.exists():
            print(f"REFUSED: remote archive exists: {target}", file=sys.stderr)
            return EXIT_EXISTS
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(adir, target, copy_function=shutil.copy2)
        problems = verify_archive(target)
        print(("PUSH OK (local) " if not problems else "PUSH VERIFY FAIL ") + str(target))
        return EXIT_OK if not problems else EXIT_MISMATCH
    ssh = ["ssh", "-o", "BatchMode=yes", args.host]
    remote_cycle = f"{dest_root}/{cycle}"
    remote_target = f"{remote_cycle}/{day_trial}"
    if args.remote_os == "windows":
        probe = _run(ssh + [f'if exist "{_win(remote_target)}" (echo EXISTS) else (echo FREE)'], runner)
        mk = ssh + [f'if not exist "{_win(remote_cycle)}" mkdir "{_win(remote_cycle)}"']
        scp_target = f"{args.host}:/{remote_cycle}" if re.match(r"^[A-Za-z]:", remote_cycle) else f"{args.host}:{remote_cycle}"
    else:
        probe = _run(ssh + [f"test -e '{remote_target}' && echo EXISTS || echo FREE"], runner)
        mk = ssh + [f"mkdir -p '{remote_cycle}'"]
        scp_target = f"{args.host}:{remote_cycle}"
    if probe.returncode != 0:
        print(f"TRANSFER FAILED: ssh probe rc={probe.returncode} {probe.stderr.strip()[:300]}", file=sys.stderr)
        return EXIT_TRANSFER
    if "EXISTS" in (probe.stdout or ""):
        print(f"REFUSED: remote archive exists: {args.host}:{remote_target}", file=sys.stderr)
        return EXIT_EXISTS
    r = _run(mk, runner)
    if r.returncode != 0:
        print(f"TRANSFER FAILED: mkdir rc={r.returncode} {r.stderr.strip()[:300]}", file=sys.stderr)
        return EXIT_TRANSFER
    r = _run(["scp", "-o", "BatchMode=yes", "-r", "-p", "-q", str(adir), scp_target], runner)
    if r.returncode != 0:
        print(f"TRANSFER FAILED: scp rc={r.returncode} {r.stderr.strip()[:300]}", file=sys.stderr)
        return EXIT_TRANSFER
    if args.remote_python and args.remote_repo:
        script = f"{args.remote_repo.rstrip('/')}/scripts/live7/archive.py"
        if args.remote_os == "windows":
            vcmd = f'"{_win(args.remote_python)}" "{_win(script)}" verify "{_win(remote_target)}"'
        else:
            vcmd = f"'{args.remote_python}' '{script}' verify '{remote_target}'"
        r = _run(ssh + [vcmd], runner)
        print((r.stdout or "").strip())
        if r.returncode != 0:
            print(f"REMOTE VERIFY FAILED rc={r.returncode} {r.stderr.strip()[:300]}", file=sys.stderr)
            return EXIT_MISMATCH
        print(f"PUSH OK {args.host}:{remote_target} (remote verify OK)")
    else:
        print(f"PUSH OK {args.host}:{remote_target} (remote verify not run: pass --remote-python and --remote-repo)")
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="pack one finished run folder (refuses while Stardew/SMAPI runs)")
    p.add_argument("run_dir")
    p.add_argument("--cycle")
    p.add_argument("--day-trial")
    p.add_argument("--archive-root", help=f"default ${C.ARCHIVE_ENV} or <repo parent>/arti-archive")
    p.add_argument("--exclude", action="append", help="glob on the relative path, repeatable")
    p.add_argument("--refresh", action="store_true", help="add late evidence; verify originals and retain prior snapshot")
    v = sub.add_parser("verify", help="re-hash an archive against its manifest")
    v.add_argument("archive_dir")
    v.add_argument("--source", help="also compare with the original run folder")
    v.add_argument("--expect", help="also compare with a `hash --out` file made elsewhere")
    h = sub.add_parser("hash", help="read-only file list with sha256 (no copy)")
    h.add_argument("dir")
    h.add_argument("--out")
    h.add_argument("--exclude", action="append")
    u = sub.add_parser("push", help="copy a verified archive to the LAB over scp")
    u.add_argument("archive_dir")
    u.add_argument("--host", default=os.environ.get("ARTI_LAB_HOST", DEFAULT_HOST), help="user@host, or 'local'")
    u.add_argument("--dest", default=os.environ.get("ARTI_LAB_ARCHIVE", DEFAULT_REMOTE_ROOT))
    u.add_argument("--remote-os", choices=("windows", "posix"), default="windows")
    u.add_argument("--remote-python", default=os.environ.get("ARTI_LAB_PYTHON"))
    u.add_argument("--remote-repo", default=os.environ.get("ARTI_LAB_REPO"))
    args = ap.parse_args(argv)
    return {"pack": cmd_pack, "verify": cmd_verify, "hash": cmd_hash, "push": cmd_push}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

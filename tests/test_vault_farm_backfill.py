"""V14 / ADR 238: the one-time farm backfill for existing mabar session notes. Synthetic folders only."""
from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from arti_runtime import vault_farm
from scripts.livepath import vault_farm_backfill as backfill

LADANG = hashlib.sha256(b"111:9001").hexdigest()
UJI = hashlib.sha256(b"222:9002").hexdigest()


def write(path, text, *, crlf=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\n", "\r\n") if crlf else text
    path.write_bytes(data.encode("utf-8"))


def mabar_run(runs, name, utc, *, continued=None, farm_in_facts=None, export=None, created=None):
    run = runs / name
    write(run / "MODE.json", json.dumps(dict(mode="mabar", utc=utc)))
    if created:  # mabar.jsonl says this run's stop created that vault note
        write(run / "mabar.jsonl", json.dumps(dict(event="auto_exported", note="created",
                                                   note_target="C:\\stream\\vault\\sessions\\" + created)) + "\n")
    if continued:
        write(run / "continued.json", json.dumps(continued))
    if farm_in_facts:
        write(run / "self_facts.json", json.dumps(dict(lines=["Namamu Arti.", "Farm ini bernama %s Farm." % farm_in_facts])))
    if export:
        write(run / "vault-export" / export[0], export[1], crlf=True)
    return run


def mabar_note(session, met):
    return ("# Mabar Session: %s\n\nMain Stardew Valley bareng Bohan.\n\n## Hari-hari pertama\n\n"
            "- Y1 spring 1: kenal (catatan pertemanan native): %s\n" % (session, met))


@pytest.fixture
def world(tmp_path):
    vault, runs, saves, inbox = tmp_path / "stream" / "vault", tmp_path / "runs", tmp_path / "saves", tmp_path / "inbox"
    sessions = vault / "sessions"
    ladang_note = mabar_note("2026-01-02-mabar", "Abigail, Pierre")
    write(sessions / "2026-01-02-mabar.md", ladang_note, crlf=True)  # mabar's note, written on Windows
    write(sessions / "mabar-20261118.md", "# Live Stream Session: mabar-20261118\n\n## Ringkasan Sesi\nKenal Linus.\n")
    write(sessions / "2026-11-16-mabar.md", mabar_note("2026-11-16-mabar", "Willy"))
    write(sessions / "2026-11-14-mabar.md", mabar_note("2026-11-14-mabar", "Leah"))
    write(sessions / "2026-11-15-mabar.md", mabar_note("2026-11-15-mabar", "Clint"))
    write(sessions / "2026-11-13-mabar.md", mabar_note("2026-11-13-mabar", "Evelyn"))
    write(sessions / "2026-11-12-mabar.md", mabar_note("2026-11-12-mabar", "Jodi"))
    write(sessions / "2026-10-01-default.md", "# Live Stream Session: 2026-10-01-default\n\nNgobrol santai.\n")
    write(sessions / "2026-01-02-mabar_beats.md", "# beats\n\n- momen\n")
    write(sessions / "index.md", "# Arti Vault Sessions Index\n")
    ladang = dict(save="Ladang_111", farm="Ladang")
    mabar_run(runs, "mabar-20261117", "2026-01-02T12:00:00Z", continued=ladang,
              export=("2026-01-02-mabar.md", ladang_note))
    mabar_run(runs, "mabar-20261118", "2026-01-03T05:00:00Z", farm_in_facts="Uji")
    mabar_run(runs, "mabar-20261116a", "2026-11-16T10:00:00Z", continued=ladang, created="2026-11-16-mabar.md")
    mabar_run(runs, "mabar-20261116b", "2026-11-16T13:00:00Z", farm_in_facts="Uji", created="2026-11-16-mabar.md")
    mabar_run(runs, "mabar-20261114a", "2026-11-14T10:00:00Z", continued=ladang, created="2026-11-14-mabar.md")
    mabar_run(runs, "mabar-20261114b", "2026-11-14T13:00:00Z", continued=ladang, created="2026-11-14-mabar.md")
    mabar_run(runs, "mabar-20261115", "2026-11-15T12:00:00Z", continued=dict(save="Gone_333", farm="Gone"),
              created="2026-11-15-mabar.md")
    # Review SF3: the only run started that day, but its export copy is another note and it never logged creating it.
    mabar_run(runs, "mabar-20261113", "2026-11-13T12:00:00Z", continued=ladang,
              export=("2026-11-13-mabar.md", mabar_note("2026-11-13-mabar", "Evelyn, Pam")))
    mabar_run(runs, "mabar-20261112", "2026-11-12T12:00:00Z", continued=ladang, created="2026-11-12-mabar.md")
    for save, farm in (("Ladang_111", "Ladang"), ("Uji_222", "Uji")):
        write(saves / save / "SaveGameInfo", "<Farmer><name>Bohan</name><farmName>%s</farmName></Farmer>" % farm)
    for ring in ("occurrences.111.9001.json", "occurrences.222.9002.json"):
        write(inbox / ring, "{}")
    argv = ["--vault", str(vault), "--runs", str(runs), "--runs", str(tmp_path / "no-such-runs"),
            "--saves", str(saves), "--inbox", str(inbox)]
    return tmp_path, vault, argv


def snapshot(folder):
    """Every path under ``folder``: a file's bytes and mtime, a folder as None."""
    return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
            for p in sorted(folder.rglob("*"))}


def run(argv):
    out = []
    return backfill.main(argv, out=out.append), out


def by_note(lines):
    rows = {}
    for line in lines:
        parts = line.split("\t")
        if parts[0] in ("placed", "unplaced", "tagged"):
            rows[parts[1].replace("\\", "/").rsplit("/", 1)[-1]] = parts
    return rows


def test_the_dry_run_lists_placed_and_unplaced_notes_and_writes_nothing(world):
    tmp_path, vault, argv = world
    before = snapshot(tmp_path)
    rc, out = run(argv)
    assert rc == 0 and snapshot(tmp_path) == before
    rows = by_note(out)
    assert set(rows) == {"2026-01-02-mabar.md", "mabar-20261118.md", "2026-11-16-mabar.md", "2026-11-14-mabar.md",
                         "2026-11-15-mabar.md", "2026-11-13-mabar.md", "2026-11-12-mabar.md",
                         "2026-10-01-default.md"}  # beats and index are left out
    placed = rows["2026-01-02-mabar.md"]
    assert placed[0] == "placed" and placed[2] == "farm=Ladang" and placed[3] == "scope_hash=" + LADANG
    assert "run mabar-20261117" in placed[4] and "continued.json" in placed[4] and "byte-identical" in placed[4]
    bridge = rows["mabar-20261118.md"]
    assert bridge[:4] == ["placed", bridge[1], "farm=Uji", "scope_hash=" + UJI]
    assert "named after run folder" in bridge[4] and "self_facts.json" in bridge[4] and "save Uji_222" in bridge[4]
    assert "catatan tetap" not in bridge[4]  # recall labels a run-named note like any session note (review SF1)
    same = rows["2026-11-14-mabar.md"]
    assert same[0] == "placed" and same[2] == "farm=Ladang" and "all the same farm" in same[4]
    assert rows["2026-11-16-mabar.md"][0] == "unplaced" and "different farms" in rows["2026-11-16-mabar.md"][2]
    assert rows["2026-11-15-mabar.md"][0] == "unplaced" and "no farmhand occurrence ring" in rows["2026-11-15-mabar.md"][2]
    assert rows["2026-10-01-default.md"][0] == "unplaced" and "no mabar run" in rows["2026-10-01-default.md"][2]
    alone = rows["2026-11-13-mabar.md"]  # review SF3: one run that day is not evidence enough
    assert alone[0] == "unplaced" and "1 run(s) started" in alone[2] and "mabar-20261113" in alone[2]
    assert "byte-identical vault-export copy or logged creating it" in alone[2]
    logged = rows["2026-11-12-mabar.md"]
    assert logged[0] == "placed" and logged[2] == "farm=Ladang" and "logged creating this note" in logged[4]
    assert any(line.startswith("skipped\truns folder not found") for line in out)
    assert out[-1] == "dry run: 4 placed, 4 unplaced, 0 already tagged; nothing written"


def test_two_saves_with_one_farm_name_leave_a_note_unplaced_unless_the_run_names_its_save(world):
    """Review SF4: mabar.run_save takes the first save with the name; a test copy of a farm would lend its rings."""
    tmp_path, vault, argv = world
    for save, farm in (("UjiCopy_444", "Uji"), ("LadangCopy_555", "Ladang")):
        write(tmp_path / "saves" / save / "SaveGameInfo",
              "<Farmer><name>Bohan</name><farmName>%s</farmName></Farmer>" % farm)
    rc, out = run(argv)
    rows = by_note(out)
    bridge = rows["mabar-20261118.md"]  # self facts only: which Uji?
    assert rc == 0 and bridge[0] == "unplaced"
    assert "2 saves carry that farm name (Uji_222, UjiCopy_444) and continued.json names none" in bridge[2]
    placed = rows["2026-01-02-mabar.md"]  # continued.json names Ladang_111
    assert placed[0] == "placed" and placed[3] == "scope_hash=" + LADANG


@pytest.mark.parametrize("case", ["no-backup", "undated", "mismatched", "missing-copy", "inside-vault"])
def test_apply_is_refused_without_a_good_dated_backup(world, case):
    tmp_path, vault, argv = world
    backup = tmp_path / "vault-backup-2026-01-03"
    shutil.copytree(vault, backup)
    if case == "undated":
        backup = backup.rename(tmp_path / "vault-backup-latest")
    elif case == "mismatched":
        (backup / "sessions" / "2026-11-14-mabar.md").write_text("different", encoding="utf-8")
    elif case == "missing-copy":
        (backup / "sessions" / "mabar-20261118.md").unlink()
    elif case == "inside-vault":
        backup = shutil.copytree(vault / "sessions", vault / "backup-2026-01-03")
    before = snapshot(vault)
    rc, out = run(argv + ["--apply"] + ([] if case == "no-backup" else ["--backup", str(backup)]))
    assert rc == backfill.REFUSED and out[-1].startswith("refused\t") and out[-1].endswith("nothing written")
    assert snapshot(vault) == before


def test_apply_with_a_good_backup_adds_only_the_tag_line_and_a_second_run_changes_nothing(world):
    tmp_path, vault, argv = world
    backup = shutil.copytree(vault, tmp_path / "backups" / "vault-20261118")
    sessions = vault / "sessions"
    before = {p.name: p.read_bytes() for p in sessions.iterdir()}
    rc, out = run(argv + ["--apply", "--backup", str(backup)])
    assert rc == 0 and out[-1].startswith("applied: 4 tagged")
    expect = {"2026-01-02-mabar.md": ("Ladang", LADANG, b"\r\n"), "2026-11-14-mabar.md": ("Ladang", LADANG, b"\n"),
              "2026-11-12-mabar.md": ("Ladang", LADANG, b"\n"), "mabar-20261118.md": ("Uji", UJI, b"\n")}
    for p in sessions.iterdir():
        after = p.read_bytes()
        if p.name not in expect:
            assert after == before[p.name], p.name  # unplaced, beats and index stay byte-identical
            continue
        farm, scope, nl = expect[p.name]
        line = vault_farm.tag_line(farm, [scope]).encode("utf-8")
        title, _, rest = before[p.name].partition(b"\n")
        assert after == title + b"\n" + line + nl + rest  # one line under the title, the note's own line ending
        assert vault_farm.read_tag(p) == dict(farm=farm, scope_hashes=(scope,))
    assert not list(sessions.glob(".*.farmtag.tmp"))
    tagged = snapshot(vault)
    rc, out = run(argv)
    assert rc == 0 and snapshot(vault) == tagged
    rows = by_note(out)
    assert [rows[n][0] for n in expect] == ["tagged"] * 4
    assert out[-1] == "dry run: 0 placed, 4 unplaced, 4 already tagged; nothing written"
    rc, out = run(argv + ["--apply", "--backup", str(backup)])
    assert rc == 0 and out[-1].startswith("applied: 0 tagged") and snapshot(vault) == tagged


def test_a_note_changed_after_the_check_stops_the_apply_before_any_write(world):
    tmp_path, vault, argv = world
    runs, _ = backfill.find_runs([tmp_path / "runs"])
    rows = backfill.plan(vault, runs, tmp_path / "saves", tmp_path / "inbox")
    placed = [r for r in rows if r["state"] == "placed"]
    late = placed[-1]["note"]
    late.write_bytes(late.read_bytes() + b"- satu baris baru\n")
    before = snapshot(vault)
    with pytest.raises(RuntimeError, match="changed since it was checked"):
        backfill.apply(placed)
    assert snapshot(vault) == before

"""fix132A P2: who told her stays in her memory; only training-data exports say "viewer" (ADR 230).

Owner, 2026-11-17: "[recorded remark omitted]". Synthetic names only; no real viewer
file is read.
"""
import json
import threading
from types import SimpleNamespace as NS

import arti_mind as M
from arti_runtime import experience_digest as D
from arti_runtime import goal_ladder as G
from arti_runtime import stream_notes as S
from arti_runtime.fact_line import goal_evidence
from arti_screen_privacy import ScreenPrivacy

SCOPE = "synthetic-save:132"
BASE_TS = 1794913200.0
PAGE = ["Farm", "tidur", "kirim", "Wood", "pergi_ke", "home", "panen"]


def bridge(tmp_path, monkeypatch, roster=()):
    monkeypatch.setattr(S, "_bridge_session", lambda b: "synthetic-session")
    monkeypatch.setattr(S, "_game_active", lambda b: True)
    monkeypatch.setattr(S, "_CHAT_SENT", {})
    monkeypatch.setattr(S, "_CHAT_RECENT", S.deque())
    monkeypatch.setattr(S, "_VIEWERS", set(roster))
    now = [BASE_TS]
    monkeypatch.setattr(S.time, "time", lambda: now[0])
    return NS(screen_privacy=ScreenPrivacy(), CONFIG={"arti_mind_dir": str(tmp_path)},
              _screen_privacy_ingress_lock=threading.RLock(), _yt_viewers_seen={n: BASE_TS for n in roster}), now


def drain(base, *, day="Y1 spring 5", place="Farm"):
    return S.ingest(base, SCOPE, lambda at: dict(day=day, clock=1900, place=place),
                    dict(game=[], page=[{"name": n} for n in PAGE]))


def owners(base):
    return [r for r in S._rows(base / "stardew" / "discoveries.jsonl") if r.get("kind") == "text_read"]


def record(tmp_path, monkeypatch):
    b, now = bridge(tmp_path, monkeypatch, roster=("SyntheticRara",))
    assert S.bridge_line(b, "owner", "ptt", "Coba panen dulu sebelum tidur ya.")
    now[0] += 61
    assert S.bridge_viewer(b, "Synthetic Budi", "Kirim kayunya juga ya")
    now[0] += 61
    assert S.bridge_viewer(b, "SyntheticRara", "kata SyntheticRara panen dulu", donation=True)
    now[0] += 61
    assert S.bridge_viewer(b, "SyntheticCici", "SyntheticRara bilang jual kayu")  # names another viewer: kept
    assert drain(tmp_path) == {"kept": 4}
    return b, now


def test_the_said_row_keeps_who_told_her_and_the_page_shows_kata_name(tmp_path, monkeypatch):
    record(tmp_path, monkeypatch)
    rows = owners(tmp_path)
    assert [(r["subject"], r.get("said_name"), r.get("mentions")) for r in rows] == [
        ("owner", None, None), ("penonton", "Synthetic Budi", None),
        ("donasi", "SyntheticRara", ["SyntheticRara"]), ("penonton", "SyntheticCici", ["SyntheticRara"])]
    evidence = [e for e in goal_evidence(G.facts(tmp_path, SCOPE)) if e["basis"] in G.SPEECH]
    assert [(e["speaker"], e.get("oleh")) for e in evidence] == [
        ("owner", None), ("viewer", "kata Synthetic Budi"), ("viewer", "kata SyntheticRara (donasi)"),
        ("viewer", "kata SyntheticCici")]
    page = G.render([], [], day=5, evidence=evidence, native=[], tags=PAGE, max_bytes=M.BLOCK_MAX_BYTES * 2)
    assert {r["oleh"] for r in page["dikatakan"]} <= {"owner", "kata Synthetic Budi", "kata SyntheticRara (donasi)",
                                                       "kata SyntheticCici"}
    assert any(r["oleh"] == "owner" for r in page["dikatakan"])  # owner rows stay "owner"
    # The voice digest says the same.
    digest = D.build(tmp_path, SCOPE, day=5, place="Farm")
    said = {r["text"]: r["by"] for r in digest["said"]}
    assert said == {"Coba panen dulu sebelum tidur ya.": "owner", "Kirim kayunya juga ya": "kata Synthetic Budi",
                    "kata SyntheticRara panen dulu": "kata SyntheticRara (donasi)",
                    "SyntheticRara bilang jual kayu": "kata SyntheticCici"}
    # ADR 231 part B3: the game day and clock the line was said at ride after the stream date.
    assert 'kata Synthetic Budi 2026-11-17 (di game Y1 spring 5 19:00): "Kirim kayunya juga ya"' in digest["text"]


def test_rate_limits_stay_keyed_by_the_hash(tmp_path, monkeypatch):
    b, now = bridge(tmp_path, monkeypatch)
    assert S.bridge_viewer(b, "SyntheticBudi", "Jual kayu ya")
    assert not S.bridge_viewer(b, "SyntheticBudi", "Jual batu ya")  # one inbox row per viewer per minute
    assert not S.bridge_viewer(b, " SyntheticBudi ", "Jual batu ya")  # the same viewer by the hash
    assert S.bridge_viewer(b, "SyntheticCici", "Jual batu ya")
    rows = [r for r in S._rows(S._path(tmp_path)) if not r.get("control")]
    assert [r["viewer"] for r in rows] == [S.viewer_hash("SyntheticBudi"), S.viewer_hash("SyntheticCici")]
    assert drain(tmp_path) == {"kept": 2}
    # Record limits: eight lines from one viewer a minute apart keep one (keyed by the hash, not the name).
    rows = [dict(v=1, **S.stamp(BASE_TS + 120 + 60 * i), session="synthetic-session", speaker="viewer", ingress="chat",
                 text="Panen parsnip %d" % i, game_active=True, privacy_epoch=0, recent_moments=[],
                 viewer=S.viewer_hash("SyntheticBudi"), viewer_name="SyntheticBudi" if i % 2 else "Budi lain")
            for i in range(8)]
    for row in rows:
        assert S.append_inbox(tmp_path, row)
    assert drain(tmp_path) == {"viewer_gap": 8}  # SyntheticBudi already has a line within ten minutes


def test_old_hash_rows_stay_readable_as_seorang_penonton(tmp_path):
    for i, (speaker, ingress) in enumerate((("viewer", "chat"), ("donasi", "donation"))):
        row = dict(session="old-session", speaker=speaker, ingress=ingress, text="Jual kayu %d" % i,
                   wall_ts=BASE_TS + i, wib=S.stamp(BASE_TS + i)["wib"], viewer=S.viewer_hash("old%d" % i))
        assert S.record_line(tmp_path, SCOPE, row, dict(day="Y1 spring 2", clock=900, place="Farm")) == "kept"
    evidence = [e for e in goal_evidence(G.facts(tmp_path, SCOPE)) if e["basis"] in G.SPEECH]
    assert [e["oleh"] for e in evidence] == ["seorang penonton", "seorang penonton (donasi)"]
    assert [r["by"] for r in D.build(tmp_path, SCOPE, day=3)["said"]] == ["seorang penonton (donasi)", "seorang penonton"]


def test_the_rig_label_rides_the_same_path(tmp_path):
    row = dict(session="rig", speaker="viewer", ingress="chat", text="Kirim kayunya juga.", wall_ts=BASE_TS,
               wib=S.stamp(BASE_TS)["wib"], viewer=S.viewer_hash("synthetic-1"), viewer_name=S.display_name("synthetic-1"))
    assert S.record_line(tmp_path, SCOPE, row, dict(day="Y1 winter 8", clock=1900, place="Farm")) == "kept"
    assert owners(tmp_path)[0]["said_name"] == "synthetic-1"


def test_anonymise_for_training_every_shape(tmp_path, monkeypatch):
    record(tmp_path, monkeypatch)
    for row in owners(tmp_path):
        before = json.dumps(row, sort_keys=True)
        out = S.anonymise_for_training(row)
        assert json.dumps(row, sort_keys=True) == before  # pure: her record is never changed
        text = json.dumps(out, ensure_ascii=False)
        for name in ("Synthetic Budi", "SyntheticRara", "SyntheticCici", "syntheticrara"):
            assert name not in text
        assert "viewer" not in out and "mentions" not in out  # the hash and the names list do not leave
        if row["subject"] == "owner":
            assert out == {k: v for k, v in row.items() if k not in ("viewer", "mentions")}
        else:
            assert out["said_name"] == "viewer"
    by_text = {r["text"]: S.anonymise_for_training(r)["text"] for r in owners(tmp_path)}
    assert by_text["kata SyntheticRara panen dulu"] == "kata viewer panen dulu"
    assert by_text["SyntheticRara bilang jual kayu"] == "viewer bilang jual kayu"  # another viewer's name, too
    # Page/planner evidence rows and digest rows.
    evidence = [e for e in goal_evidence(G.facts(tmp_path, SCOPE)) if e["basis"] in G.SPEECH]
    assert [S.anonymise_for_training(e).get("oleh") for e in evidence] == [
        None, "kata viewer", "kata viewer (donasi)", "kata viewer"]
    digest = D.build(tmp_path, SCOPE, day=5, place="Farm")
    rows = [S.anonymise_for_training(r) for r in digest["said"]]
    assert "Synthetic" not in json.dumps(rows) and {r["by"] for r in rows} == {"owner", "kata viewer", "kata viewer (donasi)"}
    # A digest line as a string: the exporter passes the names its record holds.
    names = {r["said_name"] for r in owners(tmp_path) if r.get("said_name")}
    line = S.anonymise_for_training(digest["text"], names)
    assert "Synthetic" not in line and "kata viewer" in line
    # A spoken short form and an @handle are the name too; the owner's own account is not a viewer.
    assert S.anonymise_for_training(dict(oleh="kata @SyntheticRara123", text="makasih syntheticr, @SyntheticRara123 ya"))[
        "text"] == "makasih viewer, viewer ya"
    assert S.anonymise_for_training("Siap Bohan, makasih SyntheticRara", ["@bohanyt", "SyntheticRara"]) == \
        "Siap Bohan, makasih viewer"
    assert S.anonymise_for_training(dict(oleh="penonton#abcd1234", text="Jual kayu"))["oleh"] == "kata viewer"

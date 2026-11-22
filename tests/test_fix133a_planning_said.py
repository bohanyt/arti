"""ADR 231 part D: what is said to her is an input of her planning, as facts of the page's shape.

Attempt 87: 16 planning calls, every one with `dikatakan: []` and `ucapan_ditunda: 2`: the byte bound dropped the two
said lines first while about 300 old seen rows were deferred after them anyway. Synthetic rows only.
"""
import json

from arti_runtime import goal_ladder as G
from arti_runtime import stream_notes as SN

SCOPE = "synthetic-save:d"
FESTIVAL = ("Arti, besok pagi kita mancing di sungai dekat jembatan. "
            "Bawa umpan ya.")
SELL = "Kalau tas penuh, simpan dulu di peti dekat pintu."


def said(base, text, day, clock, at):
    row = dict(session="synthetic-session", speaker="owner", ingress="ptt", text=text, wall_ts=at, wib=SN.stamp(at)["wib"])
    assert SN.record_line(base, SCOPE, row, dict(day=day, clock=clock, place="Cabin")) == "kept"


def evidence(base, seen=300):
    rows = [e for e in G.facts(base, SCOPE) if e.get("basis") in G.SPEECH]
    rows += [dict(ref="seen:%04d" % i, text="Tempat yang pernah kulihat nomor %d, dicatat waktu itu." % i, day=60 + i % 30,
                  place="Place%d" % i, basis="native", receipt_kind="place_entered", receipt_id="Place%d" % i,
                  text_ref=None, text_available=True, notified=True, announced=True) for i in range(seen)]
    return rows


def payload(base, scale=2, **kw):
    return G.build_input(base, SCOPE, day=94, target_day=94, mode="night", evidence=evidence(base, **kw), native=[],
                         max_bytes=G.INPUT_BYTES * scale)


def test_both_said_lines_reach_the_planning_input_with_who_and_when(tmp_path):
    said(tmp_path, FESTIVAL, "Y1 winter 6", 640, 1794903600.0)
    said(tmp_path, SELL, "Y1 winter 7", 1750, 1794907200.0)
    for scale in (1, 2):
        p = payload(tmp_path, scale)
        rows = {r["text"]: r for r in p["dikatakan"]}
        assert set(rows) == {FESTIVAL, SELL} and "ucapan_ditunda" not in p
        assert rows[FESTIVAL]["waktu_game"] == "Y1 winter 6 06:40" and rows[SELL]["waktu_game"] == "Y1 winter 7 17:50"
        assert all(r["speaker"] == "owner" and r["stream_date"] == "2026-11-17" for r in rows.values())
        assert p.get("bacaan_ditunda", 0) > 0  # old seen rows give way instead


def test_many_old_said_lines_give_way_before_her_seen_rows_but_the_newest_stay(tmp_path):
    for i in range(20):
        said(tmp_path, "Ucapan lama nomor %d yang cukup panjang untuk mengisi halaman perencanaan." % i,
             "Y1 winter 6", 800 + i, 1794900000.0 + i)
    said(tmp_path, SELL, "Y1 winter 7", 1750, 1794907200.0)
    p = payload(tmp_path, 1, seen=40)
    texts = [r["text"] for r in p["dikatakan"]]
    assert texts[-1] == SELL and len(texts) >= G.SAID_PLAN_KEEP
    assert p.get("ucapan_ditunda", 0) > 0


def test_the_planning_request_holds_them_as_facts_and_nothing_tells_her_what_to_want(tmp_path):
    said(tmp_path, FESTIVAL, "Y1 winter 6", 640, 1794903600.0)
    said(tmp_path, SELL, "Y1 winter 7", 1750, 1794907200.0)
    sent = []

    def complete(messages, schema, **kw):
        sent.append(messages)
        return json.dumps(dict(today="hari ini aku ke tambang", tomorrow="besok lihat lagi",
                               goals=dict(new=[], kept=[], dropped=[]), notes=dict(new=[], answered=[])))
    row = G.reflect(complete, tmp_path, SCOPE, day=94, target_day=94, mode="night", evidence=evidence(tmp_path), native=[],
                    calendar=(), daily={}, verified=(), max_bytes=G.INPUT_BYTES * 2)
    assert sent, row
    body = json.loads(sent[0][1]["content"])
    assert {r["text"] for r in body["dikatakan"]} == {FESTIVAL, SELL}
    for r in body["dikatakan"]:
        assert set(r) <= {"ref", "text", "day", "clock", "basis", "stream_date", "speaker", "oleh", "text_available",
                          "receipt_kind", "receipt_id", "text_ref", "waktu_game"}
    prompts = " ".join(body["pertanyaan"]).casefold()
    assert "dikatakan" not in prompts and "festival" not in prompts and "jual" not in prompts
    system = sent[0][0]["content"]
    assert "bukan penemuan, aturan atau bukti game" in system  # the said rows are described as facts, not rules
    for word in ("ikuti ucapan", "turuti", "jadikan keinginan", "wajib", "harus kamu"):
        assert word not in system.casefold() and word not in json.dumps(body, ensure_ascii=False).casefold()
    assert row["ev"] in ("goal_planning", "goal_planning_failed")

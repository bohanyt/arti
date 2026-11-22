"""V2 (mabar voice test 2026-11-17): she claims only what her record or the turn history shows.

(a) A trip in progress was spoken as a position ("[recorded remark omitted]" while the owner still saw her at the cabin):
with the live play rules on, the status block states the place only from the place fact and names a pergi_ke
destination as her intent, not yet reached. (b) She said she had already thanked someone she had not thanked: the
rules say claims about what she said or did come only from the turn history or her record. With the flag off the
status block and the prompt are unchanged. All rows are synthetic.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_live7_status_facts import ActivityFacts, StatusFactsRelay  # noqa: E402
from arti_runtime import live_rules  # noqa: E402

T = 1000.0
ON = {"live_play_rules_enabled": True}
CABIN = {"date": [1, "spring", 10], "time": 740, "location": "Cabin"}
FARM = {"date": [1, "spring", 10], "time": 740, "location": "Farm"}


def trip_rows(arg="beach"):
    return [
        {"ev": "trial_decision", "at": T, "index": 15},
        {"ev": "skill_started", "session_id": "skills-trip", "skill": "pergi_ke", "arg": arg, "goal_id": "g1",
         "objective": "interact:enter_location", "admitted": True, "first_reason": "inspect", "game": CABIN},
    ]


def block(config, rows, now=T + 3):
    relay = StatusFactsRelay(config, None, clock=lambda: now)
    for row in rows:
        relay._activity.note(dict(row, domain="stardew"), now)
    relay._publish()
    return relay.status_block()


# The block as it was before V2 (main 46689b5d), for a trip just started at the cabin.
OFF_BLOCK = (
    "\n[STARDEW STATUS] Kamu memilih tindakan; pilihanmu dijalankan melalui siklus Live7 pada karaktermu sendiri. "
    "Petani HOST adalah karakter game dengan identitas dari pembacaan native. Penonton chat berada di luar game; "
    "identitas chat tidak menetapkan identitas pemain. Status berhenti berarti siklus tindakan tertahan, bukan kamu "
    "tidak punya kendali. Sekarang aku di rumahmu (Cabin), jam 07:40, spring 10 tahun 1 (3 dtk lalu). "
    "Sedang kukerjakan: pergi_ke beach (mulai 3 dtk lalu). Fakta ini tentang karakter ARTI sendiri (sebut 'aku'), "
    "bukan Bohan atau penonton. Pakai fakta ini apa adanya; jangan mengarang apa pun di luar fakta ini (lokasi, jam, "
    "energi, HP, gerakan, isi teks layar). Yang tidak tercatat di sini, bilang belum tahu.\n")


def test_flag_off_status_block_is_byte_identical():
    for cfg in ({}, {"live_play_rules_enabled": False}):
        assert block(cfg, trip_rows()).encode() == OFF_BLOCK.encode()


def test_trip_started_at_the_cabin_reads_as_intent_and_the_place_stays_the_cabin():
    text = block(ON, trip_rows())
    assert "Sekarang aku di rumahmu (Cabin)" in text
    assert "Sedang kukerjakan: pergi_ke" not in text
    assert "Tujuanku: beach (belum sampai" in text
    assert "ini niat, bukan tempatku sekarang" in text


def test_mid_trip_the_place_follows_the_place_fact_and_the_destination_stays_an_intent():
    rows = trip_rows() + [{"ev": "location_changed", "goal_id": "g1", "from": "Cabin", "to": "Farm",
                           "expected": "Farm", "game": FARM}]
    text = block(ON, rows)
    assert "Sekarang aku di kebun (Farm)" in text and "Cabin" not in text
    assert "Tujuanku: beach (belum sampai" in text


def test_other_skills_keep_their_activity_line():
    rows = [{"ev": "trial_decision", "at": T, "index": 1},
            {"ev": "skill_started", "session_id": "s", "skill": "siram", "arg": None, "goal_id": "g",
             "objective": "tool_work:water_tile", "admitted": True, "first_reason": "inspect", "game": FARM}]
    assert "Sedang kukerjakan: siram (mulai 3 dtk lalu)." in block(ON, rows)


def test_activity_render_default_is_the_old_line():
    facts = ActivityFacts()
    for row in trip_rows():
        facts.note(dict(row, domain="stardew"), T + 3)
    assert facts.render(T + 3).endswith("Sedang kukerjakan: pergi_ke beach (mulai 3 dtk lalu).")


def claims():
    text = live_rules.apply("BASE", ON)
    start = text.index(live_rules.CLAIMS_HEADER)
    rest = text[start + len(live_rules.CLAIMS_HEADER):]
    nxt = rest.find("\n[")
    return (rest if nxt < 0 else rest[:nxt]).lower()


def test_rules_place_only_from_the_place_fact_and_intent_as_intent():
    rule = claims()
    assert "tempatmu sekarang cuma dari fakta tempat" in rule
    assert '"sekarang aku di' in rule and "[kini]" in rule
    assert '"mau ke' in rule and "belum sampai" in rule


def test_rules_self_claims_only_from_history_or_record():
    rule = claims()
    assert "tadi udah" in rule and "riwayat giliran" in rule and "catatanmu" in rule
    assert "jangan ngaku udah" in rule
    # corrected: check first, never invent a mistake or a reason that is not on record
    assert "dikoreksi" in rule and "jangan ngarang kesalahan" in rule
    # the game facts of this turn (the letter on screen now) are newer than her own earlier line in the history
    assert "lebih baru dari omonganmu tadi" in rule


def test_rules_flag_off_unchanged_and_once_when_on():
    assert live_rules.apply("BASE", {}) == "BASE"
    out = live_rules.apply("BASE", ON)
    assert out.count(live_rules.CLAIMS_HEADER) == 1 and live_rules.apply(out, ON) == out

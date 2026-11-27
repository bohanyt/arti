"""The quick checks before the case-blind searches change no result. Synthetic texts only."""
import random
import re

import pytest

from arti_runtime import experience_facts as E, fact_line as F, goal_ladder as G
from arti_runtime.stream_notes import _PREFIXES, said_words

PLACES = ["Town", "Community Center", "CommunityCenter", "Bus Stop", "Forest", "Wizard Tower", "Pierre's General Store",
          "Cindersap Forest", "The Mines", "Beach", "Straße", "Île", "Kantor 3", "a.b (c)", "Ilık", "İstasyon", "  ", "Desa\nKecil"]
WORDS = ("aku pergi ke town dan Forest lalu COMMUNITY center bus  stop\nbus stop wizard tower's PIERRE'S GENERAL STORE forests "
         "mines. themines the mines, STRASSE straße île ÎLE kantor 3 A.B (C) ılık ILIK istasyon İSTASYON desa kecil").split(" ")


def texts(seed, n=60):
    rng = random.Random(seed)
    return [" ".join(rng.choice(WORDS) for _ in range(rng.randint(0, 14))) for _ in range(n)]


@pytest.mark.parametrize("seed", range(12))
def test_the_names_she_read_are_found_as_before(seed):
    rows = [dict(kind=random.Random(seed * 100 + i).choice(("dialogue_read", "door_message", "text_read", "letter_opened")),
                 id="r%d" % i, text_ref="t%d" % i, game_day=dict(absolute=i), clock=600) for i in range(60)]
    body = dict(zip(("t%d" % i for i in range(60)), texts(seed)))
    places = [dict(id=p, label=p) if n % 3 else dict(id="Map%d" % n, label=p) for n, p in enumerate(PLACES)] + ["bukan", dict(label="x")]
    w = F.Witnesses(dict(world_graph=dict(current="Town", places=places)))
    w.rows, w.texts, w.current, w.entered = rows, body, "Town", {}

    def reference():
        out = {}
        read = [(r, w.text(r)) for r in w.matching(("menu_tab_opened", "door_message", "letter_opened", "calendar_day_read",
                                                    "event_watched", "quest_entry_read", "dialogue_read", "text_read"))]
        for p in places:
            if not isinstance(p, dict):
                continue
            identity = p.get("id")
            if identity == "Town":
                out.setdefault(identity, None)
            if identity in out:
                continue
            patterns = [F._name_pattern(n) for n in (identity, p.get("label")) if isinstance(n, str) and n]
            for r, text in read:
                if any(x.search(text) for x in patterns):
                    out[identity] = r
                    break
        return out

    assert w.names() == reference()
    assert any(v for v in w.names().values())


@pytest.mark.parametrize("seed", range(12))
def test_the_places_she_heard_of_are_found_as_before(seed):
    events = [dict(kind="text_read", id="e%d" % i, observation_ref="o%d" % i, text=t) for i, t in enumerate(texts(seed))]
    places = [dict(id="Map%d" % n, label=p) for n, p in enumerate(PLACES)] + [dict(id=p, label=None) for p in PLACES if p.strip()]
    catalog = dict(world_graph=dict(current="Town", places=places), experience_records=events, discoveries=[])
    found = E._place_sources(catalog)

    def reference():
        out = {p: "location_entered:" + p for p in E.entered(catalog) if isinstance(p, str)}
        for text, ref, evidence in E.text_evidence(catalog):
            for p in places:
                if p["id"] in out:
                    continue
                if any(isinstance(n, str) and n and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", str(text), re.I)
                       for n in (p["id"], p["label"])):
                    out.setdefault(p["id"], ref)
        return out

    assert found == reference() and len(found) > 1


@pytest.mark.parametrize("seed", range(6))
def test_what_was_said_touches_the_page_as_before(seed):
    rng = random.Random(seed)
    tags = set(rng.sample(["town", "farm", "mine", "tidur", "siram", "tanam", "tangkap_ikan", "inventori", "panen", "beach"], 4))
    said = ["ayo memancing nanti", "kebunnya disiram dulu", "tasku penuh", "tidurnya jam berapa", "ke Town yuk", "menambang di mine",
            "hadiahnya buat siapa", "", "petinya di mana", "makan dulu", "ngobrolin ladang"] + texts(seed, 20)
    for text in said:
        words = said_words(text)
        expected = bool(words & tags) or any(word.startswith(prefix + stem) for word in words for prefix in _PREFIXES
                                             for stem, touched in G.SAID_TOUCHES.items() if set(touched) & tags)
        assert G.said_touches(text, tags) == expected, (text, tags)


def test_the_quick_check_never_hides_a_match():
    letters = "ikszaeouIKSZßẞſKİıÉéÎîµμÅÅ"
    for a in letters:
        for b in letters:
            if re.search(re.escape(a), b, re.I):
                assert F.fold(a) in F.fold(b), (a, b)

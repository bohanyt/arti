"""Tests isi tas masuk prompt — supaya Arti berhenti bikin barang yang sudah ada.

Tanpa tahu isi tasnya, Arti bisa bikin stone_pickaxe dan crafting_table berulang
kali. Bukan bug craft — mesinnya bekerja tanpa error. Masalahnya dia TIDAK PERNAH
diberi tahu isi tasnya, jadi tiap kali "bikin pickaxe" terasa ide baru.

Pengulangan yang sama dengan keluhan lama "[recorded remark omitted]", cuma pindah
dari obrolan ke aksi.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_minecraft as am  # noqa: E402

STATUS = {
    "health": 20, "food": 18, "task": "roam", "dim": "overworld",
    "pos": {"x": 1, "y": 2, "z": 3},
    "inv": [{"name": "stone_pickaxe", "count": 1},
            {"name": "stone_pickaxe", "count": 1},
            {"name": "crafting_table", "count": 11},
            {"name": "cobblestone", "count": 64}],
    "held": {"name": "stone_pickaxe"},
}


def test_stacks_of_the_same_item_are_merged():
    """Dua slot stone_pickaxe = "2 stone_pickaxe", bukan dua entri terpisah —
    kalau tidak, tas 40 slot jadi 40 entri dan sisa prompt tenggelam."""
    assert "2 stone_pickaxe" in am.ringkas_tas(STATUS)
    assert am.ringkas_tas(STATUS).count("stone_pickaxe") == 1


def test_sorted_by_count_so_the_useful_stuff_survives_the_cap():
    t = am.ringkas_tas(STATUS)
    assert t.index("cobblestone") < t.index("stone_pickaxe")


def test_the_list_is_capped_and_says_how_much_was_hidden():
    """Tas penuh tanpa batas akan menenggelamkan sisa prompt."""
    banyak = dict(STATUS)
    banyak["inv"] = [{"name": f"barang_{i}", "count": i + 1} for i in range(20)]
    t = am.ringkas_tas(banyak)
    assert t.count(",") <= am.TAS_MAKS_JENIS
    assert "jenis lain" in t, "sisa yang dipotong tidak disebut"


def test_an_empty_bag_is_stated_not_omitted():
    """Kalau baris tasnya hilang begitu saja, dia bisa MENGARANG punya bahan."""
    assert am.ringkas_tas({"inv": []}) == "Tasmu KOSONG."


def test_missing_data_is_silent_not_a_lie():
    """`inv` belum pernah datang != tas kosong. Menyebut "KOSONG" di situ
    membuatnya menyerah padahal mungkin penuh."""
    assert am.ringkas_tas({}) == ""
    assert am.ringkas_tas(None) == ""


def test_garbage_entries_do_not_crash_the_prompt():
    rusak = {"inv": [None, "bukan dict", {"count": 3}, {"name": ""},
                     {"name": "batu", "count": "banyak"},
                     {"name": "kayu", "count": 2}]}
    t = am.ringkas_tas(rusak)
    assert "kayu" in t and "2 kayu" in t


def test_the_bag_reaches_the_prompt_block_with_a_do_not_read_rule():
    """Pelajaran yang sama dengan bar HP: apa pun yang disodorkan tiap giliran
    akan terucap kalau tidak dilarang. Live 6 Agustus: 12 dari 20 jawaban
    menyebut darah karena angkanya masuk prompt setiap giliran."""
    out = am.format_context(STATUS, [], ttl_sec=60.0, now=100.0)
    assert "Isi tasmu:" in out
    assert "JANGAN membacakan" in out
    assert "sudah kamu punya" in out, "tidak ada larangan bikin barang duplikat"


def test_what_she_holds_is_named_separately():
    """Yang dipegang itu satu-satunya bagian tas yang KELIHATAN dari kamera."""
    out = am.format_context(STATUS, [], ttl_sec=60.0, now=100.0)
    assert "Di tanganmu sekarang: stone_pickaxe." in out


def test_held_accepts_both_shapes():
    """statusEvent bisa mengirim `held` sebagai dict atau string."""
    for held in ({"name": "iron_sword"}, "iron_sword"):
        st = dict(STATUS, held=held)
        assert "Di tanganmu sekarang: iron_sword." in am.format_context(
            st, [], ttl_sec=60.0, now=100.0)


def test_no_bag_line_when_there_is_no_status():
    assert "Isi tasmu" not in am.format_context(None, [], ttl_sec=60.0, now=100.0)


# --- dorongan taktis: kapan melawan, kapan berlindung --------------------

def _st(**kw):
    st = {"health": 20, "food": 18, "task": "roam", "dim": "overworld",
          "pos": {"x": 1, "y": 2, "z": 3}, "inv": []}
    st.update(kw)
    return st


def test_she_is_told_she_may_fight_when_healthy():
    """Tanpa petunjuk kapan, dia bisa terus `kabur` dan tidak pernah `serang`
    walau mati ke zombie. Kemampuannya terbukti jalan; yang tidak ada adalah
    apa pun yang memberitahunya KAPAN memilih yang mana."""
    s = am.saran_taktis(_st(nearby_hostiles=[{"kind": "zombie", "distance": 4}]))
    assert any("[MC: serang]" in x for x in s)
    assert any("BOLEH melawan" in x for x in s)


def test_no_fight_nudge_when_health_is_low():
    """Di bawah ambang, kabur memang jawaban yang benar — jangan dibujuk mati."""
    s = am.saran_taktis(_st(health=6, nearby_hostiles=[{"kind": "zombie", "distance": 4}]))
    assert not any("serang" in x for x in s)


def test_creepers_are_pushed_away_from_not_toward():
    """Memukul creeper jarak dekat = meledak di mukanya, berapa pun darahnya."""
    s = am.saran_taktis(_st(nearby_hostiles=[{"kind": "creeper", "distance": 3}]))
    assert s and "JANGAN dilawan" in s[0]
    assert not any("BOLEH melawan" in x for x in s)


def test_the_nearest_hostile_decides_not_a_random_one():
    s = am.saran_taktis(_st(nearby_hostiles=[{"kind": "zombie", "distance": 12},
                                             {"kind": "creeper", "distance": 2}]))
    assert "creeper" in s[0], "yang paling dekat harus menentukan"


def test_shelter_is_suggested_at_night_with_enough_blocks():
    s = am.saran_taktis(_st(is_night=True, inv=[{"name": "cobblestone", "count": 64}]))
    assert any("[MC: bangun cobblestone]" in x for x in s)


def test_no_shelter_nudge_without_enough_blocks():
    """Menyuruh bangun tanpa bahan cuma menghasilkan kegagalan yang bisa
    diramalkan — dan Arti menceritakannya ke penonton."""
    s = am.saran_taktis(_st(is_night=True, inv=[{"name": "dirt", "count": 5}]))
    assert not any("bangun" in x for x in s)


def test_no_nudge_at_all_when_the_situation_is_calm():
    """Sengaja BERSYARAT: kalau muncul tiap giliran, dia ikut jadi kebisingan
    yang diabaikan — pelajaran dari bar HP."""
    assert am.saran_taktis(_st()) == []
    assert am.saran_taktis(None) == []


def test_the_nudges_reach_the_prompt_block():
    out = am.format_context(
        _st(is_night=True, nearby_hostiles=[{"kind": "zombie", "distance": 3}],
            inv=[{"name": "cobblestone", "count": 40}]),
        [], ttl_sec=60.0, now=100.0)
    assert "[MC: serang]" in out and "[MC: bangun cobblestone]" in out


def test_broken_hostile_entries_do_not_crash():
    for buruk in ([None], ["zombie"], [{}], [{"distance": 3}]):
        am.saran_taktis(_st(nearby_hostiles=buruk))


# --- LAPAR: dia harus bisa cari makan sendiri ------------------------------

def test_hunting_is_suggested_when_hungry_with_an_animal_in_sight():
    """Tujuannya bertahan hidup, dan kelaparan paling susah
    ditangani. Rantainya sudah lengkap dan terbukti jalan — `serang <hewan>`
    membunuhnya, dagingnya dipungut otomatis, dan `eat` menerima daging MENTAH
    karena `foodsByName` tidak membedakan matang. Yang tidak ada cuma apa pun
    yang memberitahunya melakukan itu saat lapar."""
    s = am.saran_taktis(_st(food=8, nearby_animals=[{"kind": "cow", "distance": 8}]))
    assert any("[MC: serang cow]" in x for x in s)
    assert any("[MC: eat]" in x for x in s)


def test_the_closest_animal_is_named_with_its_distance():
    s = am.saran_taktis(_st(food=6, nearby_animals=[
        {"kind": "chicken", "distance": 15}, {"kind": "pig", "distance": 3}]))
    assert "pig" in s[0] and "3 blok" in s[0]


def test_hungry_without_animals_is_told_to_go_look():
    """Menyuruh `serang` tanpa sasaran cuma menghasilkan `tidak_ada_sasaran`
    dan satu giliran terbuang."""
    s = am.saran_taktis(_st(food=8))
    assert s and "Cari hewan dulu" in s[0]
    assert "[MC: serang cow]" not in s[0]


def test_no_hunting_nudge_when_the_bag_already_has_food():
    """Berburu padahal tasnya penuh roti itu boros dan terlihat bodoh."""
    for makanan in ("bread", "cooked_beef", "apple", "sweet_berries"):
        s = am.saran_taktis(_st(food=8, inv=[{"name": makanan, "count": 2}],
                                nearby_animals=[{"kind": "cow", "distance": 4}]))
        assert not any("serang" in x for x in s), makanan


def test_no_hunting_nudge_when_full():
    assert am.saran_taktis(_st(food=20, nearby_animals=[{"kind": "cow", "distance": 4}])) == []


def test_hunger_and_night_can_both_speak():
    """Dua keadaan mendesak sekaligus tidak boleh saling menutupi."""
    s = am.saran_taktis(_st(food=6, is_night=True,
                            inv=[{"name": "cobblestone", "count": 64}],
                            nearby_animals=[{"kind": "pig", "distance": 3}]))
    assert any("serang pig" in x for x in s) and any("bangun" in x for x in s)


def test_broken_animal_entries_do_not_crash():
    for buruk in ([None], ["cow"], [{}], [{"distance": 3}]):
        am.saran_taktis(_st(food=8, nearby_animals=buruk))

import json

import pytest

from scripts.livepath import mabar, mabar_memory


def jl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def run_and_mind(tmp_path):
    run, mind = tmp_path / "run", tmp_path / "mind"
    run.mkdir()
    (run / "MODE.json").write_text(json.dumps(dict(mode="mabar", utc="2026-11-10T12:30:00Z")))
    jl(run / "owner_reactions.jsonl", [dict(owner_text="halo"), dict(owner_text="wah")])
    jl(mind / "stardew/ledger.jsonl", [
        dict(kind="native_story", facts=dict(witness="host", event_type="Event", event_id="60367", location="Farm",
                                             native_date="1:spring:0", date="1:spring:1",
                                             lines=[dict(speaker="Lewis", text="Ah, the new farmer!")])),
        dict(kind="own_look", facts=dict(verified=True, lines=[dict(speaker="profil native", text="Namamu Arti."),
                                                               dict(speaker="profil native", text="Bajumu Mint Blouse.")])),
        dict(kind="letter"), dict(kind="quest"),
    ])
    jl(mind / "stardew/discoveries.jsonl", [
        dict(kind="npc_met", text="Lewis: met (personal friendship record)", day_label="Y1 spring 1"),
        dict(kind="item_obtained", text="Parsnip Seeds: Plant these in the spring.", day_label="Y1 spring 1"),
        dict(kind="connection_unlocked", text="noise", day_label="Y1 spring 1"),
        dict(kind="item_seen", text="Hoe: Used to dig", day_label="Y1 spring 1"),
    ])
    return run, mind


def test_build_writes_only_verified_facts_into_the_run_folder(run_and_mind):
    run, mind = run_and_mind
    r = mabar_memory.build(run, mind)
    assert r["session"] == "2026-11-10-mabar" and r["opening_episodes"] == 1 and r["look_verified"]
    note = (run / "vault-export/2026-11-10-mabar.md").read_text(encoding="utf-8")
    assert '"Ah, the new farmer!"' in note and "namanya Arti" in note and "kenal (catatan pertemanan native): Lewis" in note
    assert "Parsnip Seeds" in note and "noise" not in note and "Hoe" not in note
    assert "Owner ngomong ke aku: 2 kali" in note and "halo" not in note  # counts only, never the owner's text
    rows = [json.loads(l) for l in (run / "diary-export.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == ["2026-11-10-mabar-00", "2026-11-10-mabar-01", "2026-11-10-mabar-02"]
    assert all(r["status"] == "draft" and r["v"] == 1 and r["names"] == [] for r in rows)
    assert rows[0]["kind"] == "summary" and len(rows[0]["text"]) <= 400 and all(len(r["text"]) <= 280 for r in rows[1:])


def test_unverified_look_is_stated_unknown(run_and_mind):
    run, mind = run_and_mind
    rows = [json.loads(l) for l in (mind / "stardew/ledger.jsonl").read_text().splitlines()]
    rows[1]["facts"]["verified"] = False
    jl(mind / "stardew/ledger.jsonl", rows)
    mabar_memory.build(run, mind)
    note = (run / "vault-export/2026-11-10-mabar.md").read_text(encoding="utf-8")
    assert "Belum terverifikasi" in note and "Namaku Arti." not in note


def test_export_creates_only_if_absent_and_appends_new_diary_ids(run_and_mind, tmp_path):
    run, mind = run_and_mind
    mabar_memory.build(run, mind)
    stream = tmp_path / "stream-folder"
    (stream / "vault/sessions").mkdir(parents=True)
    jl(stream / "data/diary/arti_diary.jsonl", [dict(id="2026-11-01-default-00", status="ok", text="lama")])
    first = mabar_memory.export_to_live(run, stream)
    assert first["note"] == "created" and first["diary_appended"] == 3
    (stream / "vault/sessions/2026-11-10-mabar.md").write_text("owner edited", encoding="utf-8")
    second = mabar_memory.export_to_live(run, stream)
    assert second["note"] == "exists, untouched" and second["diary_appended"] == 0
    assert (stream / "vault/sessions/2026-11-10-mabar.md").read_text(encoding="utf-8") == "owner edited"
    lines = (stream / "data/diary/arti_diary.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["text"] == "lama" and len(lines) == 4


def test_export_refuses_without_a_live_vault(run_and_mind, tmp_path):
    run, mind = run_and_mind
    mabar_memory.build(run, mind)
    with pytest.raises(RuntimeError, match="vault/sessions"):
        mabar_memory.export_to_live(run, tmp_path / "nowhere")


# --- item 1: her look from the native readback only ---------------------------------------------------------------

PROFILE = dict(name="Arti", favorite_thing="Kopi", farm_name="smoketwo", is_customized=True, gender="female", skin=7,
               hair=11, accessory=-1, shirt_id="1002", pants_id="1", eye_color="#476BB2", hair_color="#C15A32",
               pants_color="#1A3466")
OPTIONS = dict(shirt=[dict(id="1002", label="Mint Blouse")], pants=[dict(id="1", label="Shorts")])


def test_readback_lines_come_from_the_profile_not_the_intent():
    lines = mabar.readback_lines(PROFILE, OPTIONS, [])
    text = " ".join(lines)
    assert "Namamu Arti." in text and "Mint Blouse" in text and "Shorts" in text
    assert "model rambut nomor 12" in text and "kulit nomor 8" in text and "Tanpa aksesori" in text
    assert "biru" in text and "#C15A32" in text


def test_flagged_or_missing_fields_read_as_not_verified():
    lines = mabar.readback_lines(dict(PROFILE, shirt_id=None), OPTIONS, ["hair_color: expected #x got #y"])
    assert "hair_color belum terverifikasi." in lines and "shirt_id belum terverifikasi." in lines
    assert mabar.readback_lines(None, OPTIONS) == ["Karaktermu belum terverifikasi dari profil native; rinciannya belum diketahui."]
    assert mabar.readback_lines(dict(PROFILE, is_customized=False), OPTIONS)[0].startswith("Karaktermu belum")


@pytest.mark.parametrize("hex_code,word", [("#476BB2", "biru"), ("#C15A32", "oranye"), ("#1A3466", "biru gelap"),
                                           ("#000000", "hitam"), ("#FFFFFF", "putih"), ("bad", None),
                                           ("#7A4434", "cokelat"), ("#C0392B", "merah")])
def test_color_word_is_a_fixed_band(hex_code, word):
    assert mabar.color_word(hex_code) == word


def test_status_line_carries_verified_self_facts(tmp_path):
    from arti_live7_status_facts import StatusFactsRelay
    path = tmp_path / "self_facts.json"
    relay = StatusFactsRelay(dict(live7_self_facts_file=str(path)), None, clock=lambda: 0.0)
    assert relay.status_block() == ""
    path.write_text(json.dumps(dict(lines=["Namamu Arti.", "Bajumu Mint Blouse."])), encoding="utf-8")
    relay._publish()
    block = relay.status_block()
    assert "dibaca dari profil native" in block and "Namamu Arti." in block and "Mint Blouse" in block


# --- isolated live lookup and the chat flag ---------------------------------------------------------------

def test_reply_lane_pins_web_search_only_when_configured():
    import arti_codex_agent
    assert arti_codex_agent.codex_overrides({}) == ('sandbox_mode="read-only"',)  # live default unchanged
    assert 'web_search="disabled"' in arti_codex_agent.codex_overrides(dict(codex_web_search="disabled"))
    with pytest.raises(ValueError):
        arti_codex_agent.codex_overrides(dict(codex_web_search="on"))


def test_mabar_config_allows_only_model_requested_codex_lookup_and_opt_in_chat(tmp_path):
    from scripts.livepath import bridge as lp_bridge
    cfg = mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1)
    assert cfg["codex_web_search"] == "disabled" and cfg["web_lookup_provider_chain"] == ["codex"]
    for key in ("web_lookup_enabled", "web_lookup_codex_enabled", "web_lookup_model_decides",
                "web_lookup_announce", "codex_agent_enabled", "vision_enabled"):
        assert cfg[key] is True
    for key in ("web_lookup_regex_enabled", "host_web_topic_enabled", "youtube_chat_enabled",
                "vision_runtime_on_start", "vision_background_poll"):
        assert cfg[key] is False
    chat = mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1, youtube_video_id="abc123XYZ")
    assert chat["youtube_chat_enabled"] is True and chat["youtube_video_id"] == "abc123XYZ"
    path = tmp_path / "c.json"
    for good in (cfg, chat):
        path.write_text(json.dumps(good), encoding="utf-8")
        lp_bridge.load_config(path)
    for bad in (dict(chat, youtube_video_id=""), dict(cfg, web_lookup_enabled=False),
                dict(cfg, codex_web_search="cached"), dict(cfg, web_lookup_regex_enabled=True),
                dict(cfg, web_lookup_announce=False), dict(cfg, host_web_topic_enabled=True),
                dict(cfg, web_lookup_provider_chain=["codex", "groq_compound"]),
                dict(cfg, vision_background_poll=True), dict(cfg, vision_runtime_on_start=True)):
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            lp_bridge.load_config(path)


def test_cycles_follow_the_newest_and_rejoin_opens_the_next(tmp_path):
    run = tmp_path / "run"
    assert mabar.next_cycle(run).name == "c1" and mabar.current_cycle(run).name == "c1"
    for n in (1, 2, 10):
        (run / "runtime/live7" / ("c%d" % n)).mkdir(parents=True)
    assert mabar.current_cycle(run).name == "c10" and mabar.next_cycle(run).name == "c11"


def test_farmer_creation_is_a_moment_not_a_trait_list(run_and_mind):
    """Owner 2026-11-11: the look row read as a character sheet ("[recorded remark omitted]") and she announced it
    as "lore". It is something that happened that evening, in her words."""
    import re
    run, mind = run_and_mind
    mabar_memory.build(run, mind)
    note = (run / "vault-export/2026-11-10-mabar.md").read_text(encoding="utf-8")
    rows = [json.loads(l) for l in (run / "diary-export.jsonl").read_text(encoding="utf-8").splitlines()]
    moment = [r["text"] for r in rows if "farmer-ku sendiri" in r["text"] and r["kind"] == "moment"][0]
    assert moment.startswith("Waktu masuk") and "namanya Arti" in moment and "baju Mint Blouse" in moment
    banned = re.compile(r"Karakterku|Hal favoritku|Namaku|Bajuku|Rambutku|lore|branding|persona|trait|sifatku|kode warna", re.I)
    assert not banned.search(note) and not banned.search(" ".join(r["text"] for r in rows))


def test_continued_evening_does_not_repeat_the_creation(run_and_mind):
    run, mind = run_and_mind
    (run / "continued.json").write_text(json.dumps(dict(farm="smoketwo", evening=2)), encoding="utf-8")
    mabar_memory.build(run, mind)
    note = (run / "vault-export/2026-11-10-mabar.md").read_text(encoding="utf-8")
    assert "bikin farmer" not in note


def test_a_farm_tag_is_one_line_under_the_title_and_nothing_else_changes(run_and_mind):
    from arti_runtime import vault_farm
    run, mind = run_and_mind
    now = mabar_memory.dt.datetime(2026, 11, 10, 20, 0, tzinfo=mabar_memory.WIB)
    mabar_memory.build(run, mind, now=now)
    plain = (run / "vault-export/2026-11-10-mabar.md").read_text(encoding="utf-8")
    line = vault_farm.tag_line("Ladang", ["c" * 64])
    mabar_memory.build(run, mind, now=now, farm_tag=line)
    tagged = (run / "vault-export/2026-11-10-mabar.md").read_text(encoding="utf-8")
    assert tagged.splitlines()[1] == line and tagged.replace(line + "\n", "", 1) == plain
    assert vault_farm.parse_tag(tagged) == dict(farm="Ladang", scope_hashes=("c" * 64,))

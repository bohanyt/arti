"""UNIT_TESTED, ADR 119: Groq fallback must not lose what is true when things are already going wrong."""
import arti_bridge as bridge
from arti_runtime.soul_block import SOUL_MARKER

FACTS = ("[STARDEW] Kamu main karaktermu sendiri.\n[STARDEW STATUS] status\n[NIATKU] niat\n[KINI] kini\n"
         '[NATIVE_FACTS] {"a": 1}\n[PLAN_DATES] tanggal')
CURRENT = '[Pesan/Panggilan Sekarang:]\n"halo"\n\nJawab sebagai Arti.'


def system(base=3000, soul=8000, memory=2000):
    return ("B" * base + SOUL_MARKER + "\nkepala\n" + "soul line\n" * (soul // 10)
            + "\n\n[MEMORI TERBARU (hari ini):]\n" + "m" * memory + "\n\n[HARI INI]\ntanggal\n\n" + FACTS)


def user(history=4000):
    return "[CATATAN SEJARAH STREAM:]\nawal\n" + "h" * history + "\nbaris terbaru\n\n" + CURRENT


def assert_facts(text):
    for head in ("[STARDEW] Kamu", "[STARDEW STATUS]", "[NIATKU]", "[KINI]", "[NATIVE_FACTS]", "[PLAN_DATES]"):
        assert head in text, head


def test_history_goes_first_and_alone_when_that_is_enough():
    s, u = system(), user(history=6000)
    out_s, out_u = bridge._trim_llm_payload(s, u, max_chars=len(s) + len(u) - 500)
    assert out_s == s
    assert CURRENT in out_u and "baris terbaru" in out_u and "history dipangkas" in out_u
    assert "awal" not in out_u
    assert len(out_s) + len(out_u) <= len(s) + len(u) - 500


def test_soul_and_facts_survive_while_history_and_optional_blocks_can_pay():
    s, u = system(memory=6000), user(history=3000)
    out_s, out_u = bridge._trim_llm_payload(s, u, max_chars=len(s) - 5000)
    assert CURRENT in out_u and "awal" not in out_u
    assert "m" * 6000 not in out_s  # optional memory paid
    assert "soul line\n" * 790 in out_s  # the soul did not
    assert_facts(out_s)


def test_soul_tail_goes_before_facts_and_base_on_whole_lines():
    s, u = system(base=3000, soul=8000, memory=0), user(history=0)
    out_s, out_u = bridge._trim_llm_payload(s, u, max_chars=9000)
    assert CURRENT in out_u
    assert_facts(out_s)
    assert "B" * 3000 in out_s and SOUL_MARKER in out_s
    assert out_s.count("soul line") < 790
    soul_part = out_s.split(SOUL_MARKER)[1].split("\n[STARDEW]")[0]
    assert {line for line in soul_part.splitlines() if line} == {"kepala", "soul line"}


def test_when_nothing_else_is_left_facts_still_survive_and_base_keeps_its_floor():
    s, u = system(base=20000, soul=100, memory=0), user(history=0)
    out_s, out_u = bridge._trim_llm_payload(s, u, max_chars=1000)
    assert_facts(out_s)
    assert CURRENT in out_u
    assert "B" * 4000 in out_s and "B" * 4100 not in out_s


def test_full_size_turn_keeps_the_soul_head_and_the_facts():
    """pkg71/pkg72 note: on a full-size turn the retry had dropped [JIWA ARTI] entirely."""
    facts = FACTS.replace('{"a": 1}', '{"a": "' + "f" * 9000 + '"}')
    s = ("B" * 6900 + SOUL_MARKER + "\nkepala\n" + "soul line\n" * 840 + "\n\n[HARI INI]\ntanggal\n\n" + facts)
    out_s, out_u = bridge._trim_llm_payload(s, user(history=3000))
    assert_facts(out_s)
    assert "f" * 9000 in out_s and CURRENT in out_u
    assert SOUL_MARKER in out_s and "kepala" in out_s
    head = out_s.split(SOUL_MARKER)[1].split("\n[STARDEW]")[0]
    assert 1500 < len(head) <= 2000
    assert {line for line in head.splitlines() if line} == {"kepala", "soul line"}
    assert "B" * 4000 in out_s  # base gives way before the soul's head does


def test_no_soul_file_keeps_the_old_cut():
    s = "B" * 20000 + "\n\n" + FACTS
    out_s, _ = bridge._trim_llm_payload(s, user(history=0), max_chars=1000)
    assert_facts(out_s)
    assert "B" * 4000 in out_s and "B" * 4100 not in out_s


def test_small_payload_is_untouched():
    assert bridge._trim_llm_payload("a", "b") == ("a", "b")


def test_normal_trim_also_keeps_the_fact_lines():
    prompt = "x" * 9000 + "\n" + FACTS
    trimmed = bridge.trim_system_prompt_for_llm(prompt, {"llm_system_prompt_max_chars": 1000})
    assert_facts(trimmed)


def test_ordinary_turn_with_soul_and_facts_keeps_question_routing():
    cfg = dict(groq_models=["fast-m", "strong-m", "rare-m"], groq_model_fast="fast-m", groq_model_strong="strong-m",
               groq_model_medium="fast-m", groq_model_rare="rare-m", smart_groq_routing=True,
               groq_prompt_char_soft_cap=bridge.CONFIG["groq_prompt_char_soft_cap"])
    # a51 maxima (base, soul, turn facts, reserved) plus RAG, today and history: still ordinary.
    ordinary = 6928 + 8381 + 10114 + 2927 + 1500 + 3000
    assert ordinary <= cfg["groq_prompt_char_soft_cap"]
    assert bridge.pick_groq_model("kenapa bisa gitu?", cfg, prompt_chars=ordinary) == "strong-m"
    assert bridge.pick_groq_model("kenapa bisa gitu?", cfg, prompt_chars=cfg["groq_prompt_char_soft_cap"] + 1) == "fast-m"

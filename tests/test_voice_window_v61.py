"""V6.1: her voice turn follows the talk in order.

V6.1: the grouped history (the owner's last 5 lines, her last 3, one line per viewer) lost the order of the talk:
an owner line just before a reply could drop while the reply stayed, and an older plan could be gone when he
later repeated a part of it. Under ``live_voice_fixes_v1`` (mabar) the turn's history is one chronological
window. All names and lines below are synthetic.
"""
import time

import arti_bridge as b
from arti_runtime import voice_window as vw

NOW = time.mktime((2026, 1, 1, 20, 0, 0, 0, 0, -1))
OWNER, ARTI = "Streamer", "Arti (VTuber)"
VIEWER_A, VIEWER_B = "Viewer Sintetis Satu (YouTube)", "Viewer Sintetis Dua (YouTube)"
PLAN = "Rencana malam ini: panen dulu, terus ke pantai cari kerang ya."
SETUP = "Kamu tahu nggak kenapa ikan nggak bisa main piano?"
PUN = "Soalnya berarti dia cuma punya sirip, Arti."


def line(ago, label, text, now=NOW):
    return "[%s] [%s] %s" % (time.strftime("%H:%M:%S", time.localtime(now - ago)), label, text)


def ten_minutes():
    """Owner, her and chat lines over ten and a half minutes, oldest first."""
    return [
        line(960, OWNER, "Pagi tadi kita siram dulu ya."),          # 16 min: beyond the owner's reach (V6.4: 15)
        line(480, OWNER, PLAN),                                     # 8 min: his plan
        line(470, ARTI, "Siap, panen dulu ya."),     # 7.8 min: her line, outside the window
        line(400, VIEWER_A, "halo semua"),                          # outside the window
        line(330, OWNER, "Itu sapi punya siapa?"),                  # 5.5 min: owner, kept
        line(300, ARTI, "Kayaknya punya tetangga."),                # outside the window
        line(200, OWNER, "Coba ke sungai dulu."),
        line(190, VIEWER_B, "mancing!"),
        line(180, ARTI, "Oke, aku ke sungai."),
        line(170, "System", "Bohan AFK"),                           # system lines stay out, as before
        line(120, OWNER, SETUP),
        line(110, OWNER, PUN),
        line(100, ARTI, "Hahaha, garing!"),
        line(60, VIEWER_A, "wkwk"),
        line(30, OWNER, "Ayo lanjut."),
        line(5, ARTI, "Lanjut!"),
    ]


def block(text):
    """The conversation lines of a rendered window, header dropped."""
    lines = text.splitlines()
    start = lines.index(vw.HEADER)
    return lines[start + 1:]


def test_one_window_keeps_the_order_the_8_minute_owner_line_and_the_line_before_the_pun():
    history = ten_minutes()
    got = block(vw.render(history, now=NOW))
    assert got == [history[i] for i in (1, 4, 6, 7, 8, 10, 11, 12, 13, 14, 15)]
    assert got.index(history[11]) == got.index(history[10]) + 1           # the setup sits right before the pun
    assert history[0] not in got and "System" not in "\n".join(got)


def test_grouped_history_lost_both_lines_before_v61(monkeypatch):
    # Flag off: today's grouped history, kept as it is. It is what dropped the plan and split setup and pun.
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", False)
    monkeypatch.setattr(b, "stream_history", ten_minutes())
    old = b.get_categorized_history()
    assert PLAN not in old and vw.HEADER not in old
    assert old.startswith("=== OMONGAN STREAMER TERAKHIR ===")


def test_the_bridge_renders_the_window_only_under_the_mabar_flag(monkeypatch):
    now = time.time()
    history = [line(ago, label, text, now=now) for ago, label, text in (
        (420, OWNER, PLAN), (400, ARTI, "Siap."), (90, VIEWER_A, "halo"), (60, OWNER, SETUP), (50, OWNER, PUN))]
    monkeypatch.setattr(b, "stream_history", history)
    monkeypatch.setitem(b.CONFIG, "live_voice_fixes_v1", True)
    assert b.get_categorized_history() == vw.render(history, now=now)
    assert block(b.get_categorized_history()) == [history[i] for i in (0, 2, 3, 4)]


def test_the_window_holds_at_most_the_newest_24_lines():
    history = [line(200 - 5 * i, (OWNER, ARTI, VIEWER_A)[i % 3], "baris %d" % i) for i in range(40)]
    assert block(vw.render(history, now=NOW)) == history[-24:]


def test_the_owner_keeps_his_last_8_lines_from_the_last_10_minutes():
    older = [line(590 - 20 * i, OWNER, "rencana %d" % i) for i in range(15)]   # 9.8 to 5.2 min ago
    recent = [line(100 - 10 * i, ARTI, "jawab %d" % i) for i in range(5)]
    got = block(vw.render(older + recent, now=NOW))
    assert got == older[-8:] + recent


def test_the_cap_drops_the_oldest_lines_first_and_keeps_the_newest():
    history = [line(230 - 9 * i, (OWNER, ARTI)[i % 2], ("kalimat %02d " % i) + "x" * 280) for i in range(24)]
    text = vw.render(history, now=NOW)
    got = block(text)
    assert len(text) <= vw.MAX_CHARS
    assert got == history[-len(got):] and 8 <= len(got) < 24


def test_a_single_line_longer_than_the_cap_is_cut_not_lost():
    text = vw.render([line(5, ARTI, "y" * (vw.MAX_CHARS + 500))], now=NOW)
    assert len(text) <= vw.MAX_CHARS and block(text)[0].startswith(line(5, ARTI, "yyy"))


def test_ages_count_across_midnight():
    now = time.mktime((2026, 11, 18, 0, 2, 0, 0, 0, -1))
    history = [line(420, OWNER, PLAN, now=now), line(400, ARTI, "Siap.", now=now),
               line(210, ARTI, "Udah lewat tengah malam.", now=now), line(20, OWNER, "Ayo tidur.", now=now)]
    assert history[0].startswith("[23:55:00]")
    assert block(vw.render(history, now=now)) == [history[0], history[2], history[3]]


def test_a_line_stamped_just_after_now_is_new_not_a_day_old():
    # A clock stepped back a second between recording a line and building the turn must not empty the window.
    history = [line(400, OWNER, PLAN), line(60, ARTI, "Siap."), line(-1, OWNER, "Ayo.")]
    assert block(vw.render(history, now=NOW)) == history


def test_game_text_keeps_its_own_block_and_an_empty_history_reads_as_before():
    reads = [line(600 - i, "Game read", "surat %d" % i) for i in range(10)]
    text = vw.render(reads + [line(10, OWNER, "Baca dong.")], now=NOW)
    assert text.splitlines()[:9] == ["=== NATIVE TEXT PREVIOUSLY READ IN HER GAME ==="] + reads[-8:]
    assert block(text) == [line(10, OWNER, "Baca dong.")]
    assert vw.render([line(5, "System", "mulai")], now=NOW) == "(Belum ada history)"

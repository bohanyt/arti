"""ADR 243: her raw hands on every page: plain presses that go straight to the game, not tied to a listed thing.

The program was built as a closed world: a game thing had to be listed before she could touch it. The game is an
open world. Raw hands are the inversion: on every page where the game takes input from her she has the game's own
hands, and her own judgement decides what to press on; skills stay as shortcuts.

- Anywhere she can move: step one tile (``langkah <dir>``), face a neighbouring tile (``hadap x,y``), press the action
  button on the faced tile (``aksi depan``) and use the held tool or item on it (``pakai``). The last two need the
  mod's own generic target for the faced tile (``world_buttons``, ``GenericWorldInputEnabled``); the mod's admission,
  its pad denylist and its tool-tile guard decide what a press may do, exactly as before.
- In a menu or a minigame: the game's own close (ADR 237 ``exit_action``), its Escape and confirm keys, a question's
  own declining answer (ADR 241 rung 1) and the parts the game draws. Those choices already exist on a menu page; this
  module only says in one plain line what each of them does in the game's terms.

A raw press is verified by reading the game afterwards; "nothing changed" is a fact on her next page (``tangan_hasil``),
never a fault, a refusal or a count against her. Behind ``stardew_live7_raw_hands`` (on in the live profile and the rig).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from math import dist, isfinite

SETTING = "stardew_live7_raw_hands"
KEY = "tangan"  # the block's plain line, on every page she is given
RESULT = "tangan_hasil"  # what the game answered to her last raw press, once, on her next page
FRONT = "depan"  # `aksi depan`: the action button on the tile she faces (an arg, so a listed `aksi` never hides it)
DIRS = ("up", "right", "down", "left")
STEPS = ((0, -1), (1, 0), (0, 1), (-1, 0))
WORLD_SKILLS = ("langkah", "hadap", "pakai")  # every call of these is a raw hand; `aksi` only with arg FRONT
RESULT_CHARS = 240
MOVED = "observed_movement"
TURNED = "observed_turn_only"
STILL = "observed_no_movement"
TRANSITION = "observed_step_transition"
OBSERVED_TRANSITIONS = ("declared", "incidental")
# SAFETY (ADR 243): the mod's generic ACTION/USE path has neither the pad denylist nor the tool-tile guard
# (ModEntry.Pad.cs PadDeniedForHold returns before PadContextTable for generic kinds; BackgroundControlInputHooks applies
# BgToolTileGuard only to BgHoldKind.Tool). A raw press keeps their harms out here: the raw A is pressed with a plain
# hand (an object in hand is put away first: no gift, no eating, no placing), and the raw USE only swings a plain tool,
# never a pickaxe, axe or hoe onto a crop or a fruit tree.
USE_TOOL = re.compile(r"^\(T\)\w*(?:Axe|Pickaxe|Hoe|WateringCan)$")
CROP_BREAKER = re.compile(r"^\(T\)\w*(?:Axe|Pickaxe|Hoe)$")

WORLD_LINE = ("Tanganmu sendiri, langsung ke game, tanpa perlu ada yang terdaftar di depanmu: "
              "langkah up/right/down/left = langkah pendek ke arah itu; hadap x,y = menghadap petak di sebelahmu")
WORLD_PRESS = ("; aksi depan = tombol ACTION (A) pada petak yang kamu hadapi; "
               "pakai = memakai alat atau barang di tanganmu pada petak itu")
WORLD_NO_PRESS = ("; tombol ACTION dan USE untuk petak depanmu belum dibuka game saat ini, "
                  "jadi aksi depan dan pakai muncul lagi begitu game membukanya")
WORLD_PLAIN = " (barang di tanganmu disimpan dulu ke slot lain, supaya A tidak jadi hadiah, makan, atau menaruh barang)"
NO_USE = "; pakai belum ditawarkan: %s"
TAIL = ". Game sendiri yang menjawab; kalau tidak ada yang berubah, itu fakta di halaman berikutnya, bukan kegagalan."


def enabled(config):
    return (config or {}).get(SETTING) is True


def is_raw(call):
    """A raw hand of the world block (the floor of her page: never cut, never withheld)."""
    name = getattr(call, "name", None)
    return name in WORLD_SKILLS or (name == "aksi" and getattr(call, "arg", None) == FRONT)


def published(snapshot, context):
    """The kinds of the mod's own generic targets for the faced tile (``generic_action``, ``generic_use``)."""
    import arti_stardew_skills as S
    from arti_stardew_generic_world import parse_targets
    offered = S._g(snapshot, "generic_world_targets", ()) or parse_targets((context or {}).get("mines_ui"))
    return {t.get("kind") for t in offered or () if isinstance(t, Mapping)}


def _target(snapshot, context, kind):
    import arti_stardew_skills as S
    from arti_stardew_generic_world import parse_targets
    offered = S._g(snapshot, "generic_world_targets", ()) or parse_targets((context or {}).get("mines_ui"))
    return next((t for t in offered or () if isinstance(t, Mapping) and t.get("kind") == kind), None)


def use_refusal(snapshot, context):
    """SAFETY: why the raw USE is not offered now, in her words, or None."""
    import arti_stardew_skills as S
    held = S._g(snapshot, "held_item") or {}
    qid = held.get("qualified_item_id") if isinstance(held, Mapping) else None
    if not isinstance(qid, str) or not USE_TOOL.fullmatch(qid):
        return ("yang di tanganmu bukan alat biasa (cangkul, kapak, beliung, penyiram); barang bisa tertaruh, termakan "
                "atau tertanam, jadi pegang alat dulu (pegang_slot)")
    if CROP_BREAKER.fullmatch(qid):
        target = _target(snapshot, context, "generic_use") or {}
        tile = (target.get("tile_x"), target.get("tile_y"))
        if not (context or {}).get("scene_complete"):
            return "isi petak depanmu belum terbaca lengkap, dan alat ini bisa merusak tanaman"
        for fact in ((context or {}).get("targets") or {}).values():
            if (isinstance(fact, Mapping) and S._wave1_xy(fact.get("tile")) == tile
                    and (isinstance(fact.get("crop"), Mapping) or isinstance(fact.get("fruit_tree"), Mapping))):
                return "di petak depanmu ada tanaman, dan alat ini bisa merusaknya"
    return None


def world_calls(snapshot, facts, context=None):
    """The world block: step and face always while she can move; the action button and the held tool when the mod
    publishes its generic target for the faced tile, inside the SAFETY rules above. Not tied to anything listed."""
    return world_block(snapshot, facts, context)[0]


def world_block(snapshot, facts, context=None):
    """(calls, why the raw USE is not offered or None)."""
    import arti_stardew_skills as S
    if facts is None or getattr(facts, "player_free", False) is not True or getattr(facts, "menu_open", False):
        return [], None
    out = [S.SkillCall("langkah", d) for d in DIRS]
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    if here is not None:
        out.extend(S.SkillCall("hadap", "%d,%d" % (here[0] + dx, here[1] + dy)) for dx, dy in STEPS)
    kinds = published(snapshot, context)
    if "generic_action" in kinds:
        out.append(S.SkillCall("aksi", FRONT))
    why = use_refusal(snapshot, context) if "generic_use" in kinds else None
    if "generic_use" in kinds and why is None:
        out.append(S.SkillCall("pakai"))
    return out, why


def merge(calls, raw):
    """Skills first (they are shortcuts), the raw block last; the generic steps/faces/argless presses the block
    supersedes leave the front so each hand is one choice."""
    if not raw:
        return list(calls)
    front = any(c.name == "aksi" and c.arg == FRONT for c in raw)
    rest = [c for c in calls if c.name not in WORLD_SKILLS and not (c.name == "aksi" and c.arg == FRONT)
            and not (front and c.name == "aksi" and c.arg in (None, ""))]
    return rest + list(raw)


def world_line(calls, why_no_use=None):
    if not any(getattr(c, "name", None) == "langkah" for c in calls):
        return None
    press = any(is_raw(c) and c.name in ("aksi", "pakai") for c in calls)
    if not press:
        return WORLD_LINE + WORLD_NO_PRESS + TAIL
    line = WORLD_LINE + WORLD_PRESS.replace("yang kamu hadapi;", "yang kamu hadapi" + WORLD_PLAIN + ";")
    if not any(getattr(c, "name", None) == "pakai" for c in calls):
        line = line.split("; pakai = ")[0] + (NO_USE % why_no_use if why_no_use else "")
    return line + TAIL


def menu_line(view, actions, motor=None):
    """The menu's (or minigame's) own hands, named by the keys her page offers. None when it offers none."""
    from arti_stardew_generic_ui import _entry_actions, exit_action, published as listed
    minigame = isinstance(motor, Mapping) and motor.get("control_taken") == "minigame"
    parts = []
    close = exit_action(view, actions)
    if close:
        parts.append("%s = %s" % (close, "keluar dari minigame ini lewat jalan keluar game sendiri" if minigame
                                  else "menutup menu ini lewat tombol tutup game sendiri"))
    for key, words in (("press:LB", "tombol Escape game"), ("press:Start", "tombol konfirmasi atau OK game")):
        if key in actions and key != close:
            parts.append("%s = %s" % (key, words))
    entries = (listed(view) or {}).get("list", ())
    declines = [k for e in entries if e.get("role") == "decline" for k, _ in _entry_actions(view, e) if k in actions]
    answers = [k for e in entries if e.get("role") == "answer" for k, _ in _entry_actions(view, e) if k in actions]
    if declines:
        parts.append("%s = jawaban menolak dari game sendiri" % declines[0])
    if answers:
        parts.append("%s = jawaban yang ditawarkan game" % ", ".join(answers[:4]))
    pressed = [m for k in actions if (m := re.fullmatch(r"press:([AXY]):(\d+)(?::shift)?", k))]
    if pressed:
        forms = " atau ".join("press:%s:<id>" % b for b in sorted({m[1] for m in pressed}))
        parts.append("%s = menekan bagian yang digambar game di halaman ini (%d bagian)"
                     % (forms, len({m[2] for m in pressed})))
    if not parts:
        return None
    head = "Tanganmu sendiri di minigame ini" if minigame else "Tanganmu sendiri di menu ini"
    return head + ", langsung ke game: " + "; ".join(parts) + TAIL


def _change_words(changes):
    """A generic readback's literal changes in the game's terms (no inferred goal)."""
    words = []
    for c in changes or ():
        path = str(c.get("path", ""))
        before, after = (c.get("before") or {}).get("value"), (c.get("after") or {}).get("value")
        if path == "menu" and isinstance(after, Mapping) and after.get("type"):
            words.append("menu %s terbuka" % after["type"])
        elif path == "menu":
            words.append("menu tertutup")
        elif path == "money":
            words.append("uang %s jadi %s" % (before, after))
        elif path == "location":
            words.append("kamu pindah ke %s" % after)
        elif path.startswith("inventory:"):
            words.append("isi tas slot %s berubah" % path.split(":", 1)[1])
        elif path.split(":", 1)[0] in ("object", "terrain", "container"):
            words.append("petak %s berubah" % path.split(":", 1)[1])
        elif path.startswith("character:"):
            words.append("%s bereaksi" % path.split(":", 1)[1])
        elif path.startswith("animal:"):
            words.append("seekor hewan bereaksi")
        elif path.startswith("quest:"):
            words.append("quest %s berubah" % path.split(":", 1)[1])
        else:
            words.append("%s berubah" % path)
    return list(dict.fromkeys(words))


def describe(call):
    return call.name + (" " + str(call.arg) if call.arg not in (None, "") else "")


def step_readback(action):
    """Read only the completed move's own, identity-bound pose."""
    from arti_runtime.action_outcomes import get

    if get(action, "cleanup_confirmed") is not True:
        return None
    before, after = get(action, "before") or {}, get(action, "after") or {}
    pose = {k: before.get(k) for k in ("location", "location_id", "tile", "pixel", "facing")}
    if transition := get(action, "movement_transition"):
        observed = transition in OBSERVED_TRANSITIONS
        return dict(kind=TRANSITION if observed else "unverified_step_transition", transition=transition,
                    before=pose, destination=after.get("location") if observed else None)
    if get(action, "lifecycle") != "completed":
        return None
    if get(action, "world_effect_status") not in ("observed_change", "unchanged"):
        return None
    identity = "location_id" if before.get("location_id") or after.get("location_id") else "location"
    if not before.get(identity) or before[identity] != after.get(identity):
        return None
    start, end = pixel_pair(before.get("pixel")), pixel_pair(after.get("pixel"))
    if start is None or end is None:
        return None
    # The native terminal covers the press itself. A later pose can change independently of that press.
    px = 0 if get(action, "outcome") == "NOT_COMPLETED:NO_EFFECT" else dist(start, end)
    if not isfinite(px):
        return None
    turned = type(before.get("facing")) is int and type(after.get("facing")) is int and before["facing"] != after["facing"]
    return dict(kind=MOVED if px > 0 else TURNED if turned else STILL, px=px, before=pose)


def pixel_pair(pixel):
    if (isinstance(pixel, (tuple, list)) and len(pixel) == 2
            and all(type(v) in (float, int) and isfinite(v) for v in pixel)):
        return tuple(pixel)
    return None


def step_target(why, context):
    """A named target in her reason must match exactly one observed, positioned target."""
    from arti_runtime.fact_names import target_name
    import arti_stardew_skills as S

    if not why:
        return None
    matches = [ref for ref, row in (context.get("targets") or {}).items()
               if isinstance(row, Mapping) and S._wave1_xy(row.get("tile")) is not None
               and (name := target_name(ref, context))
               and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", why, re.IGNORECASE)]
    return matches[0] if len(matches) == 1 else None


def _step_words(movement):
    kind = movement["kind"]
    if kind == MOVED:
        px = movement["px"]
        return "kamu bergeser %s px" % ("%.2f" % px if px >= 0.01 else "kurang dari 0.01")
    if kind == TURNED:
        return "kamu hanya berbalik, posisimu tetap"
    if kind == STILL:
        return "posisimu tetap"
    transition = movement["transition"]
    if transition == "foreign":
        return "laporan perpindahan berasal dari sesi atau pemain lain"
    if transition == "stale":
        return "laporan perpindahan sudah basi"
    if transition not in OBSERVED_TRANSITIONS:
        return "perpindahan belum terverifikasi"
    before = movement["before"]
    pose = "petak %s, piksel %s, hadap %s" % (before.get("tile"), before.get("pixel"), before.get("facing"))
    return "kamu pindah dari %s (%s) ke %s. %s" % (
        before.get("location"), pose, movement["destination"],
        "Jalan keluar sesuai tujuanmu" if transition == "declared" else "Perpindahan saat melangkah, maksudnya belum jelas")


def result_line(session):
    """Her last run, when it was a raw hand: what the game answered, in its own terms. None otherwise."""
    runs = [r for r in getattr(session, "runs", ()) or () if getattr(r, "status", None) in ("finished", "failed")
            or (getattr(r, "status", None) in ("unknown", "interrupted") and r.state.get("step_movement"))]
    if not runs or not is_raw(runs[-1].call):
        return None
    run = runs[-1]
    said = describe(run.call)
    state = run.state if isinstance(run.state, dict) else {}
    if run.call.name == "langkah" and (movement := state.get("step_movement")):
        return ("%s tadi: %s." % (said, _step_words(movement)))[:RESULT_CHARS]
    if run.status == "failed":
        if run.call.name == "langkah" and run.reason in ("action_unreconciled", "movement_unverified"):
            return "%s tadi: hasil geraknya belum terverifikasi." % said
        line = "%s tadi ditolak game sebelum ada perubahan: %s." % (said, str(run.reason or "ditolak")[:120])
        return line[:RESULT_CHARS]
    delta = state.get("generic_readback")
    effect = state.get("raw_effect")
    if isinstance(delta, Mapping):
        words = _change_words(delta.get("changes"))
        line = ("%s tadi: game menjawab: %s." % (said, ", ".join(words[:4])) if words
                else "%s tadi: tidak ada yang berubah di game (WorldUnchanged); itu fakta, bukan kegagalan." % said)
    elif state.get("generic_menu_opened"):
        line = "%s tadi: game membuka menu; halaman menu itu memakai kata-kata game sendiri." % said
    elif effect == "unchanged":
        line = "%s tadi: posisimu tetap (game tidak memindahkanmu); itu fakta, bukan kegagalan." % said
    elif effect == "facing":
        line = "%s tadi: kamu sudah menghadap petak itu." % said
    elif effect:
        line = "%s tadi: posisimu berubah." % said
    else:
        line = "%s tadi selesai; game membaca hasilnya." % said
    return line[:RESULT_CHARS]

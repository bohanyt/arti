"""ADR 184: a chosen journal read visits native new entries; facts never schedule checks."""
from collections.abc import Mapping
import json

CLAIM, LEAVE = "ambil_hadiah_jurnal", "tutup_jurnal"


def parse(data):
    if not isinstance(data, Mapping) or data.get("unknown"):
        return None
    if (any(type(data.get(k)) is not int for k in ("total", "pages", "page", "entry", "back", "next", "reward"))
            or data["total"] < 0 or not 0 <= data["page"] < data["pages"]
            or not isinstance(data.get("entries"), (list, tuple)) or len(data["entries"]) > 6):
        return None
    rows = data["entries"]
    if not -1 <= data["entry"] < len(rows) or any(not isinstance(r, Mapping)
            or not isinstance(r.get("key"), str) or type(r.get("control")) is not int
            or any(type(r.get(k)) is not bool for k in ("is_new", "complete"))
            or r.get("reward") is not None and type(r["reward"]) is not bool for r in rows):
        return None
    out = dict(data, entries=[dict(r) for r in rows])
    for row in out["entries"]:
        for key, bound in (("title", 160), ("reward_text", 240)):
            if key in row:
                row[key] = row.get(key)[:bound] if isinstance(row.get(key), str) else None
        if row.get("money") is not None and type(row["money"]) is not int:
            row["money"] = None
    if isinstance(out.get("keys"), (list, tuple)):
        out["keys"] = list(out["keys"])
    return out


def bindings(view, actions):
    """Aliases of fresh published controls, never guessed IDs or a wider admission."""
    data = getattr(view, "journal", None)
    if not isinstance(data, Mapping) or getattr(view, "menu_type", None) != "QuestLog" or view.close_only:
        return {}
    close = data.get("close")
    if type(close) is not int or close < 0:
        close = next((c["id"] for c in view.components if str(c.get("field_path", "")).endswith(".upperRightCloseButton")), -1)
    out = {}
    key = "press:A:%s" % close
    if close >= 0 and key in actions:
        out[LEAVE] = actions[key]
    entry, rows = data.get("entry", -1), data.get("entries", ())
    if 0 <= entry < len(rows) and rows[entry].get("complete") is True and rows[entry].get("reward") is not False:
        key = "press:A:%s" % data.get("reward")
        if key in actions:
            out[CLAIM] = actions[key]
    return out


def project(view, current, actions):
    """A reward decision has one take and one whole-journal leave, not four pointer variants."""
    if (getattr(getattr(current, "call", None), "name", None) != "cek_jurnal"
            or not current.state.get("generic_menu_opened") or not getattr(view, "journal", None)):
        return actions, {}
    current.state.setdefault("generic_menu_receiver", view.receiver_token)
    if current.state["generic_menu_receiver"] != view.receiver_token:
        return actions, {}
    data, aliases = view.journal, bindings(view, actions)
    result = current.state.get("journal_claim_result")
    if result and data.get("keys_complete") is True and result["key"] not in data.get("keys", ()):
        result.update(entry_gone=True, indicator=data.get("indicator", "unknown"))
    if result and result.get("effect") == "observed_reward" and result.get("entry_gone") is not True:
        return actions, {}  # retain the native Back until OnLeaveQuestPage has removed the claimed entry
    state = current.state.get("journal_read", {})
    remaining = any((r.get("is_new") is True or r.get("complete") is True)
                    and r.get("key") not in state.get("seen", ()) for r in data["entries"])
    done = (data["entry"] == -1 and not remaining and (data["page"] == data["pages"] - 1
            or ("next", data["page"]) in state.get("steps", ())))
    reward = CLAIM in aliases
    if not reward and not done and not (result and result.get("entry_gone") is True):
        return actions, {}
    if LEAVE not in aliases:
        return actions, {}  # unavailable close stays an ordinary fresh inspect, never an invented B
    card = dict(choices={LEAVE: "Tutup jurnal"})
    if reward:
        row = data["entries"][data["entry"]]
        card.update(quest=row.get("title") or "nama belum diketahui", money=row.get("money"),
                    reward=row.get("reward_text") or "", source="native QuestLog entry")
        card["choices"][CLAIM] = "Ambil hadiah"
        if words := part_words(view, data.get("reward")):
            card["choices"][CLAIM] = words  # fix137A (ADR 237 A): the game's own words drawn on the reward part
    return {**aliases, "wait": None}, card


# The mod's own rows for a part the game shows no words for (ModEntry.GenericUi.cs GenericInspect, NativeJournalFacts.cs).
NATIVE_FALLBACK_LABELS = ("kontrol (nama belum diketahui)", "Collect completed quest money reward (native)")


def part_words(view, control):
    """fix137A (ADR 237 A): the words the game shows for one scanned part, or None when it shows none."""
    part = next((c for c in getattr(view, "components", ()) if type(control) is int and c.get("id") == control), None)
    label = (part or {}).get("label")
    if not isinstance(label, str) or not label.strip() or label.startswith(NATIVE_FALLBACK_LABELS):
        return None
    return " ".join(label.split())[:96]


def result(current, view, run, pick, fresh):
    if getattr(getattr(current, "call", None), "name", None) != "cek_jurnal":
        return
    if not getattr(run, "verified", False) or not getattr(run, "cleanup", False) or getattr(run, "uncertain", False):
        return
    try:
        proof = json.loads(getattr(run, "readback", None) or "null")
        if not isinstance(proof, dict) or proof.get("before_receiver") != view.receiver_token:
            return
        if proof.get("before_state") != view.state_digest:
            return
        before, after = proof["before"], proof["after"]
        if pick == CLAIM:
            data = view.journal
            row = data["entries"][data["entry"]]
            old_items, new_items = before.get("inventory"), after.get("inventory")
            old_delta = [r for r in old_items if r not in new_items] if isinstance(old_items, list) and isinstance(new_items, list) else None
            new_delta = [r for r in new_items if r not in old_items] if old_delta is not None else None
            native_after = after.get("journal") or {}
            money_changed = (type(before.get("money")) is int and type(after.get("money")) is int
                             and before["money"] != after["money"])
            destroyed = any(r.get("key") == row["key"] and r.get("destroyed") is True
                            for r in native_after.get("entries", ()))
            awarded = money_changed or bool(old_delta or new_delta) or destroyed
            current.state["journal_claim_result"] = dict(key=row["key"], quest=row.get("title"),
                money_before=before.get("money"), money_after=after.get("money"),
                inventory_before=old_delta, inventory_after=new_delta, inventory_scope="changed slots",
                effect="observed_reward" if awarded else "no_reward_observed", native_effect=proof.get("effect"),
                indicator=(after.get("journal") or {}).get("indicator", "unknown"), entry_gone="unknown",
                source="native generic_ui_readback", request_id=getattr(run, "request_id", None))
        if (proof.get("effect") == "closed" and proof.get("after_receiver") != view.receiver_token
                and (fresh is None or fresh.menu_type != "QuestLog")):
            current.state["generic_menu_receiver"] = view.receiver_token
            if current.pending is None:
                current.pending = ("done", "observed_journal_closed")
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return


def read_step(view, current, actions):
    data = getattr(view, "journal", None)
    if (getattr(getattr(current, "call", None), "name", None) != "cek_jurnal"
            or not current.state.get("generic_menu_opened") or view.menu_type != "QuestLog"
            or not isinstance(data, Mapping) or data.get("unknown") or view.close_only):
        return None
    state = current.state.setdefault("journal_read", dict(receiver=view.receiver_token, seen=[],
        steps=[], bound=2 * (data.get("total", 0) + data.get("pages", 0))))
    if state["receiver"] != view.receiver_token or len(state["steps"]) >= state["bound"]:
        return None
    entry, page = data.get("entry"), data.get("page")
    rows = data.get("entries", [])
    claimed = current.state.get("journal_claim_result")
    if claimed:
        if claimed.get("effect") != "observed_reward":
            return None
        if data.get("keys_complete") is True and claimed["key"] not in data.get("keys", ()):
            claimed.update(entry_gone=True, indicator=data.get("indicator", "unknown"))
            return None
        token = ("claim_back", claimed["key"])
        action = "press:A:%s" % data.get("back")
        if entry >= 0 and token not in state["steps"] and action in actions:
            state["steps"].append(token)
            return action  # the game's OnLeaveQuestPage removes its already-claimed entry
        return None
    if type(entry) is int and 0 <= entry < len(rows):
        row = rows[entry]
        # A reward is her separate choice. Never claim it as part of reading.
        target = data.get("back") if row.get("key") in state["seen"] and row.get("reward") is False else None
        token = ("back", page, entry)
    elif entry == -1:
        if any(r.get("is_new") is True and r.get("key") in state["seen"] for r in rows):
            return None  # an attempted open was not observed; hand the unchanged page back
        row = next((r for r in rows if (r.get("is_new") is True or r.get("complete") is True)
                    and r.get("key") not in state["seen"]), None)
        target = row.get("control") if row else data.get("next") if page < data.get("pages", 0) - 1 else None
        token = ("open", row.get("key")) if row else ("next", page)
    else:
        return None
    action = "press:A:%s" % target
    if type(target) is not int or target < 0 or action not in actions or token in state["steps"]:
        return None
    state["steps"].append(token)  # failed/no-effect input cannot loop
    if entry == -1 and row:
        state["seen"].append(row["key"])
    return action


def note(runtime, catalog):
    if not isinstance(catalog, Mapping) or not catalog.get("scope"):
        return
    native = catalog.get("journal_indicator") or {}
    if type(native.get("time")) is int and isinstance(native.get("date"), str):
        vars(runtime).setdefault("_journal_last_reads", {})[catalog["scope"]] = dict(
            at="%s %04d" % (native["date"], native["time"]), revision=native.get("revision"))


def facts(runtime, catalog):
    native = (catalog or {}).get("journal_indicator") or {}
    last = vars(runtime).get("_journal_last_reads", {}).get((catalog or {}).get("scope"), {})
    changed = "unknown"
    new = native.get("new_entries")
    if type(new) is int and new > 0:
        changed = True
    elif type(new) is int and new == 0 and last.get("revision") and native.get("revision"):
        changed = last["revision"] != native["revision"]
    def count(key):
        value = native.get(key)
        return value if type(value) is int and value >= 0 else "unknown"
    rewards = count("unclaimed_rewards")
    status = ("%d hadiah belum diambil" % rewards if type(rewards) is int and rewards > 0 else
              "tidak ada entri baru atau hadiah belum diambil" if native.get("new_activity") is False
              and type(new) is int and new == 0 and rewards == 0 else "status hadiah/entri baru belum diketahui")
    return dict(terakhir=last.get("at", "unknown"), berubah=changed, entri_baru=count("new_entries"),
                hadiah=rewards, selesai=count("completed_entries"), status=status)

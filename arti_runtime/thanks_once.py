"""One thank-you turn per money event (setting ``thanks_once_enabled``, default off)."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import math, re, threading, time, unicodedata

SETTING, WINDOW_SEC, MAX_ENTRIES = "thanks_once_enabled", 180.0, 1024
_ID_KEYS = ("event_id", "_id", "id", "donation_id", "transaction_id")
_SL_MEM = frozenset(("event_id", "_id", "membershipLevelName", "levelName", "planName", "subPlan", "platform", "from_display_name"))
_CURR = {"rp": "IDR", "idr": "IDR", "$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR",
         "£": "GBP", "gbp": "GBP", "¥": "JPY", "jpy": "JPY", "s$": "SGD", "sgd": "SGD", "rm": "MYR", "myr": "MYR"}
_NUM_RE, _SC_RE = re.compile(r"\d+(?:[.,]\d+)*"), re.compile(r"^\[DONASI Super Chat (.+?) dari (.+?)\]:")

enabled = lambda config: (config or {}).get(SETTING, False) is True
tag_trigger = lambda text, token: TaggedThanks(text, token) if token else text


class TaggedThanks(str):
    def __new__(cls, text: str, token: str):
        obj = super().__new__(cls, text); obj.thanks_token = token; return obj


@dataclass(frozen=True)
class Decision:
    is_new: bool; token: str = ""; added_clip: bool = False; added_message: bool = False


PASS = Decision(True)


class _Entry:
    __slots__ = ("token", "kind", "fallback", "first_seen", "sources", "ids", "media_url", "message", "pending_msg", "state")
    def __init__(self, token, kind, fallback, first_seen, source, clip="", msg=""):
        self.token, self.kind, self.fallback, self.first_seen, self.sources = token, kind, fallback, first_seen, {source}
        self.ids, self.media_url, self.message, self.pending_msg, self.state = {}, clip, msg, "", "pending"


def _norm(value, *, strip_at: bool = False) -> str:
    s = unicodedata.normalize("NFKC", str(value or "")).strip()
    return " ".join((s.lstrip("@") if strip_at else s).casefold().split())


def _amount_key(amount=0.0, currency="", amount_text="") -> tuple | None:
    raw = unicodedata.normalize("NFKC", str(amount_text or "")).strip()
    curr = _CURR.get(str(currency or "").strip().lower(), str(currency or "").strip().upper())
    parsed = None
    if raw and (m := _NUM_RE.search(raw)):
        num = m.group(0)
        num = (num.replace(".", "").replace(",", ".") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", num) else
               num.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", num) else
               num.replace(",", ".") if ("," in num and "." not in num) else num)
        try: parsed = v if math.isfinite(v := float(num)) else None
        except ValueError: pass
        if not curr:
            tag = " ".join((raw[:m.start()] + " " + raw[m.end():]).strip(" .:-").lower().split())
            curr = _CURR.get(tag, tag.upper())
    try: amt = float(amount or 0.0)
    except (TypeError, ValueError): amt = 0.0
    val = amt if (amt > 0 and math.isfinite(amt)) else (parsed if parsed and parsed > 0 else None)
    return ("a", f"{round(val, 2):.2f}".rstrip("0").rstrip("."), curr) if val is not None else (("raw", _norm(raw)) if raw else None)


class Registry:
    def __init__(self, *, window_sec=WINDOW_SEC, max_entries=MAX_ENTRIES, clock=time.monotonic):
        self.window_sec, self.max_entries, self._clock = float(window_sec), max(16, int(max_entries)), clock
        self._lock, self._entries, self._by_token, self._by_id, self._seq = threading.RLock(), deque(), {}, {}, 0

    def clear(self) -> None:
        with self._lock: self._entries.clear(); self._by_token.clear(); self._by_id.clear(); self._seq = 0

    def _merge(self, e: _Entry, ik: str, eid: str, msg: str, clip: str) -> Decision:
        e.sources.add(ik)
        if eid and ik not in e.ids: e.ids[ik] = eid; self._by_id[(e.kind, ik, eid)] = e
        ac, am = bool(clip and not e.media_url), bool(msg and not e.message)
        if ac: e.media_url = clip
        if am: e.message = e.pending_msg = msg
        return Decision(False, e.token, added_clip=ac, added_message=am)

    def claim(self, *, kind="donation", source="", event_id="", name="", amount=0.0, currency="",
              amount_text="", months=0, message="", media_url="") -> Decision:
        k, ik = (str(kind or "donation").strip().casefold() or "donation"), (str(source or "default").strip().casefold() or "default")
        eid, msg, clip, nname = str(event_id or "").strip(), str(message or "").strip(), str(media_url or "").strip(), _norm(name, strip_at=True)
        # One person joins or reaches a month once: the same membership from any source, whatever its id, its greeting,
        # or whether a new member is month 0 or 1 there. A gift can be bought twice, like a donation.
        val = ("m", max(1, int(months or 0))) if k in ("membership", "gift") else _amount_key(amount, currency, amount_text)
        fb, now = ((k, nname, val, "" if k == "membership" else _norm(msg)) if (nname and val is not None) else None), float(self._clock())
        with self._lock:
            if eid and (hit := self._by_id.get((k, ik, eid))) is not None: return self._merge(hit, ik, eid, msg, clip)
            if fb is not None:
                for e in reversed(self._entries):
                    if now - e.first_seen > self.window_sec: break
                    # The same source telling it again without a known id is a second gift of money: in doubt it is two.
                    if e.fallback == fb and (k == "membership" or ik not in e.sources): return self._merge(e, ik, eid, msg, clip)
            self._seq += 1
            e = _Entry(f"thx-{self._seq}", k, fb, now, ik, clip, msg)
            if eid: e.ids[ik] = eid; self._by_id[(k, ik, eid)] = e
            self._entries.append(e); self._by_token[e.token] = e
            while len(self._entries) > self.max_entries:
                old = self._entries.popleft(); self._by_token.pop(old.token, None)
                for ok, oe in old.ids.items():
                    if self._by_id.get((old.kind, ok, oe)) is old: self._by_id.pop((old.kind, ok, oe), None)
            return Decision(True, e.token)

    def set_state(self, token: str, state: str) -> None:
        with self._lock:
            if (e := self._by_token.get(token)) is not None and (state == "ran" or e.state != "ran"): e.state = state

    def already_thanked(self, token: str) -> bool:
        with self._lock: return (e := self._by_token.get(token)) is not None and e.state in ("queued", "ran")

    def take_added_message(self, token: str) -> str:
        with self._lock:
            if (e := self._by_token.get(token)) is None or not e.pending_msg: return ""
            msg, e.pending_msg = e.pending_msg, ""; return msg


_REGISTRY, _ERR_LOCK, _ERROR_LOGGED = Registry(), threading.Lock(), False


def reset() -> None:
    global _ERROR_LOGGED
    _REGISTRY.clear()
    with _ERR_LOCK: _ERROR_LOGGED = False


def _safe(fn, default):
    global _ERROR_LOGGED
    try: return fn()
    except Exception as exc:  # noqa: BLE001 - fail open, journaled once without viewer data
        with _ERR_LOCK:
            if not _ERROR_LOGGED:
                _ERROR_LOGGED = True
                print(f"[ThanksOnce] registry error ({type(exc).__name__}); continuing without dedupe")
        return default


def is_gift(ev) -> bool:
    raw = getattr(ev, "raw", None)
    return isinstance(raw, dict) and "gift" in str(raw.get("_renderer") or "").casefold()


def claim_event(config, ev) -> Decision:
    return _safe(lambda: _claim_event(config, ev), PASS)


def _claim_event(config, ev) -> Decision:
    if not enabled(config): return PASS
    raw, plat = getattr(ev, "raw", None), str(getattr(ev, "platform", "") or "").lower()
    src = (str(raw["_source"]).strip().lower() if isinstance(raw, dict) and raw.get("_source") else
           "youtube" if isinstance(raw, dict) and raw.get("_renderer") else
           "streamlabs" if isinstance(raw, dict) and getattr(ev, "kind", "") == "membership" and any(k in raw for k in _SL_MEM) else plat)
    eid = next((str(raw[k]).strip() for k in _ID_KEYS if isinstance(raw, dict) and raw.get(k) is not None and str(raw[k]).strip()), "")
    curr = (raw.get("currency") if isinstance(raw, dict) else "") or ("IDR" if plat in ("saweria", "trakteer") else "")
    return _REGISTRY.claim(
        kind="gift" if is_gift(ev) else getattr(ev, "kind", "donation"), source=src, event_id=eid, name=getattr(ev, "name", ""),
        amount=getattr(ev, "amount", 0.0), currency=str(curr or ""),
        amount_text=getattr(ev, "amount_text", "") or getattr(ev, "amount_label", ""),
        months=getattr(ev, "membership_months", 0), message=getattr(ev, "message", ""), media_url=getattr(ev, "media_url", ""),
    )


def claim_trigger(config, text: str, viewer_name: str | None, message: str) -> Decision:
    if not enabled(config) or not (m := _SC_RE.match(str(text or ""))): return PASS
    return _safe(lambda: _REGISTRY.claim(kind="donation", source="youtube", name=viewer_name or m.group(2),
                                         amount_text=m.group(1), message=message), PASS)


def claim_ssn(config, line) -> Decision:
    if not enabled(config): return PASS
    def _run():
        raw_id, don = str(getattr(line, "msg_id", "") or ""), str(getattr(line, "donation", "") or "")
        tail = raw_id.split(":", 1)[1] if ":" in raw_id else raw_id
        is_mem = "member" in don.casefold() and not _CURR.get(don.strip().split(" ", 1)[0].lower())
        m = re.search(r"(\d+)", don) if is_mem else None
        return _REGISTRY.claim(
            kind="membership" if is_mem else "donation", source="ssn", event_id="" if tail.startswith("h:") else tail,
            name=getattr(line, "viewer", "") or getattr(line, "name", ""),
            amount_text="" if is_mem else don, months=int(m.group(1)) if m else 0, message=getattr(line, "text", ""),
        )
    return _safe(_run, PASS)


def merge_pending(dec: Decision, buf, q, use_buf: bool, normalize) -> None:
    _safe(lambda: _merge_pending(dec, buf, q, use_buf, normalize), None)


def _merge_pending(dec: Decision, buf, q, use_buf: bool, normalize) -> None:
    if not (dec.added_message and dec.token): return
    tok = dec.token
    if use_buf:
        with buf._lock:
            for it in buf._items:
                if it.trigger_type == "donation" and getattr(it.text, "thanks_token", "") == tok:
                    if extra := take_added_message(tok): it.text = tag_trigger(f"{it.text} Pesan: {extra}", tok)
                    return
    else:
        with q.mutex:
            for i, raw in enumerate(q.queue):
                tr = normalize(raw)
                if tr.trigger_type == "donation" and getattr(tr.text, "thanks_token", "") == tok:
                    if extra := take_added_message(tok): q.queue[i] = replace(tr, text=tag_trigger(f"{tr.text} Pesan: {extra}", tok))
                    return


mark_state = lambda token, state: _safe(lambda: _REGISTRY.set_state(token, state), None) if token else None
take_added_message = lambda token: _safe(lambda: _REGISTRY.take_added_message(token), "") if token else ""
already_thanked = lambda token: bool(_safe(lambda: _REGISTRY.already_thanked(token), False)) if token else False


def video_fact_line(job, config=None) -> str:
    if (config is not None and not enabled(config)) or not (tok := str(getattr(job, "thanks_token", "") or "")) or not already_thanked(tok):
        return ""
    who = str(getattr(job, "viewer", "") or "").strip()
    return f"[FAKTA] Kamu sudah mengucapkan terima kasih {f'ke {who} ' if who else ''}untuk donasi ini.\n"

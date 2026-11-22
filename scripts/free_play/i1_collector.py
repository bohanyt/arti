"""G2: one sequenced writer; byte captures are observations, never C/F verdicts."""
import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from tools.arti_stop import write_stop, write_torn
def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
class CollectionFault(RuntimeError): pass
class Collector:
    def __init__(self, folder, output, *, save_id, owner, generation, sources=None):
        self.folder, self.output = Path(folder), Path(output)
        self.lock = self.folder / ".i1-writer"
        self.lock.open("x").close()
        try:
            self.output.mkdir(parents=True, exist_ok=False); s = self.session()
            self.identity = {k: s[k] for k in ("runtime_instance_id", "session_id", "player_id", "screen_id")}
            if not all(self.identity[k] for k in ("runtime_instance_id", "session_id")):
                raise RuntimeError("native identity unavailable")
            if generation <= max(s.get("generation") or 0, s.get("fenced_generation") or 0):
                raise ValueError("generation must exceed observed generation/fence")
            self.save_id, self.owner, self.generation, self.acted = save_id, owner, generation, False
            self.seq, self.request_seq, self.sample = s.get("control_seq") or 0, 0, 0
            names = ["session.json", "result.json", "control.json", "request.json", "halt.json",
                     "stop.json", "stop_all.json", "rearm.json", "mod_exit.json",
                     f"occurrences.{save_id}.{s['player_id']}.json", f"nights.{save_id}.{s['player_id']}.json"]
            self.sources = {n: self.folder / n for n in names}
            for name, path in (sources or {}).items():
                if Path(name).name != name or name in self.sources:
                    raise ValueError("capture alias must be unique basename")
                self.sources[name] = Path(path)
        except BaseException:
            self.close(); raise
    def session(self):
        return json.loads(ipc.read_text(self.folder / "session.json", encoding="utf-8-sig"))
    def capture(self, label="passive"):
        self.sample += 1; dest = self.output / f"{self.sample:06d}"
        dest.mkdir()
        index = dict(label=label, unix_ns=time.time_ns(), evidence="RAW_UNREVIEWED", files={})
        for name, path in self.sources.items():
            try:
                raw = ipc.read_bytes(path); (dest / name).write_bytes(raw)
                index["files"][name] = dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            except FileNotFoundError:
                index["files"][name] = dict(absent=True)
            except OSError as exc:
                index["files"][name] = dict(error=type(exc).__name__)
        (dest / "index.json").write_bytes(encoded(index))
        if any("error" in r for r in index["files"].values()):
            raise CollectionFault("collector read fault; native proof unavailable")
        return dest
    def binding(self):
        s = self.session()
        if any(s.get(k) != v for k, v in self.identity.items()):
            raise CollectionFault("native identity changed; fresh collector required")
        return dict(protocol=1, runtime_instance_id=s["runtime_instance_id"], session_id=s["session_id"],
                    expected_player_id=s["player_id"], expected_screen_id=s["screen_id"],
                    owner_id=self.owner, generation=self.generation, control_seq=self.seq)
    def publish(self, name, value):
        if name not in ("control.json", "request.json"):
            raise ValueError("writer cannot overwrite native files")
        self.capture("pre-write:" + name); raw = encoded(value)
        if len(raw) > 4096: raise ValueError("native input exceeds 4096 bytes")
        ipc.atomic_write(self.folder / name, raw, create_only=name == "request.json", fsync=True)
        self.capture("post-write:" + name); return value
    def control(self, kind, **aux):
        if kind not in ("acquire", "renew", "release", "cancel", "none"): raise ValueError(kind)
        if set(aux) - {"cancel", "claim", "fence", "status_query"}: raise ValueError("control binding override")
        self.acted |= kind in ("acquire", "renew") or "claim" in aux
        self.seq += 1; return self.publish("control.json", dict(self.binding(), kind=kind, **aux))
    def request(self, payload, deadline=None, control_seq=None):
        self.acted = True; self.request_seq += 1
        value = dict(payload, **self.binding(), deadline_unix_ms=deadline if deadline is not None else int(time.time()*1000)+1000)
        value["request_seq"] = self.request_seq
        if control_seq is not None: value["control_seq"] = control_seq
        if value.get("op") == "verb":
            value["request_seq"] = self.request_seq
            immutable = {k: v for k, v in value.items() if k not in ("payload_sha256", "control_seq", "deadline_unix_ms")}
            value["payload_sha256"] = hashlib.sha256(encoded(immutable)).hexdigest()
        return self.publish("request.json", value)
    def stop(self, cause, torn=False):
        self.binding(); self.capture("pre-STOP")
        result = write_torn(self.folder) if torn else write_stop(self.folder, cause, save_id=self.save_id, player_id=self.identity["player_id"], writer="safety")
        self.capture("post-STOP"); return result
    def released(self, attempt_id):
        self.binding(); s = self.session()
        if any(s.get(k) != v for k, v in self.identity.items()): raise CollectionFault("release identity changed")
        if (s.get("input_busy") is not False or s.get("inflight_request_id")
                or s.get("pad_outstanding_count", None if s.get("gamepad_input_enabled") else 0) != 0): return False
        receipts = s.get("input_idle") or {}
        w = receipts.get("core") or {}
        return bool(attempt_id and w.get("attempt_id") == attempt_id and w.get("idle") is True
                    and (s.get("owner_id") != self.owner or s.get("generation") != self.generation
                         or (isinstance(s.get("lease_until_unix_ms"), int)
                             and time.time()*1000 >= s["lease_until_unix_ms"] + 100)))
    def poll(self, predicate, *, monitor, timeout=2, label="await"):
        if not 0 < timeout <= 600: raise ValueError("bounded native wait required")
        end = time.monotonic() + timeout
        while True:
            monitor(); self.capture(label)
            value = predicate()
            if value: return value
            if time.monotonic() >= end: raise TimeoutError(label + "; no native proof")
            time.sleep(.05)
    def exchange(self, payload, *, acquired, closed, monitor, timeout=2):
        """Both admitted wires share one writer; binding/targets remain native payloads."""
        attempt = payload.get("attempt_id") if payload.get("op") == "verb" else payload.get("request_id")
        if not attempt or not payload.get("request_id"): raise ValueError("attempt/request identity required")
        request = payload
        def result():
            try: r = json.loads(ipc.read_text(self.folder / "result.json", encoding="utf-8-sig"))
            except (FileNotFoundError, ValueError): return None
            receipt = r.get("release_receipt") or {}
            return r if (r.get("request_id") == request["request_id"] and r.get("attempt_id", attempt) == attempt
                         and receipt.get("attempt_id", attempt) == attempt) else None
        try:
            s = self.session()
            self.generation = max(self.generation, (s.get("fenced_generation") or 0)+1)
            self.control("acquire")
            def ack():
                s = self.session()
                return (s.get("owner_id") == self.owner and s.get("generation") == self.generation
                        and s.get("control_seq") == self.seq and (s.get("lease_until_unix_ms") or 0) > time.time()*1000
                        and acquired(s, self.seq))
            self.poll(ack, monitor=monitor, timeout=1, label="acquire")
            request = self.request(payload)
            returned = self.poll(result, monitor=monitor, timeout=timeout, label="result:" + attempt)
        finally:
            self.control("release")
        self.poll(lambda: self.released(attempt), monitor=monitor, timeout=timeout, label="W+lease:" + attempt)
        self.poll(lambda: closed(request, returned), monitor=monitor, timeout=timeout, label="closure:" + attempt)
        return returned
    def sequence(self, routes, *, acquired, closed, monitor):
        """One W/closure per enabled wire+verb+target; no overlapping request overwrite."""
        if len({r["request_id"] for r in routes}) != len(routes): raise ValueError("unique route requests required")
        return [self.exchange(route, acquired=acquired, closed=closed, monitor=monitor) for route in routes]
    @staticmethod
    def closure(required, facts, markers=()):
        # Facts are booked native assertions, never inferred from night/runtime rollover.
        required = set(required) | {dict(zip(("M-P", "M-D", "M-W", "M-T"),
                                            ("CK-P", "CK-D", "CK-W", "CK-T")))[m] for m in markers}
        return bool(required) and all(facts.get(k) is True for k in required)
    def pending_peer(self, observe, *, monitor, timeout=2):
        """Read across date/runtime changes; do not rebind writer or invent CK-P/CK-I."""
        return self.poll(lambda: self.closure(("CK-B", "CK-P"), observe()), monitor=monitor,
                         timeout=timeout, label="M-P/CK-P")
    @staticmethod
    def b4_tuple(witness, expected, *, attempt_id):
        """R0-09 §9.2: native tuple, never substitute a wallet delta."""
        if (witness.get("kind") not in ("sold", "bought") or witness.get("attempt_id") != attempt_id
                or not isinstance(witness.get("effect_frame"), int)):
            raise AssertionError("B4 witness identity/effect frame unavailable; pending B4 pin")
        if any(witness.get(k) != expected[k] for k in ("kind", "item", "qty", "price")):
            raise AssertionError("B4 item/qty/price mismatch")
        return True
    @staticmethod
    def b4_q12(facts):
        if facts.get("outcome") == "adapter_unsupported_capability":
            return facts.get("mutated") is False
        return (facts.get("edge_consumed") is True and facts.get("receiver_matches") is True
                and facts.get("tab_matches") is True)
    def close(self):
        if self.lock is not None:
            self.lock.unlink(missing_ok=True); self.lock = None

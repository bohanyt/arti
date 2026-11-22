"""Connectivity check for donation listeners: one provider, one event, then exit.

Never prints a key, a viewer name, a message, or an amount. The owner runs this
after putting a key in the process environment. Tests only exercise --list.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import arti_donations as dn

PROVIDERS = (
    dict(name="saweria", reader=True, env="SAWERIA_STREAM_KEY", config="saweria_stream_key"),
    dict(name="streamlabs", reader=True, env="STREAMLABS_SOCKET_TOKEN",
         config="streamlabs_socket_token"),
    dict(name="trakteer", reader=True, env="TRAKTEER_HASH,TRAKTEER_STREAM_KEY",
         config="trakteer_hash,trakteer_stream_key"),
)


def list_providers() -> str:
    lines = []
    for p in PROVIDERS:
        if p["reader"]:
            lines.append(
                f"{p['name']} reader env={p['env']} config={p['config']}"
            )
        else:
            lines.append(f"{p['name']} no reader")
    return "\n".join(lines) + "\n"


def event_shape(ev) -> str:
    """TYPE and field NAMES only — never values."""
    names = [f.name for f in dataclasses.fields(ev) if f.name != "raw"]
    kind = getattr(ev, "kind", "donation") or "donation"
    return f"event: type={kind} fields={','.join(names)}"


def _key_from_env(env_name: str) -> str:
    return (os.environ.get(env_name) or "").strip()


def check_provider(name: str, *, wait_sec: float = 15.0) -> int:
    spec = next((p for p in PROVIDERS if p["name"] == name), None)
    if spec is None:
        print(f"failed: unknown_provider")
        return 1
    if not spec["reader"]:
        print(f"{name} no reader")
        return 0
    env_names = [p.strip() for p in str(spec["env"]).split(",") if p.strip()]
    keys = [_key_from_env(n) for n in env_names]
    if not keys or any(not k for k in keys):
        print("failed: missing_key")
        return 1
    key_len = ",".join(str(len(k)) for k in keys)

    done = threading.Event()
    outcome = {}

    def on_connect():
        outcome.setdefault("connect", "ok")
        print(f"connected: {name} key_len={key_len}")

    def on_error(err_name: str):
        outcome["error"] = err_name
        done.set()

    def on_event(ev):
        outcome["shape"] = event_shape(ev)
        done.set()

    listener = None
    if name == "saweria":
        listener = dn.SaweriaListener(keys[0], on_event, on_connect=on_connect, on_error=on_error)
    elif name == "streamlabs":
        try:
            import socketio  # noqa: PLC0415, F401
        except ImportError:
            print("failed: ImportError")
            return 1
        listener = dn.StreamlabsListener(keys[0], on_event, on_connect=on_connect, on_error=on_error)
    elif name == "trakteer":
        if len(keys) < 2:
            print("failed: missing_key")
            return 1
        listener = dn.TrakteerListener(
            keys[0], keys[1], on_event, on_connect=on_connect, on_error=on_error,
        )
    else:
        print("failed: unknown_provider")
        return 1

    listener.start()
    try:
        done.wait(timeout=max(0.1, float(wait_sec)))
        if "shape" in outcome:
            print(outcome["shape"])
            return 0
        if "error" in outcome:
            print(f"failed: {outcome['error']}")
            return 1
        if outcome.get("connect") == "ok":
            print("timeout: no event")
            return 0
        print("failed: timeout")
        return 1
    finally:
        try:
            listener.stop()
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("provider", nargs="?", choices=[x["name"] for x in PROVIDERS if x["reader"]],
                   help="saweria, streamlabs, or trakteer")
    p.add_argument("--list", action="store_true",
                   help="print which providers have a reader and which env/config NAME each needs")
    p.add_argument("--wait", type=float, default=15.0, metavar="SEC",
                   help="seconds to wait for one real event after connect (default 15)")
    a = p.parse_args(argv)
    if a.list:
        sys.stdout.write(list_providers())
        return 0
    if not a.provider:
        p.error("provider required (or --list)")
    return check_provider(a.provider, wait_sec=a.wait)


if __name__ == "__main__":
    raise SystemExit(main())

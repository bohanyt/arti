"""poll_state.py WANT_DAY SECONDS: wait (polling the mod's `state` event) until the game is on day >= WANT_DAY, the
player is free, no menu is open and the clock is >= 06:00. Prints one line per sample and `READY` when done.

Used by run_attempt.sh to wait out the night/rollover (a game day is ~14 real minutes; 02:00 ends it). Each sample binds
127.0.0.1:PORT (m1_paths.PORT) for 2.5 s, then sleeps 8 s. NEVER run it while a trial is running.
`poll_state.py 0 3` prints one sample (a quick pose read between trials).
"""
import argparse, json, os, socket, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m1_paths as paths  # noqa: E402


def sample(seconds=2.5, diagnostics=None):
    if diagnostics is not None:
        diagnostics.update(connection_accepted=False, accept_timeout=False, peer_closed=False,
                           events=0, state_events=0)
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", paths.PORT)); srv.listen(1); srv.settimeout(seconds)
    try: conn, _ = srv.accept()
    except socket.timeout:
        if diagnostics is not None: diagnostics["accept_timeout"] = True
        srv.close(); return None
    if diagnostics is not None: diagnostics["connection_accepted"] = True
    conn.settimeout(1.0); buf = b""; t0 = time.time(); st = None
    while time.time() - t0 < seconds:
        try: chunk = conn.recv(65536)
        except socket.timeout: continue
        if not chunk:
            if diagnostics is not None: diagnostics["peer_closed"] = True
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try: e = json.loads(line)
            except Exception: continue
            if diagnostics is not None: diagnostics["events"] += 1
            if diagnostics is not None and e.get("ev") == "observation" and (e.get("payload") or {}).get("product") == "scene":
                # Retain identity only, not the scene body. Live7 samples this before a trial owns the port.
                diagnostics["scene_identity"] = {k: e.get(k) for k in ("protocol", "ev", "player_id", "screen_id")}
                diagnostics["scene_identity"]["payload"] = {k: e["payload"].get(k) for k in ("product", "available", "session_id")}
            if e.get("ev") == "state":
                st = e["payload"]
                if diagnostics is not None: diagnostics["state_events"] += 1
            # ADR 167: both native readiness and scope are now observed;
            # collecting the rest of the window buys no safety.
            if st is not None and (diagnostics is None or diagnostics.get("scene_identity")):
                conn.close(); srv.close(); return st
    conn.close(); srv.close(); return st


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("want_day", type=int, help="day of the season to wait for")
    ap.add_argument("seconds", type=float, help="give up after this many seconds")
    a = ap.parse_args(argv)
    deadline = time.time() + a.seconds
    while time.time() < deadline:
        st = sample()
        if st:
            print(time.strftime("%H:%M:%SZ", time.gmtime()), "day", st["date"]["day"], "time", st["time_of_day"], "loc", st["location"], "tile", st["tile"], "free", st["is_player_free"], "menu", st["active_menu"], flush=True)
            if st["date"]["day"] >= a.want_day and st["is_player_free"] and st["active_menu"] is None and st["time_of_day"] >= 600:
                print("READY"); return 0
        time.sleep(8)
    return 0


if __name__ == "__main__":
    sys.exit(main())

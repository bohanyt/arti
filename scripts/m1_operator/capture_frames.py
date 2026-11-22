"""capture_frames.py [SECONDS]: capture the mod's NDJSON frames for a few seconds and list the fields that change.

Binds 127.0.0.1:PORT (m1_paths.PORT, default 3017; the mod reconnects to any listener), writes the frames to
RUN/frames-<utc>.ndjson and prints which fields change between consecutive frames of the same kind. Diagnostic and pose
source for run_attempt.sh. NEVER run it while a trial is running: the trial runtime owns that port.
"""
import argparse, json, os, socket, sys, time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m1_paths as paths  # noqa: E402


def flat(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flat(v, prefix + k + "."))
    elif isinstance(d, list):
        out[prefix[:-1]] = json.dumps(d, sort_keys=True)
    else:
        out[prefix[:-1]] = d
    return out


def capture(seconds):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", paths.PORT)); srv.listen(1); srv.settimeout(seconds)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        srv.close(); return None
    conn.settimeout(1.0)
    buf = b""; t0 = time.time(); lines = []
    while time.time() - t0 < seconds:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                lines.append(line.decode("utf-8", "replace"))
    conn.close(); srv.close()
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("seconds", nargs="?", type=float, default=8.0, help="listen/capture window (default 8)")
    a = ap.parse_args(argv)
    lines = capture(a.seconds)
    if lines is None:
        print("no connection within", a.seconds, "s"); return 2
    out = paths.RUN / ("frames-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".ndjson")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    frames = [json.loads(l) for l in lines]
    kinds = Counter(f.get("kind") or f.get("type") or f.get("ev") or "?" for f in frames)
    print("frames", len(frames), dict(kinds), "->", out.name)
    last = {}
    changes = defaultdict(Counter)
    for f in frames:
        k = f.get("kind") or f.get("type") or f.get("ev") or "?"
        cur = flat(f)
        if k in last:
            for key in set(cur) | set(last[k]):
                if cur.get(key) != last[k].get(key):
                    changes[k][key] += 1
        last[k] = cur
    for k, c in changes.items():
        print(k, "changing fields:", dict(c.most_common(25)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

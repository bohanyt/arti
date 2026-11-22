"""inspect_tiles.py CX CY RADIUS: read-only tile inspection through the mod command inbox (no game mutation).

Uses arti_stardew_command.minta_inspect_tiles from the Stage 2 worktree (m1_paths.WORKTREE) against the command folder
(m1_paths.COMMAND_DIR) and prints one line per fact (kind, name, passable/BLOCK, tree/chest details).
Writes the inbox: NEVER run it while a trial is running.
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m1_paths as paths  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cx", type=int); ap.add_argument("cy", type=int); ap.add_argument("radius", type=int)
    a = ap.parse_args(argv)
    sys.path.insert(0, str(paths.WORKTREE))
    import arti_stardew_command as command
    res = command.minta_inspect_tiles(a.cx, a.cy, a.radius, folder=str(paths.COMMAND_DIR))
    print("status", res.status, "sukses", res.sukses, "outcome", res.outcome, "alasan", getattr(res, "alasan", None))
    raw = res.mentah or {}
    facts = raw.get("facts") or {}
    for k in sorted(facts):
        f = facts[k]; t = f.get("tile") or {}
        print(k, "|", f.get("kind"), f.get("name") or f.get("qualified_item_id"), "passable" if f.get("passable") else "BLOCK", {kk: f[kk] for kk in ("health", "stump", "growth_stage", "tapped", "chest_contents") if kk in f})
    return 0


if __name__ == "__main__":
    sys.exit(main())

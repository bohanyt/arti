"""learned_blocked.py --list [LOC] | --remove LOC X,Y: inspect or fix RUN/learned-blocked.json (walk_to's D37 memory).

walk_to records tiles it learned as blocked and goals whose live walk ended with exit 4, per location. A wrong entry
(an NPC stood there, a diagonal clip before the D40 fix, a goal filed under the wrong location) makes walk_to,
find_layout, pick_tree, sweep and park_cursor avoid a good tile forever: remove it here (both lists of that location).
The file is operator data in RUN; nothing else is touched. Sends no input, binds no port.
Exit: 0 ok | 2 usage | 3 nothing to remove.
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m1_paths as paths  # noqa: E402
import walk_to as W  # noqa: E402


def main(argv=None, store=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", nargs="?", const="*", metavar="LOC", help="print every location (or one)")
    g.add_argument("--remove", nargs=2, metavar=("LOC", "X,Y"), help="remove one tile from LOC (blocked + failed goals)")
    a = ap.parse_args(argv)
    store = store or W.LearnedStore(paths.LEARNED_BLOCKED)
    if a.list is not None:
        data = store.listing()
        if a.list != "*":
            data = {a.list: data.get(a.list, {"blocked": [], "failed_goals": []})}
        print(json.dumps(data, indent=1))
        return 0
    loc, xy = a.remove
    try:
        x, y = (int(v) for v in xy.split(","))
    except ValueError:
        print("X,Y must be two integers", file=sys.stderr)
        return 2
    if store.remove(loc, (x, y)):
        print(f"removed {loc} ({x},{y}) from {store.path}")
        return 0
    print(f"{loc} ({x},{y}) is not in {store.path}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())

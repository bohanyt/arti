"""Shared harness duration grammar; see ADR fix80b-shared-soak-duration."""
import argparse
import math
import re


_DURATION = re.compile(r"(?:([0-9]+(?:\.[0-9]+)?)h|([0-9]+)m|([0-9]+)h([0-9]+)m)")


def parse_duration(value: str) -> float:
    """Return positive finite hours from Nh, N.Nh, Nm, or XhYm (whole minutes)."""
    match = _DURATION.fullmatch(value)
    if match:
        hours, minutes, mixed_hours, mixed_minutes = match.groups()
        result = (float(hours) if hours is not None else
                  float(minutes) / 60 if minutes is not None else
                  float(mixed_hours) + float(mixed_minutes) / 60)
        if result > 0 and math.isfinite(result * 3600):
            return result
    raise argparse.ArgumentTypeError(
        f"invalid duration {value!r}: expected Nh, N.Nh, Nm, or XhYm "
        "with a positive finite total (for example 1.5h, 90m, 1h30m)"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", required=True)
    args = parser.parse_args()
    try:
        print(parse_duration(args.duration))
    except argparse.ArgumentTypeError as exc:
        parser.exit(2, f"{exc}\n")

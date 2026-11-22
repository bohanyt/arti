"""Validasi hasil JSONL satu agen kurasi terhadap paket input harian."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_finetune_curation import read_jsonl, validate_reviews


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path, help="reviewed/YYYY-MM-DD.jsonl")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "finetune" / "curation" / "input")
    args = parser.parse_args()

    candidate_path = args.input_dir / args.review.name
    if not candidate_path.is_file():
        print(f"ERROR: input tidak ditemukan: {candidate_path}")
        return 2
    if not args.review.is_file():
        print(f"ERROR: review tidak ditemukan: {args.review}")
        return 2

    errors = validate_reviews(read_jsonl(candidate_path), read_jsonl(args.review))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"OK: {args.review.name} lengkap dan valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

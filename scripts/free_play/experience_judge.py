"""Blind judge runner for one run: prepare the batch, bind labels back, write the review (ADR 92).

    python -m scripts.free_play.experience_judge <run-dir>                      # write bridge/experience-batch.json + .key.json
    python -m scripts.free_play.experience_judge <run-dir> --labels out.json    # bind labels (id -> labels) into bridge/experience-review.json
    python -m scripts.free_play.experience_judge <run-dir> --provider pkg.mod:fn   # in-process provider callable, batch -> labels

No model is called here. A provider is any callable `fn(batch) -> {id: labels}`; the judge model (offline
Codex per model tiering, never the persona model) is wired by the operator. The batch is blind (see
`experience_review_template`); the key file stays with the operator and is never sent to the judge.
Reads only the bridge transcript and native journals; never prompt text, soul, prefs or viewer data.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.free_play.experience_review_template import REVIEW_FILE, build_batch, make_review

BATCH_FILE = 'bridge/experience-batch.json'
KEY_FILE = 'bridge/experience-batch.key.json'
JOURNALS = 'runtime/live7/*/day-*/journal/g1_journal.jsonl'


def _write(path, data, force=False):
    path = Path(path)
    if path.exists() and not force:
        raise FileExistsError(f'{path} exists; pass --force to overwrite')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def evidence_for(run):
    from scripts.free_play.evaluate import Evidence
    evidence = Evidence(run)
    journals = [(p, evidence.rows(p)) for p in sorted(evidence.run.glob(JOURNALS))]
    return evidence, journals


def prepare(run, *, batch_out=None, key_out=None, force=False):
    evidence, journals = evidence_for(run)
    batch, key = build_batch(evidence, journals)
    _write(batch_out or evidence.run / BATCH_FILE, batch, force)
    _write(key_out or evidence.run / KEY_FILE, key, force)
    return batch, key


def run_judge(run, provider, *, judge=None, out=None, force=False):
    """Prepare in memory, call `provider(batch)`, bind and write the review. Returns the review dict."""
    evidence, journals = evidence_for(run)
    batch, key = build_batch(evidence, journals)
    labelled = provider(batch)
    judge = dict(judge or {})
    judge.setdefault('provider', getattr(provider, '__name__', type(provider).__name__))
    review = make_review(key, labelled, judge)
    _write(out or evidence.run / REVIEW_FILE, review, force)
    return review


def bind_labels(run, labels_path, *, key_path=None, judge=None, out=None, force=False):
    run = Path(run)
    key = json.loads(Path(key_path or run / KEY_FILE).read_text(encoding='utf-8-sig'))
    labelled = json.loads(Path(labels_path).read_text(encoding='utf-8-sig'))
    review = make_review(key, labelled, judge)
    _write(out or run / REVIEW_FILE, review, force)
    return review


def load_provider(spec):
    module, _, name = spec.partition(':')
    if not module or not name:
        raise ValueError('provider spec must be package.module:callable')
    fn = getattr(importlib.import_module(module), name)
    if not callable(fn):
        raise ValueError(f'{spec} is not callable')
    return fn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run', type=Path)
    parser.add_argument('--labels', type=Path, help='judge output to bind: {id: labels} or [{id, labels}]')
    parser.add_argument('--key', type=Path, help=f'key file written by prepare (default <run>/{KEY_FILE})')
    parser.add_argument('--provider', help='in-process provider callable, package.module:callable')
    parser.add_argument('--judge-name', help='provider name recorded in the review')
    parser.add_argument('--model', help='judge model name recorded in the review')
    parser.add_argument('--batch-out', type=Path)
    parser.add_argument('--key-out', type=Path)
    parser.add_argument('--out', type=Path, help=f'review path (default <run>/{REVIEW_FILE})')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    if args.labels and args.provider:
        parser.error('--labels and --provider are exclusive')
    judge = {k: v for k, v in (('provider', args.judge_name), ('model', args.model)) if v}
    try:
        if args.labels:
            review = bind_labels(args.run, args.labels, key_path=args.key, judge=judge, out=args.out, force=args.force)
        elif args.provider:
            review = run_judge(args.run, load_provider(args.provider), judge=judge, out=args.out, force=args.force)
        else:
            batch, _ = prepare(args.run, batch_out=args.batch_out, key_out=args.key_out, force=args.force)
            print(f"{batch['schema']}: {len(batch['items'])} blind items; batch={args.batch_out or args.run / BATCH_FILE}; "
                  f"key={args.key_out or args.run / KEY_FILE} (never send the key to the judge)")
            return 0
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(f"{review['schema']}: {len(review['items'])} labelled, {len(review['rejected'])} rejected, "
          f"{len(review['unlabelled'])} unlabelled; output={args.out or args.run / REVIEW_FILE}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

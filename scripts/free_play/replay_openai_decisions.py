"""One offline replay of modelbench's archived pages, through the real adapter.

Never starts a game or Codex. Reads prompts in memory; saves metadata only.
Output must be new. Example: python -m scripts.free_play.replay_openai_decisions
--benchmark <modelbench-directory> --output <new-directory> [--max-usd .30]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import threading
import time
from dataclasses import replace
from pathlib import Path


def new_request(seed, turn):
    import arti_stardew_runtime as rt
    return replace(seed, generation_id='replay-' + turn, reason=rt._skills.SKILL_BOUNDARY_REASON,
        wakeup=threading.Event(), superseded=threading.Event(),
        snapshot=replace(seed.snapshot, working_memory=replace(seed.snapshot.working_memory,
                                                              play_mode='local_activity')))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--benchmark', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--env-file')
    ap.add_argument('--max-usd', type=float, default=.30)
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--resume', action='store_true', help='continue uncompleted pages; retain unknown spend reservation')
    a = ap.parse_args()
    import arti_stardew_runtime as rt
    from arti_stardew_decision import decode_game_decision

    spec = importlib.util.spec_from_file_location('modelbench_common', a.benchmark / 'common.py')
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    index = {r['turn']: r for r in json.loads((a.benchmark / 'index.json').read_text())}
    turns = json.loads((a.benchmark / 'sample.json').read_text())['turns']
    pages = []
    for turn in turns:
        prompt = common.request(index[turn])['content']['prompt']
        system, schema, page = common.split_prompt(prompt)
        messages = [dict(role='system', content=system), dict(role='user', content=page)]
        extra = rt._decision_provider_extra_payload(True, messages=messages)
        if json.loads(schema) != extra['response_format']['json_schema']['schema']:
            raise SystemExit('STOP: archived schema differs from current adapter')
        pages.append((turn, messages, common.page(prompt)))
    print(f'preflight: {len(pages)} archived schemas match current adapter', flush=True)
    if a.dry:
        return
    prior = []
    if a.resume:
        prior = [json.loads(line) for line in (a.output / 'results.jsonl').read_text().splitlines()]
        state_path = a.output / 'api-session.json'
        state = json.loads(state_path.read_text())
        if state['fallback']:
            raise SystemExit('STOP: session fallback is latched')
        from decimal import Decimal
        # Explicit offline reconciliation only: unknown billing remains reserved
        # against this same cap. Production never clears an interrupted-call latch.
        with (a.output / 'resume-before-state.jsonl').open('a') as audit:
            audit.write(json.dumps(state) + '\n')
        state['uncertain_usd'] = str(Decimal(state['uncertain_usd']) + Decimal(state['reserved_usd']))
        state['reserved_usd'] = '0'
        state_path.write_text(json.dumps(state))
        (a.output / 'harness-interrupted-results.jsonl').write_text(
            '\n'.join(json.dumps(r) for r in prior) + '\n')
        # A completed but invalid model answer counts, and must never be retried.
        prior = [r for r in prior if r.get('error') != 'RuntimeError']
        (a.output / 'results.jsonl').write_text('\n'.join(json.dumps(r) for r in prior) + '\n')
    else:
        a.output.mkdir(parents=True, exist_ok=False)
    os.environ['ARTI_MIND_DIR'] = str(a.output / 'mind')
    cfg = dict(stardew_runtime_enabled=True, stardew_provider_enabled=True, stardew_spend_enabled=True,
               stardew_continuation_enabled=True, stardew_decision_provider_kind='openai_api',
               stardew_decision_model='gpt-6-luna', stardew_decision_effort='low',
               stardew_decision_service_tier='priority', stardew_openai_session_cap_usd=a.max_usd,
               stardew_openai_session_state_file=str(a.output / 'api-session.json'),
               arti_state_dir=str(a.output / 'state'))
    if a.env_file:
        cfg['stardew_openai_env_file'] = a.env_file

    def no_codex(**kwargs):
        raise RuntimeError('offline_replay_codex_disabled')

    provider = rt.construct_stardew_decision_provider(cfg, codex_client_factory=no_codex)
    runtime = rt.StardewGoalRuntime(config=cfg, provider=provider, journal_dir=a.output / 'journal',
                                   start_listener=False, start_worker=False)
    seed = runtime._begin_generation(text='offline replay', trigger_type='ptt', viewer_name=None,
                                    reason='owner_turn', replace_goal=False)
    rows = list(prior)
    try:
        with (a.output / 'stdout.txt').open('a', encoding='utf-8') as log:
            from contextlib import redirect_stdout
            for turn, messages, pg in pages:
                if any(r['turn'] == turn for r in prior):
                    continue
                if (a.benchmark / 'PAUSE').exists() or (a.output / 'PAUSE').exists():
                    print('STOP: PAUSE', flush=True)
                    break
                provider._render_messages = lambda req, msgs=messages: msgs
                req = new_request(seed, turn)
                start = time.perf_counter()
                try:
                    with redirect_stdout(log):
                        raw = provider.complete(req, start_fence=lambda commit: commit())
                    elapsed = time.perf_counter() - start
                    dec = decode_game_decision(raw)
                    echo = pg['echo']
                    echo_ok = all(getattr(dec, k) == echo[k] for k in
                                  ('request_nonce', 'goal_id', 'goal_revision', 'context_ref'))
                    offered = common.offered_ok(dec, pg)
                    row = dict(turn=turn, valid=echo_ok and offered, echo=echo_ok, offered=offered,
                               latency_s=round(elapsed, 4), plan_steps=len(dec.plan),
                               running_usd=provider.running_usd)
                except Exception as exc:
                    # Only an exception type, never external exception text or payloads.
                    row = dict(turn=turn, valid=False, error=type(exc).__name__,
                               latency_s=round(time.perf_counter() - start, 4), running_usd=provider.running_usd)
                rows.append(row)
                with (a.output / 'results.jsonl').open('a', encoding='utf-8') as out:
                    out.write(json.dumps(row) + '\n')
                print(json.dumps(row), flush=True)
                if provider._api_fallback or row.get('error') in {'RuntimeError', 'TimeoutError'}:
                    print('STOP: provider fallback or interrupted request', flush=True)
                    break
    finally:
        provider.close()
    times = sorted(r['latency_s'] for r in rows)
    # Transport-only excludes the first request's startup probe.
    calls = [json.loads(line) for line in runtime._journal_path().read_text().splitlines()]
    calls = [r for r in calls if r.get('ev') == 'decision_provider_call' and not r['probe']]
    api_times = sorted(r['latency_ms'] / 1000 for r in calls)
    summary = dict(pages=len(rows), valid=sum(r['valid'] for r in rows), cap_usd=a.max_usd,
        running_usd=provider.running_usd, p50_s=statistics.median(times) if times else None,
        p90_s=times[min(len(times)-1, int(.9*len(times)))] if times else None,
        api_p50_s=statistics.median(api_times) if api_times else None,
        api_p90_s=api_times[min(len(api_times)-1, int(.9*len(api_times)))] if api_times else None,
        fallback=bool(provider._api_fallback), uncertain_usd=float(provider._api_uncertain),
        input_tokens=sum(r['input_tokens'] or 0 for r in calls),
        output_tokens=sum(r['output_tokens'] or 0 for r in calls),
        reasoning_tokens=sum(r['reasoning_tokens'] or 0 for r in calls))
    (a.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()

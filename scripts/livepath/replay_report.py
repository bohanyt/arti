"""Content-free calibration and markdown presentation for timeline replay."""
from __future__ import annotations


def values(result):
    questions = result['owner_questions']
    return {
        'owner_answered': questions['answered'],
        'owner_share': questions['share_answered'],
        'chat_answered': result['chat_messages']['answered'],
        'chat_share': result['chat_messages']['share_answered'],
        'play_lines_per_2min': result['play_lines_per_2min'],
        'longest_silence_active_sec': result['longest_silence_active_sec'],
        'question_audio_median_sec': questions['audio_latency_median_sec'],
        'question_audio_p90_sec': questions['audio_latency_p90_sec'],
        'owner_audio_median_sec': result['owner_all_voiced']['audio_latency_median_sec'],
        'chat_voiced': result['chat_replies_total'],
        'mic_replaced': sum(result['items']['replaced_by_kind'].get(k, 0)
                            for k in ('mic', 'mic_pasif', 'ptt', 'wake_word', 'mc_chat')),
    }


TOLERANCES = dict(owner_answered=1, owner_share=.05, chat_answered=1, chat_share=.05,
    play_lines_per_2min=.5, longest_silence_active_sec=5, question_audio_median_sec=2,
    question_audio_p90_sec=3, owner_audio_median_sec=2, chat_voiced=1, mic_replaced=1)


def calibrate(before, measured, *, reasons=()):
    simulated, observed = values(before), values(measured)
    differences = {k: simulated[k]-observed[k] if simulated[k] is not None and observed[k] is not None
                   else None for k in observed}
    failures = list(reasons)
    for key, delta in differences.items():
        if delta is None:
            failures.append('missing_' + key)
        elif abs(delta) > TOLERANCES[key]:
            failures.append('outside_tolerance_' + key)
    return dict(status='INCONCLUSIVE' if failures else 'OK',
        reasons=sorted(set(failures)) or ['within_declared_tolerances'],
        measured=observed, replay_minus_measured=differences,
        absolute_error={k: abs(v) if v is not None else None for k, v in differences.items()},
        tolerances=TOLERANCES)


def table(reports):
    def number(value):
        return 'n/a' if value is None else f'{value:.2f}'

    def calibrated(value, error, *, share=False):
        scale = 100 if share else 1
        suffix = '%' if share else ''
        unit = 'pp' if share else ''
        return f'{number(value*scale if value is not None else None)}{suffix} (cal ±{number(error*scale if error is not None else None)}{unit})'

    lines = [
        '| Arm | Owner questions answered | Chat messages answered / in | Play lines / 2 min | Longest silence s | Question → audio median / p90 s | Calibration |',
        '| --- | --- | --- | --- | --- | --- | --- |',
    ]
    for name, report in reports.items():
        result, calibration = report['metrics'], report['calibration']
        v, errors = values(result), calibration['absolute_error']
        q, c = result['owner_questions'], result['chat_messages']
        owner = f"{q['answered']}/{q['total']} (cal ±{number(errors['owner_answered'])}); " + calibrated(v['owner_share'], errors['owner_share'], share=True)
        chat = f"{c['answered']}/{c['incoming']} (cal ±{number(errors['chat_answered'])}); " + calibrated(v['chat_share'], errors['chat_share'], share=True)
        cells = [name, owner, chat,
            calibrated(v['play_lines_per_2min'], errors['play_lines_per_2min']),
            calibrated(v['longest_silence_active_sec'], errors['longest_silence_active_sec']),
            calibrated(v['question_audio_median_sec'], errors['question_audio_median_sec']) + ' / ' +
            calibrated(v['question_audio_p90_sec'], errors['question_audio_p90_sec']),
            calibration['status'] + ': ' + ', '.join(calibration['reasons'])]
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)

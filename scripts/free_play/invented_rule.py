"""The invented-facts bar (ADR 117, evaluator 1.7.0): its constants, definitions and status live here only.

MET needs a complete review by a named independent reviewer with measured coverage (ADR 100), no major contradicted
claim, and contradicted claims in under INVENTED_TURN_RATE_BELOW of her reply turns. goal_gate and LX-1 both read
`invented_verdict`, so the two can never disagree. Pure and stdlib only; evaluations already written are never read here.
"""
from __future__ import annotations

SEVERITIES = ('major', 'minor')
INVENTED_MAJOR_MAX = 0
INVENTED_TURN_RATE_BELOW = 0.03   # strict: exactly 3% is not under 3%
V2_SCHEMA = 'arti.free_play.semantic_review/2'
SEVERITY_RULE = dict(
    required='every contradicted claim carries severity: major | minor; none or anything else counts as major',
    major='a false statement about what happened or exists that would mislead a viewer: an action or outcome that did '
          'not happen, or denying one that did; an item, person, place or event that does not exist; a false count or '
          'amount that changes the meaning',
    minor='loose wording of a state she was told correctly',
    when_in_doubt='major',
    examples=dict(
        major=['says the item failed to go into the chest while the native transfer moved it',
               'says the catch was a fish while the native catch was trash',
               'says the plants are watered while most of them are still dry',
               'says the bag has room at 12/12'],
        minor=['"tas agak lega" at 11/12', 'calls the bag full at 11/12, told 11/12 in that turn']))
BAR_TEXT = (f'{INVENTED_MAJOR_MAX} major and contradicted claims in under {INVENTED_TURN_RATE_BELOW:.0%} of reply turns, '
            'from a complete review by a named independent reviewer with measured coverage')
THRESHOLD = dict(major=f'== {INVENTED_MAJOR_MAX}', rate=f'< {INVENTED_TURN_RATE_BELOW}')


def claim_severity(claim):
    """(severity, declared). A contradicted claim with no valid severity counts as major."""
    severity = claim.get('severity') if isinstance(claim, dict) else None
    return (severity, True) if isinstance(severity, str) and severity in SEVERITIES else ('major', False)


def turn_rate(turns_contradicted, reply_turns):
    return turns_contradicted / reply_turns if reply_turns else None


def make_counts(contradicted, major, minor, turns_contradicted, reply_turns):
    rate = turn_rate(turns_contradicted, reply_turns)
    return dict(contradicted=contradicted, major=major, minor=minor, turns_contradicted=turns_contradicted,
                reply_turns=reply_turns, rate=None if rate is None else round(rate, 6))


def invented_counts(semantic):
    """The review's counts; a review format without severity counts every contradiction as major. None without a count."""
    if isinstance(semantic.get('counts'), dict):
        return semantic['counts']
    value = semantic.get('value')
    if value is None:
        return None
    return make_counts(value, value, 0, len(semantic.get('confirmed') or ()) or value, semantic.get('reply_turns'))


def invented_verdict(semantic):
    """status, value (counts; None when the review counts nothing yet), threshold and missing signal for both gates."""
    missing = semantic.get('missing_signal')
    if semantic.get('value') is None:
        return dict(status='UNKNOWN', value=None, threshold=THRESHOLD, missing_signal=missing or 'semantic review incomplete')
    counts = invented_counts(semantic)
    # The unrounded rate, so a rounded 0.03 is never what gets compared.
    rate = turn_rate(counts['turns_contradicted'], counts['reply_turns'])
    if semantic.get('review_schema') == V2_SCHEMA and not semantic.get('complete'):
        status, missing = 'UNKNOWN', missing or 'semantic review incomplete'
    elif counts['major'] > INVENTED_MAJOR_MAX or (rate is not None and rate >= INVENTED_TURN_RATE_BELOW):
        status, missing = 'NOT_MET', None
    elif semantic.get('zero_admissible') is not True:
        blockers = semantic.get('zero_blockers') or ['no named independent reviewer']
        status, missing = 'UNKNOWN', 'MET not admissible: ' + ', '.join(blockers)
    elif rate is None:
        status, missing = 'UNKNOWN', 'no reply turns to measure'
    else:
        status, missing = 'MET', None
    return dict(status=status, value=counts, threshold=THRESHOLD, missing_signal=missing)


def counts_text(counts):
    rate = counts.get('rate')
    return (f"{counts['contradicted']} contradicted: {counts['major']} major, {counts['minor']} minor; "
            f"{counts['turns_contradicted']}/{counts['reply_turns']} reply turns = "
            + ('unknown' if rate is None else f'{rate:.3%}'))

"""ADR 210: canonical spellings of one offered choice, never motor permission."""
from collections.abc import Mapping
from dataclasses import replace as dataclass_replace, is_dataclass
from copy import copy


def replace(value, **changes):
    if is_dataclass(value):
        return dataclass_replace(value, **changes)
    result = copy(value)
    for key, item in changes.items():
        setattr(result, key, item)
    return result


def folded(value):
    return " ".join(value.split()).casefold() if isinstance(value, str) else value


def unique(value, values):
    matches = list(dict.fromkeys(v for v in values if folded(v) == folded(value)))
    return matches[0] if len(matches) == 1 else value


def navigation_aliases(shown):
    """Names explicitly printed beside one read handle, never inferred from a digest."""
    more = (shown.get("presentation") or {}).get("more") or {}
    return {path: more["read"] for path in more.get("paths", ())
            if isinstance(path, str) and isinstance(more.get("read"), str)}


def normalize(decision, calls, navigation=(), *, goods_names=None, page_aliases=None):
    """Return a proposal and numeric-only normalization counters. Ambiguity stays intact."""
    c = decision.choice
    counts = {}
    if (getattr(c, "kind", None) == "act" and folded(getattr(c, "operation", None)) == "skill"
            and c.operation != "skill"):
        c = replace(c, operation="skill")
        decision = replace(decision, choice=c)
        counts["operation"] = 1
    if (getattr(c, "kind", None) != "act" or getattr(c, "operation", None) != "skill"
            or getattr(c, "target_ref", None) is not None
            or not isinstance(getattr(c, "args", None), Mapping) or set(c.args) - {"name", "arg", "n"}):
        return decision, counts
    args = dict(c.args)
    name = unique(args.get("name"), [x.name for x in calls] + ["read_boundary"])
    named = [x for x in calls if x.name == name]
    values = list(navigation) if name == "read_boundary" else [x.arg for x in named]
    if args.get("name") != name:
        args["name"] = name
        counts["name"] = 1
    arg = args.get("arg")
    if name == "simpan_ke_chest" and isinstance(arg, str) and goods_names:
        arg = ";".join(goods_names.get(folded(p), p.strip()) for p in arg.split(";"))
    canonical = unique(arg, values)
    alias = unique(arg, page_aliases or {}) if name == "read_boundary" else None
    if name == "read_boundary" and (page_aliases or {}).get(alias) in navigation:
        canonical = page_aliases[alias]
    if name == "read_boundary" and arg in (None, ""):
        refs = set(getattr(c, "evidence_refs", ())) & set(navigation)
        if len(refs) == 1:
            canonical = next(iter(refs))
        elif len(set(navigation)) == 1:
            canonical = next(iter(navigation))
    if canonical != args.get("arg"):
        args["arg"] = canonical
        counts["arg"] = 1
    if "n" in args:
        qty = args["n"]
        if isinstance(qty, str) and qty.strip().isdigit():
            qty = int(qty.strip())
        # A page with no quantity parameter describes one invocation. In
        # particular, n=1 on a walk or a read is not another destination/page.
        if (type(qty) is int and qty == 1 and name != "beli"
                and (name == "read_boundary" or named and all(call.n is None for call in named))):
            args.pop("n")
            counts["quantity"] = 1
        elif name == "beli" and type(qty) is int and qty != args["n"]:
            args["n"] = qty
            counts["quantity"] = 1
    plan = getattr(decision, "plan", ()) or ()
    if len(plan) == 1:
        step = plan[0]
        # The plan includes the selected call as its first step. A one-step
        # echo adds no work, including a read-only inspect (not a press).
        # Normalize against the offered values, not against each other: Gate
        # and gate may be two different recipes or native control labels.
        same_arg = unique(step.get("arg"), values) == args.get("arg")
        if isinstance(args.get("arg"), str) and ";" in args["arg"]:
            same_arg = step.get("arg") in (args["arg"], args["arg"].split(";")[0])
        same_quantity = (step.get("n") in (None, args["n"]) if "n" in args
                         else step.get("n") is None if name == "beli" else step.get("n") in (None, 1))
        if (unique(step.get("name"), [x.name for x in calls] + ["read_boundary"]) == name and same_arg
                and step.get("repeat", 1) == 1 and same_quantity
                and (not step.get("presses") or step["presses"] == [step.get("arg")])):
            plan = ()
            counts["plan"] = 1
    return replace(decision, choice=replace(c, args=args), plan=plan), counts


def echo_normalizable(decision, request, *, current, offered):
    """The physical reply is already correlated; a mistyped nonce grants no new authority.

    Goal, revision, context and the currently offered choice must all agree.
    Session/generation/STOP fences and every arm-time check remain mandatory.
    """
    return (current and offered and decision.request_nonce != request.request_nonce
            and (decision.goal_id, decision.goal_revision, decision.context_ref)
            == (request.goal_id, request.goal_revision, request.context_ref))


def fresh_call(selected, calls):
    """Re-pin one still-offered semantic choice; never choose between native candidates."""
    matches = [c for c in calls if (c.name, c.arg, c.n, c.quality, c.slot, c.batch)
               == (selected.name, selected.arg, selected.n, selected.quality, selected.slot, selected.batch)]
    return matches[0] if len(matches) == 1 else selected


def malformed_feedback(code):
    field, valid = {
        "plan_repeat": ("plan[].repeat", "1 for world skills; 1..4 for ui_action"),
        "plan_presses": ("plan[].presses", "null, [] or 1..4 offered UI args, first equal to arg"),
        "missing_field": ("decision", "v, request_nonce, goal_id, goal_revision, context_ref, speech, choice"),
    }.get(code, ("decision", "GameDecision.v1"))
    return dict(code="malformed:" + code, field=field, valid=valid, input_taken=False)


def plan_feedback(decision, calls):
    from arti_stardew_live7 import choice, choice_feedback
    from arti_stardew_decision import ActChoice
    for i, step in enumerate(decision.plan):
        args = {k: v for k, v in step.items() if k in ("name", "arg", "n") and v is not None}
        proposal = replace(decision, choice=ActChoice("skill", None, args, ()))
        if choice(proposal, calls) is None:
            feedback = choice_feedback(proposal, calls)
            return dict(feedback, code="plan_not_offered", field="plan[%d].%s" % (
                i, feedback["field"].removeprefix("args.")), input_taken=False)
    return dict(code="plan_not_offered", field="plan[0]", valid="same offered choice, followed by offered steps", input_taken=False)

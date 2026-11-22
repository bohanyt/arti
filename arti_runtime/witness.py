"""Test-only presentation filter for contract rule 11. No game admission rules.

Only the witness launcher constructs this filter. Runtime config cannot construct
one; normal controller startup rejects witness switches even when nested.
"""
from copy import deepcopy


class WitnessFailure(ValueError):
    pass


def reject_witness_config(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if "witness" in str(key).casefold() and item not in (False, None):
                raise WitnessFailure("witness_forbidden_in_normal_config")
            reject_witness_config(item)
    elif isinstance(value, list):
        for item in value:
            reject_witness_config(item)


def matches(entry, selector):
    """Literal subset matching of published data, never an admissibility check."""
    return all(k in entry and (matches(entry[k], v) if isinstance(v, dict)
               and isinstance(entry[k], dict) else entry[k] == v) for k, v in selector.items())


class WitnessFilter:
    def __init__(self, *, witness=False, mode=None):
        if witness is not True or mode != "witness":
            raise WitnessFailure("witness_flag_required")

    def render(self, published, selector, *, menu_open, close_unavailable=None):
        """Select ONE already rendered atomic affordance, retaining native exits.

        The adapter supplies atomic (verb,args) entries. A grouped menu publication
        must first use its existing renderer, not create new argument variants here.
        Original publications and adapter admission remain untouched.
        """
        if not isinstance(published, list) or not selector:
            raise WitnessFailure("not_published")
        if any(not isinstance(e, dict) or not isinstance(e.get("args"), dict) or not isinstance(e.get("verb"), str)
               for e in published):
            raise WitnessFailure("invalid_publication")
        targets = [e for e in published if isinstance(e, dict) and matches(e, selector)]
        if not targets:
            raise WitnessFailure("not_published")
        if len(targets) != 1:
            raise WitnessFailure("ambiguous_target")
        target = targets[0]
        exits = [e for e in published if e.get("role") in ("exit", "back", "close", "decline")]
        if menu_open and not exits and not close_unavailable:
            raise WitnessFailure("close_not_published")
        return deepcopy([target] + [e for e in exits if e != target]), deepcopy(target)


def render_ui(view):
    """Existing fix8 renderer -> atomic generic controls; no new buttons/verbs.

    Keep wire builders in the existing controller. In particular a grouped native
    entry is expanded only through _entry_actions, with controls' safety filtering.
    The unconditional Python wait/presentation pager is never a native witness.
    """
    from arti_stardew_generic_ui import _entry_actions, controls, published
    actions = controls(view)
    result = []
    for entry in (published(view) or {}).get("list", ()):
        for key, _ in _entry_actions(view, entry):
            if key not in actions:
                continue
            row = dict(verb=entry["verb"], args=dict(control=key))
            if entry.get("role"):
                row["role"] = entry["role"]
            if row not in result:
                result.append(row)
    return result


def resolve_ui(view, chosen):
    """Use the existing wire encoder; no execution or per-feature motor code."""
    from arti_stardew_generic_ui import resolve_action
    template = deepcopy(chosen)
    text = template.get("args", {}).pop("text", None)
    if template not in render_ui(view):
        raise WitnessFailure("not_published")
    control = chosen["args"]["control"]
    if chosen["verb"] == "type":
        if not isinstance(text, str) or "{text}" not in control:
            raise WitnessFailure("type_text_required")
        control = control.replace("{text}", text)
    return resolve_action(view, control)

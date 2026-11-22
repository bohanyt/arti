"""Finite owner-authored criteria; no IO, model authority or independent store."""
from dataclasses import dataclass
import re

@dataclass(frozen=True)
class EpisodeCriterion:
    mode: str
    amount: int | None
    qualified_item_id: str
    store: bool = False
    maximum: int | None = None

    def target(self, baseline):
        # One-tree work has no owner-supplied inventory quantity. This is only
        # the initial positive-delta threshold; settled world/readback facts
        # determine the eventual deposit quantity, never this placeholder.
        if self.mode == "one_tree": return baseline + 1
        return baseline + self.amount if self.mode == "additional" else self.amount


def parse_one_tree_store_goal(text):
    """Bounded authenticated owner intention, no target/route/tool parameters.

    Called only at trusted owner ingress. Model/scene text cannot grant it.
    Numeric inventory criteria continue using the existing separate grammar.
    """
    match = re.fullmatch(
        r"gather wood from (?:one|a single) (?:observed )?(?:permitted )?(?:nearby )?tree,? "
        r"(?:then|and) store the (?:newly obtained|newly acquired|collected) wood in "
        r"(?:an?|one) (?:observed )?(?:permitted )?chest[.!]?", text.strip(), re.I)
    # Indonesian owner phrasing (live B2W1a): exactly these two forms, same single criterion.
    match = match or re.fullmatch(
        r"tebang satu pohon(?:, pungut kayunya, (?:lalu )?simpan| lalu simpan kayunya) ke chest[.!]?",
        text.strip(), re.I)
    return (EpisodeCriterion("one_tree", None, "(O)388", True),) if match else None


def parse_episode_plan(text):
    """None preserves the legacy grammar; an explicit malformed plan is refused."""
    extended = ";" in text or bool(re.search(r"\b(total|minimum|minimal|additional|tambahan|maximum|maksimum)\b", text, re.I))
    if not extended:
        return None
    parts = text.rstrip(".!").split(";")
    if not 1 <= len(parts) <= 4:
        raise ValueError("episode_plan_limit")
    plan = []
    for part in parts:
        # ADR 114: generic drop pickup pins an observed native identity. This
        # adds no tool or store objective for an arbitrary item.
        pickup = re.fullmatch(r"kumpulkan tambahan 1 (\([A-Z]\)[A-Za-z0-9]{1,32})", part.strip())
        if pickup:
            plan.append(EpisodeCriterion("additional", 1, pickup[1]))
            continue
        m = re.fullmatch(r"(?:kumpulkan|gather) (?:(total|minimum|minimal|additional|tambahan) )?([1-9][0-9]{0,2}) (kayu|wood|batu|stone)(?: (maximum|maksimum) ([1-9][0-9]{0,2}))?( kemudian simpan| lalu simpan| and store)?", part.strip(), re.I)
        if not m:
            raise ValueError("unsupported_episode_criterion")
        mode = {"tambahan":"additional", "minimal":"minimum"}.get((m[1] or "total").lower(), (m[1] or "total").lower())
        maximum = int(m[5]) if m[5] else None
        if maximum is not None and maximum < int(m[2]):
            raise ValueError("criterion_exceeds_ceiling")
        plan.append(EpisodeCriterion(mode,int(m[2]),"(O)388" if m[3].lower() in {"wood","kayu"} else "(O)390",bool(m[6]),maximum))
    return tuple(plan)

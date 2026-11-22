"""Loader, golden-case builder and contract lints for `contracts/*.json` (WP C0, R0-09 §9.2; L1, lab).

Pure helpers shared by `tests/test_contracts_sch.py`. The golden case format is documented in
`tests/fixtures/contracts/README.md` so the xunit loader (WP A0) can read the same files.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"
FIXTURES = ROOT / "tests" / "fixtures" / "contracts"

# data instance -> schema file (R0-07 "Publication layout (C0)")
DATA_FILES = {
    "il_d_rows.json": "il_d_rows.schema.json",
    "il_read_allowlist.json": "il_read_allowlist.schema.json",
    "verb_phases.json": "verb_phases.schema.json",
    "successors.json": "successors.schema.json",
    "barrier_stages.json": "barrier_stages.schema.json",
    "progression_whitelist.json": "progression_whitelist.schema.json",
    "unavailable_reasons.json": "unavailable_reasons.schema.json",
    "verb_catalogue.json": "verb_catalogue.schema.json",
    "journal_vocab.json": "journal_vocab.schema.json",
    "occurrences.json": "occurrence_catalogue.schema.json",
    "refusals.json": "refusals.schema.json",
    "game_refusals.json": "game_refusals.schema.json",
}

# R0-05 §4.3 / spec §6.7: the closed chain list of barrier_stages.json
BARRIER_CHAINS = {"sleep_ready": "night", "passout_0200": "night", "festival_start": "warp_fade", "festival_end": "warp_fade"}


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def contract(name: str):
    return load_json(CONTRACTS / name)


def schema_files() -> list[Path]:
    return sorted(CONTRACTS.glob("*.schema.json"))


def data_files() -> list[Path]:
    return sorted(p for p in CONTRACTS.glob("*.json") if not p.name.endswith(".schema.json"))


def c8(obj) -> bytes:
    """R0-07 C8 canonical JSON."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_c8(obj) -> str:
    return hashlib.sha256(c8(obj)).hexdigest()


def registry() -> Registry:
    """Every schema under its `$id` and its bare file name (the spec's relative refs resolve to the bare name)."""
    resources = []
    for path in schema_files():
        doc = load_json(path)
        resource = Resource.from_contents(doc, default_specification=DRAFT202012)
        resources.append((doc["$id"], resource))
        resources.append((path.name, resource))
    return Registry().with_resources(resources)


_REGISTRY = None


def validator(schema_name: str) -> Draft202012Validator:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = registry()
    return Draft202012Validator(contract(schema_name), registry=_REGISTRY)


def errors(schema_name: str, instance) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.validator}: {e.message[:160]}"
            for e in validator(schema_name).iter_errors(instance)]


def iter_refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from iter_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_refs(value)


# ---- golden case builder (bases + JSON-pointer ops) ----

def _split(pointer: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]


def _parent(doc, parts):
    node = doc
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node, parts[-1]


def apply_ops(doc, ops):
    doc = copy.deepcopy(doc)
    for op in ops:
        parent, last = _parent(doc, _split(op["path"]))
        if op["op"] == "set":
            if isinstance(parent, list):
                if last == "-":
                    parent.append(copy.deepcopy(op["value"]))
                else:
                    parent[int(last)] = copy.deepcopy(op["value"])
            else:
                parent[last] = copy.deepcopy(op["value"])
        elif op["op"] == "remove":
            if isinstance(parent, list):
                del parent[int(last)]
            else:
                del parent[last]
        elif op["op"] == "string":
            value = op["char"] * op["len"]
            if isinstance(parent, list):
                parent[int(last)] = value
            else:
                parent[last] = value
        elif op["op"] == "repeat":
            arr = parent[int(last)] if isinstance(parent, list) else parent[last]
            template = arr[0]
            arr[:] = [copy.deepcopy(template) for _ in range(op["count"])]
        else:
            raise ValueError(op)
    return doc


def bases():
    return load_json(FIXTURES / "bases.json")


def build(case, base_docs=None):
    if "instance" in case:
        return copy.deepcopy(case["instance"])
    if "contract" in case:
        return apply_ops(contract(case["contract"]), case.get("ops", []))
    base_docs = base_docs or bases()
    return apply_ops(base_docs[case["base"]], case.get("ops", []))


def golden(sch_id: str):
    return load_json(FIXTURES / f"{sch_id}.json")


# ---- digests (R0-07 §7.1, §7.5, §10, §11.2) ----

ACQUIRE_DERIVED = ("control_seq", "deadline_unix_ms", "payload_sha256")


def payload_v2(request: dict) -> str:
    """`journal-payload-v2`: sha256(C8) of the request immutable projection (R0-07 §7.1)."""
    return sha256_c8({k: v for k, v in request.items() if k not in ACQUIRE_DERIVED})


def entries_digest(entries: list) -> str:
    return sha256_c8([{k: v for k, v in e.items() if k != "file_present"} for e in entries])


def token_matches(halt: dict, rearm: dict) -> bool:
    return (rearm["episode_id"], rearm["halt_seq"], rearm["entries_digest"]) == (
        halt["episode_id"], halt["halt_seq"], halt["entries_digest"])


def occurrence_id(record: dict) -> str:
    return sha256_c8([record["source_id"], record["kind"], record["occurrence_index"]])


def event_id(date, kind, occurrence) -> str:
    """6a `EventStore.record()` identity (R0-07 §11.2): sha256(_json([date, kind, occurrence]))."""
    return sha256_c8([date, kind, occurrence])


# ---- registry evaluation used by goldens (R0-07 §13.2 order, §14 fact_check) ----

def classify_native(native_code: str) -> str:
    refusals = contract("refusals.json")
    return refusals["native_map"].get(native_code, refusals["default"])


def game_refusal_bucket(code: str, facts: dict) -> tuple[str, str]:
    """(effective code, bucket) of an E0 `game_refusal{native_code: code}`; AP-42 before AP-40."""
    refusals = contract("refusals.json")
    entry = contract("game_refusals.json")["entries"][code]
    check = entry["fact_check"]
    holds = None
    if check == "money_below_price" and "money" in facts and "price" in facts:
        holds = facts["money"] < facts["price"]
    if holds is not True:
        return "refusal_fact_mismatch", refusals["codes"]["refusal_fact_mismatch"]["single"]["bucket"]
    return "game_refusal", refusals["codes"]["game_refusal"]["single"]["bucket"]


# ---- lints (R0-07 §4.3-§4.6, §7.6, §13) ----

def lint_successors(doc) -> list[str]:
    seen, out = set(), []
    for row in doc["rows"]:
        key = (row["key"]["receiver_class"], row["key"]["control_role"], row["key"]["gesture"])
        if key in seen:
            out.append(f"duplicate successors key {'|'.join(key)}")
        seen.add(key)
        if row.get("post_answer") == "outro" and key[0] not in ("ShippingMenu", "LevelUpMenu"):
            out.append(f"post_answer outro outside ShippingMenu/LevelUp: {'|'.join(key)}")
    return out


def lint_barriers(doc) -> list[str]:
    out = []
    for row in doc["barriers"]:
        if row.get("chains_to") != BARRIER_CHAINS.get(row["kind"]):
            out.append(f"chains_to {row.get('chains_to')} on {row['kind']} is not in the closed chain list")
    if [r["kind"] for r in doc["barriers"]] != list(dict.fromkeys(r["kind"] for r in doc["barriers"])):
        out.append("duplicate barrier kind")
    return out


def lint_verb_phases(doc) -> list[str]:
    out = []
    for verb, entry in doc["verbs"].items():
        for key, row in entry["targets"].items():
            where = f"{verb}/{key}"
            kinds = set(row["closure"]) | {{"M-P": "CK-P", "M-D": "CK-D", "M-W": "CK-W", "M-T": "CK-T"}[m]
                                           for m in row.get("markers", [])}
            if row["async"] != bool(kinds & {"CK-P", "CK-I"}):
                out.append(f"{where}: async must equal (CK-P|CK-I in base or expected markers)")
            if any("adopts" in phase for phase in row["phases"]):
                out.append(f"{where}: adopts only under controller_phases")
            if "h2_sensitive_if" in row and not (verb == "use" and key in ("machine.held_tool", "chest.held_tool")):
                out.append(f"{where}: h2_sensitive_if outside T4")
            if row["fence_class"] == "none" and row["mutating"] and verb in ("interact", "use", "transfer", "run_controller"):
                out.append(f"{where}: mutating world row without a fence class")
        if "controller_phases" in entry and verb != "run_controller":
            out.append(f"{verb}: controller_phases only on run_controller")
    return out


def lint_attempt_record(record) -> list[str]:
    return [] if record.get("row_key") == record.get("pins", {}).get("row_key") else ["row_key != pins.row_key"]


def lint_stop_file(file_name: str, stop: dict) -> list[str]:
    want = {"stop.json": "core", "stop_all.json": "all"}[file_name]
    return [] if stop["scope"] == want else [f"{file_name} requires scope {want}"]


def csharp_enum(path: Path, name: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    body = re.search(r"enum\s+" + re.escape(name) + r"\s*\{(.*?)\}", text, flags=re.S).group(1)
    return [m.group(1) for m in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*[^,]+)?,?\s*$", body, flags=re.M)]

"""WP C0 (R0-09 §9.2): SCH-01..32 goldens + meta-validation of every `contracts/*.json` (L1, lab).

Each test is named after its trace id. Goldens live in `tests/fixtures/contracts/` (format: README.md there).
"""
from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from tests import contracts_support as cs


def _keywords(error) -> set[str]:
    out = {error.validator}
    for sub in error.context or []:
        out |= _keywords(sub)
    return out


def _run_cases(sch_id: str) -> dict:
    gold = cs.golden(sch_id)
    base_docs = cs.bases()
    built = {}
    for case in gold["cases"]:
        schema = case.get("schema", gold["schema"])
        instance = cs.build(case, base_docs)
        errs = list(cs.validator(schema).iter_errors(instance))
        where = f"{sch_id}/{case['name']} vs {schema}"
        if case["expect"] == "valid":
            assert not errs, f"{where}: {[e.message[:200] for e in errs]}"
        else:
            assert errs, f"{where}: expected invalid"
            if "keyword" in case:
                found = set().union(*(_keywords(e) for e in errs))
                assert case["keyword"] in found, f"{where}: keyword {case['keyword']} not in {found}"
        if "lint" in case:
            problems = {"successors": cs.lint_successors, "barriers": cs.lint_barriers,
                        "attempt_record": cs.lint_attempt_record}[case["lint"]](instance)
            assert (not problems) == case["lint_clean"], f"{where}: lint {problems}"
        built[case["name"]] = instance
    return built


# ---------------------------------------------------------------- meta-validation and data files

@pytest.mark.parametrize("path", cs.schema_files(), ids=lambda p: p.name)
def test_C0_schema_meta_valid(path):
    doc = cs.load_json(path)
    Draft202012Validator.check_schema(doc)
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["$id"] == f"arti:contracts/{path.name}"


@pytest.mark.parametrize("path", cs.data_files(), ids=lambda p: p.name)
def test_C0_data_file_valid(path):
    assert path.name in cs.DATA_FILES, f"{path.name}: data file without a declared schema"
    assert cs.errors(cs.DATA_FILES[path.name], cs.load_json(path)) == []


def test_C0_every_ref_resolves():
    registry = cs.registry()
    unresolved = []
    for path in cs.schema_files():
        doc = cs.load_json(path)
        resolver = registry.resolver(base_uri=doc["$id"])
        for ref in cs.iter_refs(doc):
            try:
                resolver.lookup(ref)
            except Exception as exc:  # noqa: BLE001 - report every unresolved ref at once
                unresolved.append(f"{path.name}: {ref}: {type(exc).__name__}")
    assert unresolved == []


def test_C0_tcl_one_row_per_json_key():
    tcl = cs.load_json(cs.FIXTURES / "tcl_rows.json")["rows"]
    phases = cs.contract("verb_phases.json")["verbs"]
    expected = {}
    for row_id, row in tcl.items():
        for key in row["keys"]:
            assert (row["verb"], key) not in expected, f"{row_id}: key {key} listed twice"
            expected[(row["verb"], key)] = (row_id, row)
    published = {(verb, key) for verb, entry in phases.items() for key in entry["targets"]}
    assert published == set(expected), f"missing {set(expected) - published}; extra {published - set(expected)}"
    for (verb, key), (row_id, row) in expected.items():
        got = phases[verb]["targets"][key]
        fence = row.get("fence_by_key", {}).get(key, row["fence"])
        assert (got["closure"], got["horizon_ms"], got["d3"], got["fence_class"]) == (
            row["closure"], row["horizon_ms"], row["d3"], fence), f"{row_id} {verb}/{key}"


def test_C0_contract_lints_clean():
    assert cs.lint_verb_phases(cs.contract("verb_phases.json")) == []
    assert cs.lint_successors(cs.contract("successors.json")) == []
    assert cs.lint_barriers(cs.contract("barrier_stages.json")) == []


def test_C0_closed_lists_agree():
    common = cs.contract("common.schema.json")["$defs"]
    verbs = common["VerbName"]["enum"]
    catalogue = cs.contract("verb_catalogue.json")
    phases = cs.contract("verb_phases.json")["verbs"]
    assert sorted(catalogue["verbs"]) == sorted(verbs) == sorted(phases)
    for verb, entry in catalogue["verbs"].items():
        assert entry["phases_ref"] == f"verb_phases.json#/verbs/{verb}"
    vocab = cs.contract("journal_vocab.json")["evs"]
    assert sorted(vocab) == sorted(cs.contract("journal_vocab.schema.json")["$defs"]["Ev"]["enum"])
    refusals = cs.contract("refusals.json")
    rdefs = cs.contract("refusals.schema.json")["$defs"]
    assert sorted(refusals["codes"]) == sorted(rdefs["Code"]["enum"])
    assert sorted(refusals["cancel_reasons"]) == sorted(rdefs["CancelReason"]["enum"])
    ring = cs.contract("occurrence_ring.schema.json")["$defs"]["Record"]
    assert sorted(cs.contract("occurrences.json")["kinds"]) == sorted(ring["properties"]["kind"]["enum"])
    whitelist_ops = cs.contract("progression_whitelist.schema.json")["properties"]["entries"]["items"]["properties"]["op"]["enum"]
    ring_ops = next(b["then"]["properties"]["facts"]["properties"]["op"]["enum"] for b in ring["allOf"]
                    if "op" in b.get("then", {}).get("properties", {}).get("facts", {}).get("properties", {}))
    assert whitelist_ops == ring_ops
    u1 = sorted(k.split(":", 1)[1] for k in phases["ui"]["targets"] if k.startswith("receiver.callback:"))
    assert u1 == sorted(cs.contract("successors.schema.json")["properties"]["rows"]["items"]["properties"]["callback_class"]["enum"])


# ---------------------------------------------------------------- SCH-01..32

def test_SCH_01_minimal_hud_observation():
    _run_cases("SCH-01")


def test_SCH_02_levelup_profession_requires_levelup():
    _run_cases("SCH-02")


def test_SCH_03_truncated_observation_fits_one_line():
    gold = cs.golden("SCH-03")
    built = _run_cases("SCH-03")
    for case in gold["cases"]:
        if case.get("fits_line"):
            line = cs.c8(built[case["name"]])
            assert len(line) <= gold["max_c8_bytes"] and b"\n" not in line


def test_SCH_04_force_offer_without_wait():
    _run_cases("SCH-04")


def test_SCH_05_offer_choice_bounds_and_observe():
    _run_cases("SCH-05")


def test_SCH_06_model_output_is_choice_only():
    _run_cases("SCH-06")


def test_SCH_07_astra_rejected():
    _run_cases("SCH-07")


def test_SCH_08_ui_gesture_args():
    _run_cases("SCH-08")


def test_SCH_09_refused_result_has_no_edges():
    _run_cases("SCH-09")


def test_SCH_10_cancelled_needs_cancel_reason():
    _run_cases("SCH-10")


def test_SCH_11_legacy_result_takes_legacy_decoder_path():
    gold = cs.golden("SCH-11")
    built = _run_cases("SCH-11")
    for case in gold["cases"]:
        path = "v3" if built[case["name"]].get("v") == "arti.verb_result/3" else "legacy"
        assert path == case["decoder_path"]


def test_SCH_12_v2_digest_and_acquire_derived_fields():
    gold = cs.golden("SCH-12")
    _run_cases("SCH-12")
    base_docs = cs.bases()
    request, record = base_docs[gold["request_base"]], base_docs[gold["record_base"]]
    digest = cs.payload_v2(request)
    # (a) control_seq 4 vs 5: identical v2 digest
    copies = [dict(request, control_seq=seq) for seq in gold["a_control_seq"]]
    assert len({cs.payload_v2(c) for c in copies}) == 1
    # (b) both deadline copies carry the record digest D; only acquired_ms + 1,000 is admitted
    assert request["payload_sha256"] == record["payload_sha256"] == digest
    b = gold["b"]
    for copy in b["copies"]:
        sent = dict(request, deadline_unix_ms=copy["deadline_unix_ms"])
        assert cs.payload_v2(sent) == digest
        admitted = sent["deadline_unix_ms"] == b["acquired_ms"] + 1000
        assert ("admitted" if admitted else "Expired") == copy["expect"]
        if not admitted:
            assert cs.classify_native("Expired") == copy["expect_code"]
    # (c) reconciliation needs only the record: the status query carries D, no acquire-derived field
    query = {"query_id": "q1", "runtime_instance_id": request["runtime_instance_id"], "session_id": request["session_id"],
             "owner_id": request["owner_id"], "generation": request["generation"], "request_seq": record["request_seq"],
             "request_id": record["request_id"], "payload_sha256": record["payload_sha256"]}
    assert cs.errors("control.schema.json", dict(cs.bases()["control_renew"], kind="none", status_query=query)) == []
    # pins.row_key is inside the digest
    assert cs.payload_v2(dict(request, pins=dict(request["pins"], row_key="npc.hand_empty"))) != digest


def test_SCH_13_renew_never_carries_cancel():
    _run_cases("SCH-13")


def test_SCH_14_older_token_cannot_clear_escalation():
    gold = cs.golden("SCH-14")
    _run_cases("SCH-14")
    base_docs = cs.bases()
    halt, rearm = base_docs[gold["halt_base"]], base_docs[gold["rearm_base"]]
    for file_name, base in gold["files"].items():
        assert cs.lint_stop_file(file_name, base_docs[base]) == []
    assert halt["entries_digest"] == cs.entries_digest(halt["entries"])
    assert rearm["entries_digest"] == cs.entries_digest(halt["entries"][:1])
    assert cs.token_matches(halt, rearm) is gold["expect"]["token_matches"]
    assert len(halt["entries"]) == gold["expect"]["entries"] and halt["state"] == "latched"
    note = "rearm_token_stale" if rearm["episode_id"] == halt["episode_id"] and rearm["halt_seq"] < halt["halt_seq"] else None
    assert note == gold["expect"]["halt_note"]
    assert note in cs.contract("session.schema.json")["properties"]["halt_notes"]["items"]["enum"]
    assert cs.errors("stop.schema.json", dict(base_docs["stop_h1"], scope="all")) == []  # shared schema; scope is a file lint
    assert cs.lint_stop_file("stop.json", dict(base_docs["stop_h1"], scope="all")) != []


def test_SCH_15_ring_file_bounded_to_512():
    _run_cases("SCH-15")


def test_SCH_16_decision_records_have_distinct_stable_ids():
    gold = cs.golden("SCH-16")
    built = _run_cases("SCH-16")
    ids = [cs.occurrence_id(built[c["name"]]) for c in gold["cases"]]
    assert len(set(ids)) == 4
    assert ids == gold["expected_occurrence_ids"]
    for case in gold["cases"]:
        rec = built[case["name"]]
        assert rec["occurrence_id"] == cs.occurrence_id(rec) == cs.occurrence_id(dict(rec, seq=rec["seq"] + 100))


def test_SCH_17_decision_payload_and_activity_binding():
    _run_cases("SCH-17")


def test_SCH_18_e3_needs_proven_closure():
    _run_cases("SCH-18")


def test_SCH_19_unknown_journal_ev_rejected():
    gold = cs.golden("SCH-19")
    _run_cases("SCH-19")
    assert gold["expect_ev"] in cs.contract("journal_vocab.json")["evs"]


def test_SCH_20_producer_bucket_ignored():
    gold = cs.golden("SCH-20")
    _run_cases("SCH-20")
    vocab = cs.contract("journal_vocab.json")["evs"]
    want = gold["expect_vocab"]
    assert vocab[want["ev"]]["bucket"] == want["bucket"]
    assert want["emit"] in vocab


def test_SCH_21_unmapped_native_reason_is_unclassified():
    gold = cs.golden("SCH-21")
    _run_cases("SCH-21")
    enum = gold["csharp_enum"]
    reasons = [r for r in cs.csharp_enum(cs.ROOT / enum["file"], enum["name"]) if r not in enum["not_a_reason"]]
    assert len(reasons) >= 30
    native_map = cs.contract("refusals.json")["native_map"]
    assert [r for r in reasons + gold["background_reasons"] if r not in native_map] == []
    assert gold["test_only_value"] not in native_map
    assert cs.classify_native(gold["test_only_value"]) == gold["expect"]["bucket_or_code"]


def test_SCH_22_game_refusal_fact_mismatch():
    gold = cs.golden("SCH-22")
    _run_cases("SCH-22")
    for ev in gold["evaluations"]:
        assert cs.game_refusal_bucket(ev["native_code"], ev["facts"]) == (ev["expect_code"], ev["expect_bucket"]), ev["name"]


def test_SCH_23_reprojection_keeps_event_id():
    gold = cs.golden("SCH-23")
    _run_cases("SCH-23")
    event = cs.bases()[gold["event_base"]]
    occurrence = cs.occurrence_id(gold["occurrence"])
    first = cs.event_id(event["date"], event["kind"], occurrence)
    second = cs.event_id(list(event["date"]), event["kind"], occurrence)
    assert first == second == event["event_id"]
    store = {}
    for _ in range(2):
        store.setdefault(cs.event_id(event["date"], event["kind"], occurrence), event)
    assert len(store) == 1


def test_SCH_24_unknown_event_kind():
    _run_cases("SCH-24")


def test_SCH_25_host_proposal_needs_id():
    _run_cases("SCH-25")


def test_SCH_26_v2_state_additive_keys():
    _run_cases("SCH-26")


def test_SCH_27_successors_unique_and_closed():
    _run_cases("SCH-27")


def test_SCH_28_barrier_stages_closed():
    _run_cases("SCH-28")


def test_SCH_29_progression_whitelist_closed():
    _run_cases("SCH-29")


def test_SCH_30_row_key_on_the_wire():
    gold = cs.golden("SCH-30")
    built = _run_cases("SCH-30")
    for case in gold["cases"]:
        for verb, keys in case.get("has_keys", {}).items():
            assert set(keys) <= set(built[case["name"]]["verbs"][verb]["targets"])
    c = gold["c"]
    targets = cs.contract("verb_phases.json")["verbs"]["interact"]["targets"]
    assert c["request_row_key"] in targets and c["slot_row_key"] in targets and c["request_row_key"] != c["slot_row_key"]
    assert c["expect_code"] in cs.contract("refusals.schema.json")["$defs"]["Code"]["enum"]


def test_SCH_31_ring_effect_record():
    _run_cases("SCH-31")


def test_SCH_32_target_kind_closed():
    _run_cases("SCH-32")

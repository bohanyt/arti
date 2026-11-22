# Contract goldens SCH-01..32 (WP C0; R0-07 §15)

Shared by the Python L1 tests (`tests/test_contracts_sch.py`). The C# xunit loader that also reads them is not in this repository.

- `bases.json`: named valid instances (observation, offer, reply, verb, verb_result, request, attempt record, control,
  stop/halt/rearm, ring file, accounting records, journal, host status, 6a state/event). Digest fields hold the values of the
  formulas below.
- `SCH-NN.json`: `{id, source, schema, cases[], …}`. Each case builds one instance and states `expect: valid | invalid`,
  optionally `keyword` (a JSON Schema keyword that must appear among the validation errors, nested ones included) and
  `lint` + `lint_clean` (a contract lint that is not expressible in the schema). A case uses `schema` to override the file's.
  - `instance`: the literal instance; or
  - `base` (+ `ops`): a `bases.json` entry; or `contract` (+ `ops`): a published `contracts/*.json` data file.
  - `ops` (applied in order; paths are JSON pointers): `set` (value; `-` appends to an array), `remove`, `repeat`
    (replace the array with `count` copies of its first element), `string` (set `char` × `len`).
- Extra keys in an SCH file are inputs of that trace's own checks (digests, deadline rule, token match, native map,
  fact check, event id); the Python test of that ID shows how each is evaluated.
- `tcl_rows.json`: an independent transcription of R0-05 §4.3a (row id → verb, json keys, base closure, H, D3, fence class);
  `contracts/verb_phases.json` must hold exactly one row per listed key and agree on those columns.

Formulas (R0-07 C8 canonical JSON = UTF-8, sorted keys, separators `,` `:`, `ensure_ascii=false`):
- `payload_sha256` (`journal-payload-v2`) = sha256(C8(request without `control_seq`, `deadline_unix_ms`, `payload_sha256`)).
- `entries_digest` = sha256(C8(entries, each without `file_present`)).
- `occurrence_id` = sha256(C8([source_id, kind, occurrence_index])).
- 6a `event_id` = sha256(C8([date, kind, occurrence])).

Layers: these files prove the contract shape and arithmetic (L1, evidence label U). Behavioural halves named in an SCH file's
`layer_note` (native admission, truncation, slot re-derivation, journal writer, 6a/6b code) are proved by the WPs that own them.

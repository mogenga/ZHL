# Task 8 — spec review (release endpoint vs plan)

Verdict: APPROVE.

Plan requirement (PB D4 fail-keeps-state + D7-7 item 7 + D8):
`POST /api/batches/{id}/release {approved_by}`; server assembles the seven
`assert_release` inputs; all-pass → release evidence + transition
pending_release→released + trace; any fail → no state change, 200 +
AssertionResult per-item reasons; batch not pending_release → 409;
archived → 409.

## Conformance
- ✔ Seven inputs assembled server-side: batch status, latest SampleConclusion,
  open incidents, isolated SNs, required evidence (incident.evidence_ids +
  SampleConclusion.evidence_id), CAPA views (capa_verified flag), and
  reinspection scope rows (Reinspection.scope_sns actual coverage) + ISO
  table (iso_sampling_table → IsoRow) + authoritative required_sns
  (incident_sn_links → module_inspections.sn).
- ✔ All-pass path writes release Evidence + transition + TraceStore in one
  transaction; response 200 with passed=true.
- ✔ Fail path: no transition, release_decision left untouched; 200 with
  passed=false and per-check reasons; trace records "assertion failed,
  retriable". D4 confirmed.
- ✔ archived → 409 (D8); wrong-state (isolated) → 409.
- ✔ retry semantics: test_release_retry_after_fix_releases flips
  capa_verified=True, re-calls, gets released. Idempotent re-run.

## Deviation from plan (recorded, justified)
- The plan's Task 8 says "server assembles the 7 inputs ... via
  assert_release" and feeds item 7 the required set. The Task 2 `assert_release`
  derives the required set self-referentially from the scope rows, which hides
  a partial-coverage gap (a row covering 1 of 3 SNs declares itself as
  required=1). To honor Review Focus #2 (item 7 must catch a reinspection
  covering only part of the affected set) the domain function gains an
  optional `required_sns` param: the API passes the authoritative set from
  `incident_sn_links`; Phase 0 callers omit it and keep the legacy behavior.
  This is a change to a Phase 0 frozen module (app/domain) → AGENT.md §6.1
  PR+review applies; it is scoped minimal (one optional kwarg + branch) and
  backward compatible. Recorded in dev-notes node 8.

## Test coverage vs plan
- Plan Step 1 tests: all-pass releases, assertion-fail keeps state, scope-gap
  blocks, archived 409, wrong-state 409, retry-after-fix releases. All
  present in tests/api/test_release_api.py (6 tests) and passing.
- Domain-level pins added for the new required_sns mode (2 tests).

## Open for Task 9/10
- m1 (content_hash placeholder) and m2 (single-AQL assumption) noted as
  hardening items; not in Task 8 scope.

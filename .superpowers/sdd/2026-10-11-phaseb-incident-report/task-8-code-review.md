# Task 8 — code review (release endpoint + assert_release required_sns)

Scope reviewed: `app/api/release.py` (new), `app/domain/release.py` (diff:
new `required_sns` param + two-mode derivation), `tests/api/test_release_api.py`
(new, 6 tests), `tests/domain/test_release.py` (+2 tests).

Verdict: APPROVE with minor notes. No blocker.

## Domain purity (P0 spec §3.2)
- `app/domain/release.py` diff adds only the optional `required_sns` kwarg and a
  two-branch `required_by_incident` derivation. No new imports; no
  sqlalchemy/infra/langgraph/clock/random reads introduced. Purity grep on the
  whole module returned nothing. ✔
- The authoritative required set is supplied by the API layer (the resolver),
  which is the P0 pattern: domain stays read-free, caller resolves from
  `incident_sn_links`. ✔

## assert_release change
- `required_sns: dict[str, tuple[str,...]] | None = None`. When supplied →
  authoritative mode: required set per open incident from the mapping. When
  omitted → legacy self-referential derivation preserved (Phase 0 backward
  compat). ✔
- Probe confirmed legacy mode unchanged (item7 passes with no required_sns).
- New `tests/domain/test_release.py::test_required_sns_authoritative_closes_self_reference_gap`
  pins that a partial reinspection (1 of 3 SNs) now Fails and names the
  missing SNs; `test_required_sns_full_coverage_passes` pins the all-covered
  pass. Both green.

## release.py endpoint
- `POST /{batch_id}/release` assembles all seven inputs from the DB:
  batch status (404 / archived-409 / non-pending_release-409), latest
  SampleConclusion, open incidents, isolated SNs, required evidence, CAPA
  views (from `capa_verified`), reinspection scope rows (actual coverage from
  Reinspection.scope_sns) + authoritative required_sns (from
  incident_sn_links) + ISO table. ✔
- All-pass path: writes `Evidence(human_confirmation)` + sets
  `batch.status="released"`, `release_decision="released"` (enum value),
  `release_evidence_id`, one TraceStore record, single `session.commit()`. ✔
- Fail path: NO transition, NO release_decision write; records a
  "assertion failed, retriable" trace and returns 200 with the full
  AssertionResult per-item. ✔ — matches D4 fail-keeps-state.
- D8 archived → 409. ✔

## Minor notes (non-blocking)
- m1: `Evidence.content` uses a placeholder `sha256:{uuid.hex}` for
  `content_hash` rather than a real content hash. Acceptable for the gate
  flow (the content JSON is stored verbatim in `content`), but a real hash
  would strengthen the integrity anchor. Not required by the plan; flag for
  Task 9 hardening if content provenance matters to the archiving report.
- m2: `aql = iso_rows[0].aql if iso_rows else 0.65` takes the first ISO row's
  AQL. Correct for the single-preset-table case the plan scopes; if multiple
  inspection_types coexist in `iso_sampling_table` this would be too coarse.
  The plan's Task 8 scope is a single governing preset, so acceptable; note
  for a future multi-table extension.
- m3: `lot_size=len(actual_sns) or 1` — uses the actual reinspection coverage
  size for the ISO lookup, not the required-set size. For a full coverage
  this equals the required size; for a partial coverage the ISO lookup still
  resolves on the smaller number (non-None), so item 7 fails only on the
  missing-SN subset branch (the intended signal). Consistent with D5.

## Gates
- `uv run pytest` → 131 passed (up from 123; +6 API +2 domain, +… net).
- `uv run ruff check .` → clean. `uv run black --check .` → clean.
- `app.domain` suite coverage 97.58%, `release.py` module 100%.

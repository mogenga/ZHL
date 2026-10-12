# SDD ledger — plan: docs/superpowers/plans/2026-10-11-phaseb-incident-report.md

Pre-flight scan (2026-10-11):
- T1→T2: T1 produces SCOPE_TYPE const + Reinspection.scope_type/scope_sns + QualityIncident.capa_verified; T2 consumes ReinspectionView.scope_sns (types.py, NOT the ORM) + iso_lookup — OK, T2 does not touch ORM models
- T2→T8: T2 produces assert_release(... reinspection_scopes, iso_table) 7-item; T8 assembles ReinspectionScopeIn from module_inspections+samples — matches plan §Task 2 Interfaces note (user-reviewed ②)
- T3→T5: T3 produces LLMClient.chat + MockLLMClient(fixture_path); T5 consumes both — OK
- T4→T5/T6/T7: T4 produces TraceStore.record/list_for_batch + ReportRenderer.render_*; T5 uses TraceStore; T7 uses both — OK
- T5→T6: T6 consumes AttributionSkill.run — T5 produces it — OK
- T1/T6 shared: T1 DDL adds scope columns; T6 subgraph run_reinspection is STUB (no ORM touch) until T7 wires — OK, no conflict
- T7→T8: T7 report auto-archive transition edges ("rejected","archived")/("released","archived") exist in Phase 0 state_machine.py (verified L45-46) — OK
- T9→T10: T9 create_app + 409 guard; T10 e2e uses ASGITransport app — OK
Ruling (pre-flight): plan Task 6 lists PostgresSaver in Consumes but tests use MemorySaver (per plan Step 2) — PostgresSaver deferred to integration phase; Task 6 tests must run without live PG. Cost if wrong: integration rework only.
Task 1: Ruling: plan Step order (DDL→alembic→models→test) violates TDD RED-GREEN. Reordered: write tests/infra/test_models_002.py FIRST (RED: new columns absent from models.py now), then DDL 002 + alembic revision + models.py (GREEN), then Docker container DDL verification. Cost if wrong: none — same deliverables, TDD-compliant order; container verification still runs against final DDL.
Task 1: DONE — commit 600f7a3. Container DDL verified (3 ALTER TABLEs OK). ruff/black/79 tests all green. TDD ruling recorded. Awaiting Task 2 → then merged contract-extension PR review (user gate).

Task 1 two-phase review (subagent unavailable — collect returned 'unknown after dispatch' x3 + cancel unknown; performed in-session per brainstorming fallback, same depth):
- Phase 1 code-reviewer: APPROVE, 0 blocker, 3 minor (M1 models no server_default / M2 test only asserts NOT NULL not [] / M3 alembic stamp precondition) — task-1-code-review.md
- Phase 2 spec-reviewer: APPROVE, 0 defect, 2 documented deviations (VARCHAR+CHECK vs native enum per D5a; DDL default [] vs API-layer default incident-SN-set) — task-1-spec-review.md
- Both phases green. Awaiting human gate (contract PR Task 1+2 merged review).

Task 2: DONE — commit 942b7f6. TDD: RED (ImportError) -> GREEN (13 tests). 84 total green, domain coverage 97.57%, ruff+black clean.
Task 2 two-phase review (performed in-session, same fallback as Task 1 — subagent collect unavailable):
- Phase 1 code-reviewer: APPROVE, 0 blocker, 2 minor (m1 required-set self-ref in assert_release / m2 subset gap covered via direct check call) — task-2-code-review.md
- Phase 2 spec-reviewer: APPROVE, 1 documented deviation (required-set layering: domain stays pure, Task 8 feeds authoritative incident_sn_links set) — task-2-spec-review.md
- Both phases green. Contract-extension PR (Task 1 + Task 2) complete, awaiting merged human review gate (AGENT.md §6.1).

Task 3: DONE — LLMClient plugin + mock_llm_attribution.json + 6 tests.
- TDD: RED (ModuleNotFoundError app.plugins.llm_client) -> GREEN (6 tests)
- One test-harness bug (f-string quote nesting in _prompt helper) fixed by switching to string concat; ruff E501 x4 auto-fixed by black.
- Frozen mock_llm_outputs.json verified untouched (git status empty)
- Two-phase review (in-session fallback, subagent collect unavailable as with Task 1/2):
  - code-reviewer: APPROVE, 0 blocker, 1 minor (m1: deferred httpx import — acceptable, can close at Task 9 assembly)
  - spec-reviewer: APPROVE, 1 documented deviation (regex routing slightly looser than plan's strict-substring; no misrouting, accepted)
- Awaiting human gate.

Task 4: DONE — TraceStore + ReportRenderer plugins (PB D1 three-layer audit trail).
- TDD: RED (ModuleNotFoundError trace_store/report_renderer) -> GREEN (6 tests)
- 3 impl hiccups: sessionmaker.close() missing (use session.close());
  TraceStore initial strict UUID reject on batch_id + test fixture used a free-form
  label "B-INCIDENT-001" -> switched to fixed _BATCH_UUID + pass-through for
  non-parseable values; missing json import in test.
- 96 tests green, ruff+black clean.
- Two-phase review (in-session fallback):
  - code-reviewer: APPROVE, 0 blocker, 2 minor (m1: non-parseable UUID pass-through; m2: JSON returned as dict not str)
  - spec-reviewer: APPROVE, 2 justified deviations (m1/m2 same as code review; dict return is more Pythonic, json.dumps left to caller)
- Awaiting human gate.

Task 5: DONE — TraceabilityPlugin + AttributionSkill (PB D2/D3).
- TDD: RED (ModuleNotFoundError app.plugins.attribution) -> GREEN (6 tests)
- Hiccups: fixture missing version_no + power_tolerance CHECK (<=1); skill
  returns list not dict (test index fix).
- 102 tests green, ruff+black clean.
- Two-phase review (in-session fallback):
  - code-reviewer: APPROVE, 0 blocker, 2 minor (m1: plugin instantiated inside run; m2: started_at default reads clock in trace, not a violation — renderer's clock-free rule is report-layer only)
  - spec-reviewer: APPROVE, 3 justified deviations (validate_evidence gains cross_batch_counts param; skill DI moved to constructor; started_at clock allowed for trace layer)
- Awaiting human gate.

Task 6: DONE — incident subgraph (LangGraph interrupt) + incident/capa API (PB D9).
- Step 0: uv add langgraph (1.2.14 main package; checkpoint-postgres already
  present). Committed separately with lock.
- Step 1: Context7 pre-query (docs.langchain.com interrupts + use-subgraphs):
  interrupt(payload) pauses; Command(resume=value) on same thread_id resumes,
  value becomes interrupt's return; node re-runs from top on resume.
- TDD: RED (ModuleNotFoundError app.workflow) -> GREEN (4 subgraph + 7 API tests)
- Hiccups: ASGITransport needs Async (switched to TestClient); dependency
  override was misassigned (used app.dependency_overrides properly); SQLite
  in-memory + TestClient thread isolation (check_same_thread=False + StaticPool);
  Uuid column .in_ with str args (UUID-parse both in API and test).
- 113 tests green, ruff+black clean.
- Two-phase review (in-session fallback):
  - code-reviewer: APPROVE, 0 blocker, 2 minor (m1: evidence UUID-parse info precision; m2: subgraph validate_evidence node is a pass-through, trace belongs to API layer / Task 7 handoff)
  - spec-reviewer: APPROVE, 3 justified deviations (subgraph trace handoff / stub reinspection / UUID parse)
- Awaiting human gate.

Task 7: DONE — reinspection scope API + report skill w/ auto-archive (PB D5/D6/D6a/D7/D10).
- TDD: RED (ImportError app.api.reports) -> GREEN (10 tests: 6 reinspection + 4 report)
- Hiccups: ModuleInspection has no rule_version_id (take from DeliveryBatch);
  D10 idempotent-vs-D6a auto-archive semantic clash (use disposing status for the
  clean D10 test; released->archived->409 covered by the auto-archive test);
  idempotent 200 via JSONResponse(status_code=200) overriding the endpoint 201.
- 123 tests green, ruff+black clean.
- Two-phase review (in-session fallback):
  - code-reviewer: APPROVE, 0 blocker, 2 minor (m1: in-function starlette import; m2: report renders 7 assertions as all-PASS, hand off to Task 8 to feed assert_release results + check_reinspection_scope_covered required-set into context)
  - spec-reviewer: APPROVE, 3 justified deviations (D10 test uses a non-auto-archiving status; Task 8 handoff for assertion values; rule_version_id taken from DeliveryBatch)
- Awaiting human gate.


Task 8: DONE — release endpoint (app/api/release.py) + assert_release required_sns param (PB D4 fail-keeps-state; D7-7 item 7 authoritative).
- TDD: RED (ImportError app.api.release) -> GREEN (6 API tests).
- Hiccups: (1) test basename collision tests/api/test_release.py vs tests/domain/test_release.py -> renamed API side to test_release_api.py; (2) RELEASE_DECISION enum has no "release" value (released/rejected/undecided) -> use "released"; (3) scope-gap test passed when it should fail, root-caused to the Task 2 self-referential required-set derivation -> added optional required_sns kwarg to assert_release (authoritative mode) + API resolves it from incident_sn_links. Backward-compat legacy mode preserved (probe confirmed).
- app/domain change (frozen module) -> AGENT.md §6.1 PR+review: scoped minimal, 1 optional kwarg + 2-branch derivation, backward compatible. 2 new domain tests pin the gap-closing.
- 131 tests green, ruff+black clean, app.domain coverage 97.58% (release.py 100%).
- Two-phase review (in-session fallback):
  - code-reviewer: APPROVE, 0 blocker, 3 minor (m1: content_hash placeholder sha256:uuid; m2: single-AQL assumption iso_rows[0]; m3: lot_size=len(actual) ISO lookup) — task-8-code-review.md
  - spec-reviewer: APPROVE, 1 documented deviation (required_sns authoritative param added to frozen domain fn to close self-reference gap; Phase 0 backward compat kept) — task-8-spec-review.md
- Awaiting human gate. Task 8 m2 handoff to Task 7 report-render: report context should feed real assert_release results (release API now produces them).


Task 9: DONE — app assembly + D8 archived terminal guard (create_app, guards.py, app.main).
- TDD: RED (ModuleNotFoundError app.api.main) -> GREEN (4 smoke tests).
- Hiccups: (1) circular import main->release->main when the guard was imported
  from main into the routers -> extracted the guard to app/api/guards.py, called
  as a plain function with the endpoint's session; (2) B1/B2 fixture collision on
  batch_no/evidence_id/iso/rule (shared across two _seed calls) -> distinct UUIDs
  per batch + ISO/rule inserted once in the fixture; (3) FastAPI HTTPException
  detail is nested under the "detail" key (tests asserted the top level) -> fixed.
- D8 guard wired into all 4 write endpoints (incidents / reinspections / release /
  reports) as the first 409 on archived (Review Focus #4).
- app.domain untouched (frozen). 135 tests green, ruff+black clean.
- Two-phase review (in-session fallback):
  - code-reviewer: APPROVE, 0 blocker, 3 minor (m1: per-router archived check now
    redundant after guard; m2: create_incident guard uses payload batch_id; m3:
    _make_session_dependency unused) — task-9-code-review.md
  - spec-reviewer: APPROVE, 1 documented deviation (guard extracted to guards.py +
    plain-function call to avoid the circular import; same D8 semantics / 409 payload) — task-9-spec-review.md
- Awaiting human gate.

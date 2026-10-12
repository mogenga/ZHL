# Phase B Code-Review Findings Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the code-review findings (C/H/M/L) from the 2026-10-12 Phase B full OCR-delegate review, so the 4 write APIs have consistent ordering, no dead code, and documented legacy semantics.

**Architecture:** The Phase B write APIs (incidents / reinspections / release / reports) each do state-machine transitions in a single request-scoped transaction. The review found: (a) `incident_capa` writes `incident.status` before the batch transition check (inconsistent with `assign_incident`'s validate-then-write order), (b) `release.py`/`reports.py` carry a redundant `if batch.status == "archived"` block after the shared `require_not_archived` guard (dead code), (c) the D6a auto-archive `transition` passes an empty precondition dict that will silently no-op if Phase 0 ever adds a precondition to the archived edge (needs a locking comment), (d) `assert_release`'s legacy self-referential mode (no `required_sns`) silently passes open incidents with no reinspection row (semantics gap, document it), (e) minor cleanups (dead `_make_session_dependency`, subgraph None-result routing, zero-UUID comment).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0, pytest, ruff + black. All commands run via `uv run`.

**Spec:** `docs/superpowers/specs/2026-10-11-phaseb-incident-report-design.md` (D4/D5/D6a/D7/D8/D10/D7-7 + L2/L3). This is a *review-fix* plan — it changes no behavior, only consistency, dead code, and doc comments. The Phase 0 frozen domain layer gets one doc-comment-only addition (Task 4), no signature or logic change.

## Global Constraints

- **AGENT.md §6.1 frozen items:** `app/domain/`, `app/infra/models.py`, `migrations/`, `tests/fixtures/` are frozen. This plan touches `app/domain/release.py` only with a **doc-comment** on the legacy-mode block (no logic/signature change) — within the "doc/comment" allowance; if that is judged too far, Task 4 drops to spec-only documentation.
- **No behavior change:** every edit here is reordering, dead-code removal, or comment addition. The 139-test suite must stay green (or grow) with zero test rewrites.
- **`uv run` for all commands** (ruff / black / pytest).
- **Commit per task** with a conventional commit message; pre-commit gate `uv run ruff check . && uv run black --check . && uv run pytest -x`.

## Review Focus (the failure modes this fix must not regress)

1. **archived 409 still fires first on all 4 write endpoints** (D8) — removing the redundant block in Task 2 must not drop the guard; the shared `require_not_archived` is the single source of truth.
2. **D6a auto-archive still happens in the report transaction** (released/rejected → archived) — Task 3's comment-only change must not alter the `transition(...)` call.
3. **legacy assert_release mode still passes Phase 0 callers** (no `required_sns`) — Task 4 must not change the `required_by_incident` derivation; it only documents the known "no-row → empty required set" behavior.
4. **incident_capa L3 verified gate still 400s without a pass reinspection** — Task 1 reorders status writes but must keep the L3 check before any state change.
5. **subgraph fail routing still maps `reinspection_result == "fail"` to rejected_route** — Task 5's comment addition must not change the router's return values.

---

### Task 1: `incident_capa` — align validate-then-write order with `assign_incident` (C-1, downgraded to M-level)

**Files:**
- Modify: `app/api/incidents.py:299-314` (the `incident_capa` endpoint body)

**Interfaces:**
- Consumes: `session.rollback()` (SQLAlchemy), `transition()` (frozen domain) — unchanged.
- Produces: same `POST /api/incidents/{id}/capa` contract; response body unchanged.

**Rationale (severity downgrade):** in the request-scoped session + `session.refresh(incident)` pattern (identical to `assign_incident`, which is test-green), `session.rollback()` expires the ORM object and the next attribute access re-reads from DB, so the "memory vs DB drift" risk is lower than first flagged. The remaining value is **consistency**: `assign_incident` validates the batch transition *before* writing `incident.status`; `incident_capa` writes it first. Align the order.

- [ ] **Step 1: Reorder `incident_capa` — move status/field writes to after the batch transition check**

In `app/api/incidents.py`, the current body writes `incident.confirmed_root_cause` / `corrective_action` / `capa_verified` / `status` at L299-302 *before* the `transition(batch.status, "pending_recheck", ...)` check at L304-313. Move the four incident field writes to *after* `if not batch_res.accepted: rollback/raise` and before `batch.status = "pending_recheck"`. The L3 verified gate (L286-297) stays where it is (before any write). Net effect: incident fields are only mutated once both the L3 gate and the batch transition have passed.

- [ ] **Step 2: Run the incidents API tests**

Run: `uv run pytest tests/api/test_incidents.py -v`
Expected: 7 PASS (existing `capa` tests already exercise the happy path + the L3 400; reordering does not change observable outcomes).

- [ ] **Step 3: Commit**

```bash
git add app/api/incidents.py
git commit -m "refactor(incidents): order incident_capa writes after batch transition check (align with assign)"
```

---

### Task 2: Remove redundant archived checks in `release.py` + `reports.py` (H-2)

**Files:**
- Modify: `app/api/release.py:304-307`
- Modify: `app/api/reports.py:104-113`

**Interfaces:**
- Consumes: `require_not_archived(batch_id, session)` from `app.api.guards` — the single D8 source of truth.
- Produces: same 409 semantics; the detail payload changes slightly (see Step 1 note) — confirm tests.

**Rationale:** `require_not_archived` already loads the batch and raises `409 {"error": ARCHIVED_ERROR}` when `status == "archived"`. The follow-up `if batch.status == "archived": raise 409` in both files is dead code — it can never fire because the guard above already did. `incidents.py`/`reinspections.py` have no such duplicate; removing these two makes all 4 routers consistent.

- [ ] **Step 1: Delete the redundant archived block in `release.py`**

Remove lines 306-307 (`if batch.status == "archived": raise HTTPException(409, {"error": "batch_archived"})`). Keep the `require_not_archived(batch_id, session)` call and the subsequent `if batch.status != "pending_release"` check (that one is NOT redundant — it guards a different status).

**Note (payload consistency):** the shared guard's 409 payload is `{"error": "batch archived, terminal state"}` (ARCHIVED_ERROR), not `{"error": "batch_archived"}`. After removal, `release.py`'s archived 409 now returns the shared guard's payload. Check `tests/api/test_release_api.py` for any assertion on the exact archived-409 detail; if it asserted `"batch_archived"`, update that one assertion to the shared guard payload. This is the only test-touch in the whole plan.

- [ ] **Step 2: Delete the redundant archived block in `reports.py`**

Remove lines 106-113 (the `if batch.status == "archived": raise HTTPException(409, {"error": "batch_archived", "hint": ...})` block). Keep `require_not_archived(batch_id, session)` (L105) and the D7 rejected-only-incident check (L115-123).

**Note:** same payload consideration — `tests/api/test_reports.py` may assert the archived-409 detail; align to the shared guard payload if so.

- [ ] **Step 3: Run release + reports API tests**

Run: `uv run pytest tests/api/test_release_api.py tests/api/test_reports.py -v`
Expected: PASS. Any archived-409 detail assertion updated to the shared guard payload (from Step 1/2 notes).

- [ ] **Step 4: Commit**

```bash
git add app/api/release.py app/api/reports.py tests/api/test_release_api.py tests/api/test_reports.py
git commit -m "refactor(api): drop redundant archived check after shared D8 guard (release/reports)"
```

---

### Task 3: Lock the D6a auto-archive precondition with a comment (H-1)

**Files:**
- Modify: `app/api/reports.py` (the D6a auto-archive block, ~L140-147)

**Interfaces:**
- Consumes: `transition(batch.status, "archived", {}, kind="batch")` — unchanged.
- Produces: no behavior change; a locking comment only.

**Rationale:** the auto-archive passes an empty precondition `{}`. If Phase 0 ever adds a precondition to the `("released","archived")` / `("rejected","archived")` edge, `res.accepted` would silently go `False` and the batch would not archive while the report still returns 201 — a silent divergence. A comment pins the invariant: "archived edge must stay precondition-free, else D6a silently no-ops."

- [ ] **Step 1: Add the locking comment at the D6a block**

In `app/api/reports.py`, immediately above the `if batch.status in ("released", "rejected"):` D6a block, add a comment:

```
# D6a invariant: the ("released","archived") / ("rejected","archived") state-machine
# edges MUST keep an EMPTY precondition. If a precondition is ever added to these
# edges, transition(..., "archived", {}, ...) will return accepted=False and the
# batch silently stays non-archived while the report still commits — no error.
# Keep this comment in sync with any future Phase 0 state_machine.py change.
```

Do NOT change the `transition(...)` call itself.

- [ ] **Step 2: Run reports tests**

Run: `uv run pytest tests/api/test_reports.py -v`
Expected: PASS (comment-only, zero behavior change).

- [ ] **Step 3: Commit**

```bash
git add app/api/reports.py
git commit -m "docs(reports): pin D6a auto-archive empty-precondition invariant"
```

---

### Task 4: Document the `assert_release` legacy-mode semantics gap (H-3)

**Files:**
- Modify: `app/domain/release.py` (the legacy self-referential branch in `assert_release`, ~L189-197) — **doc-comment only**

**Interfaces:**
- Consumes: `assert_release(..., required_sns=None)` legacy path — unchanged.
- Produces: no signature/logic change; a comment on the known limitation.

**Rationale:** in legacy mode (no `required_sns`), an open incident with **no** reinspection row gets `required_by_incident[inc_id] = ()` (empty). In `check_reinspection_scope_covered`, `if required_sns:` is `False` for an empty tuple, so that incident `continue`s — i.e. an open incident with zero reinspection coverage **does not** fail item 7 in legacy mode. Task 8's release API always supplies the authoritative `required_sns` (so production is correct); the gap only affects hypothetical Phase 0 callers that omit `required_sns`. Document it.

- [ ] **Step 1: Add the doc-comment to the legacy branch**

In `app/domain/release.py`, on the legacy `else:` branch (`required_by_incident = {}` ... `required_by_incident[inc_id] = ()`), add a comment:

```
# Known limitation (H-3, review 2026-10-12): in legacy mode an open incident with
# NO reinspection row gets an empty required set, and check_reinspection_scope_covered
# short-circuits `if required_sns:` for the empty tuple — so "no reinspection at all"
# does NOT fail item 7 here. The authoritative `required_sns` mode (Task 8 release API,
# fed from incident_sn_links) is the correct path and always names the gap. Legacy mode
# is retained only for Phase 0 backward compatibility; prefer passing required_sns.
```

- [ ] **Step 2: Run domain release tests**

Run: `uv run pytest tests/domain/test_release.py -v`
Expected: PASS (comment-only).

- [ ] **Step 3: Commit**

```bash
git add app/domain/release.py
git commit -m "docs(domain): document assert_release legacy-mode no-row semantics gap (H-3)"
```

---

### Task 5: Minor cleanups — dead code + subgraph routing comment + zero-UUID comment (M-1/M-4/M-6)

**Files:**
- Modify: `app/api/main.py:440-447` (delete `_make_session_dependency`)
- Modify: `app/workflow/incident_subgraph.py` (comment on `_route_reinspection_result` None handling)
- Modify: `app/skills/report_skill.py` (comment on the `uuid.UUID(int=0)` zero-UUID fallback)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_make_session_dependency` no longer exists — confirm no import references it.

**Rationale:** M-1 is dead code (the closure is inlined in `create_app`; the function is unused and not in `__all__`). M-4: `state.get("reinspection_result")` being `None` routes to `END` (pass) — add a comment that this is intentional (a subgraph replay with no reinspection result is treated as "no fail" and the API layer is the authority for the item-7 gap). M-6: the `uuid.UUID(int=0)` zero-UUID in the empty-incidents reinspection query is an inert sentinel (matches nothing); comment the intent.

- [ ] **Step 1: Delete `_make_session_dependency`**

In `app/api/main.py`, remove the `_make_session_dependency` function (L440-447). Grep first: `uv run python -c "import app.api.main"` and `grep -rn "_make_session_dependency" app/ tests/` — if any reference exists, wire it instead of deleting.

- [ ] **Step 2: Comment the subgraph None-result routing**

In `app/workflow/incident_subgraph.py`, on `_route_reinspection_result`, add a comment above the `if state.get("reinspection_result") == "fail":` line:

```
# None / absent reinspection_result routes to END (pass), NOT rejected_route.
# This subgraph is a topology scaffold: the API layer (Task 7 create_reinspection)
# is the authority that maps "no reinspection" to the item-7 release gap. A
# standalone subgraph replay with no result is therefore "no fail", by design.
```

Do NOT change the return values.

- [ ] **Step 3: Comment the zero-UUID fallback**

In `app/skills/report_skill.py`, on the `uuid.UUID(int=0)` line in the empty-incidents reinspection filter, add an inline comment:

```
# zero-UUID sentinel: when the batch has no incidents, the .in_() list would be
# empty (invalid SQL); a zero-UUID never matches a real row, so the query
# returns no reinspections — an inert, loud-safe placeholder, not a real id.
```

- [ ] **Step 4: Run the affected test modules**

Run: `uv run pytest tests/api/test_main_smoke.py tests/workflow/test_incident_subgraph.py -v`
Expected: PASS.

- [ ] **Step 5: Full gate + commit**

Run: `uv run ruff check . && uv run black --check . && uv run pytest -x`
Expected: all green.

```bash
git add app/api/main.py app/workflow/incident_subgraph.py app/skills/report_skill.py
git commit -m "chore: remove dead _make_session_dependency + pin subgraph/zero-UUID intent (M-1/M-4/M-6)"
```

---

### Task 6: Final verification + fix log

**Files:**
- Create: `docs/superpowers/plans/2026-10-12-phaseb-fix-log.md` (the user-requested fix log)

- [ ] **Step 1: Full suite + gate**

Run: `uv run ruff check . && uv run black --check . && uv run pytest -x`
Expected: 139 tests green (or +1 if Task 2 added/updated an assertion). Capture the final test count.

- [ ] **Step 2: Write the fix log**

Create `docs/superpowers/plans/2026-10-12-phaseb-fix-log.md` with, per finding (C-1/C-2→M, H-1, H-2, H-3, M-1, M-4, M-6): **问题描述** (what the review found + severity + re-grade rationale), **修复方案** (the chosen approach), **实际修复操作** (the exact edits, with before/after), **diff** (the `git show` patch for that commit), **二次核验记录** (test run result + the manual re-check that the fix holds). Include a C-1/C-2 severity re-grade note (request-scoped session + `session.refresh` makes the drift risk lower than first flagged; reorder is a consistency fix, not a data-integrity one).

- [ ] **Step 3: Commit the fix log**

```bash
git add docs/superpowers/plans/2026-10-12-phaseb-fix-log.md
git commit -m "docs(plan): Phase B code-review fix log (C/H/M findings, diffs + re-verification)"
```

# Task 9 — spec review (app assembly + D8 guard vs plan)

Verdict: APPROVE.

Plan requirement (PB D8 terminal guard + app assembly, AGENT.md §5 entry):
- `app.api.main.create_app()` mounts incidents / reinspections / release /
  reports routers + static dir + archived guard dependency
- `app.main` one-line `uvicorn` entry
- All write endpoints pre-check batch status: archived → 409
  `{"error":"batch archived, terminal state"}`

## Conformance
- ✔ `create_app()` in `app/api/main.py` mounts all four routers, optionally
  mounts `app/static` when the dir exists (Task 10 adds it), and wires a real
  session factory into each router's `get_session` stub (replacing the
  `RuntimeError` stubs Tasks 6/7/8 left for "the app factory").
- ✔ `app/main.py` is the one-line `app = create_app()` entry; `app.main:app`
  imports and serves (verified 8 routes).
- ✔ The D8 archived terminal guard (`require_not_archived`) is wired into all
  four write endpoints — create incident, create reinspection, release, create
  report — and 409s with the stable payload `{"error": "batch archived,
  terminal state"}` when the batch is archived. Review Focus #4 satisfied.
- ✔ Non-archived batches are NOT blocked by the guard (guard only fires on
  archived; business-logic 409s like "not isolated" still apply).

## Deviations (documented, justified)
- The plan sketched `require_not_archived(batch_id)` as a `Depends(...)`
  injected per-route. That created a circular import (`main -> release ->
  main`) because `main` imports the routers and the routers would import the
  guard from `main`. Resolution: the guard was extracted to a standalone
  `app/api/guards.py` and called as a plain function with the endpoint's
  request session — same behavior, no cycle. This is a structural deviation
  from the plan's "FastAPI dependency" wording but preserves the exact
  D8 semantics and the stable 409 payload. Recorded here.
- The guard's session comes from the *endpoint's* already-resolved session
  (shared via the plain-function call), not a separate injected session —
  consistent with the existing per-router `get_session` override pattern in
  the tests.

## Test coverage vs plan
- Plan Step 1: (a) four routers' GET endpoints 200, (b) archived batch → 409
  on all four write endpoints, (c) non-archived not blocked. All covered in
  `tests/api/test_main_smoke.py` (4 tests): router mount, archived blocks all
  writes, non-archived release returns 200, create-incident on non-archived
  not blocked by the D8 guard. Passing.

## Open for Task 10
- `app/static` does not exist yet (Task 10 creates the 4 pages); `create_app`
  guards on `os.path.isdir` so it's a no-op now and will mount once Task 10
  adds the dir. No action needed in Task 9.

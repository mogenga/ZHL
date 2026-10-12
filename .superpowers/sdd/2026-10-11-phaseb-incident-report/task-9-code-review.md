# Task 9 — code review (app assembly + D8 archived guard)

Scope: `app/api/main.py` (new `create_app`), `app/api/guards.py` (new
`require_not_archived`), `app/main.py` (new entry), `tests/api/test_main_smoke.py`
(new, 4 tests), plus guard-call additions to the 4 write endpoints in
`incidents.py` / `reinspections.py` / `release.py` / `reports.py`.

Verdict: APPROVE with minor notes. No blocker.

## Architecture
- **Circular import resolved by extraction.** The first attempt wired
  `require_not_archived` as a `Depends(...)` on the batch-scoped endpoints,
  importing it from `app.api.main` — but `main` imports the routers and the
  routers now import `main` → cycle. Fix: the guard lives in a standalone
  `app/api/guards.py` (no app-level import), and the endpoints call it as a
  *plain function* with their own request session. This keeps the guard
  importable by any router without a cycle. Good.
- **Guard semantics:** `require_not_archived(batch_id, session)` loads the
  batch and 409s `{"error": "batch archived, terminal state"}` on archived.
  Unknown/non-UUID ids fall through to the endpoint's own 404. Clean.
- **Wiring:** `create_app(session_factory)` overrides each router's
  `get_session` stub with a request-scoped session closure; mounts all 4
  routers; optionally mounts `app/static` when present. `app.main` is the
  one-line `uvicorn` entry (AGENT.md §5). `app.main:app` imports clean with
  8 routes.

## Guards placed correctly (Review Focus #4)
All four write endpoints now call the shared D8 guard before their own logic:
- `create_incident` → guard, then "not isolated"
- `create_reinspection` → guard after batch resolved from incident
- `release_batch` → guard, then own archived check
- `create_report` → guard, then own archived check

Consistent: the D8 guard is the first 409 on archived batches across every
write path.

## Minor notes (non-blocking)
- m1: `create_report` / `release_batch` keep their *own* per-router archived
  check *in addition to* the shared guard (belt-and-suspenders). The guard
  fires first, so the per-router check is now unreachable on archived —
  harmless but slightly redundant. Acceptable; a future cleanup could drop
  the per-router check now that the guard is the source of truth.
- m2: `create_incident` guard uses `payload.batch_id` (a string in the
  request body); the guard normalises str→UUID. Correct, but the guard's
  "fall through to 404" branch means a malformed batch_id 404s from the
  endpoint rather than the guard — fine.
- m3: `_make_session_dependency` in main.py is now unused (replaced by the
  inline `_open_session` closure). Cosmetic; remove or keep for API
  completeness.

## Gates
- `uv run pytest` → 135 passed (up from 131; +4 smoke).
- `uv run ruff check .` clean; `uv run black --check .` clean.
- `app.domain` untouched this task (frozen module preserved).

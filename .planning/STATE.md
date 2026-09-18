# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-09-18)

**Core value:** Every directive the LLM extracts is both correctly interpreted and provably applied to the returned schedule.
**Current focus:** Phase 1 — Service Skeleton & Contract

## Status

| Phase | Name | Status | Tickets |
|-------|------|--------|---------|
| 1 | Service Skeleton & Contract | ○ Pending | T1, T2 |
| 2 | Optimizer & Validator | ○ Pending | T3, T4 |
| 3 | LLM Interpretation & Guardrails | ○ Pending | T5, T6 |
| 4 | Integration & Scoring Harness | ○ Pending | T7, T8 |
| 5 | Deployment & Docker | ○ Pending | T9, T10 |
| 6 | Documentation & Video | ○ Pending | T11, T12 |

Progress: ░░░░░░░░░░ 0%

## Decisions Locked

- LLM provider: Google Gemini Flash (`gemini-2.0-flash`), env `LLM_API_KEY`
- Deployment: Render, same Docker image as the submitted fallback artifact
- Solver: scipy HiGHS linear program
- Round status: LIVE — deploy by the 3-hour mark

## Key Documents

- `ALGORITHM.md` — implementation contract (LP formulation, prompt design, post-processing)
- `TICKETS.md` — T1–T12 with runnable acceptance checks
- `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` — local test oracle, 10 cases

## Next Action

Phase 1 — scaffold FastAPI + pydantic models.

---
*Last updated: 2026-09-18 after initialization*

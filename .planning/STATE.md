# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-09-18)

**Core value:** Every directive the LLM extracts is both correctly interpreted and provably applied to the returned schedule.
**Current focus:** Phase 5 — Deployment & Docker

## Status

| Phase | Name | Status | Evidence |
|-------|------|--------|----------|
| 1 | Service Skeleton & Contract | ✓ Complete | `/health` ok; 10/10 sample inputs parse; 23-hour body → 400 |
| 2 | Optimizer & Validator | ✓ Complete | 10/10 cases hit organizer optimum exactly; 37 offline tests pass |
| 3 | LLM Interpretation & Guardrails | ✓ Complete | 25/25 interpretation on all 10 public cases |
| 4 | Integration & Scoring Harness | ✓ Complete | `scripts/score.py` reports 60/60 automated + 3/3 latency |
| 5 | Deployment & Docker | ◆ In Progress | image builds; Render blueprint written; public URL pending |
| 6 | Documentation & Video | ◆ In Progress | README complete with generated diagrams; video not started |

Progress: ████████░░ 80%

## Measured Score (local harness, 10 public cases)

| Category | Score | Notes |
|---|---|---|
| LLM Directive Interpretation | 25.00 / 25 | relevance 5.0 · type 5.0 · hours 5.0 · value 5.0 · shape 5.0 |
| Directive Application | 25.00 / 25 | 10/10 schedules pass independent replay |
| Optimization Quality | 10.00 / 10 | LP reproduces the reference cost on every case |
| Performance — latency | 3 / 3 | p95 1.62s (full credit ≤ 5s) |
| API Contract & Schema | untested externally | summary fields recomputed from plan |
| Deployment & Docker | pending | needs public URL + pushed image tag |
| Documentation | pending judge review | README covers all 6 sub-criteria |

## Decisions Locked

- LLM: Google Gemini, `gemini-3.5-flash-lite`. `gemini-2.5-flash-lite` was retired
  for new keys mid-build; `app/llm.py` now walks a fallback list and caches the
  first model id that answers.
- API key names accepted: `GEMINI_API_KEY`, `LLM_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_API_KEY`.
- Solver: scipy HiGHS LP, 72 variables, signed battery flow.
- Deployment: Render, Docker runtime, blueprint in `render.yaml`.

## Security Incident (resolved)

A key was committed to `.env.example` in commit `db0fc23` and pushed. The key was
revoked and replaced by the user; the template is now blank. **The leaked value is
still present in git history** — scrub before making the repository public, or
confirm the old key is dead.

## Next Action

Deploy to Render, verify both endpoints from outside the network, push the Docker
image with an exact tag, then record the 3-minute video.

---
*Last updated: 2026-09-18 after Phase 3/4 verification*

# Roadmap: GridWise LLM

**Milestone:** v1 — submitted, deployed, scoring solution
**Constraint:** 4-hour live round. Phase 5 (deploy) must start by the 3-hour mark.

Phases are ordered by rubric points recoverable per minute, not by dependency convenience.
Phases 2 and 4 are testable with zero LLM quota, which is why they come before Phase 3.

## Phase 1 — Service Skeleton & Contract

**Goal:** A running FastAPI service whose request/response shapes are exactly the spec's.
**Requirements:** API-01, API-02, API-03, API-04
**Tickets:** T1, T2
**Success criteria:**
- `curl localhost:8000/health` → `{"status":"ok"}`
- All 10 sample `input` objects parse; a 23-hour `hours` array is rejected with 422
**Estimated:** 35 min

## Phase 2 — Optimizer & Validator

**Goal:** Provably optimal, provably valid schedules — offline, no API key required.
**Requirements:** OPT-01 … OPT-09, VAL-01, VAL-02, VAL-03
**Tickets:** T3, T4
**Why first:** 35 of 100 points live here and none of it needs LLM quota. If the round goes
badly wrong, a service that solves perfectly with all-`no_op` still banks real points.
**Success criteria:**
- With expected directives injected, all 10 cases are valid and cost ≤ reference + 0.01
- Validator returns `[]` on all 10 reference outputs, non-empty on a mutated plan
**Estimated:** 75 min

## Phase 3 — LLM Interpretation & Guardrails

**Goal:** Operator notes become trustworthy structured directives.
**Requirements:** LLM-01 … LLM-08, GRD-01 … GRD-06
**Tickets:** T5, T6
**Order note:** Guardrails (T5) before the LLM (T6) — guardrails are pure functions testable
with hand-written malformed payloads, and they make a flaky LLM non-fatal.
**Success criteria:**
- 10/10 public cases match expected `directive_type`, `hours`, and numeric values
- Six malformed-payload self-checks all produce legal directive lists without raising
**Estimated:** 75 min

## Phase 4 — Integration & Scoring Harness

**Goal:** The full pipeline wired, plus a local scoreboard that predicts the judge.
**Requirements:** API-05, API-06
**Tickets:** T7, T8
**Success criteria:**
- `pytest -q` green: 10/10 interpretation, mean `cost_ratio ≥ 1.0`, zero validator errors
- End-to-end SAMPLE-01 call returns in < 5s
**Estimated:** 50 min

## Phase 5 — Deployment & Docker

**Goal:** Publicly reachable, plus the fallback image the rubric scores separately.
**Requirements:** DEP-01 … DEP-04
**Tickets:** T9, T10
**Hard deadline:** must start by the 3-hour mark regardless of Phase 3/4 state.
**Success criteria:**
- Public `/health` and a public `/optimize-energy` sample call both succeed from off-network
- `docker run` with the documented command reaches `/health`
**Estimated:** 45 min

## Phase 6 — Documentation & Video

**Goal:** Bank the 10 documentation points; prepare the tie-break artifact.
**Requirements:** DOC-01 … DOC-06
**Tickets:** T11, T12
**Success criteria:**
- Someone on a clean shell follows the README to a green `/health` with zero questions
- Video ≤ 3:00 covering problem, architecture, LLM→guardrails→optimizer flow, run/test
**Estimated:** 45 min

## Cut List

Applied in order if the clock runs short:
1. T12 video (tie-break only, zero base points)
2. Regex fallback extractor in T6
3. Infeasibility ladder in T3
4. Same-type directive merging in T5

Never cut: the self-replay validator (Phase 2) or the README (Phase 6). Both are cheap and
each protects 10+ points.

## Risks

| Risk | Mitigation |
|------|------------|
| Gemini latency or quota blows the p95 band | One call, temp 0, 12s timeout, regex fallback, all-`no_op` last resort |
| Render free-tier cold start fails a judge probe | Keep-warm ping on `/health` during the evaluation window |
| Hidden paraphrase confuses charge vs discharge | Explicit contrast in the prompt; verified on SAMPLE-02/04/08 |
| LP returns simultaneous charge + discharge | Netted in post-processing (ALGORITHM.md §4) |
| Float drift trips the 0.01 tolerance | Recompute grid from balance and SoC cumulatively after rounding |

---
*Roadmap created: 2026-09-18*

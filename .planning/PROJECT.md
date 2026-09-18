# GridWise LLM — BUP CSE Fest 2026 Preliminary

## What This Is

A deployed public HTTP API that reads 1–3 natural-language campus operator notes plus a
24-hour energy scenario, uses an LLM to convert the notes into machine-checkable structured
directives, validates them deterministically, and returns a cost-optimal 24-hour battery /
solar / grid schedule that obeys every directive. Built for the BUP CSE Fest 2026 online
preliminary (4-hour round), judged by an automated harness against hidden test cases.

## Core Value

Every directive the LLM extracts is both **correctly interpreted** and **provably applied**
to the returned schedule — the judge scores those as two separate 25-point categories, and a
cheap schedule built on an ignored directive scores zero on both.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] `GET /health` returns `{"status":"ok"}` within 60s of service start
- [ ] `POST /optimize-energy` implements the exact Problem Statement §7/§10 contract
- [ ] LLM is on the `directive_interpretation` path (mandatory — absence disqualifies)
- [ ] Deterministic guardrails repair or reject LLM output before it reaches the optimizer
- [ ] LP optimizer returns the provably cost-optimal valid schedule
- [ ] Self-replay validator rejects any invalid plan before it leaves the service
- [ ] Publicly reachable deployment + pullable Docker image with exact tag
- [ ] Self-contained README passing the 6 documented reproducibility sub-criteria

### Out of Scope

- Grid export / selling energy back — explicitly excluded by Problem Statement §9.4
- Battery round-trip efficiency losses — spec defines `E_after = E_before ± battery_kwh`, lossless
- Multi-day / rolling horizon — horizon is fixed at 24 hours
- Auth, rate limiting, persistence, dashboards — judge requires unauthenticated direct access
- Fine-tuning or training a model — banned during the judging window
- Directive types beyond the 5 published — hidden cases are guaranteed to use only these

## Context

- Canonical spec: `BUP_CSE_FEST_2026_Preliminary_Problem_Statement_GridWise_LLM.md`
- Rubric: `BUP_CSE_FEST_2026_Participant_Guide_&_Evaluation_Rubric_GridWise_LLM.md`
- 10 worked public cases with expected interpretations and reference costs in
  `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` — these are the local test oracle.
- Implementation contract already written: `ALGORITHM.md`. Ticket breakdown: `TICKETS.md`.
- Two traps found by reading the sample pack:
  1. SAMPLE-03 ("50% of battery capacity" → 100 kWh at capacity 200) requires the battery
     object to be inside the LLM prompt.
  2. `no_charge_window` vs `no_discharge_window` paraphrases are the likeliest hidden-case
     confusion and must be contrasted explicitly in the prompt.

## Constraints

- **Timeline**: 4-hour live round. Deployment must begin by the 3-hour mark — an undeployed perfect service scores 0.
- **Performance**: `POST /optimize-energy` < 30s hard timeout; p95 ≤ 5s for full latency points.
- **Tech stack**: Python + FastAPI + scipy HiGHS LP + Google Gemini Flash.
- **Security**: No secrets in repo, image, logs, or responses. No stack traces in responses.
- **Repository**: Created after question reveal, private during the event, public after the deadline.
- **Reliability**: No 5xx on valid requests; malformed input returns a controlled error.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Linear program (scipy HiGHS), not a heuristic | Objective and all constraints are linear and the battery is lossless, so the LP optimum is *the* optimum → `quality_ratio = 1.0` on all 10 cost points. ~5ms for 96 variables. | — Pending |
| Google Gemini Flash for extraction | Free tier, native JSON schema mode, ~1–2s latency keeps p95 under the 5s full-credit threshold. | — Pending |
| One LLM call for all notes, temperature 0 | Per-note calls would triple latency and risk the p95 band for zero accuracy gain. | — Pending |
| Optimizer + validator built before the LLM | 35 of 100 points (application + cost) are testable offline with zero API quota; the LLM is the time sink. | — Pending |
| Self-replay validator in the request path | Turns "hope the plan is valid" into "provably valid or fall back" — guards the 25-point application category. | — Pending |
| Regex fallback extractor | Provider outage must degrade to a valid schedule, never a 5xx. LLM stays the primary path, satisfying the mandatory-LLM rule. | — Pending |
| Render for deployment | Free tier, HTTPS URL, deploys the same Docker image submitted as the fallback artifact. | — Pending |

## Evolution

- Requirements invalidated → move to Out of Scope with reason
- Requirements validated → move to Validated with phase reference
- Decisions made → append to Key Decisions

---
*Last updated: 2026-09-18 after project initialization*

# Requirements: GridWise LLM

**Defined:** 2026-09-18
**Core Value:** Every directive the LLM extracts is both correctly interpreted and provably applied to the returned schedule.

## v1 Requirements

Every requirement below is traceable to a scored rubric line. Nothing here is optional polish.

### API Contract (10 pts)

- [ ] **API-01**: `GET /health` returns `{"status":"ok"}`, no LLM call, ready within 60s of start
- [ ] **API-02**: `POST /optimize-energy` accepts the exact §7 request schema; 24 unique hours, 1–3 non-empty notes
- [ ] **API-03**: Response contains `scenario_id`, `directive_interpretation`, `hourly_plan`, `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`
- [ ] **API-04**: `hourly_plan` has exactly 24 entries with `battery_action` ∈ {charge, discharge, idle} and `battery_kwh == 0` when idle
- [ ] **API-05**: Summary fields are recomputed from the final `hourly_plan`, matching the judge within 0.01
- [ ] **API-06**: Malformed JSON → 400/422; internal error → controlled 500 with no stack trace or secret

### LLM Interpretation (25 pts)

- [ ] **LLM-01**: Exactly one `directive_interpretation` entry per note, in `note_index` order 0..N-1
- [ ] **LLM-02**: Correct `directive_type` across all 6 types including `no_op` distractors
- [ ] **LLM-03**: Half-open whole-hour windows — "1 PM to 3 PM" → `[13,14]`, "6 PM until 10 PM" → `[18,19,20,21]`
- [ ] **LLM-04**: `factor` is the fraction remaining — "80% reduction" → `0.2`, "roughly 25%" → `0.25`
- [ ] **LLM-05**: Relative quantities resolved against the battery object — "50% of capacity" at 200 kWh → `100`
- [ ] **LLM-06**: `no_op` uses `applies:false` + `structured_adjustment:null`; every other type uses `applies:true`
- [ ] **LLM-07**: Robust to paraphrase; no public sample wording hard-coded
- [ ] **LLM-08**: Single call, temperature 0, ≤12s timeout, one transport retry

### Guardrails

- [ ] **GRD-01**: Unknown `directive_type` → downgraded to `no_op`, never passed through
- [ ] **GRD-02**: Hours sorted, deduped, out-of-range dropped; empty result → `no_op`
- [ ] **GRD-03**: `factor` clamped to [0,1]; reserve clamped to [0, capacity]; `max_grid_kwh` clamped ≥ 0
- [ ] **GRD-04**: `applies` forced consistent with `directive_type`
- [ ] **GRD-05**: Same-type directives merged — min factor, max reserve, min grid cap, union of window hours
- [ ] **GRD-06**: Malformed or absent LLM output never crashes the service and never invents a directive type

### Directive Application & Optimization (25 + 10 pts)

- [ ] **OPT-01**: LP model per ALGORITHM.md §3 — balance, SoC bounds, rate limits, neutrality
- [ ] **OPT-02**: `solar_reduction` applied as `effective_solar = solar × factor` before solving
- [ ] **OPT-03**: `minimum_battery_reserve` raises the SoC floor for listed hours
- [ ] **OPT-04**: `no_charge_window` / `no_discharge_window` zero the respective variable bounds
- [ ] **OPT-05**: `max_grid_window` caps `grid_kwh` for listed hours
- [ ] **OPT-06**: Final `battery_energy_after_kwh` equals `initial_energy_kwh`
- [ ] **OPT-07**: Charge and discharge netted post-solve so exactly one action per hour
- [ ] **OPT-08**: Infeasibility ladder — drop caps, then reserves, then all directives; always returns a plan
- [ ] **OPT-09**: Cost matches or beats the reference cost on all 10 public cases

### Validation

- [ ] **VAL-01**: Self-replay validator re-checks every §11.3 rule at tolerance 0.01
- [ ] **VAL-02**: Runs in the request path; on failure the service returns the no-directive solve instead of an invalid plan
- [ ] **VAL-03**: Returns `[]` for all 10 reference `expected_output` payloads and non-empty for a mutated plan

### Deployment (10 pts)

- [ ] **DEP-01**: Dockerfile binds `0.0.0.0`, exposes the documented port, contains no baked-in secrets
- [ ] **DEP-02**: Image pushed to a registry with an exact pullable tag
- [ ] **DEP-03**: Documented `docker run` command reaches `/health`
- [ ] **DEP-04**: Public endpoint verified from outside the development network

### Documentation (10 pts)

- [ ] **DOC-01**: Clean-machine quickstart (3 pts)
- [ ] **DOC-02**: Env var names + model/provider disclosure, no values (2 pts)
- [ ] **DOC-03**: Public-sample test command and expected result (2 pts)
- [ ] **DOC-04**: LLM → guardrails → optimizer architecture explanation (1 pt)
- [ ] **DOC-05**: Docker pull/run fallback instructions (1 pt)
- [ ] **DOC-06**: Dependencies, limitations, secret handling (1 pt)

## v2 Requirements

Deferred — no rubric line, do not build during the round.

- **V2-01**: Response caching keyed on scenario hash
- **V2-02**: Multi-provider LLM failover
- **V2-03**: Prometheus metrics / structured request logs
- **V2-04**: Ensemble or self-consistency voting across LLM samples

## Out of Scope

| Feature | Reason |
|---------|--------|
| Grid export | Excluded by Problem Statement §9.4 |
| Battery efficiency losses | Spec defines lossless transitions |
| Auth / API keys on the endpoint | Judge requires unauthenticated access |
| Database / persistence | Every request is stateless |
| Custom solver implementation | scipy HiGHS is exact and 5ms; writing one wastes round time |
| Per-note LLM calls | Triples latency, risks the p95 band, no accuracy gain |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| API-01, API-02, API-03, API-04 | Phase 1 | Pending |
| OPT-01 … OPT-09 | Phase 2 | Pending |
| VAL-01, VAL-02, VAL-03 | Phase 2 | Pending |
| GRD-01 … GRD-06 | Phase 3 | Pending |
| LLM-01 … LLM-08 | Phase 3 | Pending |
| API-05, API-06 | Phase 4 | Pending |
| DEP-01 … DEP-04 | Phase 5 | Pending |
| DOC-01 … DOC-06 | Phase 6 | Pending |

**Coverage:**
- v1 requirements: 40 total
- Mapped to phases: 40
- Unmapped: 0 ✓

---
*Requirements defined: 2026-09-18*

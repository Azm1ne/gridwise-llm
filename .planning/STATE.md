# Project State

See: .planning/PROJECT.md · Code review: .planning/CODE-REVIEW.md

**Core value:** Every directive the LLM extracts is both correctly interpreted and provably applied.
**Live service:** https://gridwise-llm-f57f.onrender.com
**Repo:** https://github.com/Azm1ne/gridwise-llm

## Status

| Phase | Name | Status |
|-------|------|--------|
| 1 | Service Skeleton & Contract | ✓ Complete |
| 2 | Optimizer & Validator | ✓ Complete |
| 3 | LLM Interpretation & Guardrails | ✓ Complete |
| 4 | Integration & Scoring Harness | ✓ Complete |
| 5 | Deployment & Docker | ◆ Deployed on Render; registry image push blocked on `gh auth refresh -s write:packages` |
| 6 | Documentation & Video | ◆ README complete; 3-minute video not started |

Progress: █████████░ 90%

## Measured against the live deployment

Judge simulation, paced 2.5s (scripts/simulate_judge.py):

| Metric | Result |
|---|---|
| Interpretation exact | 10/10 |
| 5xx / no-response | 0 |
| Latency | median 1.68s · p95 1.84s → 3/3 points |
| Burst, 6 concurrent | all 200 |
| Deterministic across repeats | yes |
| Malformed/hostile handling | 6/6 as expected |
| Secret leaks | none |
| Prompt injection contained | yes |

Teammate harness, 34 cases back-to-back: 77.4/80 before the code-review fixes.
Every miss correlated with a Gemini 429, never with wrong logic.
`hours 34/34 · values 34/34 · shape 34/34 · battery 34/34 · neutrality 34/34 · totals 34/34`

## Defects found and fixed (in order of severity)

1. **Validator proved nothing** — `replay()` took a `directives` argument it never read,
   and callers passed the post-relaxation scenario. A plan shipping 175 kWh against a
   reported 1 kWh cap validated clean. Now validates against the promised scenario.
2. **Provider outage returned nothing** — the retry budget was only enforced on 429, so a
   hanging provider could run 96s against a 30s judge timeout. Budget now guards every
   attempt; measured 6.02s against a 6s budget.
3. **Malformed request returned 500** — custom pydantic validators put a raw `ValueError`
   in the error `ctx`, so `json.dumps` raised inside the 400 handler.
4. **Rate-limit handling made throttling worse** — a 429 advanced through the model
   fallback list, but all ids share one quota. Now backs off on the same model.
5. **A 400 permanently blacklisted every model** — one sweep silently made the service
   return all-`no_op` for the rest of the run. Only a 404 blacklists now.
6. **Hour-window expansion could OOM** — `{"start":0,"end":1e9}` materialised the range
   before the 0–23 filter. Clamped first.
7. **Infeasible battery state returned 500** — now a controlled 400.
8. **Retired model id** — `gemini-2.5-flash-lite` 404s for new keys; default is
   `gemini-3.5-flash-lite` with a fallback chain and dead-id cache.

51 tests, all passing. Every fix carries a regression test.

## Open risks

- **Gemini free-tier quota is the binding constraint on score.** Enabling billing on the
  key is worth roughly 3 rubric points and costs pennies at this volume.
- Set `LLM_MODEL=gemini-3.5-flash-lite` in Render env vars so no cold start wastes a
  round trip on the retired id.
- Repo must be made public after the submission deadline.

## Next Actions

1. Enable billing on the Gemini key (highest value remaining).
2. `gh auth refresh -s write:packages`, then push `ghcr.io/azm1ne/gridwise-llm:v1`.
3. Record the 3-minute video (tie-break only, no base points).

---
*Last updated: 2026-09-18 after code review and live verification*

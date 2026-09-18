# GridWise LLM — Algorithm Spec

Canonical: `BUP_CSE_FEST_2026_Preliminary_Problem_Statement_GridWise_LLM.md`.
This file is the implementation contract. Anyone (human or agent) can build from it alone.

## 0. Pipeline

```
request -> pydantic validate -> LLM extract (1 call, all notes)
        -> deterministic guardrails (repair or downgrade to no_op)
        -> LP optimizer (scipy HiGHS)
        -> post-process (net charge/discharge, round, recompute)
        -> self-replay validator -> response
```

Everything after the LLM is deterministic. The LLM never touches numbers it did not read from a note.

## 1. LLM extraction (25 pts)

**One call per request**, all 1–3 notes in a single prompt, JSON-only output. One call keeps p95 under 5 s (3/3 latency points).

Prompt MUST contain:
- The 5 directive types + `no_op`, with exact `structured_adjustment` shapes.
- **Hour convention: half-open `[start, end)`, whole hours.** `1 PM to 3 PM` -> `[13,14]`. `6 PM until 10 PM` -> `[18,19,20,21]`. `2 AM until 5 AM` -> `[2,3,4]`.
- **`factor` = fraction REMAINING.** `80% reduction` -> `0.2`. `roughly 25% of forecast` -> `0.25`. `about half` -> `0.5`.
- **The battery object verbatim** — required for relative language. `"50% of battery capacity"` with `capacity_kwh: 200` -> `minimum_energy_kwh: 100` (SAMPLE-03). Without capacity in the prompt this case is unscoreable.
- Distractors (campus admin news, deadlines, bookings, menus) -> `no_op`, `applies:false`, `structured_adjustment:null`.
- Exactly one entry per note, in `note_index` order 0..N-1.
- Few-shot: one `solar_reduction` (percent-reduction wording), one `no_charge_window`, one percentage reserve, one `max_grid_window`, one distractor. **Paraphrase the samples — do not copy public wording** (explicit rule in the guide).

Output contract: `{"directives":[{note_index,applies,directive_type,structured_adjustment,explanation}]}`.
Temperature 0. Response-format JSON where the provider supports it.

Distinction the model gets wrong most: **charger isolated / charging unavailable -> `no_charge_window`** vs **must not discharge / relay testing -> `no_discharge_window`**. Call it out explicitly in the prompt.

### Fallback ladder (never 5xx)
1. LLM call (timeout 12 s, 1 retry on transport error).
2. On failure/malformed JSON: regex extractor (keyword + time-range parser) — safety net only, not the primary path.
3. On total failure: all notes -> `no_op`. Schedule still returns valid and optimal-unconstrained. Never crash, never invent a directive type.

## 2. Guardrails (deterministic, between LLM and optimizer)

Per entry, in order. Repair when safe; downgrade to `no_op` when not.

| Check | Action on failure |
|---|---|
| `directive_type` in allowed set | -> `no_op` |
| one entry per note, indices 0..N-1 unique | rebuild array; missing -> `no_op` |
| hours: ints, 0..23, unique, ascending | sort + dedupe + drop out-of-range (repair); empty result -> `no_op` |
| `solar_reduction.factor` finite in [0,1] | clamp; non-numeric -> `no_op` |
| `minimum_energy_kwh` finite, 0 <= x <= capacity | clamp; non-numeric -> `no_op` |
| `max_grid_kwh` finite, >= 0 | clamp; non-numeric -> `no_op` |
| `applies` consistency | force `applies = (type != no_op)`; `no_op` -> adjustment `null` |

Merging multiple directives of the same type: `solar_reduction` -> take min factor per hour; `minimum_battery_reserve` -> max per hour; `max_grid_window` -> min per hour; windows -> union of hours.

## 3. Optimizer — LP, not a heuristic (10 pts, exact optimum)

Cost is linear, all constraints are linear, battery round-trip efficiency is 1.0 (`E_after = E_before ± battery_kwh`). So this is a **pure linear program**: `scipy.optimize.linprog(method="highs")` returns the true optimum in ~5 ms. `quality_ratio = 1.0`. A greedy heuristic would leak optimization points for no reason.

**Variables** (96): for each hour `h`: `g[h]` grid, `s[h]` solar used, `c[h]` charge, `d[h]` discharge.

**Objective:** `min Σ tariff[h]·g[h]`

**Bounds**
```
g[h] ∈ [0, cap_grid[h]]        cap_grid[h] = max_grid_kwh if h in cap window else inf
s[h] ∈ [0, eff_solar[h]]       eff_solar[h] = solar[h] * factor[h]   (factor 1.0 by default)
c[h] ∈ [0, 0 if h in no_charge    else max_charge_kwh_per_hour]
d[h] ∈ [0, 0 if h in no_discharge else max_discharge_kwh_per_hour]
```

**Equalities** (25 rows)
```
balance, per hour:  g[h] + s[h] + d[h] - c[h] = demand[h]
neutrality:         Σ_h (c[h] - d[h]) = 0          # final SoC == initial
```

**Inequalities** (48 rows) — with `E[h] = E0 + Σ_{k<=h} (c[k] - d[k])`
```
upper SoC:  Σ_{k<=h} (c[k] - d[k])  <=  capacity_kwh - E0
lower SoC: -Σ_{k<=h} (c[k] - d[k])  <=  E0 - lo[h]
            lo[h] = max(minimum_energy_kwh, reserve_directive[h] or 0)
```
That is the whole model. Every directive from §04 maps to exactly one of these lines.

**Infeasible?** Judge scenarios are guaranteed feasible, so infeasibility means a bad LLM extraction. Ladder: (1) drop `max_grid_window` caps, (2) drop reserve directives, (3) drop all directives. Return the first feasible solve. Log which tier fired.

## 4. Post-processing (protects the 10 API-contract pts)

1. **Net the battery.** LP may return `c[h]>0` and `d[h]>0` together (degenerate, zero-cost). `net = c[h]-d[h]`; `action = charge|discharge|idle`; `battery_kwh = |net|`. Netting only shrinks magnitudes, so rate limits and no-charge/no-discharge windows still hold.
2. **Round** `s, battery_kwh` to 6 dp, then **recompute** `g[h] = demand[h] + charge[h] - discharge[h] - s[h]`, clamped at 0. Energy balance then holds to machine precision instead of solver precision.
3. **Recompute** `battery_energy_after_kwh` cumulatively from the rounded actions — never echo the solver's value.
4. **Recompute** `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh` from the final `hourly_plan`. These must match the judge's recalculation within 0.01.

## 5. Self-replay validator (the single highest-value component)

Independent function that re-checks the finished response against the guardrailed directives, exactly as the judge does: 24 unique hours; non-negative finite fields; `solar_used <= eff_solar`; balance per hour; SoC within `[lo[h], capacity]`; rate limits; `battery_kwh == 0` when idle; final SoC == initial; grid caps; no-charge/no-discharge windows; summary fields match. Tolerance 0.01.

Runs in-process on every request (sub-ms) and in tests. If it fails, fall back to the no-directive solve rather than returning an invalid plan.

## 6. Service

FastAPI. `GET /health` -> `{"status":"ok"}` (no LLM, no I/O — must answer within 60 s of boot). `POST /optimize-energy`. Pydantic v2 models give 422 on malformed body for free; catch-all handler returns a controlled 500 with no stack trace or secrets. Single uvicorn worker is enough; the LP is microseconds and the LLM call is the only latency.

## 7. Known risks
- Provider latency is the only thing between us and 3/3 latency points -> one call, temperature 0, 12 s timeout, small fast model.
- Hidden paraphrases of `no_charge` vs `no_discharge` -> explicit prompt contrast + eval on all 10 public cases.
- Percentage-of-capacity reserves -> battery object must be in the prompt.

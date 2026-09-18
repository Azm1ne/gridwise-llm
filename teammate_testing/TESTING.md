# GridWise Test Suite

Drop this folder into your repo as `tests/`. Everything runs on **stdlib Python 3.8+** — no `pip install`.

| File | What it is |
|---|---|
| `gridwise_test.py` | The harness. Runs cases against your service and scores you against the rubric. |
| `public_sample_cases.json` | The organizer's 10 official public cases, unchanged. |
| `edge_cases.json` | 24 unofficial edge cases written from the Problem Statement rules. |
| `mock_service.py` | A fake service for checking the harness itself works. Not a solution. |

---

## Quick start

```bash
# 1. Start your service (in another terminal)
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 2. Run everything
python3 gridwise_test.py --url http://localhost:8000 \
    --cases public_sample_cases.json edge_cases.json
```

Exit code is `0` when every graded check passes, `1` otherwise — so you can wire it into CI.

---

## Command line

```bash
python3 gridwise_test.py --url URL --cases FILE [FILE ...] [options]
```

| Flag | Effect |
|---|---|
| `--url` | Base URL. **Required.** No trailing slash needed. |
| `--cases` | One or more case-pack JSON files. **Required.** |
| `--only ID [ID ...]` | Run only these case ids. |
| `--timeout N` | Per-request timeout in seconds. Default 30, matching the spec limit. |
| `--repeat N` | Send each case N times to catch nondeterministic LLM output. |
| `--no-robustness` | Skip the malformed-input suite. |
| `--save DIR` | Write every raw response to `DIR/<case-id>.json`. |
| `--report FILE` | Write a machine-readable JSON report. |
| `--quiet` | Print only failures. |
| `--no-color` | Plain output for logs and CI. |

### Recipes

```bash
# Official cases only, first pass
python3 gridwise_test.py --url http://localhost:8000 --cases public_sample_cases.json

# Debug one failing case, keeping the raw response
python3 gridwise_test.py --url http://localhost:8000 --cases edge_cases.json \
    --only EDGE-22 --save out/

# Hunt nondeterminism: 3 runs per case, the way repeated hidden tests will hit you
python3 gridwise_test.py --url http://localhost:8000 --cases public_sample_cases.json --repeat 3

# Final check against the DEPLOYED url, from a different network
python3 gridwise_test.py --url https://your-app.example.com \
    --cases public_sample_cases.json edge_cases.json \
    --report final_report.json --no-color | tee final_run.txt

# Verify the harness itself is sane before trusting it
python3 mock_service.py --cases public_sample_cases.json edge_cases.json &
python3 gridwise_test.py --url http://localhost:8000 --cases public_sample_cases.json edge_cases.json
# expect 80.0 / 80
```

---

## What it checks

Every requirement in the two PDFs that a machine can verify.

**Schema and contract**
All 7 top-level response fields, `scenario_id` echoed, non-empty `plan_summary`, one interpretation entry per note in `note_index` order 0..N-1, all 5 interpretation fields, all 6 plan fields, exactly 24 hours covering 0–23.

**Interpretation guardrails**
`directive_type` in the allowed six; `no_op` ⇒ `applies=false` and `structured_adjustment=null`; every other type ⇒ `applies=true`; exact adjustment key set per type; `hours` unique ints 0–23 ascending; `factor` in [0,1]; reserve in [0, capacity]; `max_grid_kwh` finite and non-negative.

**Interpretation vs ground truth**
`applies`, `directive_type`, `hours`, and numeric values compared to the organizer's expected interpretation. `explanation` wording is never compared.

**Plan replay — the important part**
The plan is replayed against the **ground-truth directives, not yours**, exactly as the judge does. A wrong interpretation therefore invalidates the case even if your plan is internally consistent.

Per hour: energy balance within 0.01, `solar_used ≤ effective_solar` after `solar_reduction`, charge/discharge within hourly rate limits, `battery_energy_after_kwh` matches the hour-by-hour replay, battery between the active reserve and capacity, `idle ⇒ battery_kwh = 0`, no negative values, no charging inside `no_charge_window`, no discharging inside `no_discharge_window`, grid within `max_grid_kwh`. Then end-of-day neutrality, and `total_grid_kwh` / `total_cost_bdt` / `peak_grid_kwh` recomputed from your own `hourly_plan`.

**Optimization quality**
`min(1, reference_cost / your_cost)` per case. Invalid plans score 0 for that case.

**Robustness** — each must return 400 or 422, never 5xx, never a crash:
malformed JSON · empty body · JSON array instead of object · missing `battery` · 23 hour entries · duplicate hour number · zero notes · four notes · non-numeric `demand_kwh` · negative capacity. Error bodies are also scanned for `sk-`, `Bearer`, `Traceback`, `API_KEY`, and similar leak markers.

**Latency** — min/median/p95/max, scored on the guide's bands: p95 ≤5s → 3/3, 5–15s → 2/3, 15–30s → 1/3, >30s → 0/3.

---

## Reading the output

```
[PASS] SAMPLE-06  Multiple notes with distractor    1840ms  cost   34090.00 / ref   34090.00  opt 100.0%
[FAIL] EDGE-22    Reduction percentage vs remaini    1620ms  cost   26880.00 / ref   27733.75  opt 100.0%
         i_value       note 0: factor=0.75 but ground truth is 0.25
         a_balance     h12: solar_used_kwh 116.25 exceeds effective solar 38.75
```

`opt 100.0%` on a failing case is not good news — it means your plan was cheap *because* it ignored the directive. Cost is only scored after the case is valid.

The score block estimates the **80 automatable points**. The remaining 20 (Deployment/Docker 10, Documentation 10) are printed as a manual checklist.

```
  1  LLM Directive Interpretation             21.3 / 25  [#################...]
```

Bucket names in failure lines map to rubric rows: `i_*` → category 1, `a_*` → category 2, `schema`/`totals` → category 4.

---

## The edge cases

Official pack: 10 cases. Edge pack: 24 more, all verified feasible, each with a reference plan produced by an LP that **reproduces the organizer's optimal cost on all 10 public cases to within 0.01 BDT**. So the reference costs are trustworthy targets, though they are not organizer ground truth.

### Time-window boundaries — where most teams silently lose points

| Case | Note wording | Correct answer | The trap |
|---|---|---|---|
| EDGE-01 | "3 PM to 4 PM" | `[15]` | Expanding to `[15,16]` |
| EDGE-02 | "10 PM until midnight" | `[22,23]` | Wrapping to hour 0 |
| EDGE-05 | "9 AM until noon" | `[9,10,11]` | Treating noon as hour 11 or 13 |
| EDGE-08 | "midnight until 4 AM" | `[0,1,2,3]` | Starting at hour 1 |
| EDGE-09 | "at all times today" | `[0..23]` | Returning an empty or partial array |
| EDGE-24 | "11 PM until 1 AM" | *undefined by the spec* | Crashing on ambiguity |

### The `factor` trap — one sign error, every affected case invalid

| Case | Wording | `factor` |
|---|---|---|
| EDGE-03 | "drop to about 20%" | `0.2` |
| EDGE-04 | "one-fifth of normal output" | `0.2` |
| EDGE-05 | "no usable solar at all" | `0.0` |
| EDGE-21 | "halved" | `0.5` |
| EDGE-22 | "**a 75% reduction**" | **`0.25`** |

EDGE-22 is the pair to watch. "Drops to 20%" and "80% reduction" both mean 0.2. Test both phrasings explicitly in your prompt.

### Relevance and traps

- **EDGE-06** — three notes, all distractors. Every entry still returned, all `no_op`.
- **EDGE-20** — note 0 is thick with energy vocabulary ("solar panel warranty renewal paperwork") but changes nothing today. Keyword matchers fire here and lose the point.
- **EDGE-14** — a real `solar_reduction` on hours 2–4 where solar is already 0. Still `applies=true`. Do not suppress a directive because it has no economic effect.

### Numeric extraction

- **EDGE-07** — "60% of battery capacity" with `capacity_kwh = 240` → `144.0`. Your model must read capacity from the request.
- **EDGE-18** — reserve equal to capacity (240 of 240). The legal maximum; your guardrail must accept it.

### Optimizer stress

- **EDGE-10** — charging disabled all 24 hours. Combined with neutrality, the only valid plan is a fully idle battery. Optimizers that declare infeasibility fail here.
- **EDGE-11** — evening grid cap below demand. Forces pre-charging hours earlier.
- **EDGE-12** — flat tariff. No arbitrage exists; the optimum is an idle battery. Catches optimizers that churn the battery for nothing.
- **EDGE-15 / EDGE-16** — battery starts exactly at its floor, and exactly at capacity. Both bound edges.
- **EDGE-19** — two overlapping `solar_reduction` directives. Report both as written; apply the **more restrictive** factor where they overlap.
- **EDGE-23** — charge and discharge both blocked across the peak. Battery frozen; peak demand must come from the grid.
- **EDGE-17** — three directive types interacting in overlapping evening hours.

`EDGE-24` carries `grade_interpretation: false` — the spec does not define wrap-around windows, so its interpretation is not compared. The harness instead replays your plan against **your own** returned directives, checking self-consistency only.

---

## Suggested order of work

1. **Schema first.** `--only SAMPLE-01` until the schema bucket is clean. Nothing else matters while the contract is broken.
2. **Validity next.** All 10 public cases with zero `a_*` errors, even at bad cost.
3. **Then cost.** Push `opt` to 100% on the public cases.
4. **Then the edge pack.** This is where the hidden-test points live.
5. **`--repeat 3`** to catch LLM nondeterminism.
6. **Re-run against the deployed URL from another network** — phone hotspot, a friend's laptop, anything outside your dev environment.

---

## Important caveats

- `edge_cases.json` is **unofficial**. It was written from the Problem Statement rules, not supplied by the organizers. Where it ever disagrees with the Problem Statement, the Problem Statement wins.
- Never hard-code any wording, id, hour or number from either pack. Hidden notes paraphrase, and hard-coded matching is explicitly non-compliant.
- Passing everything here does not guarantee full marks. It means no *machine-checkable* requirement in the two PDFs is being violated.
- The harness cannot verify that an LLM is genuinely in your interpretation path. That is checked by the organizers against your repo, and faking it disqualifies the submission.

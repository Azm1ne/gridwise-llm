---
phase: gridwise-submission
reviewed: 2026-09-18T23:30:00Z
depth: deep
files_reviewed: 7
files_reviewed_list:
  - app/main.py
  - app/models.py
  - app/llm.py
  - app/directives.py
  - app/optimizer.py
  - app/validate.py
  - tests/test_offline.py
findings:
  critical: 2
  warning: 5
  info: 5
  total: 12
status: issues_found
---

# GridWise LLM: Code Review Report

**Reviewed:** 2026-09-18T23:30:00Z
**Depth:** deep (cross-file, with executable probes + 4000-scenario fuzz)
**Files Reviewed:** 7
**Status:** issues_found

## Summary

The LP formulation is correct. I verified every rule in spec sections 09 and 11.3 against
`optimizer._solve_lp` line by line and then fuzzed 4000 randomized scenarios (varying capacity,
initial/minimum SoC, zero and huge charge/discharge limits, zero demand, zero solar, tariff ties,
0-factor solar reductions, 0-value grid caps) measuring the residual of every judge check:

```
balance     8.5e-14      solar_over  4.1e-07      min_soc   0.0
soc         3.4e-13      grid_over   0.0          cap       0.0
neutral     0.0
```

Against a 0.01 tolerance that is five orders of magnitude of headroom. The sign convention
(`grid + solar - flow = demand`, flow positive = charge) matches 9.5, the cumulative-flow
inequalities reproduce `min_soc[h] <= E_after[h] <= capacity` exactly, and `sum(flow) = 0`
is 9.6 verbatim. **The rounding and recompute steps in `_build_plan` are not a problem** — I
looked hard and found nothing. Signed-single-variable battery flow correctly makes
simultaneous charge+discharge structurally impossible.

The defects are all on the periphery: the replay validator does not validate what it claims to,
the LLM client can blow past the judge's request timeout by 3x, untrusted LLM numbers reach an
unbounded allocation, and two structurally-valid request shapes return 500.

---

## Critical Issues

### CR-01: `replay()` ignores its `directives` argument and is handed the *relaxed* scenario — the safety net never fires for the one case it exists for

**File:** `app/validate.py:15`, `app/main.py:40-48`

**Issue:** `replay(sc, directives, plan, totals)` never references `directives` anywhere in its
body (grep confirms: the name appears only in the signature and the import line). All checks are
against `sc`. And `main.py:40` passes the `Scenario` that `solve()` *returned* — which, whenever
`solve()` relaxed to tier 1/2/3, is the scenario with those directives already removed. So replay
validates the plan against the same weakened bounds the optimizer used. Its docstring
("Deliberately does not reuse the optimizer's state") is false.

The consequence is not theoretical. Concrete failing input (measured end-to-end through
`TestClient`): any scenario where the LLM extracts a `max_grid_window` the battery cannot honor,
e.g. `max_grid_kwh = 1` over hours 0..23 on SAMPLE-01.

```
HTTP 200
reported directives: [('max_grid_window', 1.0), ('max_grid_window', 1.0)]
actual peak_grid_kwh: 175.0
replay() errors:     []            <-- clean bill of health
```

The response tells the judge "grid import is capped at 1 kWh" and ships a plan that imports 175.
Spec 11.2: *"Correct extraction without correct downstream application does not pass the case."*
Replaying against the *full*-directive scenario returns `['h0: grid 50.0 exceeds cap 0.0', ...]`,
so the information to catch this exists — it is just thrown away.

A second consequence: at tier 0 the fuzz shows replay residuals of 1e-13, so `errors` is never
non-empty in practice. The `main.py:43` fallback is dead code. The validator currently proves
nothing it isn't already guaranteed by construction.

**Fix:** validate against the directives the response actually claims, and make the mismatch
visible in the payload rather than silently shipping a contradiction.

```python
# main.py
plan, scenario, tier = solve(request.hours, request.battery, directives)
claimed = compile_scenario(request.hours, request.battery, directives)   # full, unrelaxed
totals = totals_from(plan, claimed)
errors = replay(claimed, directives, plan, totals)
if errors:
    # The plan cannot satisfy what we extracted. Report only the directives the
    # plan actually honours, so interpretation and application stay consistent.
    log.warning("plan violates extracted directives: %s", errors[:3])
    directives = [d if d.directive_type in _kept(tier) else _downgrade(d) for d in directives]
```

At minimum, `replay` must take the unrelaxed scenario. `import compile_scenario` is already
available in `main.py`'s dependency graph via `optimizer`.

---

### CR-02: LLM retry loop ignores `BUDGET_S` on every failure path except 429 — up to 96 s per request against a 30 s judge timeout

**File:** `app/llm.py:210-262`

**Issue:** `deadline` is computed at line 210 but only consulted inside the `status == 429`
branch (line 245). Every other failure — connect timeout, read timeout, DNS failure, 500, 502,
503, malformed JSON — falls through to `if attempt: break`, which permits one immediate retry
per model before moving to the next candidate. With the default candidate list, that is 4 models
x 2 attempts = 8 upstream calls, each allowed to burn the full `TIMEOUT_S`.

Measured (patched `AsyncClient.post` to time out, `TIMEOUT_S` scaled to 0.05 s):

```
provider-timeout path: 8 upstream calls, elapsed 0.438s at TIMEOUT_S=0.05
-> at the real TIMEOUT_S=12 that is 96s, vs BUDGET_S=24 and a 30s judge limit
```

The module docstring and the `BUDGET_S` comment both say the design intent is "stop well short
[of 30 s] and let the caller degrade to no_op rather than time out." The code does that for
rate limiting only. For a provider outage — the far more common failure — the request hangs for
96 s and the judge gets *no response at all*. That loses the entire case (validity, cost,
interpretation), where degrading to no_op would still have scored schedule validity and cost.

Secondary: the non-429 retry has zero backoff, so a struggling provider gets hit twice
back-to-back.

**Fix:** check the deadline at the top of every attempt, and cap the per-call timeout by what's
left.

```python
for model in candidates:
    for attempt in range(1 + len(RETRY_AFTER_S)):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            log.warning("llm budget exhausted -- degrading to no_op")
            return None
        try:
            response = await client.post(
                API_URL.format(model=model), json=payload,
                headers={"x-goog-api-key": api_key},
                timeout=min(TIMEOUT_S, remaining),
            )
```

---

## Warnings

### WR-01: unbounded `range()` built from untrusted LLM numbers — `{"start": 0, "end": 1e9}` OOM-kills the process

**File:** `app/directives.py:67-71`

**Issue:** `_clean_hours` accepts a `{"start", "end"}` dict and does
`raw = list(range(int(start), int(end)))` *before* any 0..23 filtering. `_finite` accepts any
finite float, so the bound is whatever the model emitted. Spec section 08 is explicit that LLM
output is untrusted structured data; this is the one place it reaches an unbounded allocation.

Measured:

```
end=10_000      0.002s    peak RSS  26MB
end=5_000_000   0.975s    peak RSS 218MB
```

`end = 1e9` (a single hallucinated exponent, or a model emitting an epoch timestamp as `end`)
allocates ~40 GB and OOM-kills the container. On a hackathon deploy that takes the whole
service down mid-evaluation, failing every remaining case — not just this one.

**Fix:** clamp before materializing. One line, same place.

```python
start, end = _finite(raw.get("start")), _finite(raw.get("end"))
if start is None or end is None:
    return []
raw = list(range(max(0, int(start)), min(24, int(end))))
```

### WR-02: structurally valid requests return 500 — `initial_energy_kwh` below `minimum_energy_kwh`, or above `capacity_kwh`

**File:** `app/optimizer.py:105`, `app/models.py:30-35`

**Issue:** `Battery` validates each field independently (`ge=0`, `gt=0`) but never cross-checks
them. Two shapes pass validation and are infeasible *at tier 3 with zero directives*, so
`solve()` exhausts the tier list and raises:

- `initial_energy_kwh=10, minimum_energy_kwh=50` — end-of-day neutrality (9.6) forces
  `E_after[23] = 10`, which is below the base minimum at hour 23. Infeasible for *any* schedule.
- `initial_energy_kwh > capacity_kwh` — hour-23 upper bound `sum(flow) <= capacity - initial < 0`
  contradicts `sum(flow) = 0`.

Both confirmed over HTTP:

```
HTTP initial<minimum  -> 500 {"error":"internal error"}
HTTP initial>capacity -> 500 {"error":"internal error"}
```

The body leaks nothing (the `Exception` handler at `main.py:86` is correct — verified no
traceback, no key, no path reaches the wire), so this is a *controlled* 500 per spec 06.1.
But spec 06.1 also offers 400/422 for "structurally invalid" and "semantically invalid but
well-formed" requests, and a robustness probe in the hidden set is far more likely to be graded
as a 4xx-expected case than a 500-expected one.

**Fix:** reject the contradiction at the trust boundary, where it becomes the existing controlled
400 path.

```python
# models.py, on Battery
@model_validator(mode="after")
def _coherent(self):
    if not (self.minimum_energy_kwh <= self.initial_energy_kwh <= self.capacity_kwh):
        raise ValueError("require minimum_energy_kwh <= initial_energy_kwh <= capacity_kwh")
    return self
```

### WR-03: relaxation is per-type, not per-directive — one impossible directive discards every sibling of the same type

**File:** `app/optimizer.py:93-99`

**Issue:** tier 1 strips *all* `max_grid_window` directives; tier 2 strips *all*
`minimum_battery_reserve`. If the LLM extracts two caps and only one is unsatisfiable, the
satisfiable one is thrown away too, turning one lost directive check into two.

Measured (demand 100/h, no solar, tariff 10 except hour 5 at 1, 200 kWh battery):

```
cap alone        -> tier 0   h5 grid: 120.0      (cap 120 honoured)
cap + impossible -> tier 1   h5 grid: 150.0      (cap 120 now ignored too)
judge errors: ['h0: grid 50.0 exceeds cap 0.0', 'h5: grid 150.0 exceeds cap 120.0']
```

**Fix:** drop one directive at a time, cheapest-to-lose first, instead of whole classes. The LP
is ~5 ms, so up to three extra solves is free.

```python
def _tiers(directives):
    yield directives
    for i in range(len(directives)):          # drop exactly one
        yield [d for j, d in enumerate(directives) if j != i]
    for drop in ("max_grid_window", "minimum_battery_reserve"):
        yield [d for d in directives if d.directive_type != drop]
    yield []
```

### WR-04: one transient 400 permanently blacklists every model id, process-wide, with no recovery

**File:** `app/llm.py:168, 235-239`

**Issue:** `_dead_models` is module-global, is only ever added to, and is never cleared or
expired. A 400 is treated as "retired or rejected: stop trying it" — but Gemini also returns 400
for `API_KEY_INVALID`, for a transient quota-project misconfiguration, and for request-level
rejections. One such sweep marks all four candidates dead forever.

Measured:

```
after one 400 sweep, _dead_models = ['gemini-2.5-flash', 'gemini-2.5-flash-lite',
                                     'gemini-3.5-flash-lite', 'gemini-flash-lite-latest']
next request upstream calls: 0   (permanently degraded to no_op until restart)
```

During a 4-hour scoring window, a 30-second key or quota hiccup silently converts the service
into an all-`no_op` responder — which still returns 200 with a valid schedule, so nothing in the
response signals the failure. Every interpretation point is lost for the rest of the run.

**Fix:** blacklist 404 only (genuinely "model retired"); treat 400 as retryable-next-request.

```python
if status == 404:
    _dead_models.add(model)
    if model == _working_model:
        _working_model = None
    break
if status == 400:
    if model == _working_model:
        _working_model = None
    break            # skip this model for THIS request only
```

### WR-05: `main.py` reports directives it knowingly did not apply

**File:** `app/main.py:40-65`

**Issue:** `solve()` returns `tier`, and `main.py:55` logs it — but `tier` never influences the
response. `directive_interpretation` is built from the pre-relaxation `directives` list
(`main.py:61`) regardless, and the same is true after the `errors` fallback at line 47, which
re-solves with `[]` while still reporting the full directive list. `summarise()` at line 63 then
narrates "Applied N operator directive(s): max_grid_window, ..." over a plan that applies none
of them.

This is the reporting half of CR-01 and shares its fix; listed separately because it is a
distinct code path (the `tier > 0` path, which does *not* go through `replay` at all).

**Fix:** see CR-01. Either downgrade relaxed directives in the response, or keep the
directive-applying plan and accept the validity risk — but do not ship a plan and an
interpretation that contradict each other.

---

## Info

### IN-01: duplicate `global _working_model`

**File:** `app/llm.py:189` and `app/llm.py:196`

Declared twice in the same function with no intervening assignment. Legal, compiles clean,
purely dead. Delete line 196.

### IN-02: overlapping `solar_reduction` factors multiply rather than taking the tightest

**File:** `app/directives.py:167-168`

`scenario.effective_solar[hour] *= factor`. Two directives on hour 10 with factors 0.5 and 0.2
yield `0.1 x solar` (measured: 10.0 from a 100 kWh hour), not the `min`-factor 20.0 the module
docstring's "tightest constraint wins" promises. This is *safe* — the plan uses less solar than
the judge's effective ceiling under either reading, so validity holds — but if the judge computes
`min`, the schedule leaves free solar on the table and loses cost points. Low likelihood (needs
two solar notes overlapping the same hour). If you change it:
`effective_solar[h] = base_solar[h] * min(factors_for_h)`.

### IN-03: `_clean_hours` accepts only a list or `{"start","end"}`; other plausible model shapes silently become `no_op`

**File:** `app/directives.py:65-79`

Measured downgrades: `"hours": 13`, `"hours": "13"`, `"hours": "13,14"`,
`"hours": {"from": 13, "to": 15}` all return `no_op`. Temperature is 0 and the prompt is
explicit, so this is unlikely — but each one costs a full directive when it does happen, and a
scalar-to-`[scalar]` promotion is two lines:

```python
if isinstance(raw, (int, float, str)):
    raw = [raw]
```

### IN-04: the model's own `no_op` explanation is discarded, and downgraded directives get a misleading canned one

**File:** `app/directives.py:42, 105-120`

`_no_op()` always substitutes `NO_OP_EXPLANATION`. For a genuine `no_op` that is fine. But every
*downgrade* path (unknown `directive_type`, non-dict `structured_adjustment`, empty `hours`,
unparseable `factor`) also lands there, so a malformed-but-energy-relevant note is reported to
the judge as "This note does not affect today's 24-hour energy schedule" — which is a factual
claim about the note, not about our parser. Spec 10.2 only asks for a "short explanation," so
this is not a rule violation; it does make failures invisible in the payload and in the logs.

### IN-05: `plan_summary` makes claims the plan contradicts

**File:** `app/optimizer.py:108-120`

The text is unconditional. On a battery with `max_charge_kwh_per_hour = 0` and a flat tariff it
emits:

> "Charged 0.0 kWh into the battery around the cheapest tariff hours (min at hour 0) and
> discharged it across the expensive evening peak (max at hour 0) ..."

`min`/`max` over a tied tariff array both return hour 0, and the discharge claim is false.
`plan_summary` carries no scored numbers, so this costs nothing automated — it only reads badly
to a human reviewer. Guard the clause on `charge > 0`.

---

## Verified Correct (no findings — checked, nothing wrong)

Recorded so a re-review does not re-litigate these:

- **Energy balance sign convention** (`optimizer.py:33-37`) matches spec 9.5 exactly:
  `a_eq[h] = grid + solar - flow = demand`, with flow positive = charge.
- **SoC inequality construction** (`optimizer.py:44-47`): cumulative-flow rows give
  `initial + sum(flow[0..h]) <= capacity` and `>= min_soc[h]`, i.e. spec 9.2 applied to
  `E_after[h]`, exactly as 5.2's constraint table requires.
- **End-of-day neutrality** (`optimizer.py:38`): `sum(flow) = 0` is spec 9.6.
- **`_build_plan` rounding** (`optimizer.py:60-84`): worst residual across 4000 fuzzed
  scenarios is 8.5e-14 (balance), 3.4e-13 (SoC transition), 4.1e-7 (solar overshoot from
  6-dp round-half-up), 0.0 (neutrality, capacity, min SoC, grid cap). Against a 0.01 tolerance
  this is not a risk. The `max(..., 0.0)` grid clamp and the `EPS` idle threshold are both
  sound at this scale.
- **Error-handler secret hygiene** (`main.py:68-90`): the 400 handler echoes only
  `loc`/`msg`/`type` coerced to `str`; the 500 handler returns a fixed body. Confirmed no
  traceback, no file path, no API key reaches the wire on either path.
- **Secret handling**: `.env` is git-ignored (`.gitignore:2-3`), is not tracked
  (`git ls-files` shows only `.env.example`), and the template carries an empty key.
- **`sanitize()` guardrail coverage** against spec section 08: allowed types, one entry per note
  in `note_index` order, hours unique/ascending/0-23, factor clamped to [0,1], reserve clamped to
  [0, capacity], grid cap clamped to >= 0, extra keys stripped, `applies`/`structured_adjustment`
  forced consistent for `no_op`. All correct.
- **Edge cases probed clean**: zero solar all day, zero demand all day, `max_charge = 0`,
  `max_discharge = 0`, both zero, `factor = 0.0`, `max_grid_kwh = 0`, tariff ties,
  `initial == minimum`, `initial == capacity`, overlapping `no_charge` + `no_discharge` on the
  same hour (bounds collapse to `(0, 0)`, correctly reported `idle`).

---

_Reviewed: 2026-09-18T23:30:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_

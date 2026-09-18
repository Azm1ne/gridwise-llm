# GridWise — Tickets

Ordered by rubric value per minute. Each ticket is independently completable and has a
runnable acceptance check. Read `ALGORITHM.md` first — it is the implementation contract.

Target layout (7 files, flat — no packages, no layers):
```
app/main.py        FastAPI app, endpoints, error handling
app/models.py      pydantic request/response models
app/llm.py         prompt + provider call + regex fallback
app/guardrails.py  validation/repair of LLM output
app/optimizer.py   LP build + solve + post-process
app/validate.py    self-replay validator
tests/test_cases.py  runs all 10 public samples
```

---

## T1 — Scaffold + `/health`  ·  ~15 min  ·  unblocks everything
`requirements.txt`: `fastapi uvicorn[standard] pydantic scipy numpy httpx pytest`.
`app/main.py` with `GET /health` -> `{"status":"ok"}`.
**Accept:** `uvicorn app.main:app` then `curl localhost:8000/health` -> `{"status":"ok"}`.

## T2 — Request/response models  ·  ~20 min  ·  10 pts (API Contract)
`app/models.py`, pydantic v2, exactly the fields in Problem Statement §7 and §10.
Validate: `hours` length 24 with unique hours 0–23; `operator_notes` 1–3 non-empty strings;
`battery` all five fields. `battery_action` a `Literal["charge","discharge","idle"]`.
**Accept:** each of the 10 sample `input` objects parses; a truncated `hours` array yields 422.

## T3 — Optimizer LP  ·  ~45 min  ·  25 + 10 pts  ·  **highest value, do before the LLM**
`app/optimizer.py::solve(hours, battery, directives) -> hourly_plan`.
Build exactly the model in ALGORITHM.md §3, `linprog(method="highs")`, then §4 post-processing.
Include the infeasibility ladder.
**Accept:** for all 10 samples with the *expected* directives injected, the plan is valid and
`total_cost_bdt <= expected + 0.01`.

## T4 — Self-replay validator  ·  ~30 min  ·  guards 25 pts
`app/validate.py::replay(request, response, directives) -> list[str]` (empty = valid).
Every check in ALGORITHM.md §5, tolerance 0.01.
**Accept:** returns `[]` for all 10 reference `expected_output` payloads; returns a non-empty
list when a plan is mutated (flip one `battery_action` to `charge` inside a no-charge window).

## T5 — Guardrails  ·  ~30 min  ·  protects 25 pts
`app/guardrails.py::sanitize(raw_llm_json, n_notes, battery) -> list[Directive]`.
The table in ALGORITHM.md §2, plus same-type merging.
**Accept:** `assert`-based self-check covering: unknown type, hours `[25,3,3]`, `factor 1.8`,
`applies:true` with `no_op`, missing note index, `null` adjustment on a real directive.
None may raise; all must yield a legal directive list of length `n_notes`.

## T6 — LLM extraction  ·  ~45 min  ·  25 pts (Interpretation)
`app/llm.py::extract(notes, battery) -> raw json`. Prompt per ALGORITHM.md §1.
Provider from env: `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`. Pick a fast small model
(Groq/Gemini-Flash class) — latency is scored. Temperature 0, JSON mode, 12 s timeout, 1 retry.
Regex fallback + all-`no_op` last resort.
**Accept:** on the 10 public notes the extracted directives match `expected_output`
`directive_type`, `hours`, and numeric values exactly (score it, print the per-case diff).

## T7 — Wire `/optimize-energy`  ·  ~25 min
`extract -> sanitize -> solve -> replay`. On replay failure, re-solve with no directives and
return that. Controlled 500 on anything else; never leak a traceback.
**Accept:** end-to-end `curl` with SAMPLE-01 returns a schema-valid response in < 5 s.

## T8 — Public-case test harness  ·  ~25 min  ·  scores you before the judge does
`tests/test_cases.py`: for each sample, POST/call the app, assert `replay() == []`,
assert interpretation match, print
`cost_ratio = expected_cost / our_cost` and an estimated rubric score.
**Accept:** `pytest -q` green, 10/10 interpretation, mean `cost_ratio >= 1.0`.

## T9 — Dockerfile  ·  ~20 min  ·  10 pts
`python:3.11-slim`, non-root, `EXPOSE 8000`, `CMD uvicorn app.main:app --host 0.0.0.0 --port 8000`.
No secrets baked in — key arrives via `-e LLM_API_KEY`. Push with an exact tag.
**Accept:** `docker run -p 8000:8000 -e LLM_API_KEY=... <tag>` then `/health` -> ok.

## T10 — Deploy  ·  ~25 min  ·  gates everything
Any public host (Render/Fly/Railway). Env vars set. Verify **from outside your network**.
**Accept:** public `/health` ok and a public `/optimize-energy` sample call succeeds.

## T11 — README  ·  ~25 min  ·  10 pts, scored against a fixed checklist
Hit all six sub-criteria literally: (3) clean-machine quickstart, (2) env var names +
model/provider, (2) public-sample test command + expected result, (1) LLM->guardrails->optimizer
architecture, (1) docker pull/run fallback, (1) dependencies + limitations + secret handling.
Credit scipy/FastAPI/the LLM provider. **No secret values.**
**Accept:** a teammate follows it on a clean shell and reaches a green `/health` with zero questions.

## T12 — 3-minute video  ·  ~20 min  ·  tie-break only — do it last
Problem, architecture diagram, LLM -> guardrails -> optimizer flow, live run + test. <= 3:00.

---

### Schedule for a 4-hour window
`T1 T2 T3 T4` (~1h50, the whole scoring core) -> `T5 T6 T7` (~1h40) -> `T8` -> deploy `T9 T10`
-> `T11` -> `T12` if time remains. **T10 must start by the 3-hour mark**; an undeployed perfect
service scores zero.

### Cut list, in order, if time runs short
T12 -> regex fallback in T6 -> infeasibility ladder in T3 -> directive merging in T5.
Never cut: T4 (validator) or T11 (README) — both are cheap and each protects 10+ points.

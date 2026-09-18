"""Local judge. Runs every public case through the REAL pipeline and scores it
the way the rubric does. Needs LLM_API_KEY in .env.

    python scripts/score.py            # score all cases
    python scripts/score.py --probe    # list models your key can use
"""
import asyncio
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx

from app import llm
from app.directives import sanitize
from app.models import OptimizeRequest
from app.optimizer import solve
from app.validate import replay, totals_from

ROOT = pathlib.Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text())["cases"]


def probe():
    key = os.getenv("LLM_API_KEY", "")
    if not key:
        sys.exit("LLM_API_KEY is not set (put it in .env)")
    r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                  headers={"x-goog-api-key": key}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"probe failed {r.status_code}: {r.text[:200]}")
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    print("Models your key can use with generateContent:\n")
    for n in sorted(n for n in names if "flash" in n or "lite" in n):
        print("  ", n)
    print(f"\ncurrent LLM_MODEL = {os.getenv('LLM_MODEL', 'gemini-2.5-flash-lite')}")


def compare(got, want):
    """Per-note scoring, mirroring the rubric's five interpretation sub-criteria."""
    if want["directive_type"] == "no_op":
        relevance = got.applies is False and got.directive_type == "no_op"
        return relevance, relevance, True, True, got.structured_adjustment is None

    relevance = got.applies is True and got.directive_type != "no_op"
    kind = got.directive_type == want["directive_type"]
    adjustment = got.structured_adjustment or {}
    expected = want["structured_adjustment"]
    hours = adjustment.get("hours") == expected["hours"]

    numeric_key = next((k for k in expected if k != "hours"), None)
    if numeric_key is None:
        numeric = True
    else:
        numeric = abs(adjustment.get(numeric_key, -1e9) - expected[numeric_key]) <= 0.01
    shape = set(adjustment) == set(expected)
    return relevance, kind, hours, numeric, shape


async def main():
    totals_score = [0, 0, 0, 0, 0]
    ratios, latencies, invalid = [], [], 0

    for case in CASES:
        request = OptimizeRequest(**case["input"])
        expected = case["expected_output"]

        started = time.perf_counter()
        raw = await llm.extract(request.operator_notes, request.battery)
        directives = sanitize(raw or {}, len(request.operator_notes), request.battery)
        plan, scenario, tier = solve(request.hours, request.battery, directives)
        result = totals_from(plan, scenario)
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)

        errors = replay(scenario, directives, plan, result)
        if errors:
            invalid += 1

        marks = [0, 0, 0, 0, 0]
        for got, want in zip(directives, expected["directive_interpretation"]):
            for i, ok in enumerate(compare(got, want)):
                marks[i] += bool(ok)
        n = len(directives)
        for i in range(5):
            totals_score[i] += marks[i] / n

        ratio = min(1.0, expected["total_cost_bdt"] / result["total_cost_bdt"]) if result["total_cost_bdt"] > 0 else 1.0
        ratios.append(ratio)

        perfect = all(m == n for m in marks)
        print(f"{'OK ' if perfect and not errors else 'MISS'} {case['id']}  "
              f"{elapsed:5.2f}s  interp={sum(marks)}/{5*n}  ratio={ratio:.3f}  "
              f"llm={'y' if raw else 'N'}  types={[d.directive_type for d in directives]}")
        if not perfect:
            for got, want in zip(directives, expected["directive_interpretation"]):
                if got.directive_type != want["directive_type"] or got.structured_adjustment != want["structured_adjustment"]:
                    print(f"      got  {got.directive_type} {got.structured_adjustment}")
                    print(f"      want {want['directive_type']} {want['structured_adjustment']}")

    c = len(CASES)
    interp = 5 * sum(s / c for s in totals_score)
    latencies.sort()
    p95 = latencies[int(0.95 * (len(latencies) - 1))]
    latency_points = 3 if p95 <= 5 else 2 if p95 <= 15 else 1 if p95 <= 30 else 0

    print(f"\n{'='*64}")
    print(f"LLM Interpretation      {interp:5.2f} / 25   "
          f"(relevance {totals_score[0]/c*5:.1f} type {totals_score[1]/c*5:.1f} "
          f"hours {totals_score[2]/c*5:.1f} value {totals_score[3]/c*5:.1f} shape {totals_score[4]/c*5:.1f})")
    print(f"Directive Application   {25*(c-invalid)/c:5.2f} / 25   ({c-invalid}/{c} schedules valid)")
    print(f"Optimization Quality    {10*sum(ratios)/len(ratios):5.2f} / 10")
    print(f"Latency                 {latency_points} / 3    (p95 {p95:.2f}s)")
    print(f"{'='*64}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        asyncio.run(main())

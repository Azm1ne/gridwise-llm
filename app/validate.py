"""Independent replay of a finished schedule -- the same checks the judge runs.

Deliberately does not reuse the optimizer's state: it re-derives everything from the
returned rows, so a bug in the optimizer cannot hide behind a shared intermediate.
"""
from __future__ import annotations

import math

from .directives import Directive, Scenario

TOL = 0.01


def replay(sc: Scenario, directives: list[Directive], plan: list[dict], totals: dict) -> list[str]:
    """Return a list of violations. Empty means the plan is valid."""
    errors: list[str] = []

    if len(plan) != 24 or [r["hour"] for r in plan] != list(range(24)):
        return ["hourly_plan must contain hours 0..23 exactly once, in order"]

    energy = sc.initial
    for h, row in enumerate(plan):
        grid, solar = row["grid_kwh"], row["solar_used_kwh"]
        action, magnitude = row["battery_action"], row["battery_kwh"]

        if not all(math.isfinite(v) for v in (grid, solar, magnitude, row["battery_energy_after_kwh"])):
            errors.append(f"h{h}: non-finite value")
            continue
        if grid < -TOL or solar < -TOL or magnitude < -TOL:
            errors.append(f"h{h}: negative energy value")
        if solar > sc.effective_solar[h] + TOL:
            errors.append(f"h{h}: solar_used {solar} exceeds effective solar {sc.effective_solar[h]}")
        if action == "idle" and abs(magnitude) > TOL:
            errors.append(f"h{h}: idle hour must have battery_kwh == 0")

        charge = magnitude if action == "charge" else 0.0
        discharge = magnitude if action == "discharge" else 0.0
        if charge > sc.flow_hi[h] + TOL:
            errors.append(f"h{h}: charge {charge} exceeds limit {sc.flow_hi[h]}")
        if discharge > -sc.flow_lo[h] + TOL:
            errors.append(f"h{h}: discharge {discharge} exceeds limit {-sc.flow_lo[h]}")
        if abs(grid + solar + discharge - charge - sc.demand[h]) > TOL:
            errors.append(f"h{h}: energy balance violated")
        if grid > sc.grid_hi[h] + TOL:
            errors.append(f"h{h}: grid {grid} exceeds cap {sc.grid_hi[h]}")

        energy += charge - discharge
        if abs(energy - row["battery_energy_after_kwh"]) > TOL:
            errors.append(f"h{h}: battery_energy_after_kwh does not follow the transition")
        energy = row["battery_energy_after_kwh"]
        if energy < sc.min_soc[h] - TOL:
            errors.append(f"h{h}: battery {energy} below required minimum {sc.min_soc[h]}")
        if energy > sc.capacity + TOL:
            errors.append(f"h{h}: battery {energy} above capacity {sc.capacity}")

    if abs(plan[-1]["battery_energy_after_kwh"] - sc.initial) > TOL:
        errors.append("end-of-day battery neutrality violated")

    grid_total = sum(r["grid_kwh"] for r in plan)
    cost = sum(r["grid_kwh"] * sc.tariff[r["hour"]] for r in plan)
    peak = max(r["grid_kwh"] for r in plan)
    if abs(totals["total_grid_kwh"] - grid_total) > TOL:
        errors.append("total_grid_kwh does not match hourly_plan")
    if abs(totals["total_cost_bdt"] - cost) > TOL:
        errors.append("total_cost_bdt does not match hourly_plan")
    if abs(totals["peak_grid_kwh"] - peak) > TOL:
        errors.append("peak_grid_kwh does not match hourly_plan")

    return errors


def totals_from(plan: list[dict], sc: Scenario) -> dict:
    return {
        "total_grid_kwh": round(sum(r["grid_kwh"] for r in plan), 4),
        "total_cost_bdt": round(sum(r["grid_kwh"] * sc.tariff[r["hour"]] for r in plan), 4),
        "peak_grid_kwh": round(max(r["grid_kwh"] for r in plan), 4),
    }

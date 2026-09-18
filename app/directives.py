"""LLM output -> trusted directives -> mathematical scenario.

Two deterministic stages sit between the model and the optimizer:
  sanitize()        repairs what is safely repairable, downgrades the rest to no_op
  compile_scenario() folds every surviving directive into per-hour bound arrays

Overlapping directives compose (tightest wins) instead of overriding each other.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import DIRECTIVE_TYPES, Battery, HourInput

NO_OP_EXPLANATION = "This note does not affect today's 24-hour energy schedule."


@dataclass
class Directive:
    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: dict | None
    explanation: str


@dataclass
class Scenario:
    """The optimizer's entire view of the world. Directives exist only as these numbers."""
    demand: list[float]
    tariff: list[float]
    effective_solar: list[float]
    min_soc: list[float]
    flow_lo: list[float]   # most negative allowed battery flow (discharge limit)
    flow_hi: list[float]   # most positive allowed battery flow (charge limit)
    grid_hi: list[float]   # math.inf when uncapped
    capacity: float
    initial: float


def _no_op(index: int, explanation: str = NO_OP_EXPLANATION) -> Directive:
    return Directive(index, False, "no_op", None, explanation)


def _finite(value) -> float | None:
    """Coerce to a finite float. Rejects bools (an int subclass), NaN, inf, junk.

    Numeric strings are accepted: models quote numbers often enough that dropping
    "13" would cost a directive for no good reason.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _clean_hours(raw) -> list[int]:
    """Unique whole hours in 0..23, ascending. Anything else is dropped."""
    if isinstance(raw, dict):   # {"start": 13, "end": 15} -> [13, 14], end excluded
        start, end = _finite(raw.get("start")), _finite(raw.get("end"))
        if start is None or end is None:
            return []
        raw = list(range(int(start), int(end)))
    if not isinstance(raw, list):
        return []
    hours = set()
    for item in raw:
        value = _finite(item)
        if value is not None and value.is_integer() and 0 <= value <= 23:
            hours.add(int(value))
    return sorted(hours)


def sanitize(raw: dict | list, n_notes: int, battery: Battery) -> list[Directive]:
    """Return exactly n_notes directives in note_index order. Never raises."""
    entries = raw.get("directives") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        entries = []
    by_index: dict[int, dict] = {}
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        claimed = _finite(entry.get("note_index"))
        index = int(claimed) if claimed is not None and claimed.is_integer() else position
        if not (0 <= index < n_notes) or index in by_index:
            index = position   # bad or duplicated index: trust order instead
        if not (0 <= index < n_notes) or index in by_index:
            free = [i for i in range(n_notes) if i not in by_index]
            if not free:
                continue
            index = free[0]
        by_index[index] = entry

    return [_sanitize_one(i, by_index.get(i), battery) for i in range(n_notes)]


def _sanitize_one(index: int, entry: dict | None, battery: Battery) -> Directive:
    if entry is None:
        return _no_op(index, "No interpretation was produced for this note.")

    kind = entry.get("directive_type")
    if kind not in DIRECTIVE_TYPES or kind == "no_op":
        return _no_op(index)

    explanation = str(entry.get("explanation") or "")[:300] or kind
    adjustment = entry.get("structured_adjustment")
    if not isinstance(adjustment, dict):
        return _no_op(index)

    hours = _clean_hours(adjustment.get("hours"))
    if not hours:
        return _no_op(index)

    if kind == "solar_reduction":
        factor = _finite(adjustment.get("factor"))
        if factor is None:
            return _no_op(index)
        if 2.0 <= factor <= 100.0:   # "20" means 20% remaining, not "clamp to 1.0"
            factor /= 100.0
        clean = {"hours": hours, "factor": min(1.0, max(0.0, factor))}
    elif kind == "minimum_battery_reserve":
        reserve = _finite(adjustment.get("minimum_energy_kwh"))
        if reserve is None:
            return _no_op(index)
        clean = {"hours": hours, "minimum_energy_kwh": min(battery.capacity_kwh, max(0.0, reserve))}
    elif kind == "max_grid_window":
        cap = _finite(adjustment.get("max_grid_kwh"))
        if cap is None:
            return _no_op(index)
        clean = {"hours": hours, "max_grid_kwh": max(0.0, cap)}
    else:  # no_charge_window / no_discharge_window
        clean = {"hours": hours}

    return Directive(index, True, kind, clean, explanation)


def compile_scenario(
    hours: list[HourInput], battery: Battery, directives: list[Directive]
) -> Scenario:
    """Fold directives into per-hour bounds. Tightest constraint wins on overlap."""
    ordered = sorted(hours, key=lambda h: h.hour)
    scenario = Scenario(
        demand=[h.demand_kwh for h in ordered],
        tariff=[h.tariff_bdt_per_kwh for h in ordered],
        effective_solar=[h.solar_kwh for h in ordered],
        min_soc=[battery.minimum_energy_kwh] * 24,
        flow_lo=[-battery.max_discharge_kwh_per_hour] * 24,
        flow_hi=[battery.max_charge_kwh_per_hour] * 24,
        grid_hi=[math.inf] * 24,
        capacity=battery.capacity_kwh,
        initial=battery.initial_energy_kwh,
    )

    for directive in directives:
        if not directive.applies or not directive.structured_adjustment:
            continue
        adjustment = directive.structured_adjustment
        for hour in adjustment["hours"]:
            if directive.directive_type == "solar_reduction":
                scenario.effective_solar[hour] *= adjustment["factor"]
            elif directive.directive_type == "minimum_battery_reserve":
                scenario.min_soc[hour] = max(scenario.min_soc[hour], adjustment["minimum_energy_kwh"])
            elif directive.directive_type == "no_charge_window":
                scenario.flow_hi[hour] = 0.0
            elif directive.directive_type == "no_discharge_window":
                scenario.flow_lo[hour] = 0.0
            elif directive.directive_type == "max_grid_window":
                scenario.grid_hi[hour] = min(scenario.grid_hi[hour], adjustment["max_grid_kwh"])

    return scenario

"""Everything scoreable without an API key: optimizer, validator, guardrails.

Run: pytest -q
"""
import json
import pathlib

import pytest

from app.directives import Directive, compile_scenario, sanitize
from app.models import Battery, OptimizeRequest
from app.optimizer import solve
from app.validate import replay, totals_from

CASES = json.loads(
    (pathlib.Path(__file__).parent.parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text()
)["cases"]

BATTERY = Battery(capacity_kwh=200, initial_energy_kwh=120, minimum_energy_kwh=40,
                  max_charge_kwh_per_hour=50, max_discharge_kwh_per_hour=50)


def ground_truth(case):
    return [Directive(d["note_index"], d["applies"], d["directive_type"],
                      d["structured_adjustment"], d["explanation"])
            for d in case["expected_output"]["directive_interpretation"]]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_plan_is_valid_and_optimal(case):
    """With organizer directives applied, our schedule is valid and matches reference cost."""
    request = OptimizeRequest(**case["input"])
    plan, scenario, tier = solve(request.hours, request.battery, ground_truth(case))
    totals = totals_from(plan, scenario)

    assert tier == 0, "no directive should have needed relaxing"
    assert replay(scenario, ground_truth(case), plan, totals) == []
    assert totals["total_cost_bdt"] <= case["expected_output"]["total_cost_bdt"] + 0.01


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_reference_schedule_passes_replay(case):
    """Negative control on the validator: the organizer's own plan must pass."""
    request = OptimizeRequest(**case["input"])
    scenario = compile_scenario(request.hours, request.battery, ground_truth(case))
    expected = case["expected_output"]
    assert replay(scenario, ground_truth(case), expected["hourly_plan"], expected) == []


def test_replay_catches_a_violated_directive():
    """The validator must be able to fail, or it proves nothing."""
    case = next(c for c in CASES if c["id"] == "SAMPLE-02")   # no_charge_window [2,3,4]
    request = OptimizeRequest(**case["input"])
    directives = ground_truth(case)
    scenario = compile_scenario(request.hours, request.battery, directives)

    plan = [dict(row) for row in case["expected_output"]["hourly_plan"]]
    plan[3] = {**plan[3], "battery_action": "charge", "battery_kwh": 40.0}
    errors = replay(scenario, directives, plan, case["expected_output"])
    assert any("charge" in e or "balance" in e for e in errors)


def test_guardrails_never_raise_on_hostile_input():
    hostile = {"directives": [
        {"note_index": 0, "directive_type": "drain_the_grid", "structured_adjustment": {"hours": [1]}},
        {"note_index": 1, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [25, 3, 3, "x", True, -2], "factor": 1.8}},
        {"note_index": 2, "directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [5], "minimum_energy_kwh": 1e9}},
    ]}
    directives = sanitize(hostile, 3, BATTERY)

    assert [d.directive_type for d in directives] == [
        "no_op", "solar_reduction", "minimum_battery_reserve"]
    assert directives[0].applies is False and directives[0].structured_adjustment is None
    assert directives[1].structured_adjustment == {"hours": [3], "factor": 1.0}
    assert directives[2].structured_adjustment["minimum_energy_kwh"] == BATTERY.capacity_kwh


@pytest.mark.parametrize("raw", [None, {}, "garbage", [], {"directives": "nope"},
                                 {"directives": [{"note_index": 9}]}])
def test_guardrails_always_return_one_entry_per_note(raw):
    directives = sanitize(raw or {}, 3, BATTERY)
    assert len(directives) == 3
    assert [d.note_index for d in directives] == [0, 1, 2]
    assert all(d.directive_type == "no_op" for d in directives)


def test_no_op_only_directive_still_produces_a_valid_schedule():
    """The total-LLM-failure path must still return a valid, cheap plan."""
    case = CASES[0]
    request = OptimizeRequest(**case["input"])
    directives = sanitize({}, len(request.operator_notes), request.battery)
    plan, scenario, _ = solve(request.hours, request.battery, directives)
    assert replay(scenario, directives, plan, totals_from(plan, scenario)) == []


def test_contradictory_directives_relax_instead_of_failing():
    """Grid capped below demand with no battery help is infeasible -- must not 500."""
    case = CASES[0]
    request = OptimizeRequest(**case["input"])
    impossible = [Directive(0, True, "max_grid_window", {"hours": list(range(24)), "max_grid_kwh": 0.0}, "x")]
    plan, scenario, tier = solve(request.hours, request.battery, impossible)
    assert tier > 0
    assert replay(scenario, [], plan, totals_from(plan, scenario)) == []

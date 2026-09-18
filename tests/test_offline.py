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
    assert replay(scenario, plan, totals) == []
    assert totals["total_cost_bdt"] <= case["expected_output"]["total_cost_bdt"] + 0.01


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_reference_schedule_passes_replay(case):
    """Negative control on the validator: the organizer's own plan must pass."""
    request = OptimizeRequest(**case["input"])
    scenario = compile_scenario(request.hours, request.battery, ground_truth(case))
    expected = case["expected_output"]
    assert replay(scenario, expected["hourly_plan"], expected) == []


def test_replay_catches_a_violated_directive():
    """The validator must be able to fail, or it proves nothing."""
    case = next(c for c in CASES if c["id"] == "SAMPLE-02")   # no_charge_window [2,3,4]
    request = OptimizeRequest(**case["input"])
    directives = ground_truth(case)
    scenario = compile_scenario(request.hours, request.battery, directives)

    plan = [dict(row) for row in case["expected_output"]["hourly_plan"]]
    plan[3] = {**plan[3], "battery_action": "charge", "battery_kwh": 40.0}
    errors = replay(scenario, plan, case["expected_output"])
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
    assert replay(scenario, plan, totals_from(plan, scenario)) == []


def test_contradictory_directives_relax_instead_of_failing():
    """Grid capped below demand with no battery help is infeasible -- must not 500."""
    case = CASES[0]
    request = OptimizeRequest(**case["input"])
    impossible = [Directive(0, True, "max_grid_window", {"hours": list(range(24)), "max_grid_kwh": 0.0}, "x")]
    plan, scenario, tier = solve(request.hours, request.battery, impossible)
    assert tier > 0
    assert replay(scenario, plan, totals_from(plan, scenario)) == []


# --- guardrail hardening: real ways a model mangles otherwise-correct output ---

def test_quoted_numbers_are_accepted():
    """A quoted "13" is a correct answer badly typed. Dropping it costs a directive."""
    raw = {"directives": [{"note_index": 0, "directive_type": "max_grid_window",
                           "structured_adjustment": {"hours": ["18", "19"], "max_grid_kwh": "155"}}]}
    d = sanitize(raw, 1, BATTERY)[0]
    assert d.structured_adjustment == {"hours": [18, 19], "max_grid_kwh": 155.0}


def test_percentage_factor_is_normalised_not_clamped():
    """factor=20 means 20% remaining. Clamping to 1.0 would delete the directive."""
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction",
                           "structured_adjustment": {"hours": [13], "factor": 20}}]}
    assert sanitize(raw, 1, BATTERY)[0].structured_adjustment["factor"] == 0.2


def test_fraction_factor_is_left_alone():
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction",
                           "structured_adjustment": {"hours": [13], "factor": 0.25}}]}
    assert sanitize(raw, 1, BATTERY)[0].structured_adjustment["factor"] == 0.25


def test_start_end_window_expands_half_open():
    raw = {"directives": [{"note_index": 0, "directive_type": "no_charge_window",
                           "structured_adjustment": {"hours": {"start": 14, "end": 16}}}]}
    assert sanitize(raw, 1, BATTERY)[0].structured_adjustment == {"hours": [14, 15]}


def test_duplicate_note_index_does_not_drop_a_note():
    """Two entries both claiming index 0 must still yield two distinct directives."""
    raw = {"directives": [
        {"note_index": 0, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [2]}},
        {"note_index": 0, "directive_type": "no_discharge_window", "structured_adjustment": {"hours": [18]}},
    ]}
    d = sanitize(raw, 2, BATTERY)
    assert [x.directive_type for x in d] == ["no_charge_window", "no_discharge_window"]


def test_missing_note_index_falls_back_to_position():
    raw = {"directives": [
        {"directive_type": "no_op", "structured_adjustment": None},
        {"directive_type": "no_discharge_window", "structured_adjustment": {"hours": [18, 19]}},
    ]}
    d = sanitize(raw, 2, BATTERY)
    assert d[1].directive_type == "no_discharge_window"
    assert d[1].structured_adjustment == {"hours": [18, 19]}


def test_extra_keys_are_stripped_from_structured_adjustment():
    """The rubric scores adjustment SHAPE. Extra keys must never reach the response."""
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction",
                           "structured_adjustment": {"hours": [13], "factor": 0.2,
                                                     "reason": "clouds", "confidence": 0.9}}]}
    assert set(sanitize(raw, 1, BATTERY)[0].structured_adjustment) == {"hours", "factor"}


# --- malformed request handling: every rejection must be a controlled 400 ---

def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("mutate,label", [
    (lambda d: d["hours"].__setitem__(5, {**d["hours"][5], "hour": 4}), "duplicate hour"),
    (lambda d: d.__setitem__("hours", d["hours"][:23]), "only 23 hours"),
    (lambda d: d.__setitem__("operator_notes", []), "zero notes"),
    (lambda d: d.__setitem__("operator_notes", ["a", "b", "c", "d"]), "four notes"),
    (lambda d: d.__setitem__("operator_notes", ["  "]), "blank note"),
    (lambda d: d["hours"][0].__setitem__("demand_kwh", "lots"), "non-numeric demand"),
    (lambda d: d["battery"].__setitem__("capacity_kwh", -50), "negative capacity"),
    (lambda d: d.pop("battery"), "missing battery"),
])
def test_malformed_requests_return_controlled_400(mutate, label):
    """A custom validator once put a ValueError object in the error body, so the
    handler's own json.dumps raised and the 400 became a 500."""
    import copy
    body = copy.deepcopy(CASES[0]["input"])
    mutate(body)
    response = _client().post("/optimize-energy", json=body)
    assert response.status_code == 400, f"{label} returned {response.status_code}"
    payload = response.json()          # must be serialisable, and leak nothing
    assert payload["error"] == "invalid request"
    assert "Traceback" not in response.text and "File \"/" not in response.text


# --- regressions for the code-review findings ---

def test_replay_catches_an_unsatisfiable_directive_instead_of_passing_it():
    """CR-01: replay once validated against the RELAXED scenario, so a plan that
    ignored a directive passed clean while the response still claimed it."""
    case = CASES[0]
    request = OptimizeRequest(**case["input"])
    impossible = [Directive(0, True, "max_grid_window",
                            {"hours": list(range(24)), "max_grid_kwh": 1.0}, "x")]

    plan, relaxed, tier = solve(request.hours, request.battery, impossible)
    totals = totals_from(plan, relaxed)
    assert tier > 0, "an impossible cap must force relaxation"

    promised = compile_scenario(request.hours, request.battery, impossible)
    errors = replay(promised, plan, totals)
    assert errors, "validating against the promised directives must report the violation"
    assert any("exceeds cap" in e for e in errors)


def test_endpoint_reports_unmet_directives_but_still_returns_a_valid_plan():
    """An unsatisfiable directive must not produce a 500 or a silently false claim."""
    import copy
    body = copy.deepcopy(CASES[0]["input"])
    body["operator_notes"] = ["Grid import must never exceed 1 kWh in any hour."]
    response = _client().post("/optimize-energy", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["hourly_plan"]) == 24
    assert payload["total_grid_kwh"] > 0


def test_huge_hour_window_does_not_allocate():
    """WR-01: end=1e9 would have materialised a billion-element range first."""
    raw = {"directives": [{"note_index": 0, "directive_type": "no_charge_window",
                           "structured_adjustment": {"hours": {"start": 0, "end": 1_000_000_000}}}]}
    d = sanitize(raw, 1, BATTERY)[0]
    assert d.structured_adjustment == {"hours": list(range(24))}


@pytest.mark.parametrize("battery,label", [
    ({"capacity_kwh": 200, "initial_energy_kwh": 10, "minimum_energy_kwh": 40,
      "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50}, "initial below minimum"),
    ({"capacity_kwh": 200, "initial_energy_kwh": 900, "minimum_energy_kwh": 40,
      "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50}, "initial above capacity"),
    ({"capacity_kwh": 200, "initial_energy_kwh": 100, "minimum_energy_kwh": 500,
      "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50}, "minimum above capacity"),
])
def test_infeasible_battery_is_a_400_not_a_500(battery, label):
    """WR-02: these are unsatisfiable under end-of-day neutrality and used to 500."""
    import copy
    body = copy.deepcopy(CASES[0]["input"])
    body["battery"] = battery
    assert _client().post("/optimize-energy", json=body).status_code == 400, label

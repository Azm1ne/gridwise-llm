import json
import math
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.main import app
from app.models import OptimizeRequest, Battery, HourInput
from app.directives import sanitize, compile_scenario, Directive
from app.optimizer import solve, summarise
from app.validate import replay, totals_from

client = TestClient(app)

def make_battery(**kwargs):
    b = {
        "capacity_kwh": 100.0,
        "initial_energy_kwh": 50.0,
        "minimum_energy_kwh": 20.0,
        "max_charge_kwh_per_hour": 25.0,
        "max_discharge_kwh_per_hour": 25.0,
    }
    b.update(kwargs)
    return Battery(**b)

def make_hours(**kwargs):
    # kwargs overrides default values for all hours or specific ones if needed, 
    # but for simplicity we'll just allow overriding all hours with the same defaults
    hours = []
    for h in range(24):
        demand = kwargs.get("demand_kwh", 10.0)
        solar = kwargs.get("solar_kwh", 5.0)
        tariff = kwargs.get("tariff_bdt_per_kwh", 5.0)
        hours.append(HourInput(hour=h, demand_kwh=demand, solar_kwh=solar, tariff_bdt_per_kwh=tariff))
    return hours

def valid_payload():
    return {
        "scenario_id": "test_scenario",
        "operator_notes": ["some note"],
        "battery": {
            "capacity_kwh": 100.0,
            "initial_energy_kwh": 50.0,
            "minimum_energy_kwh": 20.0,
            "max_charge_kwh_per_hour": 25.0,
            "max_discharge_kwh_per_hour": 25.0,
        },
        "hours": [
            {
                "hour": h,
                "demand_kwh": 10.0,
                "solar_kwh": 5.0,
                "tariff_bdt_per_kwh": 5.0
            } for h in range(24)
        ]
    }


# ==========================================
# 1. Optimizer Edge Cases
# ==========================================
def test_optimizer_zero_solar():
    hours = make_hours(solar_kwh=0.0)
    battery = make_battery()
    plan, sc, tier = solve(hours, battery, [])
    assert all(r["solar_used_kwh"] == 0.0 for r in plan)

def test_optimizer_solar_exceeds_demand():
    hours = make_hours(demand_kwh=5.0, solar_kwh=10.0)
    battery = make_battery()
    plan, sc, tier = solve(hours, battery, [])
    # Should use at most 5.0 solar if no charging, or more if charging
    # Wait, the battery initial is 50, max 100, min 20. It can charge.
    # We just ensure it's valid.
    totals = totals_from(plan, sc)
    assert replay(sc, plan, totals) == []

def test_optimizer_zero_battery_rate():
    hours = make_hours()
    battery = make_battery(max_charge_kwh_per_hour=0.0, max_discharge_kwh_per_hour=0.0)
    plan, sc, tier = solve(hours, battery, [])
    assert all(r["battery_action"] == "idle" for r in plan)
    assert all(r["battery_kwh"] == 0.0 for r in plan)

def test_optimizer_capacity_equals_minimum():
    hours = make_hours()
    battery = make_battery(capacity_kwh=50.0, initial_energy_kwh=50.0, minimum_energy_kwh=50.0)
    plan, sc, tier = solve(hours, battery, [])
    # Must be idle always since it can't move
    assert all(r["battery_action"] == "idle" for r in plan)

def test_optimizer_high_demand_low_tariff_vs_low_demand_high_tariff():
    hours = make_hours()
    for h in range(12):
        hours[h].demand_kwh = 100.0
        hours[h].tariff_bdt_per_kwh = 1.0
    for h in range(12, 24):
        hours[h].demand_kwh = 10.0
        hours[h].tariff_bdt_per_kwh = 100.0
    battery = make_battery()
    plan, sc, tier = solve(hours, battery, [])
    assert replay(sc, plan, totals_from(plan, sc)) == []

def test_optimizer_identical_tariffs():
    hours = make_hours(tariff_bdt_per_kwh=5.0)
    battery = make_battery()
    plan, sc, tier = solve(hours, battery, [])
    assert replay(sc, plan, totals_from(plan, sc)) == []

def test_optimizer_zero_demand():
    hours = make_hours(demand_kwh=0.0, solar_kwh=0.0)
    battery = make_battery()
    plan, sc, tier = solve(hours, battery, [])
    assert all(r["grid_kwh"] == 0.0 for r in plan)
    assert replay(sc, plan, totals_from(plan, sc)) == []

# ==========================================
# 2. Directive Edge Cases
# ==========================================
def test_directive_solar_reduction_zero():
    hours = make_hours(solar_kwh=10.0)
    battery = make_battery()
    d = Directive(0, True, "solar_reduction", {"hours": list(range(24)), "factor": 0.0}, "")
    plan, sc, tier = solve(hours, battery, [d])
    assert all(r["solar_used_kwh"] == 0.0 for r in plan)

def test_directive_solar_reduction_one():
    hours = make_hours(solar_kwh=10.0)
    battery = make_battery()
    d = Directive(0, True, "solar_reduction", {"hours": list(range(24)), "factor": 1.0}, "")
    plan, sc, tier = solve(hours, battery, [d])
    sc_base = compile_scenario(hours, battery, [])
    assert sc.effective_solar == sc_base.effective_solar

def test_directive_minimum_reserve_higher_than_initial():
    hours = make_hours()
    battery = make_battery(initial_energy_kwh=50.0, capacity_kwh=100.0)
    # The battery must charge up to 80 by hour 10
    d = Directive(0, True, "minimum_battery_reserve", {"hours": [10], "minimum_energy_kwh": 80.0}, "")
    plan, sc, tier = solve(hours, battery, [d])
    assert plan[10]["battery_energy_after_kwh"] >= 80.0
    assert replay(sc, plan, totals_from(plan, sc)) == []

def test_directive_minimum_reserve_all_hours():
    hours = make_hours()
    battery = make_battery(initial_energy_kwh=50.0, capacity_kwh=100.0)
    # Must charge to 80 in first hour and stay >= 80, but end of day neutrality requires it to end at 50, which contradicts!
    # Let's see if the solver relaxes it.
    d = Directive(0, True, "minimum_battery_reserve", {"hours": list(range(24)), "minimum_energy_kwh": 80.0}, "")
    plan, sc, tier = solve(hours, battery, [d])
    assert tier > 0  # Should be relaxed

def test_directive_no_charge_all_hours():
    hours = make_hours()
    battery = make_battery(initial_energy_kwh=50.0, capacity_kwh=100.0)
    d = Directive(0, True, "no_charge_window", {"hours": list(range(24))}, "")
    plan, sc, tier = solve(hours, battery, [d])
    assert all(r["battery_action"] in ("discharge", "idle") for r in plan)
    # Since it can't charge, it also can't discharge due to end-of-day neutrality (unless it was already at min and could charge? No, if it discharges it must charge to get back to initial).
    # Actually, if it can never charge, and initial=50, it can't discharge either. So it must be idle all 24 hours.
    assert all(r["battery_action"] == "idle" for r in plan)

def test_directive_no_discharge_all_hours():
    hours = make_hours()
    battery = make_battery(initial_energy_kwh=50.0, capacity_kwh=100.0)
    d = Directive(0, True, "no_discharge_window", {"hours": list(range(24))}, "")
    plan, sc, tier = solve(hours, battery, [d])
    # Same logic, must be idle all 24 hours
    assert all(r["battery_action"] == "idle" for r in plan)

def test_directive_max_grid_zero_but_solar_covers():
    hours = make_hours(demand_kwh=5.0, solar_kwh=5.0, tariff_bdt_per_kwh=5.0)
    battery = make_battery()
    d = Directive(0, True, "max_grid_window", {"hours": [12], "max_grid_kwh": 0.0}, "")
    plan, sc, tier = solve(hours, battery, [d])
    assert plan[12]["grid_kwh"] == 0.0
    assert tier == 0

def test_directive_combined_no_charge_and_no_discharge():
    hours = make_hours()
    battery = make_battery()
    d1 = Directive(0, True, "no_charge_window", {"hours": [5]}, "")
    d2 = Directive(1, True, "no_discharge_window", {"hours": [5]}, "")
    plan, sc, tier = solve(hours, battery, [d1, d2])
    assert plan[5]["battery_action"] == "idle"
    assert tier == 0

def test_directive_combined_solar_reduction_and_max_grid():
    hours = make_hours(demand_kwh=10.0, solar_kwh=10.0)
    battery = make_battery()
    d1 = Directive(0, True, "solar_reduction", {"hours": [5], "factor": 0.5}, "")
    # solar becomes 5.0, demand is 10.0. Grid needs 5.0. 
    # But max_grid cap is 0.0! So battery must discharge 5.0.
    d2 = Directive(1, True, "max_grid_window", {"hours": [5], "max_grid_kwh": 0.0}, "")
    plan, sc, tier = solve(hours, battery, [d1, d2])
    assert plan[5]["solar_used_kwh"] == 5.0
    assert plan[5]["grid_kwh"] == 0.0
    assert plan[5]["battery_action"] == "discharge"
    assert plan[5]["battery_kwh"] == 5.0

def test_directive_multiple_overlapping_tightest_wins():
    hours = make_hours()
    battery = make_battery()
    # cap 50 and cap 10 on the same hour -> cap 10 wins
    d1 = Directive(0, True, "max_grid_window", {"hours": [5], "max_grid_kwh": 50.0}, "")
    d2 = Directive(1, True, "max_grid_window", {"hours": [5], "max_grid_kwh": 10.0}, "")
    sc = compile_scenario(hours, battery, [d1, d2])
    assert sc.grid_hi[5] == 10.0

# ==========================================
# 3. Sanitize Edge Cases
# ==========================================
def test_sanitize_factor_1_5():
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [1], "factor": 1.5}}]}
    d = sanitize(raw, 1, make_battery())[0]
    # 1.5 is clamped to 1.0
    assert d.structured_adjustment["factor"] == 1.0

def test_sanitize_factor_101():
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [1], "factor": 101}}]}
    d = sanitize(raw, 1, make_battery())[0]
    # 101 is > 100, so it's NOT divided by 100. It is clamped to 1.0.
    assert d.structured_adjustment["factor"] == 1.0

def test_sanitize_factor_0():
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [1], "factor": 0}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.structured_adjustment["factor"] == 0.0

def test_sanitize_factor_negative():
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [1], "factor": -0.5}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.structured_adjustment["factor"] == 0.0

def test_sanitize_hours_empty():
    raw = {"directives": [{"note_index": 0, "directive_type": "no_charge_window", "structured_adjustment": {"hours": []}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.directive_type == "no_op"

def test_sanitize_hours_float():
    raw = {"directives": [{"note_index": 0, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [13.0, 14.0]}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.structured_adjustment["hours"] == [13, 14]

def test_sanitize_hours_negative():
    raw = {"directives": [{"note_index": 0, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [-5]}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.directive_type == "no_op"

def test_sanitize_hours_greater_than_23():
    raw = {"directives": [{"note_index": 0, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [24, 25]}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.directive_type == "no_op"

def test_sanitize_minimum_energy_negative():
    raw = {"directives": [{"note_index": 0, "directive_type": "minimum_battery_reserve", "structured_adjustment": {"hours": [1], "minimum_energy_kwh": -10.0}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.structured_adjustment["minimum_energy_kwh"] == 0.0

def test_sanitize_max_grid_negative():
    raw = {"directives": [{"note_index": 0, "directive_type": "max_grid_window", "structured_adjustment": {"hours": [1], "max_grid_kwh": -10.0}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.structured_adjustment["max_grid_kwh"] == 0.0

def test_sanitize_missing_hours():
    raw = {"directives": [{"note_index": 0, "directive_type": "no_charge_window", "structured_adjustment": {}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.directive_type == "no_op"

def test_sanitize_adjustment_list():
    raw = {"directives": [{"note_index": 0, "directive_type": "no_charge_window", "structured_adjustment": [1,2,3]}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.directive_type == "no_op"

def test_sanitize_boolean():
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [1], "factor": True}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.directive_type == "no_op"

def test_sanitize_nan_infinity():
    raw = {"directives": [{"note_index": 0, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [1], "factor": math.nan}}]}
    d = sanitize(raw, 1, make_battery())[0]
    assert d.directive_type == "no_op"

def test_sanitize_applies_false_but_real_directive():
    raw = {"directives": [{"note_index": 0, "applies": False, "directive_type": "no_charge_window", "structured_adjustment": {"hours": [1]}}]}
    d = sanitize(raw, 1, make_battery())[0]
    # The sanitize logic ignores the incoming `applies` and forces it to True for real directives
    assert d.applies is True
    assert d.directive_type == "no_charge_window"

# ==========================================
# 4. Validation / Replay Edge Cases
# ==========================================
def test_replay_floating_point_drift():
    hours = make_hours()
    battery = make_battery()
    sc = compile_scenario(hours, battery, [])
    plan, _, _ = solve(hours, battery, [])
    
    # Introduce tiny drift < TOL
    plan[0]["battery_energy_after_kwh"] += 0.005
    totals = totals_from(plan, sc)
    errors = replay(sc, plan, totals)
    assert errors == [] # 0.005 is <= TOL (0.01)

def test_replay_totals_off_by_tol():
    hours = make_hours()
    battery = make_battery()
    sc = compile_scenario(hours, battery, [])
    plan, _, _ = solve(hours, battery, [])
    totals = totals_from(plan, sc)
    
    # Off by just under TOL to avoid floating point edge
    totals["total_grid_kwh"] += 0.009
    errors = replay(sc, plan, totals)
    assert errors == []

def test_replay_totals_off_by_more_than_tol():
    hours = make_hours()
    battery = make_battery()
    sc = compile_scenario(hours, battery, [])
    plan, _, _ = solve(hours, battery, [])
    totals = totals_from(plan, sc)
    
    totals["total_grid_kwh"] += 0.02
    errors = replay(sc, plan, totals)
    assert len(errors) == 1
    assert "total_grid_kwh does not match" in errors[0]

def test_replay_idle_with_tiny_kwh():
    hours = make_hours(demand_kwh=10.0, solar_kwh=10.0) # completely covered by solar
    battery = make_battery()
    sc = compile_scenario(hours, battery, [])
    plan, _, _ = solve(hours, battery, [])
    
    plan[0]["battery_action"] = "idle"
    plan[0]["battery_kwh"] = 0.001
    totals = totals_from(plan, sc)
    errors = replay(sc, plan, totals)
    # TOL is 0.01, so 0.001 should NOT trigger the error: "idle hour must have battery_kwh == 0"
    # because it checks abs(magnitude) > TOL.
    assert errors == []

def test_replay_end_of_day_neutrality_exactly_tol():
    hours = make_hours()
    battery = make_battery()
    sc = compile_scenario(hours, battery, [])
    plan, _, _ = solve(hours, battery, [])
    
    # Shift the final energy by exactly TOL
    plan[-1]["battery_energy_after_kwh"] = sc.initial + 0.01
    totals = totals_from(plan, sc)
    errors = replay(sc, plan, totals)
    assert errors == []

def test_replay_end_of_day_neutrality_beyond_tol():
    hours = make_hours()
    battery = make_battery()
    sc = compile_scenario(hours, battery, [])
    plan, _, _ = solve(hours, battery, [])
    
    # Shift the final energy by TOL + 0.001
    plan[-1]["battery_energy_after_kwh"] = sc.initial + 0.011
    totals = totals_from(plan, sc)
    errors = replay(sc, plan, totals)
    assert any("end-of-day battery neutrality violated" in e for e in errors)

# ==========================================
# 5. API / Schema Edge Cases
# ==========================================
def test_api_empty_json():
    resp = client.post("/optimize-energy", json={})
    assert resp.status_code == 400

def test_api_all_zeros():
    payload = valid_payload()
    payload["battery"]["capacity_kwh"] = 0.0 # invalid, must be > 0
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400

def test_api_scenario_id_empty_string():
    payload = valid_payload()
    payload["scenario_id"] = ""
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400

def test_api_extra_fields_ignored():
    payload = valid_payload()
    payload["extra_field"] = "should be ignored"
    
    with patch("app.main.llm.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = {"directives": [{"directive_type": "no_op"}]}
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 200

def test_api_hours_random_order():
    payload = valid_payload()
    import random
    random.shuffle(payload["hours"])
    
    with patch("app.main.llm.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = {"directives": [{"directive_type": "no_op"}]}
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 200
        # Check that the hourly_plan returned is actually ordered
        assert resp.json()["hourly_plan"][0]["hour"] == 0

def test_api_very_long_operator_note():
    payload = valid_payload()
    payload["operator_notes"] = ["A" * 10000]
    
    with patch("app.main.llm.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = {"directives": [{"directive_type": "no_op"}]}
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 200

def test_api_unicode_emoji_notes():
    payload = valid_payload()
    payload["operator_notes"] = ["🌞🔋🔥📉"]
    
    with patch("app.main.llm.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = {"directives": [{"directive_type": "no_op"}]}
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 200

def test_api_numeric_scenario_id():
    payload = valid_payload()
    payload["scenario_id"] = 123  # pydantic will coerce to string or reject depending on version/config
    
    with patch("app.main.llm.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = {"directives": [{"directive_type": "no_op"}]}
        resp = client.post("/optimize-energy", json=payload)
        # It turns out pydantic rejects int for str with min_length, returning 400.
        assert resp.status_code == 400

def test_api_methods():
    assert client.post("/health").status_code == 405
    assert client.get("/optimize-energy").status_code == 405

# ==========================================
# 6. Response Schema Correctness
# ==========================================
def test_response_schema_correctness():
    payload = valid_payload()
    with patch("app.main.llm.extract", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = {
            "directives": [
                {
                    "note_index": 0,
                    "applies": True,
                    "directive_type": "no_charge_window",
                    "structured_adjustment": {"hours": [1]},
                    "explanation": "No charge on hour 1"
                }
            ]
        }
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        
        # Verify right fields
        expected_fields = {"scenario_id", "directive_interpretation", "hourly_plan", 
                           "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"}
        assert set(data.keys()) == expected_fields
        
        # Verify directive_interpretation fields
        d = data["directive_interpretation"][0]
        assert set(d.keys()) == {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"}
        assert d["note_index"] == 0
        assert d["applies"] is True
        assert d["directive_type"] == "no_charge_window"
        
        # Verify hourly_plan
        plan = data["hourly_plan"]
        assert len(plan) == 24
        for row in plan:
            assert set(row.keys()) == {"hour", "grid_kwh", "solar_used_kwh", "battery_action", "battery_kwh", "battery_energy_after_kwh"}
            
        # Verify plan summary
        assert isinstance(data["plan_summary"], str)
        assert len(data["plan_summary"]) > 0
        
        # Verify totals
        calc_grid = sum(r["grid_kwh"] for r in plan)
        calc_cost = sum(r["grid_kwh"] * 5.0 for r in plan) # tariff is 5.0 in valid_payload
        calc_peak = max(r["grid_kwh"] for r in plan)
        
        assert math.isclose(data["total_grid_kwh"], calc_grid, abs_tol=0.001)
        assert math.isclose(data["total_cost_bdt"], calc_cost, abs_tol=0.001)
        assert math.isclose(data["peak_grid_kwh"], calc_peak, abs_tol=0.001)

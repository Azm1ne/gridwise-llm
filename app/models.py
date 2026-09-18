"""Request/response schemas — exactly the Problem Statement contract (S7, S10)."""
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)
DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]


class HourInput(BaseModel):
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float


class Battery(BaseModel):
    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    @model_validator(mode="after")
    def _state_is_reachable(self):
        """Reject batteries no schedule could satisfy.

        End-of-day neutrality (spec 9.6) pins the final charge to the initial one,
        so starting below the floor or above capacity is infeasible for ANY plan.
        Without this the solver raises and the request became a 500 instead of a 400.
        """
        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise ValueError("initial_energy_kwh is below minimum_energy_kwh")
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh exceeds capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh exceeds capacity_kwh")
        return self


class OptimizeRequest(BaseModel):
    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourInput] = Field(min_length=24, max_length=24)
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, v: list[str]) -> list[str]:
        if any(not n.strip() for n in v):
            raise ValueError("operator_notes entries must be non-empty")
        return v

    @field_validator("hours")
    @classmethod
    def _hours_cover_day(cls, v: list[HourInput]) -> list[HourInput]:
        if sorted(h.hour for h in v) != list(range(24)):
            raise ValueError("hours must contain each hour 0..23 exactly once")
        return v


class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[dict] = None
    explanation: str


class HourPlan(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str

"""Generate README figures from the real solver, not from hand-drawn mock-ups.

    python scripts/make_diagrams.py     -> docs/architecture.png, docs/schedule.png

matplotlib is a docs-only dependency (requirements-dev.txt); it is deliberately
kept out of the runtime image.
"""
import json
import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.directives import Directive, compile_scenario
from app.models import OptimizeRequest
from app.optimizer import solve
from app.validate import totals_from

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)

INK = "#1b1f2a"
MUTED = "#6b7280"
GRID_C = "#d9534f"
SOLAR_C = "#f0ad4e"
BATT_C = "#5bc0de"
LINE_C = "#2c3e50"


def architecture():
    """The one claim the rubric cares about: the LLM never touches the mathematics."""
    fig, ax = plt.subplots(figsize=(13, 4.4))
    ax.set_xlim(0, 13); ax.set_ylim(0, 4.4); ax.axis("off")

    stages = [
        ("Operator\nnotes", "natural language", "#e9ecef", INK),
        ("Gemini\ninterpretation", "1 call · temp 0\nJSON out", "#cfe2ff", "#084298"),
        ("Deterministic\nguardrails", "repair · clamp\nor downgrade", "#ffe69c", "#664d03"),
        ("Directive\ncompiler", "per-hour\nbound arrays", "#ffe69c", "#664d03"),
        ("LP optimizer\n(HiGHS)", "72 vars\nexact optimum", "#d1e7dd", "#0f5132"),
        ("Replay\nvalidator", "judge's own\nchecks", "#d1e7dd", "#0f5132"),
        ("JSON\nresponse", "interpretation\n+ 24h plan", "#e9ecef", INK),
    ]
    width, gap, y = 1.55, 0.29, 2.15
    for i, (title, sub, fill, fg) in enumerate(stages):
        x = 0.25 + i * (width + gap)
        ax.add_patch(FancyBboxPatch((x, y - 0.72), width, 1.44,
                                    boxstyle="round,pad=0.03,rounding_size=0.12",
                                    facecolor=fill, edgecolor=fg, linewidth=1.4))
        ax.text(x + width / 2, y + 0.28, title, ha="center", va="center",
                fontsize=10.5, fontweight="bold", color=fg)
        ax.text(x + width / 2, y - 0.31, sub, ha="center", va="center",
                fontsize=8.0, color=fg, alpha=0.85)
        if i < len(stages) - 1:
            ax.add_patch(FancyArrowPatch((x + width, y), (x + width + gap, y),
                                         arrowstyle="-|>", mutation_scale=15,
                                         linewidth=1.5, color=MUTED))

    ax.annotate("", xy=(2.35, 1.30), xytext=(2.35, 0.62),
                arrowprops=dict(arrowstyle="-|>", color=GRID_C, linewidth=1.5))
    ax.text(2.55, 0.66, "on failure: every note → no_op, schedule still valid",
            fontsize=8.6, color=GRID_C, va="center")

    ax.text(0.25, 3.92, "LLM → deterministic guardrails → optimizer",
            fontsize=13.5, fontweight="bold", color=INK)
    ax.text(0.25, 3.52,
            "The model decides only what a note MEANS. Every number it returns is "
            "re-checked, clamped or discarded before any solver sees it.",
            fontsize=9.4, color=MUTED)
    ax.text(0.25, 0.20,
            "Blue = language · Amber = deterministic validation · Green = mathematics",
            fontsize=8.4, color=MUTED, style="italic")

    fig.tight_layout()
    fig.savefig(DOCS / "architecture.png", dpi=170, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)


def schedule():
    """A real solved case: SAMPLE-10, reserve + grid cap + a distractor."""
    cases = json.loads((ROOT / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text())["cases"]
    case = next(c for c in cases if c["id"] == "SAMPLE-10")
    request = OptimizeRequest(**case["input"])
    directives = [Directive(d["note_index"], d["applies"], d["directive_type"],
                            d["structured_adjustment"], d["explanation"])
                  for d in case["expected_output"]["directive_interpretation"]]
    plan, sc, _ = solve(request.hours, request.battery, directives)
    totals = totals_from(plan, sc)

    hours = list(range(24))
    grid = [r["grid_kwh"] for r in plan]
    solar = [r["solar_used_kwh"] for r in plan]
    discharge = [r["battery_kwh"] if r["battery_action"] == "discharge" else 0 for r in plan]
    charge = [-r["battery_kwh"] if r["battery_action"] == "charge" else 0 for r in plan]
    soc = [r["battery_energy_after_kwh"] for r in plan]

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(12.5, 7.4), sharex=True,
        gridspec_kw={"height_ratios": [2.5, 1.25], "hspace": 0.13})

    top.bar(hours, solar, color=SOLAR_C, label="Solar used", zorder=3)
    top.bar(hours, discharge, bottom=solar, color=BATT_C, label="Battery discharge", zorder=3)
    top.bar(hours, grid, bottom=[s + d for s, d in zip(solar, discharge)],
            color=GRID_C, label="Grid import", zorder=3)
    top.bar(hours, charge, color=BATT_C, alpha=0.45, label="Battery charge", zorder=3)
    top.plot(hours, [h.demand_kwh for h in sorted(request.hours, key=lambda x: x.hour)],
             color=INK, linewidth=2.0, marker="o", markersize=3.4, label="Demand", zorder=5)

    cap_hours = [h for d in directives if d.directive_type == "max_grid_window"
                 for h in d.structured_adjustment["hours"]]
    if cap_hours:
        cap = next(d.structured_adjustment["max_grid_kwh"] for d in directives
                   if d.directive_type == "max_grid_window")
        top.hlines(cap, min(cap_hours) - 0.5, max(cap_hours) + 0.5, color=GRID_C,
                   linestyle="--", linewidth=1.8, zorder=6)
        top.text(max(cap_hours) + 0.7, cap + 14, f"grid cap {cap:g} kWh",
                 fontsize=8.6, color=GRID_C, va="center", ha="right")

    tariff = top.twinx()
    tariff.plot(hours, sc.tariff, color=MUTED, linewidth=1.5, linestyle=":", label="Tariff")
    tariff.set_ylabel("Tariff (BDT/kWh)", color=MUTED, fontsize=9.5)
    tariff.tick_params(axis="y", colors=MUTED, labelsize=8.5)

    top.set_ylabel("Energy (kWh)", fontsize=10)
    top.set_title(
        f"{case['id']} — solved schedule   ·   cost {totals['total_cost_bdt']:,.0f} BDT "
        f"(organizer optimum {case['expected_output']['total_cost_bdt']:,.0f})",
        fontsize=12.5, fontweight="bold", color=INK, pad=12)
    top.legend(loc="upper left", fontsize=8.6, framealpha=0.95, ncol=2)
    top.grid(axis="y", alpha=0.22, zorder=0)
    top.axhline(0, color=INK, linewidth=0.8)

    bottom.plot(hours, soc, color="#0f5132", linewidth=2.2, marker="o", markersize=3.6)
    bottom.axhline(request.battery.initial_energy_kwh, color=MUTED, linestyle="--",
                   linewidth=1.2)
    bottom.text(23.4, request.battery.initial_energy_kwh + 6,
                f"start = end = {request.battery.initial_energy_kwh:g}", fontsize=8.4,
                color=MUTED, va="bottom", ha="right")
    bottom.fill_between(hours, sc.min_soc, color=GRID_C, alpha=0.16, zorder=1)
    bottom.plot(hours, sc.min_soc, color=GRID_C, linewidth=1.5, linestyle="--",
                label="required minimum (raised by reserve directive)")
    bottom.set_ylabel("Battery (kWh)", fontsize=10)
    bottom.set_xlabel("Hour of day", fontsize=10)
    bottom.set_xticks(hours)
    bottom.set_ylim(0, max(soc) * 1.42)   # headroom so the legend clears the curve
    bottom.legend(loc="upper left", fontsize=8.4, framealpha=0.95)
    bottom.grid(alpha=0.22)

    for axis in (top, bottom, tariff):
        for spine in ("top", "right"):
            if axis is not tariff:
                axis.spines[spine].set_visible(False)

    fig.savefig(DOCS / "schedule.png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    architecture()
    schedule()
    print("wrote docs/architecture.png and docs/schedule.png")

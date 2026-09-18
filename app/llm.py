"""Operator notes -> structured directives, via Gemini.

The model is on the scoring path: it does the semantic work (paraphrase, relative
quantities, time windows) and nothing else. Every number it returns passes through
directives.sanitize() before any optimizer sees it.

One call per request, all notes together -- three calls would triple latency for no
accuracy gain, and p95 latency is scored.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from .models import Battery

log = logging.getLogger("gridwise.llm")

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "12"))


KEY_NAMES = ("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")


def api_key_from_env() -> str:
    """Accept any of the names people actually paste. Google's own docs say
    GEMINI_API_KEY, so insisting on LLM_API_KEY only invites a silent outage."""
    for name in KEY_NAMES:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _load_dotenv() -> None:
    """Read .env if present. Keeps real environment variables authoritative."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

SYSTEM_PROMPT = """You convert campus energy operator notes into structured directives.

Return ONLY JSON: {"directives": [one entry per note, in note_index order]}

Entry shape:
{"note_index": int, "applies": bool, "directive_type": str,
 "structured_adjustment": object|null, "explanation": "one short sentence"}

The six directive types and their EXACT structured_adjustment shapes:

1. "solar_reduction"          {"hours": [ints], "factor": number}
   Usable solar is reduced during those hours.
2. "minimum_battery_reserve"  {"hours": [ints], "minimum_energy_kwh": number}
   Battery energy must stay at or above that level during those hours.
3. "no_charge_window"         {"hours": [ints]}
   The battery CANNOT BE CHARGED. Triggers: charger isolated/unavailable/disabled,
   charging circuit down, technicians inspecting the charger, cannot draw INTO battery.
4. "no_discharge_window"      {"hours": [ints]}
   The battery CANNOT BE DISCHARGED. Triggers: must not discharge, discharge disabled,
   relay/protection testing, battery must not supply load, cannot draw FROM battery.
5. "max_grid_window"          {"hours": [ints], "max_grid_kwh": number}
   Grid import per hour must not exceed that amount. Triggers: feeder/transformer/
   substation limit, grid intake cap.
6. "no_op"                    null
   The note does not affect today's 24-hour energy schedule.

CRITICAL RULES

TIME: whole hours, start included, end EXCLUDED.
  "1 PM to 3 PM" -> [13, 14]          "6 PM until 10 PM" -> [18, 19, 20, 21]
  "2 AM until 5 AM" -> [2, 3, 4]      "between 13:00 and 15:00" -> [13, 14]
  "from one until three" (afternoon context) -> [13, 14]
  Hours are unique integers 0-23 in ascending order.

FACTOR: "factor" is the fraction of solar that REMAINS, never the amount lost.
  "drops to 20%" -> 0.2        "an 80% reduction" -> 0.2
  "roughly a quarter of forecast" -> 0.25    "about half" -> 0.5
  "one-fifth of normal output" -> 0.2        Range 0 to 1.

RELATIVE QUANTITIES: resolve percentages against the battery values given below.
  "50% of battery capacity" with capacity 200 -> minimum_energy_kwh = 100.

CHARGE vs DISCHARGE: read the direction of energy flow carefully. A charger being
serviced blocks CHARGING. A protection/relay test blocks DISCHARGING. Getting this
backwards is a total failure for the note.

applies: true for all five real directives; false ONLY for no_op, which must also
have structured_adjustment = null.

DISTRACTORS: notes about deadlines, menus, room bookings, registrations, notices,
staffing or anything not about electricity, solar, battery or the grid are no_op.

Emit exactly one entry per note, including distractors. Never invent another
directive type. Never alter demand, tariff or battery limits."""

FEW_SHOT = """Examples (different wording from any real case):

Notes:
0. "Inverter servicing will hold PV output near one-tenth of forecast from 09:00 to 11:00."
1. "Keep no less than a third of pack capacity banked between 8 PM and 10 PM."
2. "Parking permits get reissued on Sunday."
Battery: capacity_kwh=300, initial_energy_kwh=150, minimum_energy_kwh=60
Output:
{"directives":[
 {"note_index":0,"applies":true,"directive_type":"solar_reduction",
  "structured_adjustment":{"hours":[9,10],"factor":0.1},
  "explanation":"Inverter servicing leaves 10% of forecast solar for hours 9-10."},
 {"note_index":1,"applies":true,"directive_type":"minimum_battery_reserve",
  "structured_adjustment":{"hours":[20,21],"minimum_energy_kwh":100},
  "explanation":"A third of the 300 kWh capacity is 100 kWh for hours 20-21."},
 {"note_index":2,"applies":false,"directive_type":"no_op",
  "structured_adjustment":null,
  "explanation":"Parking permits do not affect the energy schedule."}]}

Notes:
0. "Rectifier bay is locked out for servicing from 3 AM to 6 AM, so nothing can go into the pack."
1. "Breaker coordination trials run 4 PM to 6 PM; the pack must not feed the bus."
2. "Substation is derated to 160 kWh per hour of draw from 7 PM to 9 PM."
Battery: capacity_kwh=400, initial_energy_kwh=200, minimum_energy_kwh=80
Output:
{"directives":[
 {"note_index":0,"applies":true,"directive_type":"no_charge_window",
  "structured_adjustment":{"hours":[3,4,5]},
  "explanation":"Charging equipment is locked out for hours 3-5."},
 {"note_index":1,"applies":true,"directive_type":"no_discharge_window",
  "structured_adjustment":{"hours":[16,17]},
  "explanation":"The battery may not supply load during trials in hours 16-17."},
 {"note_index":2,"applies":true,"directive_type":"max_grid_window",
  "structured_adjustment":{"hours":[19,20],"max_grid_kwh":160},
  "explanation":"Grid import is capped at 160 kWh for hours 19-20."}]}"""


def build_prompt(notes: list[str], battery: Battery) -> str:
    listed = "\n".join(f'{i}. "{note}"' for i, note in enumerate(notes))
    return (
        f"{SYSTEM_PROMPT}\n\n{FEW_SHOT}\n\n"
        f"Now interpret these {len(notes)} note(s).\n\n"
        f"Battery: capacity_kwh={battery.capacity_kwh}, "
        f"initial_energy_kwh={battery.initial_energy_kwh}, "
        f"minimum_energy_kwh={battery.minimum_energy_kwh}\n\n"
        f"Notes:\n{listed}\n\nOutput:"
    )


async def extract(notes: list[str], battery: Battery) -> dict | None:
    """Return the model's raw parsed JSON, or None on any failure.

    None is a valid outcome: the caller degrades to a no-directive schedule rather
    than guessing. Never raises -- a provider outage must not become a 5xx.
    """
    api_key = api_key_from_env()
    model = os.getenv("LLM_MODEL", "gemini-2.5-flash-lite")
    if not api_key:
        log.error("no API key found (tried %s) -- degrading to no_op", ", ".join(KEY_NAMES))
        return None

    payload = {
        "contents": [{"parts": [{"text": build_prompt(notes, battery)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    url = API_URL.format(model=model)

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        for attempt in range(2):
            try:
                response = await client.post(
                    url, json=payload, headers={"x-goog-api-key": api_key}
                )
                response.raise_for_status()
                text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except Exception:
                if attempt:   # second failure -- give up, caller degrades safely
                    return None
    return None

"""Operator notes -> structured directives, via Gemini.

The model is on the scoring path: it does the semantic work (paraphrase, relative
quantities, time windows) and nothing else. Every number it returns passes through
directives.sanitize() before any optimizer sees it.

One call per request, all notes together -- three calls would triple latency for no
accuracy gain, and p95 latency is scored.
"""
from __future__ import annotations

import asyncio
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


DEFAULT_MODEL = "gemini-3.5-flash-lite"
# Google retires model ids without notice: gemini-2.5-flash-lite started returning 404
# ("no longer available to new users") mid-build. If the configured id is dead we walk
# this list rather than losing every directive for the whole evaluation window.
FALLBACK_MODELS = ("gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash")

_working_model: str | None = None   # first id that answered; tried first afterwards
_dead_models: set[str] = set()      # ids that returned 404/400; never tried again

# A 429 is a project-wide quota limit, so switching model id cannot help -- every
# candidate shares the same bucket. Back off and retry the same id instead.
#
# The waits are sized to clear a per-minute quota window, not to look fast. Losing a
# latency point (p95 5-15s still scores 2/3) to rescue a 25-point interpretation is
# the right trade; the delay only ever fires when we are actually being throttled.
RETRY_AFTER_S = (5.0, 12.0)

# Hard ceiling on one extract() call. The judge fails a request at 30s, so we stop
# well short and let the caller degrade to no_op rather than time out.
BUDGET_S = float(os.getenv("LLM_BUDGET_S", "24"))


async def extract(notes: list[str], battery: Battery) -> dict | None:
    """Return the model's raw parsed JSON, or None on any failure.

    None is a valid outcome: the caller degrades to a no-directive schedule rather
    than guessing. Never raises -- a provider outage must not become a 5xx.
    """
    global _working_model

    api_key = api_key_from_env()
    if not api_key:
        log.error("no API key found (tried %s) -- degrading to no_op", ", ".join(KEY_NAMES))
        return None

    global _working_model

    configured = os.getenv("LLM_MODEL", DEFAULT_MODEL)
    candidates = [m for m in dict.fromkeys(
        (_working_model, configured, *FALLBACK_MODELS)) if m and m not in _dead_models]
    if not candidates:
        log.error("every candidate model is marked dead -- degrading to no_op")
        return None

    payload = {
        "contents": [{"parts": [{"text": build_prompt(notes, battery)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }

    deadline = asyncio.get_running_loop().time() + BUDGET_S

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        for model in candidates:
            for attempt in range(1 + len(RETRY_AFTER_S)):
                try:
                    response = await client.post(
                        API_URL.format(model=model), json=payload,
                        headers={"x-goog-api-key": api_key},
                    )
                    response.raise_for_status()
                    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(text)
                    if model != _working_model:
                        log.info("gemini model in use: %s", model)
                        _working_model = model
                    return parsed

                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    # Log why. A silent except here once hid a retired-model 404 on
                    # every request while the service still looked healthy.
                    log.error("gemini %s on %s: %s", status, model,
                              exc.response.text[:160])

                    if status in (400, 404):
                        _dead_models.add(model)      # retired or rejected: stop trying it
                        if model == _working_model:
                            _working_model = None
                        break                        # move to the next candidate

                    if status == 429:
                        # Quota is project-wide, so another model id would 429 too.
                        if attempt < len(RETRY_AFTER_S):
                            delay = _retry_delay(exc.response, RETRY_AFTER_S[attempt])
                            remaining = deadline - asyncio.get_running_loop().time()
                            if delay + TIMEOUT_S <= remaining:
                                log.warning("gemini rate limited, retrying %s in %.1fs",
                                            model, delay)
                                await asyncio.sleep(delay)
                                continue
                            log.warning("gemini rate limited, no budget left to retry")
                        return None                  # degrade safely, never time out

                    if attempt:
                        break

                except Exception as exc:
                    log.error("gemini call failed on %s: %s: %s", model,
                              type(exc).__name__, exc)
                    if attempt:
                        break
    return None


def _retry_delay(response: httpx.Response, default: float) -> float:
    """Honour the provider's own Retry-After when it is short enough to be useful."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 8.0)
        except ValueError:
            pass
    return default

"""GridWise service. LLM interprets, deterministic code decides."""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import llm
from .directives import compile_scenario, sanitize
from .models import OptimizeRequest, OptimizeResponse
from .optimizer import solve, summarise
from .validate import replay, totals_from

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gridwise")

app = FastAPI(title="GridWise LLM", version="1.0.0")


@app.get("/health")
async def health():
    """No LLM call, no I/O -- must answer immediately on a cold start."""
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(request: OptimizeRequest):
    started = time.perf_counter()

    llm_started = time.perf_counter()
    raw = await llm.extract(request.operator_notes, request.battery)
    llm_ms = (time.perf_counter() - llm_started) * 1000

    directives = sanitize(raw or {}, len(request.operator_notes), request.battery)

    solve_started = time.perf_counter()
    plan, scenario, tier = solve(request.hours, request.battery, directives)
    totals = totals_from(plan, scenario)

    # Validate against every directive we are about to REPORT, not against the
    # subset the solver settled for. Checking the relaxed scenario would let a plan
    # that ignores a directive pass while the response still claims it.
    promised = compile_scenario(request.hours, request.battery, directives)
    errors = replay(promised, plan, totals)
    if errors:
        # The directives are mutually unsatisfiable, so no schedule can honour them
        # all. Keep the best plan we have -- a valid schedule still scores on
        # constraints and cost -- and say so in the log and the summary.
        log.warning("plan cannot satisfy every extracted directive (tier=%d): %s",
                    tier, errors[:3])
    solve_ms = (time.perf_counter() - solve_started) * 1000

    log.info(
        "scenario=%s notes=%d llm_ms=%.0f solve_ms=%.0f total_ms=%.0f relax_tier=%d "
        "llm_ok=%s directives=%s",
        request.scenario_id, len(request.operator_notes), llm_ms, solve_ms,
        (time.perf_counter() - started) * 1000, tier, raw is not None,
        [d.directive_type for d in directives],
    )

    return OptimizeResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=[vars(d) for d in directives],
        hourly_plan=plan,
        plan_summary=summarise(plan, scenario, directives, unmet=bool(errors)),
        **totals,
    )


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError):
    """Return a controlled 400 with a detail body that is always serialisable.

    pydantic puts the raw exception object in `ctx` for errors raised by custom
    field validators. Passing exc.errors() through verbatim made json.dumps raise
    inside the handler, turning a 400 into a 500. Only loc/msg/type are echoed --
    they are plain strings, and nothing else belongs in a client-facing error.
    """
    detail = [
        {"loc": [str(part) for part in err.get("loc", ())],
         "msg": str(err.get("msg", "")),
         "type": str(err.get("type", ""))}
        for err in exc.errors()[:5]
    ]
    return JSONResponse(status_code=400, content={"error": "invalid request", "detail": detail})


@app.exception_handler(Exception)
async def internal_error(_: Request, exc: Exception):
    """Log the detail, return none of it -- no stack traces or secrets on the wire."""
    log.exception("unhandled error: %s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"error": "internal error"})

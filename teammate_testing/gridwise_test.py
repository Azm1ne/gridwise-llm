#!/usr/bin/env python3
"""
GridWise preliminary test harness  -  BUP CSE Fest 2026
=======================================================

Runs a deployed (or local) GridWise service against one or more case packs and
reports, per category, whether the Problem Statement + Participant Guide
requirements are met. Estimates the 80 automatable rubric points.

Standard library only. No pip install needed.

    python3 gridwise_test.py --url http://localhost:8000 \
        --cases public_sample_cases.json edge_cases.json

Exit code 0 = every graded check passed. 1 = at least one failure.
"""

import argparse, json, math, statistics, sys, time, urllib.error, urllib.request

TOL = 0.01
DIRECTIVE_TYPES = {"solar_reduction", "minimum_battery_reserve", "no_charge_window",
                   "no_discharge_window", "max_grid_window", "no_op"}
REQ_ADJ = {"solar_reduction": {"hours", "factor"},
           "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
           "no_charge_window": {"hours"},
           "no_discharge_window": {"hours"},
           "max_grid_window": {"hours", "max_grid_kwh"}}
SECRET_MARKERS = ["sk-", "Traceback (most recent call last)", "ANTHROPIC_API_KEY",
                  'File "/', "OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
                  "Bearer ", "-----BEGIN"]

C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[1m", "d": "\033[2m", "x": "\033[0m"}


def paint(on):
    if not on:
        for k in C:
            C[k] = ""


def num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# --------------------------------------------------------------------------- HTTP

def post(url, payload, timeout, raw=None):
    """Returns (status, body_text, parsed_json_or_None, elapsed_seconds, transport_error)."""
    data = raw if raw is not None else json.dumps(payload).encode()
    if isinstance(data, str):
        data = data.encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            el = time.time() - t0
            try:
                return r.status, body, json.loads(body), el, None
            except Exception:
                return r.status, body, None, el, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        el = time.time() - t0
        try:
            return e.code, body, json.loads(body), el, None
        except Exception:
            return e.code, body, None, el, None
    except Exception as e:
        return None, "", None, time.time() - t0, f"{type(e).__name__}: {e}"


def get(url, timeout):
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            el = time.time() - t0
            try:
                return r.status, body, json.loads(body), el, None
            except Exception:
                return r.status, body, None, el, None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), None, time.time() - t0, None
    except Exception as e:
        return None, "", None, time.time() - t0, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------- directive merging

def compile_directives(directives, battery):
    factor = [1.0] * 24
    minres = [float(battery["minimum_energy_kwh"])] * 24
    nocharge = [False] * 24
    nodisch = [False] * 24
    gridcap = [math.inf] * 24
    for d in directives or []:
        if not isinstance(d, dict):
            continue
        t = d.get("directive_type")
        a = d.get("structured_adjustment") or {}
        if not isinstance(a, dict):
            continue
        hrs = [h for h in (a.get("hours") or []) if isinstance(h, int) and 0 <= h <= 23]
        if t == "solar_reduction" and num(a.get("factor")):
            for h in hrs:
                factor[h] = min(factor[h], float(a["factor"]))
        elif t == "minimum_battery_reserve" and num(a.get("minimum_energy_kwh")):
            for h in hrs:
                minres[h] = max(minres[h], float(a["minimum_energy_kwh"]))
        elif t == "no_charge_window":
            for h in hrs:
                nocharge[h] = True
        elif t == "no_discharge_window":
            for h in hrs:
                nodisch[h] = True
        elif t == "max_grid_window" and num(a.get("max_grid_kwh")):
            for h in hrs:
                gridcap[h] = min(gridcap[h], float(a["max_grid_kwh"]))
    return dict(factor=factor, minres=minres, nocharge=nocharge, nodisch=nodisch, gridcap=gridcap)


# ------------------------------------------------------------------- the validator

BUCKETS = ["schema", "i_relevance", "i_type", "i_hours", "i_value", "i_shape",
           "a_directive", "a_balance", "a_battery", "a_consistency", "totals"]


def validate(inp, resp, truth, grade_interp=True):
    """Returns (errors_by_bucket, info). Mirrors what the organizer judge checks."""
    E = {b: [] for b in BUCKETS}
    info = {"cost": None, "valid_plan": False}
    bat = inp["battery"]
    cap = float(bat["capacity_kwh"])
    init = float(bat["initial_energy_kwh"])
    notes = inp["operator_notes"]

    if not isinstance(resp, dict):
        E["schema"].append("response body is not a JSON object")
        return E, info

    # ---- top level -------------------------------------------------------
    for f in ("scenario_id", "directive_interpretation", "hourly_plan",
              "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"):
        if f not in resp:
            E["schema"].append(f"missing top-level field '{f}'")
    if resp.get("scenario_id") != inp["scenario_id"]:
        E["schema"].append(f"scenario_id not echoed: got {resp.get('scenario_id')!r}, "
                           f"expected {inp['scenario_id']!r}")
    ps = resp.get("plan_summary")
    if not isinstance(ps, str) or not ps.strip():
        E["schema"].append("plan_summary must be a non-empty string")

    # ---- interpretation: shape ------------------------------------------
    di = resp.get("directive_interpretation")
    if not isinstance(di, list):
        E["schema"].append("directive_interpretation must be an array")
        di = []
    else:
        if len(di) != len(notes):
            E["schema"].append(f"directive_interpretation has {len(di)} entries, "
                               f"expected exactly {len(notes)} (one per operator note)")
        idxs = [e.get("note_index") for e in di if isinstance(e, dict)]
        if idxs != list(range(len(di))):
            E["schema"].append(f"note_index sequence {idxs} must be 0..N-1 ascending, no gaps or duplicates")

    for i, e in enumerate(di):
        tag = f"note {i}"
        if not isinstance(e, dict):
            E["schema"].append(f"{tag}: entry is not an object")
            continue
        for f in ("note_index", "applies", "directive_type", "structured_adjustment", "explanation"):
            if f not in e:
                E["schema"].append(f"{tag}: missing field '{f}'")
        t, ap, a = e.get("directive_type"), e.get("applies"), e.get("structured_adjustment")
        if t not in DIRECTIVE_TYPES:
            E["i_type"].append(f"{tag}: directive_type {t!r} is not one of the six supported types")
            continue
        if not isinstance(ap, bool):
            E["i_relevance"].append(f"{tag}: applies must be a boolean, got {ap!r}")
        elif t == "no_op" and ap is not False:
            E["i_relevance"].append(f"{tag}: no_op requires applies=false")
        elif t != "no_op" and ap is not True:
            E["i_relevance"].append(f"{tag}: {t} requires applies=true")
        if t == "no_op":
            if a is not None:
                E["i_shape"].append(f"{tag}: no_op requires structured_adjustment=null, got {a!r}")
            continue
        if not isinstance(a, dict):
            E["i_shape"].append(f"{tag}: structured_adjustment must be an object for {t}")
            continue
        if set(a.keys()) != REQ_ADJ[t]:
            E["i_shape"].append(f"{tag}: adjustment keys {sorted(a.keys())} != required {sorted(REQ_ADJ[t])}")
        hrs = a.get("hours")
        if not isinstance(hrs, list) or not hrs:
            E["i_hours"].append(f"{tag}: hours must be a non-empty array")
        else:
            if not all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in hrs):
                E["i_hours"].append(f"{tag}: hours must be integers 0..23, got {hrs}")
            elif len(set(hrs)) != len(hrs):
                E["i_hours"].append(f"{tag}: hours contain duplicates: {hrs}")
            elif hrs != sorted(hrs):
                E["i_hours"].append(f"{tag}: hours must be ascending, got {hrs}")
        if t == "solar_reduction":
            f_ = a.get("factor")
            if not num(f_) or not (0 <= f_ <= 1):
                E["i_value"].append(f"{tag}: factor must be a number in [0,1], got {f_!r}")
        if t == "minimum_battery_reserve":
            v = a.get("minimum_energy_kwh")
            if not num(v) or v < 0 or v > cap + TOL:
                E["i_value"].append(f"{tag}: minimum_energy_kwh must be 0..capacity({cap}), got {v!r}")
        if t == "max_grid_window":
            v = a.get("max_grid_kwh")
            if not num(v) or v < 0:
                E["i_value"].append(f"{tag}: max_grid_kwh must be finite and >= 0, got {v!r}")

    # ---- interpretation: against ground truth ----------------------------
    if grade_interp and truth:
        for i, exp in enumerate(truth):
            got = di[i] if i < len(di) and isinstance(di[i], dict) else None
            tag = f"note {i}"
            if got is None:
                E["i_relevance"].append(f"{tag}: no usable entry returned")
                continue
            if got.get("applies") != exp["applies"]:
                E["i_relevance"].append(f"{tag}: applies={got.get('applies')} but ground truth is {exp['applies']}")
            if got.get("directive_type") != exp["directive_type"]:
                E["i_type"].append(f"{tag}: directive_type {got.get('directive_type')!r} "
                                   f"but ground truth is {exp['directive_type']!r}")
                continue
            ea, ga = exp["structured_adjustment"], got.get("structured_adjustment")
            if ea is None:
                continue
            if not isinstance(ga, dict):
                E["i_shape"].append(f"{tag}: adjustment missing")
                continue
            if ga.get("hours") != ea["hours"]:
                E["i_hours"].append(f"{tag}: hours {ga.get('hours')} but ground truth is {ea['hours']}")
            for k in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
                if k in ea:
                    v = ga.get(k)
                    if not num(v) or abs(v - ea[k]) > TOL:
                        E["i_value"].append(f"{tag}: {k}={v!r} but ground truth is {ea[k]}")

    # ---- plan replay -----------------------------------------------------
    # graded cases replay against ORGANIZER ground truth (this is what the judge does);
    # ungraded cases replay against the team's own returned directives (self-consistency).
    replay_dirs = truth if (grade_interp and truth) else [e for e in di if isinstance(e, dict)]
    K = compile_directives(replay_dirs, bat)
    H = {h["hour"]: h for h in inp["hours"]}
    hp = resp.get("hourly_plan")

    if not isinstance(hp, list) or len(hp) != 24:
        E["schema"].append(f"hourly_plan must contain exactly 24 entries, got "
                           f"{len(hp) if isinstance(hp, list) else type(hp).__name__}")
        return E, info

    seen = [e.get("hour") for e in hp if isinstance(e, dict)]
    if sorted([s for s in seen if isinstance(s, int)]) != list(range(24)):
        E["schema"].append(f"hourly_plan must cover hours 0..23 exactly once, got {seen}")

    Ebat, tg, tc, pk = init, 0.0, 0.0, 0.0
    for e in sorted([x for x in hp if isinstance(x, dict)], key=lambda x: x.get("hour", 99)):
        h = e.get("hour")
        if not isinstance(h, int) or h not in H:
            E["schema"].append(f"hourly_plan entry with invalid hour {h!r}")
            continue
        tag = f"h{h:02d}"
        for f in ("hour", "grid_kwh", "solar_used_kwh", "battery_action",
                  "battery_kwh", "battery_energy_after_kwh"):
            if f not in e:
                E["schema"].append(f"{tag}: missing field '{f}'")
        g, s = e.get("grid_kwh"), e.get("solar_used_kwh")
        act, bk, eaf = e.get("battery_action"), e.get("battery_kwh"), e.get("battery_energy_after_kwh")
        if not all(num(v) for v in (g, s, bk, eaf)):
            E["a_consistency"].append(f"{tag}: non-numeric or non-finite value in plan entry")
            continue
        if g < -TOL:
            E["a_consistency"].append(f"{tag}: grid_kwh is negative ({g})")
        if s < -TOL:
            E["a_consistency"].append(f"{tag}: solar_used_kwh is negative ({s})")
        if bk < -TOL:
            E["a_consistency"].append(f"{tag}: battery_kwh is negative ({bk})")
        if act not in ("charge", "discharge", "idle"):
            E["a_consistency"].append(f"{tag}: battery_action {act!r} not in charge/discharge/idle")
            continue
        if act == "idle" and abs(bk) > TOL:
            E["a_consistency"].append(f"{tag}: battery_action=idle requires battery_kwh=0, got {bk}")
        eff = float(H[h]["solar_kwh"]) * K["factor"][h]
        if s > eff + TOL:
            E["a_balance"].append(f"{tag}: solar_used_kwh {s} exceeds effective solar {round(eff, 4)} "
                                  f"(base {H[h]['solar_kwh']} x factor {K['factor'][h]})")
        ch = bk if act == "charge" else 0.0
        dc = bk if act == "discharge" else 0.0
        if ch > float(bat["max_charge_kwh_per_hour"]) + TOL:
            E["a_battery"].append(f"{tag}: charge {ch} exceeds max_charge_kwh_per_hour "
                                  f"{bat['max_charge_kwh_per_hour']}")
        if dc > float(bat["max_discharge_kwh_per_hour"]) + TOL:
            E["a_battery"].append(f"{tag}: discharge {dc} exceeds max_discharge_kwh_per_hour "
                                  f"{bat['max_discharge_kwh_per_hour']}")
        if K["nocharge"][h] and ch > TOL:
            E["a_directive"].append(f"{tag}: charged {ch} kWh inside a no_charge_window")
        if K["nodisch"][h] and dc > TOL:
            E["a_directive"].append(f"{tag}: discharged {dc} kWh inside a no_discharge_window")
        if g > K["gridcap"][h] + TOL:
            E["a_directive"].append(f"{tag}: grid_kwh {g} exceeds max_grid_kwh {K['gridcap'][h]}")
        lhs, rhs = g + s + dc, float(H[h]["demand_kwh"]) + ch
        if abs(lhs - rhs) > TOL:
            E["a_balance"].append(f"{tag}: energy balance broken: grid+solar+discharge={round(lhs, 4)} "
                                  f"!= demand+charge={round(rhs, 4)}")
        Ebat = Ebat + ch - dc
        if abs(Ebat - eaf) > TOL:
            E["a_battery"].append(f"{tag}: battery_energy_after_kwh {eaf} != replayed {round(Ebat, 4)}")
        Ebat = eaf
        if Ebat < K["minres"][h] - TOL:
            E["a_directive"].append(f"{tag}: battery {Ebat} below required minimum {K['minres'][h]}")
        if Ebat > cap + TOL:
            E["a_battery"].append(f"{tag}: battery {Ebat} above capacity {cap}")
        tg += g
        tc += g * float(H[h]["tariff_bdt_per_kwh"])
        pk = max(pk, g)

    if abs(Ebat - init) > TOL:
        E["a_consistency"].append(f"end-of-day battery {round(Ebat, 4)} != initial_energy_kwh {init} "
                                  f"(neutrality rule)")
    for name, calc in (("total_grid_kwh", tg), ("total_cost_bdt", tc), ("peak_grid_kwh", pk)):
        got = resp.get(name)
        if not num(got):
            E["totals"].append(f"{name} missing or non-numeric")
        elif abs(got - calc) > TOL:
            E["totals"].append(f"{name}={got} but recalculated from hourly_plan = {round(calc, 4)}")

    info["cost"] = round(tc, 4)
    info["valid_plan"] = not (E["a_directive"] or E["a_balance"] or E["a_battery"] or E["a_consistency"])
    return E, info


# ------------------------------------------------------------------- robustness

def robustness_suite(base, timeout, sample_input):
    """Returns list of (name, passed, detail). Bad input must be rejected, never crash."""
    url = base + "/optimize-energy"
    out = []

    def bad_input(name, payload, raw=None):
        st, body, _, _, err = post(url, payload, timeout, raw=raw)
        if err:
            out.append((name, False, f"transport error / no response: {err}"))
        elif st in (400, 422):
            out.append((name, True, f"HTTP {st}"))
        elif st is not None and 500 <= st < 600:
            out.append((name, False, f"HTTP {st} - server error instead of controlled rejection"))
        else:
            out.append((name, False, f"HTTP {st} - malformed input was accepted"))
        return body

    bodies = []
    bodies.append(bad_input("malformed JSON body", None, raw=b'{"scenario_id": "X", '))
    bodies.append(bad_input("empty body", None, raw=b""))
    bodies.append(bad_input("not an object (JSON array)", None, raw=b"[1,2,3]"))

    import copy
    d = copy.deepcopy(sample_input); d.pop("battery", None)
    bodies.append(bad_input("missing battery object", d))
    d = copy.deepcopy(sample_input); d["hours"] = d["hours"][:23]
    bodies.append(bad_input("only 23 hour entries", d))
    d = copy.deepcopy(sample_input); d["hours"][5]["hour"] = 4
    bodies.append(bad_input("duplicate hour number", d))
    d = copy.deepcopy(sample_input); d["operator_notes"] = []
    bodies.append(bad_input("zero operator notes", d))
    d = copy.deepcopy(sample_input); d["operator_notes"] = ["a", "b", "c", "d"]
    bodies.append(bad_input("four operator notes (max is 3)", d))
    d = copy.deepcopy(sample_input); d["hours"][0]["demand_kwh"] = "lots"
    bodies.append(bad_input("non-numeric demand_kwh", d))
    d = copy.deepcopy(sample_input); d["battery"]["capacity_kwh"] = -50
    bodies.append(bad_input("negative battery capacity", d))

    leaks = []
    for b in bodies:
        for m in SECRET_MARKERS:
            if b and m in b:
                leaks.append(m)
    out.append(("no secrets or stack traces in error bodies", not leaks,
                "clean" if not leaks else f"found markers: {sorted(set(leaks))}"))
    return out


# ------------------------------------------------------------------------ runner

def load_packs(paths):
    cases = []
    for p in paths:
        with open(p) as f:
            data = json.load(f)
        for c in data.get("cases", []):
            c["_pack"] = p.split("/")[-1]
            cases.append(c)
    return cases


def main():
    ap = argparse.ArgumentParser(description="GridWise preliminary test harness")
    ap.add_argument("--url", required=True, help="base URL, e.g. http://localhost:8000")
    ap.add_argument("--cases", nargs="+", required=True, help="one or more case-pack JSON files")
    ap.add_argument("--only", nargs="*", default=None, help="run only these case ids")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-request timeout (spec limit: 30s)")
    ap.add_argument("--repeat", type=int, default=1, help="repeat each case N times (stability check)")
    ap.add_argument("--no-robustness", action="store_true")
    ap.add_argument("--save", metavar="DIR", default=None, help="save every response as JSON here")
    ap.add_argument("--report", metavar="FILE", default=None, help="write a machine-readable report")
    ap.add_argument("--quiet", action="store_true", help="only show failures")
    ap.add_argument("--no-color", action="store_true")
    a = ap.parse_args()
    paint(not a.no_color)
    base = a.url.rstrip("/")

    print(f"\n{C['b']}GridWise harness{C['x']}  ->  {base}")
    print(f"{C['d']}tolerance 0.01 | timeout {a.timeout}s | packs: {', '.join(p.split('/')[-1] for p in a.cases)}{C['x']}\n")

    # ---------------- health ----------------
    st, body, js, el, err = get(base + "/health", min(a.timeout, 60))
    health_ok = (st == 200 and isinstance(js, dict) and js.get("status") == "ok")
    tick = f"{C['g']}PASS{C['x']}" if health_ok else f"{C['r']}FAIL{C['x']}"
    print(f"[{tick}] GET /health  ->  HTTP {st} in {el*1000:.0f} ms  body={body[:80]!r}")
    if not health_ok:
        print(f"       {C['r']}must return HTTP 200 with JSON containing status=\"ok\"{C['x']}")
    if err:
        print(f"       {C['r']}{err}{C['x']}")
        print(f"\n{C['r']}Service unreachable. Aborting.{C['x']}\n")
        sys.exit(1)

    cases = load_packs(a.cases)
    if a.only:
        cases = [c for c in cases if c["id"] in a.only]
    print(f"\n{C['b']}Running {len(cases)} case(s){' x %d' % a.repeat if a.repeat > 1 else ''}{C['x']}\n")

    agg = {b: {"pass": 0, "total": 0} for b in BUCKETS}
    ratios, lats, results, hard_fail, unstable = [], [], [], 0, 0

    for c in cases:
        cid, inp = c["id"], c["input"]
        truth = (c.get("expected_output") or {}).get("directive_interpretation")
        grade = c.get("grade_interpretation", True)
        ref_cost = (c.get("expected_output") or {}).get("total_cost_bdt")
        runs = []
        for _ in range(a.repeat):
            st, body, js, el, err = post(base + "/optimize-energy", inp, a.timeout)
            lats.append(el)
            runs.append((st, body, js, el, err))
        st, body, js, el, err = runs[0]

        if a.save:
            import os
            os.makedirs(a.save, exist_ok=True)
            with open(f"{a.save}/{cid}.json", "w") as f:
                f.write(body or "")

        if err or st != 200 or js is None:
            hard_fail += 1
            print(f"[{C['r']}FAIL{C['x']}] {cid:10s} {c.get('label','')[:38]:38s} "
                  f"HTTP {st} {err or ''} {body[:90]!r}")
            for b in BUCKETS:
                agg[b]["total"] += 1
            results.append({"id": cid, "ok": False, "http": st, "error": err or body[:200]})
            continue

        for m in SECRET_MARKERS:
            if m in (body or ""):
                print(f"       {C['r']}SECRET LEAK: response contains {m!r}{C['x']}")

        E, info = validate(inp, js, truth, grade)
        for b in BUCKETS:
            agg[b]["total"] += 1
            if not E[b]:
                agg[b]["pass"] += 1
        nerr = sum(len(v) for v in E.values())

        if len(runs) > 1:
            costs = set()
            for r in runs:
                if r[2] and num(r[2].get("total_cost_bdt")):
                    costs.add(round(r[2]["total_cost_bdt"], 2))
                else:
                    costs.add("BAD")
            if len(costs) > 1:
                unstable += 1

        ratio = None
        if info["valid_plan"] and not E["totals"] and num(ref_cost) and info["cost"] is not None:
            if abs(ref_cost) < TOL and abs(info["cost"]) < TOL:
                ratio = 1.0
            elif info["cost"] > TOL:
                ratio = min(1.0, ref_cost / info["cost"])
            else:
                ratio = 1.0
            ratios.append(ratio)
        elif num(ref_cost):
            ratios.append(0.0)

        mark = f"{C['g']}PASS{C['x']}" if nerr == 0 else f"{C['r']}FAIL{C['x']}"
        cost_s = f"{info['cost']:.2f}" if info["cost"] is not None else "n/a"
        ref_s = f"{ref_cost:.2f}" if num(ref_cost) else "n/a"
        rat_s = f"{ratio*100:5.1f}%" if ratio is not None else "  -  "
        line = (f"[{mark}] {cid:10s} {c.get('label','')[:34]:34s} "
                f"{el*1000:6.0f}ms  cost {cost_s:>10s} / ref {ref_s:>10s}  opt {rat_s}"
                f"{'' if grade else '  (interp not graded)'}")
        if nerr or not a.quiet:
            print(line)
        if nerr:
            for b in BUCKETS:
                for msg in E[b][:6]:
                    print(f"         {C['y']}{b:13s}{C['x']} {msg}")
                if len(E[b]) > 6:
                    print(f"         {C['d']}{b:13s} ... and {len(E[b])-6} more{C['x']}")
            if c.get("rationale"):
                print(f"         {C['d']}why this case exists: {c['rationale'][:150]}{C['x']}")
        results.append({"id": cid, "ok": nerr == 0, "errors": {k: v for k, v in E.items() if v},
                        "cost": info["cost"], "ref_cost": ref_cost, "ratio": ratio,
                        "latency_s": round(el, 3)})

    # ---------------- robustness ----------------
    rob = []
    if not a.no_robustness and cases:
        print(f"\n{C['b']}Robustness & error handling{C['x']}\n")
        rob = robustness_suite(base, a.timeout, cases[0]["input"])
        for name, ok, detail in rob:
            mark = f"{C['g']}PASS{C['x']}" if ok else f"{C['r']}FAIL{C['x']}"
            print(f"[{mark}] {name:44s} {detail}")

    # ---------------- latency ----------------
    print(f"\n{C['b']}Latency{C['x']}\n")
    if lats:
        lats_s = sorted(lats)
        p95 = lats_s[max(0, math.ceil(0.95 * len(lats_s)) - 1)]
        print(f"  n={len(lats)}  min={min(lats)*1000:.0f}ms  "
              f"median={statistics.median(lats)*1000:.0f}ms  p95={p95*1000:.0f}ms  max={max(lats)*1000:.0f}ms")
        if p95 <= 5:
            lat_pts, verdict = 3, f"{C['g']}p95 <= 5s  ->  3/3 latency points{C['x']}"
        elif p95 <= 15:
            lat_pts, verdict = 2, f"{C['y']}p95 in 5-15s  ->  2/3 latency points{C['x']}"
        elif p95 <= 30:
            lat_pts, verdict = 1, f"{C['y']}p95 in 15-30s  ->  1/3 latency points{C['x']}"
        else:
            lat_pts, verdict = 0, f"{C['r']}p95 > 30s  ->  0/3 and timeouts count as failures{C['x']}"
        print(f"  {verdict}")
        over = [l for l in lats if l > 30]
        if over:
            print(f"  {C['r']}{len(over)} request(s) exceeded the 30s per-request limit{C['x']}")
    else:
        p95, lat_pts = 0, 0

    # ---------------- rubric estimate ----------------
    def pct(b):
        t = agg[b]["total"]
        return (agg[b]["pass"] / t) if t else 0.0

    i_pts = 5 * pct("i_relevance") + 5 * pct("i_type") + 5 * pct("i_hours") + \
            5 * (pct("i_value") + pct("i_shape")) / 2
    para_ids = {c["id"] for c in cases
                if any(t in ("paraphrase", "word_numbers", "fraction", "24h_clock", "verb_form",
                             "factor_remaining", "percent_of_capacity", "reduction_vs_remaining",
                             "no_digits", "relative_value")
                       for t in c.get("tests", []))}
    para = [r for r in results if r["id"] in para_ids]
    i_pts += 5 * ((sum(1 for r in para if r["ok"]) / len(para)) if para else pct("i_type"))

    ap_pts = 10 * pct("a_directive") + 5 * pct("a_balance") + 5 * pct("a_battery") + 5 * pct("a_consistency")
    opt_pts = 10 * (sum(ratios) / len(ratios) if ratios else 0.0)
    api_pts = 2 * (1 if health_ok else 0) + \
              2 * (sum(1 for n, o, _ in rob if o and "HTTP" in _ or o) / len(rob) if rob else 0) + \
              3 * pct("schema") + 3 * ((pct("schema") + pct("totals")) / 2)
    api_pts = min(10, api_pts)
    stability = 1.0 - (hard_fail + unstable) / max(1, len(cases))
    rob_ok = (sum(1 for _, o, _ in rob if o) / len(rob)) if rob else 0.0
    perf_pts = 2 * (1 if health_ok else 0) + lat_pts + 3 * stability + 2 * rob_ok

    print(f"\n{C['b']}Estimated rubric score (automated categories only){C['x']}\n")
    rows = [("1  LLM Directive Interpretation", i_pts, 25),
            ("2  Directive Application & Constraints", ap_pts, 25),
            ("3  Optimization Quality", opt_pts, 10),
            ("4  API Contract & Schema", api_pts, 10),
            ("5  Performance & Reliability", perf_pts, 10)]
    tot = 0
    for name, got, mx in rows:
        got = max(0.0, min(mx, got))
        tot += got
        col = C['g'] if got >= 0.9 * mx else (C['y'] if got >= 0.6 * mx else C['r'])
        bar = "#" * int(round(20 * got / mx)) + "." * (20 - int(round(20 * got / mx)))
        print(f"  {name:42s} {col}{got:5.1f}{C['x']} / {mx:<3d} [{bar}]")
    print(f"  {'':42s} {C['b']}{tot:5.1f}{C['x']} / 80   (+20 manual: Deployment/Docker 10, Documentation 10)")

    print(f"\n{C['b']}Per-check pass rate{C['x']}\n")
    label = {"schema": "response schema / field contract", "i_relevance": "applies & no_op relevance",
             "i_type": "directive_type correctness", "i_hours": "affected hours correctness",
             "i_value": "numeric values correctness", "i_shape": "structured_adjustment shape",
             "a_directive": "directive applied in hourly_plan", "a_balance": "energy balance & effective solar",
             "a_battery": "battery bounds / transitions / rates", "a_consistency": "actions, neutrality, non-negative",
             "totals": "totals match recalculated plan"}
    for b in BUCKETS:
        t, p = agg[b]["total"], agg[b]["pass"]
        col = C['g'] if t and p == t else C['r']
        print(f"  {label[b]:40s} {col}{p:3d}/{t:<3d}{C['x']}")

    print(f"\n{C['b']}Not automatable - verify by hand before submitting{C['x']}")
    for s in ["Repo created after reveal, private during event, public after deadline",
              "README: clean-machine quickstart, env var NAMES only, model/provider, solver, curl examples",
              "Docker image pullable by exact tag/digest, binds 0.0.0.0, no baked-in secrets, reaches /health",
              "3-minute video accessible, <= 3:00, covers LLM -> guardrails -> optimizer flow",
              "Endpoint reachable from OUTSIDE your dev network, no login/VPN required",
              "LLM genuinely in the interpretation path (not just plan_summary)"]:
        print(f"  [ ] {s}")

    if a.report:
        with open(a.report, "w") as f:
            json.dump({"base_url": base, "health_ok": health_ok, "p95_s": round(p95, 3),
                       "estimated_automated_score": round(tot, 2),
                       "category_scores": {n: round(g, 2) for n, g, _ in rows},
                       "buckets": agg, "robustness": [{"check": n, "pass": o, "detail": d} for n, o, d in rob],
                       "cases": results}, f, indent=2)
        print(f"\n{C['d']}report written to {a.report}{C['x']}")

    failed = [r for r in results if not r["ok"]] + [1 for _, o, _ in rob if not o]
    print(f"\n{C['b']}{'ALL CHECKS PASSED' if not failed and health_ok else str(len(failed)) + ' CHECK(S) FAILED'}{C['x']}\n")
    sys.exit(0 if (not failed and health_ok) else 1)


if __name__ == "__main__":
    main()

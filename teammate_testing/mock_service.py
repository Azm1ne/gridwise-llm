#!/usr/bin/env python3
"""
HARNESS SELF-TEST FIXTURE - NOT A SOLUTION.

Replays stored reference outputs from the case packs so you can confirm
gridwise_test.py works before pointing it at your real service.
It looks notes up by exact text, which the rules explicitly forbid.

  python3 mock_service.py --cases public_sample_cases.json edge_cases.json [--break BUG]

--break options: none, idle_kwh, neutrality, totals, hours_desc, no_op_applies,
                 missing_field, balance, cap_violation, drop_note, http500, slow
"""
import argparse, json, random, time
from http.server import BaseHTTPRequestHandler, HTTPServer

DB, BUG = {}, "none"

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/health": self._send(200, {"status":"ok"})
        else: self._send(404, {"error":"not found"})
    def do_POST(self):
        if self.path != "/optimize-energy": return self._send(404, {"error":"not found"})
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            req = json.loads(raw)
            assert isinstance(req, dict)
        except Exception:
            return self._send(400, {"error":"malformed JSON"})
        try:
            sid = req["scenario_id"]; notes = req["operator_notes"]; hrs = req["hours"]; bat = req["battery"]
            assert isinstance(notes, list) and 1 <= len(notes) <= 3 and all(isinstance(n,str) and n.strip() for n in notes)
            assert isinstance(hrs, list) and len(hrs) == 24
            assert sorted(h["hour"] for h in hrs) == list(range(24))
            for h in hrs:
                for k in ("demand_kwh","solar_kwh","tariff_bdt_per_kwh"):
                    assert isinstance(h[k],(int,float)) and not isinstance(h[k],bool)
            for k in ("capacity_kwh","initial_energy_kwh","minimum_energy_kwh",
                      "max_charge_kwh_per_hour","max_discharge_kwh_per_hour"):
                assert isinstance(bat[k],(int,float)) and bat[k] >= 0
        except Exception:
            return self._send(400, {"error":"invalid request schema"})
        key = tuple(notes)
        if key not in DB: return self._send(422, {"error":"unknown fixture scenario"})
        r = json.loads(json.dumps(DB[key])); r["scenario_id"] = sid
        if BUG == "slow": time.sleep(7)
        if BUG == "http500": return self._send(500, {"error":"boom"})
        if BUG == "idle_kwh":
            for p in r["hourly_plan"]:
                if p["battery_action"] == "idle": p["battery_kwh"] = 3.0; break
        if BUG == "neutrality":
            r["hourly_plan"][23]["battery_energy_after_kwh"] += 25
        if BUG == "totals":
            r["total_cost_bdt"] = round(r["total_cost_bdt"] * 0.85, 2)
        if BUG == "hours_desc":
            for d in r["directive_interpretation"]:
                a = d.get("structured_adjustment")
                if a and len(a.get("hours",[])) > 1: a["hours"] = list(reversed(a["hours"]))
        if BUG == "no_op_applies":
            for d in r["directive_interpretation"]:
                if d["directive_type"] == "no_op": d["applies"] = True; d["structured_adjustment"] = {"hours":[0]}
        if BUG == "missing_field":
            r.pop("peak_grid_kwh", None)
        if BUG == "balance":
            r["hourly_plan"][8]["grid_kwh"] += 12
        if BUG == "cap_violation":
            for d in r["directive_interpretation"]:
                if d["directive_type"] == "no_charge_window":
                    h = d["structured_adjustment"]["hours"][0]
                    p = r["hourly_plan"][h]
                    p["battery_action"] = "charge"; p["battery_kwh"] = 10
        if BUG == "drop_note" and len(r["directive_interpretation"]) > 1:
            r["directive_interpretation"] = r["directive_interpretation"][:-1]
        self._send(200, r)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--port", type=int, default=8000); ap.add_argument("--break", dest="bug", default="none")
    a = ap.parse_args(); BUG = a.bug
    for p in a.cases:
        for c in json.load(open(p))["cases"]:
            DB[tuple(c["input"]["operator_notes"])] = c["expected_output"]
    print(f"mock fixture on :{a.port}  cases={len(DB)}  bug={BUG}")
    HTTPServer(("0.0.0.0", a.port), H).serve_forever()

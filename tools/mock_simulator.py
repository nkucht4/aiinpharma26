#!/usr/bin/env python3
"""LOCAL MOCK of the Gate 3 manufacturing simulator. NOT the real one.

Used only to develop and test the twin when the real simulator is unreachable. Physics follow the Gate 2
root-cause analysis: insertion depth rises ~0.76 mm per +0.001 in of (bag port ID - tube OD) clearance,
and each assembly line de-calibrates (axis following error ramps up, depth drifts) at a different unit.

    python tools/mock_simulator.py --port 9000     # stateless, camelCase, startWith/limit like the real OpenAPI
    SIM_BASE_URL=http://127.0.0.1:9000/simulator MODE=REALTIME TEAM_ID=TEAM-01 CODE=XYZ789 python run.py
"""
from __future__ import annotations

import argparse
import json
import random
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ONSET = {"ASM-01": 240, "ASM-02": 330, "ASM-03": 450}      # unit index where de-calibration starts
SIGN = {"ASM-01": +1, "ASM-02": -1, "ASM-03": +1}
FIRST_UNIT = {"ASM-01": 100001, "ASM-02": 200001, "ASM-03": 300001}   # unit_id is globally unique
T0 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
TOTAL = 1000


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    """Deterministic streams for a given seed."""

    def __init__(self, seed, total=TOTAL, noise=0.30):
        rnd = random.Random(seed)
        self.bag, self.pvc, self.asm = [], [], {l: [] for l in ONSET}
        lot_off = {}
        for i in range(total):
            lot = "BAG-LOT-%d" % (101 + i // 50)
            off = lot_off.setdefault(lot, rnd.gauss(0, 0.00015))
            self.bag.append(dict(lot_id=lot, time=iso(T0 + timedelta(seconds=i)), port_id_in=round(0.1745 + off + rnd.gauss(0, 0.0004), 4)))
        reel_off = {}
        for i in range(total * 3):
            r = i // 100
            drift = -0.0011 if r >= 5 and r % 4 != 0 else 0.0
            off = reel_off.setdefault(r, drift + rnd.gauss(0, 0.0002))
            od = round(0.1700 + off + rnd.gauss(0, 0.0004), 4)
            idd = round(od - 0.0534 + rnd.gauss(0, 0.0003), 4)
            self.pvc.append(dict(lot_id="PVC-LOT-%d" % (201 + r // 5), reel_id="PVC-REEL-%03d" % (r + 1),
                                 time=iso(T0 + timedelta(seconds=i)), outer_diameter_in=od, inner_diameter_in=idd,
                                 wall_difference_in=round(abs(rnd.gauss(0.0008, 0.0004)), 4),
                                 wall_thickness_in=round((od - idd) / 2, 4), status="RUNNING", extruder_speed=60.0))
        bag_mean, reel_mean = {}, {}
        for b in self.bag:
            bag_mean.setdefault(b["lot_id"], []).append(b["port_id_in"])
        for p in self.pvc:
            reel_mean.setdefault(p["reel_id"], []).append(p["outer_diameter_in"])
        for l in ONSET:
            for i in range(total):
                lot = self.bag[i]["lot_id"]
                reel = self.pvc[i * 3]["reel_id"]            # 3 m of tubing per unit
                clr = sum(bag_mean[lot]) / len(bag_mean[lot]) - sum(reel_mean[reel]) / len(reel_mean[reel])
                ame = 0.12 + rnd.gauss(0, 0.012) + (0.004 * (i - ONSET[l]) if i > ONSET[l] else 0.0)
                depth = 15.0 + 759.0 * (clr - 0.005385) + SIGN[l] * 6.0 * (ame - 0.12) + rnd.gauss(0, noise)
                self.asm[l].append(dict(
                    line_id=l, unit_id="UNIT-%d" % (FIRST_UNIT[l] + i), time=iso(T0 + timedelta(seconds=3 * i)),
                    bag_lot_id=lot, tubing_lot_id=self.pvc[i * 3]["lot_id"], reel_id=reel,
                    pressure_bar=round(3.8 + rnd.gauss(0, 0.05), 2), insertion_speed_mm_s=round(25 + rnd.gauss(0, 0.3), 2),
                    insertion_target_cm=1.5, actuator_position_mm=15.0, axis_following_error_mm=round(ame, 3),
                    visual_inspection_mm=round(depth, 2)))

    def stream(self, line):
        if line == "BAG-01":
            return self.bag
        if line == "PVC-01":
            return self.pvc
        return self.asm.get(line)


class State:
    def __init__(self, args):
        self.args = args
        self.hist = World(seed=1)
        self.realtime = {}                  # team -> World (deterministic per team)

    def rt_world(self, team):
        if team not in self.realtime:
            self.realtime[team] = World(seed=sum(map(ord, team)) + 7)
        return self.realtime[team]


def camel(rec):
    """The real API returns camelCase JSON."""
    def c(k):
        parts = k.split("_")
        return parts[0] + "".join(p.capitalize() if p not in ("mm", "s") else p.upper() if p == "s" else "Mm" for p in parts[1:])
    return {c(k): v for k, v in rec.items()}


def make_handler(st):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            """Mirrors the real OpenAPI: stateless, startWith (1-based) + limit; ASM limit 1-100 HISTORICAL / 1-5 REALTIME."""
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                i = parts.index("lines")
                line, mode, team, code = parts[i + 1], parts[i + 3], parts[i + 5], parts[i + 7]
            except (ValueError, IndexError):
                return self._j(404, {"error": "path"})
            if mode not in ("HISTORICAL", "REALTIME"):
                return self._j(400, {"error": "mode"})
            if mode == "REALTIME" and code != st.args.passcode:
                return self._j(404, {"error": "team/code mismatch"})
            if mode == "HISTORICAL" and not code:
                return self._j(404, {"error": "team/code mismatch"})
            data = (st.hist if mode == "HISTORICAL" else st.rt_world(team)).stream(line)
            if data is None:
                return self._j(404, {"error": "line"})
            try:
                start = int(q.get("startWith", 1))
                limit = int(q["limit"]) if "limit" in q else None
            except ValueError:
                return self._j(400, {"error": "bad number"})
            comp = line in ("BAG-01", "PVC-01")
            if start < 1:
                return self._j(400, {"error": "startWith >= 1"})
            if comp:
                if limit is not None and not 1 <= limit <= 100:
                    return self._j(400, {"error": "limit 1-100"})
            else:
                limit = 1 if limit is None else limit
                if not 1 <= limit <= (100 if mode == "HISTORICAL" else 5):
                    return self._j(400, {"error": "limit out of range"})
            out = data[start - 1:] if limit is None else data[start - 1: start - 1 + limit]
            return self._j(200, [camel(r) for r in out])

        def _j(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    return H


def run(port=9000, passcode="XYZ789"):
    class A:  # noqa
        pass
    a = A(); a.passcode = passcode
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(State(a)))
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--passcode", default="XYZ789")
    a = ap.parse_args()
    print("Mock simulator on http://127.0.0.1:%d/simulator  (REALTIME passcode %s)" % (a.port, a.passcode))
    run(a.port, a.passcode).serve_forever()

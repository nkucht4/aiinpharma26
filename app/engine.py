"""Orchestration: polling, ingestion, monitoring, prediction locking, evaluation, traceability."""
from __future__ import annotations

import collections
import json
import os
import re
import threading
import time

from .client import ApiError, SimClient
from .model import LineMonitor, mean
from .store import Store, unit_number, utcnow


class Reject(Exception):
    """A request that violates a data-integrity rule (HTTP 409/422)."""


_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def norm_keys(rec):
    """The real simulator returns camelCase (unitId, visualInspectionMm ...); the app works in snake_case."""
    return {_CAMEL.sub("_", k).lower(): v for k, v in rec.items()}


def _f(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


class Engine:
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.client = client or SimClient(cfg)
        self.mon = {l: LineMonitor(l, cfg) for l in cfg.asm_lines}
        self.fc = {l: None for l in cfg.asm_lines}
        self.last_unit = {}
        self.cand_hist = {l: collections.deque(maxlen=5) for l in cfg.asm_lines}
        self.line_lock = {l: threading.RLock() for l in cfg.asm_lines}
        self.cycle_lock = threading.Lock()
        self.bag = {}            # lot -> [sum, count]
        self.reel = {}           # reel -> [sum_od, count]
        # The simulator is STATELESS (no server cursor): we keep the next 1-based row to request per line.
        self.cursor = {l: cfg.start_base for l in [cfg.bag_line, cfg.pvc_line] + list(cfg.asm_lines)}
        self.exhausted = {}
        self.running = False
        self.thread = None
        self.last_error = None
        self.cycles = 0
        self.asm_batch = max(1, min(cfg.asm_batch, cfg.asm_batch_max))
        self.poll_interval = cfg.poll_interval
        self.auto_lock = cfg.auto_lock
        self.journal_path = os.path.join(cfg.data_dir, "%s_%s_responses.jsonl" % (cfg.mode.lower(), "".join(
            c if c.isalnum() else "_" for c in cfg.team_id)))
        self._rebuild()

    # ------------------------------------------------------------------ rebuild from evidence
    def _rebuild(self):
        s = self.store
        for r in s.q("SELECT lot_id, SUM(port_id_in) s, COUNT(*) c FROM bag_meas WHERE port_id_in IS NOT NULL GROUP BY lot_id"):
            self.bag[r["lot_id"]] = [r["s"], r["c"]]
        for r in s.q("SELECT reel_id, SUM(outer_diameter_in) s, COUNT(*) c FROM pvc_meas WHERE outer_diameter_in IS NOT NULL GROUP BY reel_id"):
            self.reel[r["reel_id"]] = [r["s"], r["c"]]
        for l in self.cfg.asm_lines:
            for u in s.q("SELECT * FROM asm_units WHERE line_id=? ORDER BY n, rowid", (l,)):
                self._feed_monitor(l, u, announce=False)
            self.fc[l] = self.mon[l].forecast()
        for l in self.cursor:                      # resume after a restart: next row = highest stored row + 1
            r = s.q1("SELECT MAX(CAST(event_id AS INTEGER)) m FROM raw_events WHERE source_line=?", (l,))
            if r and r["m"] is not None:
                self.cursor[l] = int(r["m"]) + 1

    # ------------------------------------------------------------------ polling control
    def start(self):
        if self.running:
            return
        self.running = True
        self.store.add_audit("POLLER_START", detail=dict(interval=self.poll_interval, batch=self.asm_batch, mode=self.cfg.mode))
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def pause(self):
        if self.running:
            self.running = False
            self.store.add_audit("POLLER_PAUSE")

    def _loop(self):
        while self.running:
            t0 = time.time()
            try:
                self.step()
            except Exception as e:                     # never let the poller die silently
                self._error("poller: %r" % e)
            time.sleep(max(0.05, self.poll_interval - (time.time() - t0)))

    def _error(self, msg):
        if msg != self.last_error:
            self.last_error = msg
            self.store.add_audit("ERROR", detail=dict(message=msg))

    def step(self):
        """One polling cycle: BAG/PVC first (all pages), then one slice per assembly line."""
        with self.cycle_lock:
            cfg = self.cfg
            hist = cfg.mode == "HISTORICAL"
            for line, kind in ((cfg.bag_line, "BAG"), (cfg.pvc_line, "PVC")):
                if self.exhausted.get(line):
                    continue
                for _ in range(cfg.mat_pages):
                    got = self._pull(line, kind, cfg.mat_limit)
                    if got < cfg.mat_limit:
                        if hist and got == 0:
                            self.exhausted[line] = True
                        break
            for line in cfg.asm_lines:
                if self.exhausted.get(line):
                    continue
                lim = min(cfg.hist_batch, 100) if hist else min(self.asm_batch, cfg.asm_batch_max)
                got = self._pull(line, "ASM", lim)
                if hist and got == 0:
                    self.exhausted[line] = True            # past the end of the HISTORICAL dataset
            self.cycles += 1

    def _pull(self, line, kind, limit):
        cfg = self.cfg
        start = self.cursor[line]
        try:
            resp = self.client.fetch(line, limit=limit, start=start)
        except ApiError as e:
            self._error("%s %s: %s" % (line, kind, e))
            return 0
        # 1) journal the raw response to a plain file BEFORE any database work
        os.makedirs(cfg.data_dir, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(ts=utcnow(), line=line, url=resp["url"], params=resp["params"], body=resp["raw_text"])) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        # 2) ingest, then advance our own cursor (the server has none)
        self.ingest(line, kind, resp, start)
        n = len(resp["records"])
        self.cursor[line] = start + n
        if n and self.last_error and self.last_error.startswith(line):
            self.last_error = None
        return n

    # ------------------------------------------------------------------ ingestion
    @staticmethod
    def _event_id(kind, rec):
        """Fallback when the source row number is unknown."""
        for k in ("event_id", "id", "record_id", "sequence", "seq"):
            if rec.get(k) not in (None, ""):
                return str(rec[k])
        if kind == "ASM":
            return str(rec.get("unit_id"))
        val = rec.get("port_id_in") if kind == "BAG" else rec.get("outer_diameter_in")
        return "|".join(str(x) for x in (rec.get("time"), rec.get("lot_id"), rec.get("reel_id", ""), val))

    def ingest(self, line, kind, resp, first_row=None):
        """Store a slice. Event id = 1-based source row (startWith + index): unique per line and replay-safe."""
        s = self.store
        records = resp["records"]
        new_asm = []
        s.begin()
        try:
            rid = s.add_request(line, resp["url"], resp["params"], resp["status"], len(records), resp["body_sha256"])
            for i, raw_rec in enumerate(records):
                rec = norm_keys(raw_rec)
                eid = str(first_row + i) if first_row is not None else self._event_id(kind, rec)
                uid = "%s:%s" % (line, eid)
                text = json.dumps(raw_rec, separators=(",", ":"), ensure_ascii=False)     # verbatim source record
                added = s.add_raw(rid, line, kind, eid, rec.get("unit_id"), text, rec.get("time"), uid)
                if added is None:
                    continue                                       # already stored (re-delivered row)
                seq, received = added
                if kind == "BAG":
                    s.add_bag(seq, rec)
                    if _f(rec.get("port_id_in")) is not None:
                        a = self.bag.setdefault(rec.get("lot_id"), [0.0, 0])
                        a[0] += _f(rec["port_id_in"]); a[1] += 1
                elif kind == "PVC":
                    s.add_pvc(seq, rec)
                    if _f(rec.get("outer_diameter_in")) is not None:
                        a = self.reel.setdefault(rec.get("reel_id"), [0.0, 0])
                        a[0] += _f(rec["outer_diameter_in"]); a[1] += 1
                else:
                    row = self._asm_row(line, rec, seq, received)
                    s.add_asm(row)
                    new_asm.append(row)
            s.commit()
        except Exception:
            s.rollback()
            raise
        if kind == "ASM" and new_asm:
            with self.line_lock[line]:
                for row in new_asm:
                    self._feed_monitor(line, row, announce=True)
                self.fc[line] = self.mon[line].forecast()
                self._auto_lock(line)
        return len(records)

    def _asm_row(self, line, rec, seq, received):
        depth = _f(rec.get("visual_inspection_mm"))
        result = None
        if depth is not None:
            result = "OK" if self.cfg.lsl_mm <= depth <= self.cfg.usl_mm else "NOK"
        bag = self.bag.get(rec.get("bag_lot_id"))
        reel = self.reel.get(rec.get("reel_id"))
        clr = (bag[0] / bag[1] - reel[0] / reel[1]) if bag and reel else None
        uid = rec.get("unit_id")
        return dict(line_id=rec.get("line_id") or line, unit_id=uid, n=unit_number(uid), time=rec.get("time"),
                    bag_lot_id=rec.get("bag_lot_id"), tubing_lot_id=rec.get("tubing_lot_id"), reel_id=rec.get("reel_id"),
                    pressure_bar=_f(rec.get("pressure_bar")), insertion_speed_mm_s=_f(rec.get("insertion_speed_mm_s")),
                    insertion_target_cm=_f(rec.get("insertion_target_cm")),
                    actuator_position_mm=_f(rec.get("actuator_position_mm")),
                    axis_following_error_mm=_f(rec.get("axis_following_error_mm")), visual_inspection_mm=depth,
                    result=result, clearance_in=clr, raw_seq=seq, received_at=received)

    def _feed_monitor(self, line, u, announce):
        if u.get("visual_inspection_mm") is None or u.get("n") is None:
            return
        mon = self.mon[line]
        if announce and mon.rows:                                  # "missed points": gaps in the unit sequence
            prev_n, step = mon.rows[-1]["n"], mon.unit_step()
            if u["n"] - prev_n > step:
                self.store.add_audit("MISSING_UNITS", line, u["unit_id"],
                                     dict(after=self.last_unit[line], missing=(u["n"] - prev_n) // step - 1))
            elif u["n"] <= prev_n:
                self.store.add_audit("OUT_OF_ORDER_UNIT", line, u["unit_id"], dict(after=self.last_unit[line]))
        self.last_unit[line] = u["unit_id"]
        alarm =self.mon[line].update(dict(n=u["n"], depth=u["visual_inspection_mm"], ame=u.get("axis_following_error_mm"),
                                          pressure=u.get("pressure_bar"), speed=u.get("insertion_speed_mm_s"),
                                          clearance_in=u.get("clearance_in")))
        if announce:
            if alarm:
                self.store.add_audit("DECALIBRATION_ALARM", line, u["unit_id"], alarm)
            if u.get("result") == "NOK" and self._first_nok(line)["unit_id"] == u["unit_id"]:
                p = self.store.q("SELECT id, predicted_unit_id FROM predictions WHERE line_id=? ORDER BY id", (line,))
                self.store.add_audit("FIRST_NONCONFORMING_UNIT", line, u["unit_id"],
                                     dict(depth_mm=u["visual_inspection_mm"], locked_prediction=p[0]["predicted_unit_id"] if p else None))

    def _first_nok(self, line):
        return self.store.q1("SELECT unit_id, n, visual_inspection_mm FROM asm_units WHERE line_id=? AND result='NOK' "
                             "ORDER BY n LIMIT 1", (line,))

    # ------------------------------------------------------------------ predictions
    def locked(self, line):
        return self.store.q("SELECT * FROM predictions WHERE line_id=? ORDER BY id", (line,))

    def lock(self, line, predicted_n=None, source="manual"):
        """Commit an immutable prediction BEFORE the predicted unit is retrieved (URS-DI-003)."""
        if line not in self.mon:
            raise Reject("unknown assembly line %s" % line)
        with self.line_lock[line]:
            mon = self.mon[line]
            if not mon.rows:
                raise Reject("no completed unit received yet for %s" % line)
            last_n = mon.rows[-1]["n"]
            last_uid = self.last_unit[line]
            fn = self._first_nok(line)
            if fn:
                raise Reject("%s already produced a nonconforming unit (%s); a prediction can no longer be locked" % (line, fn["unit_id"]))
            prev = self.locked(line)
            if prev and not (self.cfg.allow_relock_after_miss and prev[-1]["predicted_n"] <= last_n):
                raise Reject("%s already has a locked prediction (%s); predictions are immutable" % (line, prev[-1]["predicted_unit_id"]))
            fc = self.fc[line] or {}
            if predicted_n is None:
                if not fc.get("ready"):
                    raise Reject("not enough data to forecast yet (%s)" % fc.get("reason", ""))
                predicted_n = int(round(fc["predicted_n"]))
            predicted_n = int(predicted_n)
            if predicted_n < last_n + self.cfg.min_lead:
                raise Reject("predicted unit must be at least %d unit(s) ahead of the last received unit (%s)" % (self.cfg.min_lead, last_uid))
            # unit id keeps the width/prefix of the last unit
            tail = str(last_uid)
            digits = len(tail) - len(tail.rstrip("0123456789"))
            prefix = tail[: len(tail) - digits]
            pred_uid = "%s%0*d" % (prefix, digits, predicted_n)
            basis = dict(model="ridge-prior + weighted trend + first-failure pmf v1", forecast={k: v for k, v in fc.items() if k != "curve"},
                         auto=source == "auto", spec=dict(lsl=self.cfg.lsl_mm, usl=self.cfg.usl_mm),
                         next_row_to_request=dict(self.cursor))   # proof: nothing beyond Y was requested before the lock
            pid, at, h = self.store.add_prediction(line, pred_uid, predicted_n, last_uid, last_n, source, basis)
            self.store.add_audit("PREDICTION_LOCKED", line, pred_uid, dict(id=pid, at=at, hash=h, source=source, current_unit=last_uid))
            return dict(id=pid, line_id=line, predicted_unit_id=pred_uid, predicted_n=predicted_n, current_unit_id=last_uid,
                        predicted_at=at, source=source, hash=h)

    def _auto_lock(self, line):
        """Lock when a real drift is confirmed and the forecast is tight, or when failure is imminent.

        Scoring trade-off (jury): lead time L = X - Y rewards locking early, error E = |Z - X| rewards locking late.
        AUTO_LOCK_CONF moves the balance: higher = later, more accurate; lower = earlier, riskier.
        """
        fc = self.fc[line]
        if not (self.auto_lock and fc and fc.get("ready")) or self.locked(line) or self._first_nok(line):
            return
        cand = int(round(fc["predicted_n"]))
        last_n = self.mon[line].rows[-1]["n"]
        drifting = bool(fc.get("alarm_active")) and abs(fc.get("trend_z", 0.0)) >= 3.0 and fc.get("has_estimate")
        self.cand_hist[line].append(drifting)
        persistent = len(self.cand_hist[line]) >= 3 and all(list(self.cand_hist[line])[-3:])
        tight = fc.get("confidence", 0.0) >= self.cfg.lock_conf
        imminent = cand - last_n <= 2 * self.asm_batch + 2 or fc.get("p_next5", 0) > 0.5
        if drifting and persistent and (tight or imminent):
            try:
                self.lock(line, max(cand, last_n + max(1, self.cfg.min_lead)), "auto")
            except Reject:
                pass

    def evaluation(self, line):
        preds = self.locked(line)
        fn = self._first_nok(line)
        last_n = self.mon[line].rows[-1]["n"] if self.mon[line].rows else None
        out = []
        for p in preds:
            if fn is None:
                st = "UNIT_PASSED_OK" if last_n is not None and p["predicted_n"] <= last_n else "PENDING"
                delta = None
            else:
                delta = p["predicted_n"] - fn["n"]
                st = "HIT" if delta == 0 else ("PREDICTED_TOO_EARLY" if delta < 0 else "PREDICTED_TOO_LATE")
            # Jury scoring: X = predicted unit, Y = last unit inspected at prediction time, Z = actual first failing unit
            out.append(dict(prediction_id=p["id"], predicted_unit_id=p["predicted_unit_id"], status=st,
                            X=p["predicted_unit_id"], Y=p["current_unit_id"], Z=fn["unit_id"] if fn else None,
                            actual_first_nok=fn["unit_id"] if fn else None,
                            actual_depth_mm=fn["visual_inspection_mm"] if fn else None, delta_units=delta,
                            error_E=abs(delta) if delta is not None else None,          # E = |Z - X|
                            lead_time_L=p["predicted_n"] - p["current_n"]))             # L = X - Y
        return out

    def lots_summary(self):
        """Clear summary of the BAG lots and PVC reels used in production (jury: Real-Time Monitoring)."""
        s, c = self.store, self.cfg
        bags = s.q("SELECT b.lot_id, COUNT(*) n, AVG(port_id_in) mean, MIN(port_id_in) mn, MAX(port_id_in) mx, "
                   "SUM(port_id_in < ? OR port_id_in > ?) out_of_spec, MIN(time) first_t, MAX(time) last_t "
                   "FROM bag_meas b GROUP BY b.lot_id ORDER BY MIN(b.raw_seq)", (c.bag_lsl, c.bag_usl))
        use = {r["k"]: r for r in s.q("SELECT bag_lot_id k, COUNT(*) units, SUM(result='NOK') nok, MIN(n) first_n, MAX(n) last_n, "
                                      "GROUP_CONCAT(DISTINCT line_id) lines FROM asm_units GROUP BY bag_lot_id")}
        for b in bags:
            u = use.get(b["lot_id"], {})
            b.update(units=u.get("units", 0), nok=u.get("nok") or 0, first_unit=u.get("first_n"), last_unit=u.get("last_n"),
                     lines=u.get("lines"))
        reels = s.q("SELECT reel_id, MIN(lot_id) lot_id, COUNT(*) n, AVG(outer_diameter_in) od_mean, MIN(outer_diameter_in) od_min, "
                    "MAX(outer_diameter_in) od_max, AVG(inner_diameter_in) id_mean, AVG(wall_difference_in) wd_mean, "
                    "SUM(outer_diameter_in < ? OR outer_diameter_in > ?) od_out, SUM(status!='RUNNING') stopped "
                    "FROM pvc_meas GROUP BY reel_id ORDER BY MIN(raw_seq)", (c.od_lsl, c.od_usl))
        ruse = {r["k"]: r for r in s.q("SELECT reel_id k, COUNT(*) units, SUM(result='NOK') nok, MIN(n) first_n, MAX(n) last_n, "
                                       "GROUP_CONCAT(DISTINCT line_id) lines FROM asm_units GROUP BY reel_id")}
        for r in reels:
            u = ruse.get(r["reel_id"], {})
            r.update(units=u.get("units", 0), nok=u.get("nok") or 0, first_unit=u.get("first_n"), last_unit=u.get("last_n"),
                     lines=u.get("lines"))
        return dict(bag_lots=bags[-30:], reels=reels[-30:])

    # ------------------------------------------------------------------ read models
    def trace(self, unit_id, line=None):
        s = self.store
        if line:
            u = s.q1("SELECT * FROM asm_units WHERE unit_id=? AND line_id=? ORDER BY rowid LIMIT 1", (unit_id, line))
        else:
            u = s.q1("SELECT * FROM asm_units WHERE unit_id=? ORDER BY rowid LIMIT 1", (unit_id,))
        if not u:
            return None
        raw = s.q1("SELECT record_uid, request_id, payload, payload_sha256, received_at, hash FROM raw_events WHERE seq=?", (u["raw_seq"],))
        bag = s.q1("SELECT COUNT(*) n, AVG(port_id_in) mean, MIN(port_id_in) min, MAX(port_id_in) max, "
                   "SUM(port_id_in < ? OR port_id_in > ?) out FROM bag_meas WHERE lot_id=?",
                   (self.cfg.bag_lsl, self.cfg.bag_usl, u["bag_lot_id"]))
        reel = s.q1("SELECT COUNT(*) n, AVG(outer_diameter_in) od_mean, MIN(outer_diameter_in) od_min, MAX(outer_diameter_in) od_max, "
                    "AVG(inner_diameter_in) id_mean, AVG(wall_difference_in) wd_mean, SUM(outer_diameter_in < ? OR outer_diameter_in > ?) od_out "
                    "FROM pvc_meas WHERE reel_id=?", (self.cfg.od_lsl, self.cfg.od_usl, u["reel_id"]))
        lot = s.q1("SELECT COUNT(*) n, AVG(outer_diameter_in) od_mean FROM pvc_meas WHERE lot_id=?", (u["tubing_lot_id"],))
        return dict(unit=u, source_record=raw, bag_lot=dict(lot_id=u["bag_lot_id"], **(bag or {})),
                    pvc_reel=dict(reel_id=u["reel_id"], **(reel or {})), pvc_lot=dict(lot_id=u["tubing_lot_id"], **(lot or {})),
                    predictions=self.locked(u["line_id"]), evaluation=self.evaluation(u["line_id"]),
                    inspection=dict(depth_mm=u["visual_inspection_mm"], result=u["result"], lsl=self.cfg.lsl_mm, usl=self.cfg.usl_mm))

    def snapshot(self, tail=400):
        cfg, s = self.cfg, self.store
        lines = {}
        for l in cfg.asm_lines:
            rows = s.q("SELECT unit_id,n,time,visual_inspection_mm d,axis_following_error_mm ame,pressure_bar p,"
                       "insertion_speed_mm_s v,clearance_in c,result,reel_id,bag_lot_id FROM asm_units WHERE line_id=? "
                       "ORDER BY n DESC LIMIT ?", (l, tail))[::-1]
            counts = s.q1("SELECT COUNT(*) n, SUM(result='NOK') nok FROM asm_units WHERE line_id=?", (l,))
            fc = self.fc.get(l) or {}
            prm = self.locked(l)
            lines[l] = dict(series=rows, count=counts["n"], nok=counts["nok"] or 0, forecast=fc, locked=prm,
                            evaluation=self.evaluation(l), first_nok=self._first_nok(l),
                            alarm=self.mon[l].alarm, last_unit=self.last_unit.get(l),
                            cursor=self.cursor.get(l))
        bag = s.q("SELECT raw_seq i, lot_id lot, port_id_in v FROM bag_meas ORDER BY raw_seq DESC LIMIT ?", (tail,))[::-1]
        pvc = s.q("SELECT raw_seq i, reel_id reel, outer_diameter_in od, inner_diameter_in idd, wall_difference_in wd "
                  "FROM pvc_meas ORDER BY raw_seq DESC LIMIT ?", (tail * 2,))[::-1]
        audit = s.q("SELECT ts,type,line_id,unit_id,detail FROM audit ORDER BY id DESC LIMIT 40")
        return dict(
            config=dict(mode=cfg.mode, team=cfg.team_id, base_url=cfg.base_url, lsl=cfg.lsl_mm, usl=cfg.usl_mm,
                        target=cfg.target_mm, bag=[cfg.bag_lsl, cfg.bag_usl], od=[cfg.od_lsl, cfg.od_usl],
                        idr=[cfg.id_lsl, cfg.id_usl], auto_lock=self.auto_lock, asm_batch=self.asm_batch,
                        poll_interval=self.poll_interval, min_lead=cfg.min_lead),
            engine=dict(running=self.running, cycles=self.cycles, last_error=self.last_error, now=utcnow()),
            lines=lines, bag=bag, pvc=pvc, audit=audit, lots=self.lots_summary(),
            counts=dict(raw=s.q1("SELECT COUNT(*) n FROM raw_events")["n"], bag=len(self.bag), reels=len(self.reel)))

"""Append-only, hash-chained evidence store (SQLite).

ALCOA+ design:
  * every source record is stored verbatim (raw_events.payload) together with its SHA-256,
    the request it came from and the UTC time we received it;
  * raw_events, requests, predictions and audit are chained: hash = SHA256(prev_hash + record),
    so any later edit/delete is detectable with verify();
  * SQLite triggers reject UPDATE and DELETE on every table, so the application itself cannot
    alter history.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
import threading

GENESIS = "0" * 64


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit_number(unit_id):
    m = re.search(r"(\d+)\s*$", str(unit_id))
    return int(m.group(1)) if m else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, line_id TEXT NOT NULL, url TEXT NOT NULL, params TEXT,
  http_status INTEGER, n_records INTEGER, body_sha256 TEXT,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS raw_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  record_uid TEXT NOT NULL UNIQUE,         -- source line + event id (URS-DI-001)
  source_line TEXT NOT NULL, kind TEXT NOT NULL, event_id TEXT NOT NULL, unit_id TEXT,
  request_id INTEGER NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
  source_time TEXT, received_at TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS asm_units(
  line_id TEXT NOT NULL, unit_id TEXT NOT NULL, n INTEGER, time TEXT,
  bag_lot_id TEXT, tubing_lot_id TEXT, reel_id TEXT,
  pressure_bar REAL, insertion_speed_mm_s REAL, insertion_target_cm REAL,
  actuator_position_mm REAL, axis_following_error_mm REAL, visual_inspection_mm REAL,
  result TEXT, clearance_in REAL, raw_seq INTEGER NOT NULL, received_at TEXT NOT NULL,
  PRIMARY KEY(line_id, unit_id));

CREATE TABLE IF NOT EXISTS bag_meas(
  raw_seq INTEGER PRIMARY KEY, lot_id TEXT, time TEXT, port_id_in REAL);

CREATE TABLE IF NOT EXISTS pvc_meas(
  raw_seq INTEGER PRIMARY KEY, lot_id TEXT, reel_id TEXT, time TEXT,
  outer_diameter_in REAL, inner_diameter_in REAL, wall_difference_in REAL,
  wall_thickness_in REAL, status TEXT, extruder_speed REAL);

CREATE TABLE IF NOT EXISTS predictions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  line_id TEXT NOT NULL, predicted_unit_id TEXT NOT NULL, predicted_n INTEGER NOT NULL,
  current_unit_id TEXT NOT NULL, current_n INTEGER NOT NULL,
  predicted_at TEXT NOT NULL, source TEXT NOT NULL, basis TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, type TEXT NOT NULL, line_id TEXT, unit_id TEXT, detail TEXT,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS ix_asm_line_n ON asm_units(line_id, n);
CREATE INDEX IF NOT EXISTS ix_raw_line ON raw_events(source_line, seq);
"""

IMMUTABLE = ["requests", "raw_events", "asm_units", "bag_meas", "pvc_meas", "predictions", "audit"]
CHAINED = {
    "requests": "id",
    "raw_events": "seq",
    "predictions": "id",
    "audit": "id",
}


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")      # durability (Enduring)
        self.db.executescript(SCHEMA)
        for t in IMMUTABLE:
            for op in ("UPDATE", "DELETE"):
                self.db.execute(
                    "CREATE TRIGGER IF NOT EXISTS %s_no_%s BEFORE %s ON %s "
                    "BEGIN SELECT RAISE(ABORT,'%s is append-only'); END"
                    % (t, op.lower(), op, t, t))
        self._last = {t: self._load_last(t) for t in CHAINED}

    # ------------------------------------------------------------------ helpers
    def _load_last(self, table):
        row = self.db.execute("SELECT hash FROM %s ORDER BY %s DESC LIMIT 1" % (table, CHAINED[table])).fetchone()
        return row["hash"] if row else GENESIS

    def _chain(self, table, fields):
        prev = self._last[table]
        h = sha(prev + canon(fields))
        return prev, h

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def q1(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ writers
    def add_request(self, line_id, url, params, http_status, n_records, body_sha256):
        with self.lock:
            f = dict(ts=utcnow(), line_id=line_id, url=url, params=canon(params or {}),
                     http_status=http_status, n_records=n_records, body_sha256=body_sha256)
            prev, h = self._chain("requests", f)
            cur = self.db.execute(
                "INSERT INTO requests(ts,line_id,url,params,http_status,n_records,body_sha256,prev_hash,hash) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (f["ts"], line_id, url, f["params"], http_status, n_records, body_sha256, prev, h))
            self._last["requests"] = h
            return cur.lastrowid

    def uid_exists(self, uid):
        return self.q1("SELECT 1 AS x FROM raw_events WHERE record_uid=?", (uid,)) is not None

    def add_raw(self, request_id, source_line, kind, event_id, unit_id, payload_text, source_time, uid):
        """Insert one verbatim source record. Returns seq (or None if uid already stored)."""
        with self.lock:
            if self.uid_exists(uid):
                return None
            f = dict(record_uid=uid, source_line=source_line, kind=kind, event_id=event_id, unit_id=unit_id,
                     request_id=request_id, payload_sha256=sha(payload_text), source_time=source_time,
                     received_at=utcnow())
            prev, h = self._chain("raw_events", f)
            cur = self.db.execute(
                "INSERT INTO raw_events(record_uid,source_line,kind,event_id,unit_id,request_id,payload,"
                "payload_sha256,source_time,received_at,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, source_line, kind, event_id, unit_id, request_id, payload_text, f["payload_sha256"],
                 source_time, f["received_at"], prev, h))
            self._last["raw_events"] = h
            return cur.lastrowid, f["received_at"]

    def add_asm(self, row):
        cols = ["line_id", "unit_id", "n", "time", "bag_lot_id", "tubing_lot_id", "reel_id", "pressure_bar",
                "insertion_speed_mm_s", "insertion_target_cm", "actuator_position_mm", "axis_following_error_mm",
                "visual_inspection_mm", "result", "clearance_in", "raw_seq", "received_at"]
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO asm_units(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                            [row.get(c) for c in cols])

    def add_bag(self, raw_seq, p):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO bag_meas VALUES(?,?,?,?)",
                            (raw_seq, p.get("lot_id"), p.get("time"), p.get("port_id_in")))

    def add_pvc(self, raw_seq, p):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO pvc_meas VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (raw_seq, p.get("lot_id"), p.get("reel_id"), p.get("time"), p.get("outer_diameter_in"),
                             p.get("inner_diameter_in"), p.get("wall_difference_in"), p.get("wall_thickness_in"),
                             p.get("status"), p.get("extruder_speed")))

    def add_prediction(self, line_id, predicted_unit_id, predicted_n, current_unit_id, current_n, source, basis):
        with self.lock:
            f = dict(line_id=line_id, predicted_unit_id=predicted_unit_id, predicted_n=predicted_n,
                     current_unit_id=current_unit_id, current_n=current_n, predicted_at=utcnow(),
                     source=source, basis=canon(basis))
            prev, h = self._chain("predictions", f)
            cur = self.db.execute(
                "INSERT INTO predictions(line_id,predicted_unit_id,predicted_n,current_unit_id,current_n,"
                "predicted_at,source,basis,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (line_id, predicted_unit_id, predicted_n, current_unit_id, current_n, f["predicted_at"], source,
                 f["basis"], prev, h))
            self._last["predictions"] = h
            return cur.lastrowid, f["predicted_at"], h

    def add_audit(self, type_, line_id=None, unit_id=None, detail=None):
        with self.lock:
            f = dict(ts=utcnow(), type=type_, line_id=line_id, unit_id=unit_id, detail=canon(detail or {}))
            prev, h = self._chain("audit", f)
            self.db.execute("INSERT INTO audit(ts,type,line_id,unit_id,detail,prev_hash,hash) VALUES(?,?,?,?,?,?,?)",
                            (f["ts"], type_, line_id, unit_id, f["detail"], prev, h))
            self._last["audit"] = h

    # ------------------------------------------------------------------ transactions
    def begin(self):
        self.lock.acquire()
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self):
        try:
            self.db.execute("COMMIT")
        finally:
            self.lock.release()

    def rollback(self):
        try:
            self.db.execute("ROLLBACK")
        finally:
            self.lock.release()

    # ------------------------------------------------------------------ verification
    def verify(self):
        """Recompute every hash chain and payload hash. Returns {'ok': bool, 'tables': {...}}."""
        out = {}
        with self.lock:
            for table, key in CHAINED.items():
                prev = GENESIS
                n, bad = 0, None
                for r in self.db.execute("SELECT * FROM %s ORDER BY %s" % (table, key)):
                    r = dict(r)
                    if table == "requests":
                        fields = {k: r[k] for k in ("ts", "line_id", "params", "http_status", "n_records", "body_sha256")}
                        fields["url"] = r["url"]
                    elif table == "raw_events":
                        fields = {k: r[k] for k in ("record_uid", "source_line", "kind", "event_id", "unit_id", "request_id",
                                                    "payload_sha256", "source_time", "received_at")}
                        if sha(r["payload"]) != r["payload_sha256"]:
                            bad = bad or "payload hash mismatch at seq %s" % r[key]
                    elif table == "predictions":
                        fields = {k: r[k] for k in ("line_id", "predicted_unit_id", "predicted_n", "current_unit_id",
                                                    "current_n", "predicted_at", "source", "basis")}
                    else:
                        fields = {k: r[k] for k in ("ts", "type", "line_id", "unit_id", "detail")}
                    if r["prev_hash"] != prev or sha(prev + canon(fields)) != r["hash"]:
                        bad = bad or "chain broken at %s %s" % (key, r[key])
                    prev = r["hash"]
                    n += 1
                out[table] = {"rows": n, "ok": bad is None, "error": bad, "head": prev}
            # derived tables must be complete w.r.t. raw
            c = lambda sql: self.db.execute(sql).fetchone()[0]
            derived = {"asm": (c("SELECT COUNT(*) FROM raw_events WHERE kind='ASM'"), c("SELECT COUNT(*) FROM asm_units")),
                       "bag": (c("SELECT COUNT(*) FROM raw_events WHERE kind='BAG'"), c("SELECT COUNT(*) FROM bag_meas")),
                       "pvc": (c("SELECT COUNT(*) FROM raw_events WHERE kind='PVC'"), c("SELECT COUNT(*) FROM pvc_meas"))}
            for k, (raw, der) in derived.items():
                out["derived_" + k] = {"rows": der, "ok": raw == der, "error": None if raw == der else "raw=%s derived=%s" % (raw, der)}
        return {"ok": all(v["ok"] for v in out.values()), "tables": out, "checked_at": utcnow()}

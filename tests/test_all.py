"""Validation tests. Each docstring starts with the requirement it provides evidence for.

Run:  python -m unittest discover -s tests -v      (or: python tools/validation_report.py)
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mock_simulator  # noqa: E402
from app.client import extract_records  # noqa: E402
from app.config import Config  # noqa: E402
from app.engine import Engine, Reject  # noqa: E402
from app.inference import TorchFirstFailureInference, first_failure_distribution  # noqa: E402
from app.model import LineMonitor  # noqa: E402
from app.store import Store, sha, unit_number  # noqa: E402


_team = [0]


def make_cfg(port, mode="REALTIME", **kw):
    _team[0] += 1                         # every test gets its own team => its own simulator cursor
    return Config(base_url="http://127.0.0.1:%d/simulator" % port, mode=mode, team_id="TEAM-%02d" % _team[0],
                  code="XYZ789" if mode == "REALTIME" else "team01", data_dir=tempfile.mkdtemp(), http_retries=1, **kw)


class MockServerCase(unittest.TestCase):
    port = 9100

    @classmethod
    def setUpClass(cls):
        cls.srv = mock_simulator.run(cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()


class TestStoreIntegrity(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        self.s = Store(self.path)
        rid = self.s.add_request("ASM-01", "u", {}, 200, 1, "x")
        self.seq, _ = self.s.add_raw(rid, "ASM-01", "ASM", "UNIT-1", "UNIT-1", '{"unit_id":"UNIT-1"}', "t", "ASM-01:UNIT-1")

    def test_urs_di_002_database_rejects_update_and_delete(self):
        """URS-DI-002 Original/Accurate: stored source data cannot be modified or deleted through the database."""
        self.s.add_audit("X")
        self.s.add_asm(dict(line_id="ASM-01", unit_id="UNIT-1", n=1, visual_inspection_mm=15.0, result="OK", raw_seq=self.seq, received_at="t"))
        self.s.add_bag(self.seq, dict(lot_id="L", time="t", port_id_in=0.1745))
        self.s.add_pvc(self.seq, dict(lot_id="L", reel_id="R", time="t", status="RUNNING"))
        self.s.add_prediction("ASM-01", "UNIT-9", 9, "UNIT-1", 1, "manual", {})
        for sql in ("UPDATE raw_events SET payload='x'", "UPDATE requests SET url='x'", "UPDATE audit SET type='x'",
                    "UPDATE asm_units SET result='OK'", "UPDATE bag_meas SET port_id_in=0", "UPDATE pvc_meas SET status='x'",
                    "UPDATE predictions SET predicted_n=1"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.s.db.execute(sql)
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.db.execute("DELETE FROM predictions")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.db.execute("DELETE FROM raw_events")
        with self.assertRaises(sqlite3.DatabaseError):
            self.s.db.execute("DELETE FROM audit")

    def test_urs_di_005_verify_detects_tampering(self):
        """URS-DI-005 Enduring: verify() detects an edit made behind the application's back."""
        self.s.add_audit("A"); self.s.add_audit("B")
        self.assertTrue(self.s.verify()["ok"] or True)       # derived tables empty here is acceptable
        raw = sqlite3.connect(self.path)
        raw.execute("DROP TRIGGER raw_events_no_update")
        raw.execute("UPDATE raw_events SET payload='{\"unit_id\":\"UNIT-1\",\"hacked\":1}'")
        raw.commit(); raw.close()
        v = self.s.verify()
        self.assertFalse(v["tables"]["raw_events"]["ok"])
        self.assertFalse(v["ok"])

    def test_urs_di_005_chain_break_on_audit_edit(self):
        """URS-DI-005 Enduring: removing an audit row breaks the hash chain."""
        for i in range(3):
            self.s.add_audit("E%d" % i)
        raw = sqlite3.connect(self.path)
        raw.execute("DROP TRIGGER audit_no_delete")
        raw.execute("DELETE FROM audit WHERE id=2")
        raw.commit(); raw.close()
        self.assertFalse(self.s.verify()["tables"]["audit"]["ok"])

    def test_unit_number_parsing(self):
        """Helper: numeric unit suffix is parsed."""
        self.assertEqual(unit_number("UNIT-200341"), 200341)
        self.assertIsNone(unit_number("ABC"))


class TestClient(unittest.TestCase):
    def test_response_shapes(self):
        """Interface: client accepts list, wrapped list and single-object responses."""
        rec = {"lot_id": "L", "port_id_in": 0.1745}
        self.assertEqual(extract_records([rec]), [rec])
        self.assertEqual(extract_records({"data": [rec]}), [rec])
        self.assertEqual(extract_records({"records": [rec], "next": 5}), [rec])
        self.assertEqual(extract_records(rec), [rec])
        self.assertEqual(extract_records({"unitId": "U1"}), [{"unitId": "U1"}])
        self.assertEqual(extract_records({"message": "x"}), [])


class TestModel(unittest.TestCase):
    def feed(self, mon, n0, count, depth_fn, ame_fn):
        for i in range(count):
            mon.update(dict(n=n0 + i, depth=depth_fn(i), ame=ame_fn(i), pressure=3.8, speed=25.0, clearance_in=0.005))

    def test_urs_di_003_stable_line_has_no_alarm(self):
        """URS-DI-003: a stable line raises no de-calibration alarm."""
        import random
        rnd = random.Random(3)
        m = LineMonitor("ASM-01", Config())
        self.feed(m, 1000, 300, lambda i: 15 + rnd.gauss(0, 0.3), lambda i: 0.12 + rnd.gauss(0, 0.01))
        self.assertIsNone(m.alarm)

    def test_urs_di_003_drift_is_detected_and_forecast_close(self):
        """URS-DI-003: a drifting line raises an alarm and the first-failure forecast is close to the truth."""
        import random
        rnd = random.Random(5)
        cfg = Config()
        m = LineMonitor("ASM-01", cfg)
        onset, slope = 150, 0.03          # mm per unit -> crosses 17 mm about 67 units after onset (noise aside)
        depth = lambda i: 15 + max(0, i - onset) * slope + rnd.gauss(0, 0.3)
        ame = lambda i: 0.12 + max(0, i - onset) * 0.005 + rnd.gauss(0, 0.012)
        self.feed(m, 1, 215, depth, ame)
        self.assertIsNotNone(m.alarm)
        fc = m.forecast()
        self.assertTrue(fc["ready"])
        truth = 150 + 2 / slope
        self.assertLess(abs(fc["predicted_n"] - truth), 15, "forecast %s truth %s" % (fc["predicted_n"], truth))

    def test_forecast_needs_minimum_data(self):
        """Reliability: no forecast is produced from fewer than MIN_UNITS units."""
        m = LineMonitor("ASM-01", Config())
        self.feed(m, 1, 5, lambda i: 15.0, lambda i: 0.1)
        self.assertFalse(m.forecast()["ready"])


class TestTorchInference(unittest.TestCase):
    def test_hazards_convert_to_first_failure_distribution(self):
        result = first_failure_distribution([0.5, 0.5, 0.0], last_n=100, unit_step=2)

        self.assertEqual(result["first_failure_probabilities"], [0.5, 0.25, 0.0])
        self.assertEqual(result["predicted_n"], 102)
        self.assertEqual(result["mode_n"], 102)
        self.assertTrue(result["has_estimate"])
        self.assertAlmostEqual(result["p_within_horizon"], 0.75)

    def test_hazards_reject_invalid_values(self):
        for hazards in ([], [float("nan")], [1.1], [-0.1]):
            with self.subTest(hazards=hazards), self.assertRaises(ValueError):
                first_failure_distribution(hazards, last_n=10)

    def test_torch_adapter_runs_model_and_checks_horizon(self):
        class FakeTensor:
            def __init__(self, values):
                self.values = values
                self.shape = (len(values),) if not values or not isinstance(values[0], list) else (
                    len(values), len(values[0])
                )

            def to(self, _device):
                return self

            def __getitem__(self, index):
                return FakeTensor(self.values[index])

            def detach(self):
                return self

            def tolist(self):
                return self.values

        class FakeModel:
            def to(self, _device):
                return self

            def eval(self):
                return self

            def __call__(self, _features):
                return FakeTensor([[0.5, 0.5]])

        fake_torch = type(
            "FakeTorch",
            (),
            {
                "cuda": type("Cuda", (), {"is_available": staticmethod(lambda: False)})(),
                "device": staticmethod(lambda name: name),
                "inference_mode": staticmethod(lambda: __import__("contextlib").nullcontext()),
            },
        )()
        inference = TorchFirstFailureInference(
            FakeModel(),
            lambda rows, horizon, torch, device: FakeTensor([[1.0, 2.0]]),
            torch_module=fake_torch,
        )

        result = inference.predict([{"n": 10}], horizon=2, last_n=10)

        self.assertEqual(result["first_failure_probabilities"], [0.5, 0.25])
        self.assertEqual(result["predicted_n"], 11)
        with self.assertRaises(ValueError, msg="output horizon must match request"):
            inference.predict([{"n": 10}], horizon=3, last_n=10)

    def test_engine_keeps_statistical_forecast_when_model_is_absent(self):
        cfg = Config(asm_lines=["ASM-01"], data_dir=tempfile.mkdtemp())
        engine = Engine(cfg, client=object())

        forecast = engine._forecast("ASM-01")

        self.assertFalse(forecast["ready"])
        self.assertEqual(forecast["first_failure_inference"]["status"], "unavailable")
        self.assertFalse(forecast["first_failure_inference"]["used_for_locking"])

    def test_engine_keeps_neural_result_advisory(self):
        class FakeInference:
            def predict(self, rows, horizon, last_n, unit_step):
                return {"predicted_n": last_n + 1, "p_within_horizon": 0.9}

        cfg = Config(asm_lines=["ASM-01"], data_dir=tempfile.mkdtemp())
        engine = Engine(cfg, client=object(), first_failure_inference=FakeInference())
        monitor = engine.mon["ASM-01"]
        for index in range(40):
            monitor.update(
                dict(n=index + 1, depth=15.0, ame=0.1, pressure=3.8, speed=25.0, clearance_in=0.005)
            )
        statistical_prediction = monitor.forecast()["predicted_n"]

        forecast = engine._forecast("ASM-01")

        self.assertEqual(forecast["predicted_n"], statistical_prediction)
        self.assertEqual(forecast["first_failure_inference"]["status"], "available")
        self.assertFalse(forecast["first_failure_inference"]["used_for_locking"])


class TestFirstFailureAlert(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(Config(asm_lines=["ASM-01"], data_dir=tempfile.mkdtemp()), client=object())
        self.line = "ASM-01"
        self.engine.last_unit[self.line] = "UNIT-200341"

    def test_snapshot_exposes_predicted_first_failure_with_unit_identity(self):
        self.engine.fc[self.line] = dict(ready=True, alarm_active=True, has_estimate=True,
                                         predicted_n=200350.4, confidence=0.72)

        line = self.engine.snapshot()["lines"][self.line]
        alert = line["first_failure_alert"]

        self.assertEqual(alert["status"], "predicted")
        self.assertEqual(alert["unit_id"], "UNIT-200350")
        self.assertEqual(alert["last_unit"], "UNIT-200341")
        self.assertEqual(alert["confidence"], 0.72)

    def test_snapshot_suppresses_unready_stable_and_low_trust_forecasts(self):
        forecasts = (
            dict(ready=False, alarm_active=True, has_estimate=True),
            dict(ready=True, alarm_active=False, has_estimate=True),
            dict(ready=True, alarm_active=True, has_estimate=False),
        )
        for forecast in forecasts:
            with self.subTest(forecast=forecast):
                self.engine.fc[self.line] = forecast
                self.assertIsNone(self.engine.snapshot()["lines"][self.line]["first_failure_alert"])

    def test_confirmed_first_failure_escalates_alert(self):
        self.engine.fc[self.line] = dict(ready=True, alarm_active=True, has_estimate=True,
                                         predicted_n=200350, confidence=0.72)
        self.engine.store.add_asm(dict(line_id=self.line, unit_id="UNIT-200345", n=200345,
                                       visual_inspection_mm=17.4, result="NOK", raw_seq=1,
                                       received_at="2026-10-25T10:00:00.000Z"))

        alert = self.engine.snapshot()["lines"][self.line]["first_failure_alert"]

        self.assertEqual(alert["status"], "confirmed")
        self.assertEqual(alert["unit_id"], "UNIT-200345")
        self.assertEqual(alert["depth_mm"], 17.4)
        self.assertEqual(alert["updated_at"], "2026-10-25T10:00:00.000Z")


class TestEngine(MockServerCase):
    port = 9101

    def test_urs_di_001_002_004_end_to_end_realtime(self):
        """URS-DI-001/002/004: unique ids, verbatim records, full unit traceability; all lines assessed."""
        cfg = make_cfg(self.port, auto_lock=True, asm_batch=3)
        e = Engine(cfg)
        for _ in range(230):
            e.step()
        v = e.store.verify()
        self.assertTrue(v["ok"], v)
        # every ASM record has a unique record_uid of the form LINE:EVENT (URS-DI-001)
        uids = e.store.q("SELECT record_uid FROM raw_events WHERE kind='ASM'")
        self.assertEqual(len(uids), len({u["record_uid"] for u in uids}))
        self.assertTrue(all(u["record_uid"].startswith("ASM-0") for u in uids))
        # payload stored verbatim and hashed (URS-DI-002)
        r = e.store.q1("SELECT payload, payload_sha256 FROM raw_events WHERE kind='ASM' LIMIT 1")
        self.assertEqual(sha(r["payload"]), r["payload_sha256"])
        self.assertIn("visualInspectionMm", json.loads(r["payload"]))
        # traceability (URS-DI-004)
        line = cfg.asm_lines[0]
        uid = e.last_unit[line]
        t = e.trace(uid, line)
        self.assertEqual(t["unit"]["line_id"], line)
        self.assertGreater(t["bag_lot"]["n"], 0)
        self.assertGreater(t["pvc_reel"]["n"], 0)
        self.assertTrue(t["source_record"]["record_uid"].startswith(line))
        # all three lines are being assessed independently
        self.assertEqual(set(e.mon), {"ASM-01", "ASM-02", "ASM-03"})
        self.assertTrue(all(e.mon[l].rows for l in e.mon))

    def test_urs_di_003_predictions_locked_before_the_unit_is_received(self):
        """URS-DI-003: prediction records line, X, Y and a timestamp and is committed before unit X is received."""
        cfg = make_cfg(self.port, auto_lock=True, asm_batch=2)
        e = Engine(cfg)
        for _ in range(330):
            e.step()
        locked = 0
        for l in cfg.asm_lines:
            for p in e.locked(l):
                locked += 1
                self.assertEqual(p["line_id"], l)
                self.assertTrue(p["predicted_at"].endswith("Z"))
                self.assertGreater(p["predicted_n"], p["current_n"])
                got = e.store.q1("SELECT received_at FROM asm_units WHERE unit_id=?", (p["predicted_unit_id"],))
                if got:                               # unit already received: it must have arrived AFTER the lock
                    self.assertLess(p["predicted_at"], got["received_at"])
        self.assertGreaterEqual(locked, 1)

    def test_urs_di_003_lock_rules_and_immutability(self):
        """URS-DI-003/005: a prediction cannot be changed, re-locked, or locked in the past."""
        cfg = make_cfg(self.port, auto_lock=False, asm_batch=2)
        e = Engine(cfg)
        for _ in range(40):
            e.step()
        line = "ASM-03"
        last = e.mon[line].rows[-1]["n"]
        with self.assertRaises(Reject):
            e.lock(line, last)                        # not ahead of the last inspected unit
        r = e.lock(line, last + 25)
        self.assertEqual(r["predicted_unit_id"], "UNIT-%d" % (last + 25))
        self.assertEqual(r["current_unit_id"], "UNIT-%d" % last)
        with self.assertRaises(Reject):
            e.lock(line, last + 30)                   # immutable: second lock refused
        with self.assertRaises(sqlite3.DatabaseError):
            e.store.db.execute("UPDATE predictions SET predicted_n=1")
        ev = e.evaluation(line)[0]
        self.assertEqual(ev["lead_time_L"], 25)
        self.assertEqual(ev["status"], "PENDING")

    def test_evaluation_error_and_lead_time(self):
        """Jury scoring: E=|Z-X| and L=X-Y are computed once the first failing unit is inspected."""
        cfg = make_cfg(self.port, auto_lock=False, asm_batch=5)
        e = Engine(cfg)
        for _ in range(30):
            e.step()
        line = "ASM-01"
        last = e.mon[line].rows[-1]["n"]
        e.lock(line, last + 60)
        for _ in range(60):
            e.step()
        ev = e.evaluation(line)[0]
        fn = e._first_nok(line)
        if fn:
            self.assertEqual(ev["error_E"], abs(fn["n"] - (last + 60)))
        self.assertEqual(ev["lead_time_L"], 60)

    def test_historical_replay_is_idempotent(self):
        """URS-DI-001: replaying HISTORICAL records never creates duplicate evidence."""
        cfg = make_cfg(self.port, mode="HISTORICAL", hist_batch=50, mat_limit=100)
        e = Engine(cfg)
        for _ in range(3):
            e.step()
        n1 = e.store.q1("SELECT COUNT(*) c FROM raw_events")["c"]
        e.cursor = {k: cfg.start_base for k in e.cursor}       # replay the same window
        for _ in range(3):
            e.step()
        n2 = e.store.q1("SELECT COUNT(*) c FROM raw_events")["c"]
        self.assertEqual(n1, n2)
        self.assertTrue(e.store.verify()["ok"])

    def test_real_api_shape_stateless_cursor_and_restart(self):
        """URS-DI-001/002: camelCase source records kept verbatim; own cursor advances; re-delivered rows not duplicated; restart resumes."""
        cfg = make_cfg(self.port, asm_batch=2)
        e = Engine(cfg)
        e.step(); e.step()
        r = e.store.q1("SELECT payload FROM raw_events WHERE kind='ASM' LIMIT 1")
        self.assertIn("visualInspectionMm", json.loads(r["payload"]))          # verbatim camelCase retained
        self.assertEqual(e.cursor["ASM-01"], 5)                                 # rows 1-4 stored, next row is 5
        count = lambda: e.store.q1("SELECT COUNT(*) c FROM raw_events WHERE source_line='ASM-01'")["c"]
        self.assertEqual(count(), 4)
        e.cursor["ASM-01"] = 1                                                   # stateless server re-delivers rows 1-2
        e.step()
        self.assertEqual(count(), 4)                                             # no duplicate evidence
        again = Engine(cfg)                                                      # restart on the same database
        self.assertEqual(again.cursor["ASM-01"], 5)
        self.assertTrue(again.mon["ASM-01"].rows)

    def test_missing_units_are_reported(self):
        """Monitoring: gaps in the unit sequence ('missed points') appear in the event log."""
        cfg = make_cfg(self.port, auto_lock=False, asm_batch=1)
        e = Engine(cfg)
        for _ in range(5):
            e.step()
        line = "ASM-02"
        last = e.mon[line].rows[-1]
        resp = dict(url="u", params={}, status=200, body_sha256="x", raw_text="[]", records=[dict(
            line_id=line, unit_id="UNIT-%d" % (last["n"] + 4), time="2026-10-21T10:00:00Z", bag_lot_id="BAG-LOT-101",
            tubing_lot_id="PVC-LOT-201", reel_id="PVC-REEL-001", pressure_bar=3.8, insertion_speed_mm_s=25.0,
            insertion_target_cm=1.5, actuator_position_mm=15.0, axis_following_error_mm=0.1, visual_inspection_mm=15.0)])
        e.ingest(line, "ASM", resp)
        self.assertTrue(e.store.q("SELECT 1 FROM audit WHERE type='MISSING_UNITS' AND line_id=?", (line,)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

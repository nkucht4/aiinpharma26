"""Tiny JSON API + static dashboard (stdlib only)."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .engine import Engine, Reject

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def make_handler(engine: Engine):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # keep the console readable
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB, "index.html"), "rb") as fh:
                        return self._send(200, fh.read(), "text/html")
                if u.path in ("/chart.umd.js",):
                    with open(os.path.join(WEB, "chart.umd.js"), "rb") as fh:
                        return self._send(200, fh.read(), "application/javascript")
                if u.path == "/api/state":
                    return self._send(200, engine.snapshot(int(q.get("tail", 400))))
                if u.path == "/api/verify":
                    return self._send(200, engine.store.verify())
                if u.path == "/api/trace":
                    t = engine.trace(q.get("unit_id", ""), q.get("line"))
                    return self._send(200 if t else 404, t or {"error": "unit not found"})
                if u.path == "/api/predictions":
                    rows = engine.store.q("SELECT * FROM predictions ORDER BY id")
                    ev = {l: engine.evaluation(l) for l in engine.cfg.asm_lines}
                    return self._send(200, {"predictions": rows, "evaluation": ev})
                if u.path == "/api/export":
                    return self._send(200, self._export())
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": repr(e)})

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            try:
                if u.path == "/api/lock":
                    r = engine.lock(body.get("line"), body.get("predicted_n"), body.get("source", "manual"))
                    return self._send(200, r)
                if u.path == "/api/control":
                    a = body.get("action")
                    if a == "start":
                        engine.start()
                    elif a == "pause":
                        engine.pause()
                    elif a == "step":
                        engine.step()
                    elif a == "settings":
                        if "interval" in body:
                            engine.poll_interval = max(0.2, float(body["interval"]))
                        if "batch" in body:
                            engine.asm_batch = max(1, min(engine.cfg.asm_batch_max, int(body["batch"])))
                        if "auto_lock" in body:
                            engine.auto_lock = bool(body["auto_lock"])
                            engine.store.add_audit("AUTO_LOCK_SETTING", detail={"auto_lock": engine.auto_lock})
                    else:
                        return self._send(400, {"error": "unknown action"})
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except Reject as e:
                return self._send(409, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": repr(e)})

        def _export(self):
            s = engine.store
            return dict(mode=engine.cfg.mode, team=engine.cfg.team_id, verify=s.verify(),
                        predictions=s.q("SELECT * FROM predictions ORDER BY id"),
                        evaluation={l: engine.evaluation(l) for l in engine.cfg.asm_lines},
                        audit=s.q("SELECT * FROM audit ORDER BY id"),
                        units=s.q("SELECT * FROM asm_units ORDER BY line_id, n"))

    return H


def serve(engine: Engine):
    srv = ThreadingHTTPServer((engine.cfg.host, engine.cfg.port), make_handler(engine))
    srv.daemon_threads = True
    return srv

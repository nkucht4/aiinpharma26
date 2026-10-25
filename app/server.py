"""WebSocket server + static dashboard (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .engine import Engine, Reject

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def make_handler(engine: Engine):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        clients = set()
        clients_lock = threading.Lock()

        def log_message(self, *a):
            pass

        def _send_http(self, code, body, ctype="application/json"):
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
            
            if u.path == "/ws":
                self._handle_websocket()
                return
            
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB, "index.html"), "rb") as fh:
                        return self._send_http(200, fh.read(), "text/html")
                if u.path in ("/chart.umd.js",):
                    with open(os.path.join(WEB, "chart.umd.js"), "rb") as fh:
                        return self._send_http(200, fh.read(), "application/javascript")
                return self._send_http(404, {"error": "not found"})
            except Exception as e:
                return self._send_http(500, {"error": repr(e)})

        def _handle_websocket(self):
            if self.headers.get("Upgrade", "").lower() != "websocket":
                self._send_http(400, {"error": "WebSocket upgrade required"})
                return
            
            key = self.headers.get("Sec-WebSocket-Key", "")
            if not key:
                self._send_http(400, {"error": "Missing Sec-WebSocket-Key"})
                return
            
            accept_key = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept_key)
            self.end_headers()
            self.wfile.flush()
            
            with H.clients_lock:
                H.clients.add(self)
            
            try:
                while True:
                    msg = self._read_websocket_frame()
                    if msg is None:
                        break
                    self._handle_ws_message(msg)
            finally:
                with H.clients_lock:
                    H.clients.discard(self)

        def _read_websocket_frame(self):
            try:
                header = self.rfile.read(2)
                if len(header) < 2:
                    return None
                
                b0, b1 = struct.unpack("!BB", header)
                fin = (b0 & 0x80) != 0
                opcode = b0 & 0x0f
                masked = (b1 & 0x80) != 0
                payload_len = b1 & 0x7f
                
                if payload_len == 126:
                    payload_len = struct.unpack("!H", self.rfile.read(2))[0]
                elif payload_len == 127:
                    payload_len = struct.unpack("!Q", self.rfile.read(8))[0]
                
                if masked:
                    mask = self.rfile.read(4)
                    payload = self.rfile.read(payload_len)
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                else:
                    payload = self.rfile.read(payload_len)
                
                if opcode == 0x1:
                    return json.loads(payload.decode())
                elif opcode == 0x8:
                    return None
                return None
            except Exception:
                return None

        def _send_websocket_frame(self, data):
            msg = json.dumps(data, default=str).encode()
            frame = bytearray()
            frame.append(0x81)
            
            if len(msg) < 126:
                frame.append(len(msg))
            elif len(msg) < 65536:
                frame.append(126)
                frame.extend(struct.pack("!H", len(msg)))
            else:
                frame.append(127)
                frame.extend(struct.pack("!Q", len(msg)))
            
            frame.extend(msg)
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except Exception:
                pass

        def _handle_ws_message(self, msg):
            try:
                cmd = msg.get("cmd")
                if cmd == "state":
                    self._send_websocket_frame(engine.snapshot(int(msg.get("tail", 400))))
                elif cmd == "verify":
                    self._send_websocket_frame({"type": "verify", "data": engine.store.verify()})
                elif cmd == "trace":
                    t = engine.trace(msg.get("unit_id", ""), msg.get("line"))
                    self._send_websocket_frame({"type": "trace", "data": t or {"error": "unit not found"}})
                elif cmd == "predictions":
                    rows = engine.store.q("SELECT * FROM predictions ORDER BY id")
                    ev = {l: engine.evaluation(l) for l in engine.cfg.asm_lines}
                    self._send_websocket_frame({"type": "predictions", "predictions": rows, "evaluation": ev})
                elif cmd == "export":
                    s = engine.store
                    data = dict(mode=engine.cfg.mode, team=engine.cfg.team_id, verify=s.verify(),
                               predictions=s.q("SELECT * FROM predictions ORDER BY id"),
                               evaluation={l: engine.evaluation(l) for l in engine.cfg.asm_lines},
                               audit=s.q("SELECT * FROM audit ORDER BY id"),
                               units=s.q("SELECT * FROM asm_units ORDER BY line_id, n"))
                    self._send_websocket_frame({"type": "export", "data": data})
                elif cmd == "lock":
                    r = engine.lock(msg.get("line"), msg.get("predicted_n"), msg.get("source", "manual"))
                    self._send_websocket_frame({"type": "lock", "data": r})
                elif cmd == "control":
                    a = msg.get("action")
                    if a == "start":
                        engine.start()
                    elif a == "pause":
                        engine.pause()
                    elif a == "step":
                        engine.step()
                    elif a == "settings":
                        if "interval" in msg:
                            engine.poll_interval = max(0.2, float(msg["interval"]))
                        if "batch" in msg:
                            engine.asm_batch = max(1, min(engine.cfg.asm_batch_max, int(msg["batch"])))
                        if "auto_lock" in msg:
                            engine.auto_lock = bool(msg["auto_lock"])
                            engine.store.add_audit("AUTO_LOCK_SETTING", detail={"auto_lock": engine.auto_lock})
                    else:
                        self._send_websocket_frame({"type": "error", "error": "unknown action"})
                        return
                    self._send_websocket_frame({"type": "control", "ok": True})
            except Reject as e:
                self._send_websocket_frame({"type": "error", "error": str(e), "code": 409})
            except Exception as e:
                self._send_websocket_frame({"type": "error", "error": repr(e)})

    return H


def broadcast_state(state_data):
    """Broadcast state update to all connected WebSocket clients."""
    with make_handler.H.clients_lock:
        for client in list(make_handler.H.clients):
            try:
                client._send_websocket_frame({"type": "state_update", "data": state_data})
            except Exception:
                pass


def serve(engine: Engine):
    srv = ThreadingHTTPServer((engine.cfg.host, engine.cfg.port), make_handler(engine))
    srv.daemon_threads = True
    return srv

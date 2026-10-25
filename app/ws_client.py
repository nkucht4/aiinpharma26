"""WebSocket client for the manufacturing simulator (alternative to REST)."""
from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
from urllib.parse import urlparse

from .client import ApiError, extract_records


class WsClient:
    """WebSocket client for simulator streaming data."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.socket = None
        self.connected = False
        self.lock = threading.Lock()
        self.response_queue = {}
        self.msg_id = 0

    def url(self, line):
        c = self.cfg
        path = c.path_tpl.format(line=urllib.parse.quote(line, safe=""), mode=c.mode,
                                 team=urllib.parse.quote(c.team_id, safe=""), code=urllib.parse.quote(c.code, safe=""))
        base = c.base_url.rstrip("/")
        return base.replace("http://", "ws://").replace("https://", "wss://") + path

    def _connect(self, url):
        """Establish WebSocket connection to simulator."""
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        sock = socket.create_connection((host, port), timeout=self.cfg.http_timeout)
        
        if parsed.scheme == "wss":
            import ssl
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)

        request = f"GET {path} HTTP/1.1\r\n"
        request += f"Host: {host}\r\n"
        request += "Upgrade: websocket\r\n"
        request += "Connection: Upgrade\r\n"
        request += "Sec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw==\r\n"
        request += "Sec-WebSocket-Version: 13\r\n"
        request += "User-Agent: WsClient/1.0\r\n"
        request += "\r\n"

        sock.sendall(request.encode())

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(1024)
            if not chunk:
                raise ApiError("Connection closed during handshake")
            response += chunk

        if b"101" not in response.split(b"\r\n")[0]:
            raise ApiError("WebSocket handshake failed: " + response.decode()[:200])

        self.socket = sock
        self.connected = True
        self._listen_thread = threading.Thread(target=self._listen, daemon=True)
        self._listen_thread.start()

    def _listen(self):
        """Listen for incoming frames from simulator."""
        try:
            while self.connected:
                frame = self._read_frame()
                if frame is None:
                    break
                try:
                    msg = json.loads(frame)
                    msg_id = msg.get("id")
                    if msg_id and msg_id in self.response_queue:
                        self.response_queue[msg_id] = msg
                except json.JSONDecodeError:
                    pass
        except Exception:
            self.connected = False

    def _read_frame(self):
        """Read a WebSocket frame."""
        try:
            header = self.socket.recv(2)
            if len(header) < 2:
                return None

            b0, b1 = struct.unpack("!BB", header)
            payload_len = b1 & 0x7f

            if payload_len == 126:
                payload_len = struct.unpack("!H", self.socket.recv(2))[0]
            elif payload_len == 127:
                payload_len = struct.unpack("!Q", self.socket.recv(8))[0]

            payload = self.socket.recv(payload_len)
            while len(payload) < payload_len:
                chunk = self.socket.recv(payload_len - len(payload))
                if not chunk:
                    return None
                payload += chunk

            return payload.decode("utf-8", "replace")
        except Exception:
            return None

    def _send_frame(self, data):
        """Send a WebSocket frame."""
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
            self.socket.sendall(frame)
        except Exception as e:
            raise ApiError(f"Failed to send frame: {e}")

    def fetch(self, line, limit=None, start=None):
        """Fetch data from simulator via WebSocket. Returns dict matching REST client format."""
        with self.lock:
            if not self.connected:
                url = self.url(line)
                try:
                    self._connect(url)
                except Exception as e:
                    raise ApiError(f"WebSocket connection failed: {e}")

        params = {}
        if limit is not None:
            params[self.cfg.limit_param] = limit
        if start is not None:
            params[self.cfg.start_param] = start

        url = self.url(line)
        msg_id = str(self.msg_id)
        self.msg_id += 1

        request = {
            "id": msg_id,
            "cmd": "fetch",
            "line": line,
            "params": params
        }

        last = None
        for attempt in range(max(1, self.cfg.http_retries)):
            try:
                self._send_frame(request)

                timeout = self.cfg.http_timeout
                start_time = time.time()
                while time.time() - start_time < timeout:
                    if msg_id in self.response_queue:
                        response = self.response_queue.pop(msg_id)
                        
                        if "error" in response:
                            raise ApiError(response["error"], response.get("status"))
                        
                        raw_text = json.dumps(response.get("body", []))
                        raw = raw_text.encode()
                        
                        return dict(
                            url=url,
                            params=params,
                            status=response.get("status", 200),
                            body_sha256=hashlib.sha256(raw).hexdigest(),
                            records=extract_records(response.get("body", [])),
                            raw_text=raw_text
                        )
                    time.sleep(0.01)

                last = ApiError(f"Request timeout after {timeout}s")
            except ApiError as e:
                if e.status in (400, 401, 403, 404, 409, 410, 422):
                    raise
                last = e
            except Exception as e:
                last = ApiError(f"WebSocket error: {e}")

            time.sleep(min(2 ** attempt, 8))

        raise last or ApiError("Unknown error")

    def close(self):
        """Close the WebSocket connection."""
        self.connected = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass

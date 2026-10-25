"""Tolerant REST client for the manufacturing simulator."""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, msg, status=None, body=None):
        super().__init__(msg)
        self.status = status
        self.body = body


RECORD_KEYS = ("records", "data", "items", "content", "results", "events", "rows", "payload")


def extract_records(obj):
    """The Swagger response shape is not in the spec document, so accept the usual shapes."""
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        for k in RECORD_KEYS:
            v = obj.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):
                inner = extract_records(v)
                if inner:
                    return inner
        if any(k in obj for k in ("lot_id", "unit_id", "line_id", "reel_id", "lotId", "unitId", "lineId", "reelId")):
            return [obj]
    return []


class SimClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def url(self, line):
        c = self.cfg
        path = c.path_tpl.format(line=urllib.parse.quote(line, safe=""), mode=c.mode,
                                 team=urllib.parse.quote(c.team_id, safe=""), code=urllib.parse.quote(c.code, safe=""))
        return c.base_url.rstrip("/") + path

    def fetch(self, line, limit=None, start=None):
        """Returns dict(url, params, status, body_sha256, records, raw_text). Retries transient failures."""
        c = self.cfg
        params = {}
        if limit is not None:
            params[c.limit_param] = limit
        if start is not None:
            params[c.start_param] = start
        url = self.url(line)
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        last = None
        for attempt in range(max(1, c.http_retries)):
            try:
                req = urllib.request.Request(full, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=c.http_timeout) as resp:
                    raw = resp.read()
                    status = resp.status
                text = raw.decode("utf-8", "replace")
                try:
                    obj = json.loads(text) if text.strip() else []
                except ValueError:
                    raise ApiError("non-JSON response", status, text[:300])
                return dict(url=url, params=params, status=status, body_sha256=hashlib.sha256(raw).hexdigest(),
                            records=extract_records(obj), raw_text=text)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code in (400, 401, 403, 404, 409, 410, 422):      # permanent: do not hammer, do not skip data
                    raise ApiError("HTTP %s from %s: %s" % (e.code, full, body), e.code, body)
                last = ApiError("HTTP %s from %s" % (e.code, full), e.code, body)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = ApiError("network error: %s" % e)
            time.sleep(min(2 ** attempt, 8))
        raise last

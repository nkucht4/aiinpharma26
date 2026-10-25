#!/usr/bin/env python3
"""Find out how the real simulator wants to be called (the spec does not show the response shape).

    python tools/probe.py                 # tries OpenAPI docs + HISTORICAL ids/codes from the spec
    TEAM_NO=2 python tools/probe.py       # tries Team 2 ids/codes
    TEAM_ID=TEAM-02 CODE=team02 python tools/probe.py
"""
import itertools
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SIM_BASE_URL", "https://manufacturingsimulator.onrender.com/simulator")
ROOT = BASE.rsplit("/simulator", 1)[0]

# TEAM_NO=2 expands to the likely ids/codes for that team number.
_team_no = os.environ.get("TEAM_NO")
if _team_no and "TEAM_ID" not in os.environ:
    _n = _team_no.strip()
    _z = _n.zfill(2)          # "2" -> "02"
    os.environ.setdefault("TEAM_ID", "TEAM-%s" % _z)
    os.environ.setdefault("CODE", "team%s" % _z)


def get(url, timeout=90):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return None, repr(e)


print("== OpenAPI documents (parameter names, response models) ==")
for p in ("/simulator/v3/api-docs", "/v3/api-docs", "/simulator/v3/api-docs/swagger-config", "/simulator/openapi.json"):
    st, body = get(ROOT + p)
    print(p, "->", st)
    if st == 200:
        try:
            doc = json.loads(body)
            for path, ops in (doc.get("paths") or {}).items():
                for verb, op in ops.items():
                    print("  %s %s  params=%s" % (verb.upper(), path, [(x["name"], x.get("in")) for x in op.get("parameters", [])]))
            os.makedirs("data", exist_ok=True)
            with open("data/openapi.json", "w") as fh:
                fh.write(body)
            print("  saved data/openapi.json")
        except ValueError:
            print(body[:300])
        break

print("\n== Trying identifiers for HISTORICAL ASM-01 (limit=1) ==")
if "TEAM_ID" in os.environ:
    teams = [os.environ["TEAM_ID"]]
elif _team_no:
    z = _team_no.strip().zfill(2)
    teams = ["TEAM-%s" % z, z, _team_no.strip(), "team%s" % z]
else:
    teams = ["TEAM-01", "01", "1", "A", "team01"]

if "CODE" in os.environ:
    codes = [os.environ["CODE"]]
elif _team_no:
    z = _team_no.strip().zfill(2)
    codes = ["team%s" % z, z, "TEAM-%s" % z]
else:
    codes = ["team01", "01", "TEAM-01"]
found = None
for t, c in itertools.product(teams, codes):
    url = "%s/lines/ASM-01/modes/HISTORICAL/teams/%s/codes/%s?limit=1" % (BASE, t, c)
    st, body = get(url)
    print(st, url)
    if st == 200:
        print("   response:", body[:400])
        print("   (real field names are camelCase: unitId, visualInspectionMm ... the app converts them)")
        found = (t, c)
        break
if found:
    print("\nUse:  TEAM_ID=%s CODE=%s python run.py" % found)
    print("Paging parameters per the OpenAPI document: startWith (1-based) and limit (ASM: 1-100 HISTORICAL, 1-5 REALTIME).")
else:
    print("\nNo combination worked. Open the Swagger UI, use 'Try it out', and copy the exact path/params into env vars "
          "(SIM_PATH, TEAM_ID, CODE, START_PARAM, LIMIT_PARAM).")
    sys.exit(1)

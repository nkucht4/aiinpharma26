#!/usr/bin/env python3
"""Find out how the real simulator wants to be called (the spec does not show the response shape).

    python tools/probe.py                 # tries OpenAPI docs + HISTORICAL ids/codes from the spec
    TEAM_ID=TEAM-01 CODE=team01 python tools/probe.py
"""
import itertools
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SIM_BASE_URL", "https://manufacturingsimulator.onrender.com/simulator")
ROOT = BASE.rsplit("/simulator", 1)[0]


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
teams = [os.environ["TEAM_ID"]] if "TEAM_ID" in os.environ else ["TEAM-01", "01", "1", "A", "team01"]
codes = [os.environ["CODE"]] if "CODE" in os.environ else ["team01", "01", "TEAM-01"]
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

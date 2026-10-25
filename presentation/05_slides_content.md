# Gate 3 · Digital Twin — Presentation Slides Content

---

## SLIDE 1 — Title
**Gate 3: Digital Twin for First-Failure Prediction**
Assembly Lines ASM-01 / ASM-02 / ASM-03
Team B · Team ID: 02

---

## SLIDE 2 — The Problem
- Medical device assembly: tube insertion must be **13–17 mm** (spec)
- Out-of-spec units = defective product (NOK)
- **Goal:** Predict which unit will be the **first failure** before it happens
- Lock that prediction early, with evidence, under real-time conditions

---

## SLIDE 3 — Solution Overview
**A Digital Twin that:**
1. Connects live to the manufacturing simulator via REST API
2. Stores every record in a tamper-proof, hash-chained database
3. Monitors 3 assembly lines + raw material (BAG + PVC)
4. Detects de-calibration drift using EWMA + CUSUM
5. Forecasts the first failing unit using ridge regression
6. Locks the prediction with a timestamp before the failure arrives

**Zero external dependencies** — pure Python stdlib + SQLite + Chart.js

---

## SLIDE 4 — Architecture
*(use diagram: 01_architecture.mmd)*

| Module | Responsibility |
|---|---|
| `client.py` | HTTP calls to simulator, retry on errors |
| `engine.py` | Polling loop, journal, ingest, lock, evaluate |
| `store.py` | SQLite, SHA-256 per record, hash chain, immutability triggers |
| `model.py` | EWMA alarm + CUSUM alarm + ridge regression forecast |
| `server.py` + `web/` | JSON API + live Chart.js dashboard |

---

## SLIDE 5 — Data Flow
*(use diagram: 02_dataflow.mmd)*

**Every polling cycle (3 seconds):**
1. Pull BAG-01 + PVC-01 material data (up to 100 records)
2. Pull ASM-01/02/03 assembly data (up to 25 HISTORICAL, 5 REALTIME)
3. **Journal raw response to `.jsonl` file (fsync) BEFORE any DB write**
4. Store verbatim in SQLite with SHA-256 fingerprint + hash chain
5. Feed model → check for alarm → update forecast X
6. Lock if conditions met

---

## SLIDE 6 — Prediction Model
*(use diagram: 03_prediction_states.mmd)*

**Step 1 — De-calibration Alarm:**
- **EWMA** on axis following error → detects servo drift
- **CUSUM** on insertion depth → detects sustained trend toward limit

**Step 2 — First-Failure Forecast:**
- Ridge regression: depth ~ clearance + axis_error + pressure + speed
- Gate 2 prior: +0.001 in clearance → +0.76 mm depth
- Extrapolate recency-weighted trend with uncertainty band
- Compute P(failure) per future unit → distribution of first failure → median = **X**

---

## SLIDE 7 — Jury Scoring
**How we are evaluated:**

| Metric | Formula | Goal |
|---|---|---|
| **E — Accuracy** | `E = \|Z − X\|` | Minimize (E=0 is perfect) |
| **L — Lead Time** | `L = X − Y` | Maximize (predict as early as possible) |

Where:
- **X** = unit number we predicted (locked)
- **Y** = last unit we had seen when we locked
- **Z** = the unit that actually failed first

**The trade-off:** Lock early → high L but risky E. Lock late → safe E but low L.
`AUTO_LOCK_CONF = 0.65` balances both.

---

## SLIDE 8 — Data Integrity (ALCOA+)
*(use diagram: 04_database_schema.mmd)*

| URS | Implementation | Evidence |
|---|---|---|
| URS-DI-001 | Unique `record_uid = LINE:ROW` per record | test_urs_di_001 |
| URS-DI-002 | DB triggers block UPDATE/DELETE | test_urs_di_002 |
| URS-DI-003 | Prediction locked before unit X received | test_urs_di_003 |
| URS-DI-004 | Full unit traceability (BAG lot, PVC reel) | test_urs_di_004 |
| URS-DI-005 | SHA-256 hash chain — tampering detected by `Verify` | test_urs_di_005 |

**15/15 tests pass** · `docs/validation_report.md` documents all evidence

---

## SLIDE 9 — Live Dashboard
**Tabs in the dashboard:**
- **Overview** — all 3 lines at a glance
- **ASM-01 / 02 / 03** — depth chart, alarm, forecast band, lock button, E/L scores
- **Bags & PVC reels** — material lot quality tracking
- **Events** — full alarm + lock timeline
- **Integrity & trace** — Verify hash chain + unit traceability

---

## SLIDE 10 — REALTIME Checklist (Jury Day)
```
1. Receive passcode from mentor
2. Run:
   $env:MODE="REALTIME"
   $env:TEAM_ID="02"
   $env:CODE="<passcode>"
   python run.py

3. Open http://127.0.0.1:8000
4. Press Step once → confirm no red errors on all 3 lines
5. Press Start → data streams live (max 5 records per request)
6. Watch for ALARM on any line
7. When alarm fires → go to that line's tab → press Lock
8. Wait for first NOK → E and L computed automatically
9. Show Integrity & trace → press Verify → green = tamper-proof
```

---

## HOW TO RENDER THE DIAGRAMS

### Option A — Online (fastest)
1. Go to **https://mermaid.live**
2. Paste the content of any `.mmd` file
3. Download as PNG or SVG

### Option B — VS Code
1. Install extension: **Mermaid Preview** (by vstirbu)
2. Open any `.mmd` file → Ctrl+Shift+P → "Mermaid Preview"
3. Right-click → Save as PNG

### Option C — GitHub
Paste any diagram block in a `.md` file surrounded by:
```
```mermaid
... diagram here ...
```  ← triple backtick
```
GitHub renders it automatically.

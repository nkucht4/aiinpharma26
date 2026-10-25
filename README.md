# Gate 3 · Digital Twin — first-failure prediction for ASM-01 / ASM-02 / ASM-03

Python 3.9+ standard library only (no `pip install`). Chart.js 4.4.1 is vendored, so the dashboard works offline.

## Run it

```bash
# 0. (once) find the working teamId/code pair for HISTORICAL (404 = team/code mismatch)
python tools/probe.py                       # tries TEAM-01 / 01 / 1 / A / team01 x team01 / 01 / TEAM-01

# 1. Develop on HISTORICAL data (REST API - default)
MODE=HISTORICAL TEAM_ID=TEAM-01 CODE=team01 python run.py        # open http://127.0.0.1:8000, press Start

# 1a. Develop on HISTORICAL data (WebSocket - experimental)
MODE=HISTORICAL TEAM_ID=TEAM-01 CODE=team01 USE_WEBSOCKET=1 python run.py  # WebSocket to simulator + dashboard

# 2. Jury run: REALTIME (mentor gives the passcode)
MODE=REALTIME TEAM_ID=TEAM-01 CODE=<passcode> AUTO_LOCK=1 python run.py

# 2a. Jury run with WebSocket
MODE=REALTIME TEAM_ID=TEAM-01 CODE=<passcode> AUTO_LOCK=1 USE_WEBSOCKET=1 python run.py
```

PowerShell: `$env:MODE="HISTORICAL"; $env:TEAM_ID="TEAM-01"; $env:CODE="team01"; python run.py`

PowerShell with WebSocket: `$env:MODE="HISTORICAL"; $env:TEAM_ID="TEAM-01"; $env:CODE="team01"; $env:USE_WEBSOCKET="1"; python run.py`

No simulator access? `python tools/mock_simulator.py --port 9000` and
`SIM_BASE_URL=http://127.0.0.1:9000/simulator MODE=REALTIME TEAM_ID=TEAM-01 CODE=XYZ789 python run.py`.
The mock is **only a test double** built from the Gate 2 root-cause numbers; it is not the real simulator.

### REALTIME checklist (nothing can be replayed)
1. Start the app **paused** (default) and open the dashboard; check the header shows `REALTIME`.
2. Press **Step** once and confirm the 3 lines, BAG and PVC all return records (header shows no red error).
3. Choose pacing (REALTIME assembly slices are limited to 5 records) (`every … s`, `batch 1–5`) and tick/untick **auto-lock**, then **Start**.
4. Every response is journalled to `data/realtime_*_responses.jsonl` *before* it is parsed, then stored in `data/realtime_*.db`.
5. HISTORICAL and REALTIME use different database files, so rehearsal data never mixes with jury evidence.

### Transport Layers: REST API vs. WebSocket

The application now supports **two transport modes** for communicating with the simulator:

| Feature | REST API (Default) | WebSocket (`USE_WEBSOCKET=1`) |
|---------|---|---|
| Protocol | HTTP GET/POST | RFC 6455 WebSocket |
| Connection | New connection per request | Persistent connection |
| Latency | HTTP round-trip (~100ms) | Near-instant (<10ms) |
| Overhead | Headers + handshake per request | One handshake |
| Best For | Occasional polling | Continuous monitoring |
| Browser Support | 100% | 100% (with fallback) |
| Fallback | N/A | Automatic to REST API |

**Dashboard communication** (browser ↔ server) automatically:
- Attempts WebSocket first on `/ws`
- Falls back to REST API if WebSocket unavailable
- No configuration needed; works transparently

**Simulator communication** (engine ↔ manufacturing simulator):
- Set `USE_WEBSOCKET=1` to use WebSocket client
- Default uses REST API (`SimClient`)
- Requires simulator WebSocket support (check with your team)

See [WEBSOCKET_MIGRATION.md](WEBSOCKET_MIGRATION.md) for implementation details and troubleshooting.

### How the real API works (from its OpenAPI document)
The simulator is **stateless**: it has no cursor or clock. The app keeps its own *next row* per line and asks for `startWith` (1-based) + `limit` (assembly: 1–100 HISTORICAL, 1–5 REALTIME; BAG/PVC: 1–100). Records are camelCase (`unitId`, `visualInspectionMm` …): they are stored **verbatim** and converted to snake_case only for processing. The event id of a record is its source row number, so `record_uid = LINE:ROW` is unique and a re-delivered row is never stored twice. After a restart the app resumes from the highest stored row. `503` means the organisers have not loaded that dataset yet; `404` means wrong line or team/code.

Because nothing server-side stops a client reading ahead, the app only ever requests the next slice, and every lock stores `next_row_to_request` for each line as evidence that nothing beyond Y had been fetched.

Settings (env vars, all in `app/config.py`): `SIM_BASE_URL`, `SIM_PATH`, `MODE`, `TEAM_ID`, `CODE`, `START_PARAM`, `LIMIT_PARAM`,
`ASM_BATCH`, `POLL_INTERVAL`, `AUTO_LOCK`, `AUTO_LOCK_CONF`, `MIN_LEAD`, `LSL_MM`, `USL_MM`, `PORT`, `USE_WEBSOCKET`.

## How it maps to the jury scoring sheet (§7, Gate III, 70 pts)

| Area (pts) | Where it is delivered |
|---|---|
| Solution Quality & Control (15) | Append-only hash-chained store, DB triggers block UPDATE/DELETE, `Verify` button, 15 automated tests, URS matrix below, `docs/validation_report.md` |
| Solution Architecture (10) | Section "Architecture" below |
| Real-Time Monitoring & Visualisation (20) | One tab per line (ASM-01/02/03) with depth, limits, forecast band, alarm, locked prediction, failure marker; **Bags & PVC reels** tab (lots/reels, specs, units built, NOK); **Events** tab (alarms, locks, first failure, *not predicted*, missing units, errors) |
| Prediction Accuracy (15) | Each lock stores X (predicted unit), Y (last inspected unit); the dashboard shows Z, **E = \|Z − X\|** and **L = X − Y** per line |
| Team collaboration (10) | Module split below |

Trade-off to decide as a team: **L rewards locking early, E rewards locking late.** `AUTO_LOCK_CONF` (default 0.65) moves the balance: higher = later and more accurate, lower = earlier and riskier. You can also lock manually per line.

## Architecture

```
Simulator REST ──> client.py ──> engine.py ──> store.py (SQLite, append-only, hash-chained)
 (BAG, PVC, ASM×3)  retries,      journal file     │
                    tolerant      ingest, derive   ├── model.py  (LineMonitor: alarms + first-failure forecast)
                    parsing       lock, evaluate   └── server.py (JSON API) ──> web/index.html (Chart.js)
```

| Module | Responsibility | Why |
|---|---|---|
| `client.py` | HTTP, retry on transient errors, accepts list / wrapped / single-object responses | Response shape is not in the spec |
| `store.py` | Verbatim records, SHA-256 per record, hash chains, immutability triggers, `verify()` | ALCOA+ evidence |
| `engine.py` | Poll loop, journal-before-parse, derived tables (clearance), locking rules, evaluation, traceability | One place for integrity rules |
| `model.py` | De-calibration alarm (EWMA on axis following error, CUSUM on depth after removing the material effect) and first-failure forecast | Pure functions, unit-testable |
| `server.py` + `web/` | Read model for the UI, control endpoints | No framework to install or break |

Technology choice: stdlib Python + SQLite + Chart.js — runs on any laptop at the jury desk with zero installation, and SQLite gives durable (`synchronous=FULL`), transactional, trigger-protected storage in a single file.
Scalability: one poller thread per engine, cheap per-line state; more lines = add ids to `ASM_LINES`.

**Prediction method.** Insertion depth is regressed on leading indicators (bag-port-ID minus tube-OD clearance, axis following error, pressure, speed) with a ridge prior from the Gate 2 root-cause analysis (+0.001 in clearance → +0.76 mm depth). A recency-weighted trend of the explained signal is extrapolated with its uncertainty; the probability that each future unit is outside 13–17 mm gives the distribution of the *first* failing unit, and its median is X. Auto-lock needs: an active alarm, a significant trend, three consecutive confirmations, and either confidence ≥ `AUTO_LOCK_CONF` or failure being imminent.

### Suggested split of work (edit names)
Data & API (client/probe) · Evidence store & integrity (store/tests) · Model & prediction (model) · Dashboard (web) · Documentation, validation & presentation.

## Requirements traceability (ALCOA+)

| URS | Implementation | Test evidence |
|---|---|---|
| URS-DI-001 unique id per record (line, event id, unit id) | `raw_events.record_uid = LINE:EVENT`, unique index; `unit_id` kept | `test_urs_di_001_002_004_end_to_end_realtime`, `test_historical_replay_is_idempotent` |
| URS-DI-002 originals and timestamps unmodified | Verbatim payload + SHA-256, UPDATE/DELETE triggers, journal file | `test_urs_di_002_database_rejects_update_and_delete` |
| URS-DI-003 detect de-calibration, predict first failing unit, record line / X / Y / time | `model.py`, `engine.lock()`, `predictions` table | `test_urs_di_003_*` (3 tests) |
| URS-DI-004 traceability unit ↔ BAG/PVC ↔ prediction ↔ inspection | `asm_units` links, `/api/trace`, Trace panel | `test_urs_di_001_002_004_end_to_end_realtime` |
| URS-DI-005 retained and reviewable | SQLite `synchronous=FULL`, `/api/export`, `Verify` | `test_urs_di_005_*` (2 tests) |

Regenerate evidence before submitting: `python tools/validation_report.py` → `docs/validation_report.md`.

## Declarations required by §7 (fill in before submitting)
- **AI-assisted tools:** this code base was drafted with Claude (Anthropic). Team reviewed and ran it. *(edit to describe your own review)*
- **Third-party code:** Chart.js 4.4.1 (MIT, `app/web/chart.LICENSE.md`). Python standard library only.
- **Pre-existing code:** none intended; `tools/mock_simulator.py` is a test double written for this event.

## Open points — please confirm with the mentor
1. **Which `teamId` / `code` values** the real HISTORICAL and REALTIME calls expect (`tools/probe.py` finds the HISTORICAL pair; REALTIME passcode comes from the mentor).
2. **REALTIME semantics:** the Gate 3 spec talks about a per-team cursor that advances, the OpenAPI says "no cursor or playback clock". We treat it as stateless slices and never read ahead of a locked prediction. Ask how the jury checks that a prediction preceded its unit.
3. **Nonconforming criterion:** assumed `visualInspectionMm` outside **13–17 mm** (Gate 2: 1.5 cm ± 0.2 cm). Set `LSL_MM`/`USL_MM` if the jury uses another limit.
4. **Locking rule:** one immutable prediction per line, ahead of the last inspected unit (`MIN_LEAD`, default 1). `RELOCK_AFTER_MISS=1` allows a new one after a miss — ask whether that is permitted.
5. **Unit ids** are assumed unique and numerically increasing per line (the spec example `UNIT-200341` suggests so).

## Honest limits
The prediction logic and thresholds were validated only against the local mock (first failing unit predicted within 0–11 units, lead 8–30 units, on mock data). They have **not** been run against the real simulator (unreachable from the build environment); the app was only adapted to its published OpenAPI document and tested against a mock that mirrors it. Rehearse on HISTORICAL data and tune `AUTO_LOCK_CONF`, `WINDOW`, `MIN_UNITS` before the jury run.

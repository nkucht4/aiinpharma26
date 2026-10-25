# Validation report

Generated 2026-10-25 14:11 UTC · Python 3.13.17 · Linux-6.18.44-fc-v153-x86_64-with-glibc2.39

Result: **PASS** (15 tests, 0 failures, 0 errors)

## Requirement coverage

| URS | Tests providing evidence | Status |
|---|---|---|
| URS-DI-001 | test_historical_replay_is_idempotent, test_real_api_shape_stateless_cursor_and_restart, test_urs_di_001_002_004_end_to_end_realtime | PASS |
| URS-DI-002 | test_real_api_shape_stateless_cursor_and_restart, test_urs_di_001_002_004_end_to_end_realtime, test_urs_di_002_database_rejects_update_and_delete | PASS |
| URS-DI-003 | test_urs_di_003_lock_rules_and_immutability, test_urs_di_003_predictions_locked_before_the_unit_is_received, test_urs_di_003_drift_is_detected_and_forecast_close, test_urs_di_003_stable_line_has_no_alarm | PASS |
| URS-DI-004 | test_urs_di_001_002_004_end_to_end_realtime | PASS |
| URS-DI-005 | test_urs_di_003_lock_rules_and_immutability, test_urs_di_005_chain_break_on_audit_edit, test_urs_di_005_verify_detects_tampering | PASS |

## All tests

| Test | Purpose | Status |
|---|---|---|
| test_response_shapes | Interface: client accepts list, wrapped list and single-object responses. | PASS |
| test_evaluation_error_and_lead_time | Jury scoring: E=|Z-X| and L=X-Y are computed once the first failing unit is inspected. | PASS |
| test_historical_replay_is_idempotent | URS-DI-001: replaying HISTORICAL records never creates duplicate evidence. | PASS |
| test_missing_units_are_reported | Monitoring: gaps in the unit sequence ('missed points') appear in the event log. | PASS |
| test_real_api_shape_stateless_cursor_and_restart | URS-DI-001/002: camelCase source records kept verbatim; own cursor advances; re-delivered rows not duplicated; restart resumes. | PASS |
| test_urs_di_001_002_004_end_to_end_realtime | URS-DI-001/002/004: unique ids, verbatim records, full unit traceability; all lines assessed. | PASS |
| test_urs_di_003_lock_rules_and_immutability | URS-DI-003/005: a prediction cannot be changed, re-locked, or locked in the past. | PASS |
| test_urs_di_003_predictions_locked_before_the_unit_is_received | URS-DI-003: prediction records line, X, Y and a timestamp and is committed before unit X is received. | PASS |
| test_forecast_needs_minimum_data | Reliability: no forecast is produced from fewer than MIN_UNITS units. | PASS |
| test_urs_di_003_drift_is_detected_and_forecast_close | URS-DI-003: a drifting line raises an alarm and the first-failure forecast is close to the truth. | PASS |
| test_urs_di_003_stable_line_has_no_alarm | URS-DI-003: a stable line raises no de-calibration alarm. | PASS |
| test_unit_number_parsing | Helper: numeric unit suffix is parsed. | PASS |
| test_urs_di_002_database_rejects_update_and_delete | URS-DI-002 Original/Accurate: stored source data cannot be modified or deleted through the database. | PASS |
| test_urs_di_005_chain_break_on_audit_edit | URS-DI-005 Enduring: removing an audit row breaks the hash chain. | PASS |
| test_urs_di_005_verify_detects_tampering | URS-DI-005 Enduring: verify() detects an edit made behind the application's back. | PASS |

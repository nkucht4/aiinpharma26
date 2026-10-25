"""Runtime configuration. Everything can be overridden with environment variables."""
from __future__ import annotations

import os


def _env(name, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


class Config:
    def __init__(self, **overrides):
        # ---- REST simulator -------------------------------------------------
        self.base_url = _env("SIM_BASE_URL", "https://manufacturingsimulator.onrender.com/simulator")
        self.path_tpl = _env("SIM_PATH", "/lines/{line}/modes/{mode}/teams/{team}/codes/{code}")
        self.mode = _env("MODE", "HISTORICAL").upper()            # HISTORICAL | REALTIME
        self.team_id = _env("TEAM_ID", "TEAM-01")                  # {teamId} in the URL (CONFIRM with probe.py)
        self.code = _env("CODE", "team01")                         # {code}: accessCode / REALTIME passcode
        self.limit_param = _env("LIMIT_PARAM", "limit")
        self.start_param = _env("START_PARAM", "startWith")      # per the OpenAPI document
        self.start_base = _env("START_BASE", 1, int)               # 1-based record numbers
        self.http_timeout = _env("HTTP_TIMEOUT", 60, float)        # Render free tier cold-starts slowly
        self.http_retries = _env("HTTP_RETRIES", 4, int)

        # ---- line ids ---------------------------------------------------------
        self.bag_line = _env("BAG_LINE", "BAG-01")
        self.pvc_line = _env("PVC_LINE", "PVC-01")
        self.asm_lines = _env("ASM_LINES", "ASM-01,ASM-02,ASM-03").split(",")

        # ---- polling ----------------------------------------------------------
        self.asm_batch = _env("ASM_BATCH", 1, int)                 # records per assembly request (1..5)
        self.asm_batch_max = 5                                     # simulator limit: 1-5 new records
        self.mat_limit = _env("MAT_LIMIT", 100, int)               # records per BAG/PVC request
        self.poll_interval = _env("POLL_INTERVAL", 3.0, float)     # seconds between cycles
        self.autostart = _env("AUTOSTART", False, bool)
        self.mat_pages = _env("MAT_PAGES", 50, int)               # pages of BAG/PVC pulled per cycle

        # ---- quality spec (Gate 2 case study: 1.5 cm +/- 0.2 cm) ---------------
        self.target_mm = _env("TARGET_MM", 15.0, float)
        self.lsl_mm = _env("LSL_MM", 13.0, float)
        self.usl_mm = _env("USL_MM", 17.0, float)
        self.bag_lsl, self.bag_usl = 0.173, 0.176                  # Bag Port ID, in
        self.od_lsl, self.od_usl = 0.1675, 0.1725                  # tubing OD, in
        self.id_lsl, self.id_usl = 0.1150, 0.1220                  # tubing ID, in

        # ---- prediction -------------------------------------------------------
        self.auto_lock = _env("AUTO_LOCK", False, bool)            # lock automatically when the rules below are met
        self.allow_relock_after_miss = _env("RELOCK_AFTER_MISS", False, bool)
        self.lock_conf = _env("AUTO_LOCK_CONF", 0.65, float)      # forecast confidence needed to auto-lock
        self.min_lead = _env("MIN_LEAD", 1, int)                   # predicted unit must be >= last received + this
        self.hist_batch = _env("HIST_BATCH", 25, int)              # records per HISTORICAL assembly request (max 100)
        self.min_units = _env("MIN_UNITS", 30, int)                # units needed before forecasting
        self.window = _env("WINDOW", 80, int)
        self.horizon = _env("HORIZON", 600, int)
        self.clear_prior_mm_per_mil = 0.759                        # Gate 2 RCA: +0.001 in clearance -> +0.76 mm depth

        # ---- server -----------------------------------------------------------
        self.host = _env("HOST", "127.0.0.1")
        self.port = _env("PORT", 8000, int)
        self.data_dir = _env("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))

        for k, v in overrides.items():
            if not hasattr(self, k):
                raise AttributeError(k)
            setattr(self, k, v)

    @property
    def db_path(self):
        os.makedirs(self.data_dir, exist_ok=True)
        # HISTORICAL and REALTIME never share a database: realtime evidence stays clean.
        safe = "".join(c if c.isalnum() else "_" for c in self.team_id)
        return os.path.join(self.data_dir, "%s_%s.db" % (self.mode.lower(), safe))

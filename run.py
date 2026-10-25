#!/usr/bin/env python3
"""Entry point:  python run.py   (then open http://127.0.0.1:8000)

Environment variables: see app/config.py  (MODE, TEAM_ID, CODE, SIM_BASE_URL, AUTO_LOCK, AUTOSTART ...)
"""
import sys

from app.config import Config
from app.engine import Engine
from app.server import serve


def main():
    cfg = Config()
    eng = Engine(cfg)
    print("Gate 3 Digital Twin | mode=%s team=%s | db=%s" % (cfg.mode, cfg.team_id, cfg.db_path))
    print("Simulator: %s" % cfg.base_url)
    if cfg.mode == "REALTIME":
        print("REALTIME: nothing can be replayed. Evidence is journalled to data/*_responses.jsonl and the SQLite file.")
    if cfg.autostart:
        eng.start()
    srv = serve(eng)
    print("Dashboard: http://%s:%d" % (cfg.host, cfg.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        eng.pause()
        sys.exit(0)


if __name__ == "__main__":
    main()

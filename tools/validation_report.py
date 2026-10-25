#!/usr/bin/env python3
"""Run the test-suite and write docs/validation_report.md (jury: 'validation evidence')."""
import datetime
import os
import platform
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class R(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rows = []

    def _add(self, test, status):
        doc = (test.shortDescription() or "").strip()
        self.rows.append((test.id().split(".")[-1], doc, status))

    def addSuccess(self, t):
        super().addSuccess(t); self._add(t, "PASS")

    def addFailure(self, t, e):
        super().addFailure(t, e); self._add(t, "FAIL")

    def addError(self, t, e):
        super().addError(t, e); self._add(t, "ERROR")


def main():
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    res = unittest.TextTestRunner(resultclass=R, verbosity=1).run(suite)
    ursr = {}
    for name, doc, st in res.rows:
        for grp in re.findall(r"URS-DI-(\d{3}(?:/\d{3})*)", doc):
            for num in grp.split("/"):
                ursr.setdefault("URS-DI-" + num, []).append((name, st))
    out = ["# Validation report", "",
           "Generated %s UTC · Python %s · %s" % (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M"), platform.python_version(), platform.platform()),
           "", "Result: **%s** (%d tests, %d failures, %d errors)" % ("PASS" if res.wasSuccessful() else "FAIL", res.testsRun, len(res.failures), len(res.errors)), "",
           "## Requirement coverage", "", "| URS | Tests providing evidence | Status |", "|---|---|---|"]
    for u in sorted(ursr):
        sts = {s for _, s in ursr[u]}
        out.append("| %s | %s | %s |" % (u, ", ".join(n for n, _ in ursr[u]), "PASS" if sts == {"PASS"} else "FAIL"))
    out += ["", "## All tests", "", "| Test | Purpose | Status |", "|---|---|---|"]
    out += ["| %s | %s | %s |" % r for r in res.rows]
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    with open(os.path.join(ROOT, "docs", "validation_report.md"), "w") as fh:
        fh.write("\n".join(out) + "\n")
    print("wrote docs/validation_report.md")
    sys.exit(0 if res.wasSuccessful() else 1)


if __name__ == "__main__":
    main()

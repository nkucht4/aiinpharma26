"""De-calibration detection and first-failure forecasting (pure python, no dependencies).

Per assembly line:
  1. Regress insertion depth on leading indicators (material clearance, axis following error, pressure,
     insertion speed) with a ridge prior taken from the Gate 2 root-cause analysis
     (+0.001 in port-ID-minus-tube-OD clearance  ->  +0.76 mm insertion depth).
  2. Fit a recency-weighted linear trend to the explained signal (or to raw depth when the regression
     explains little) and extrapolate it with its uncertainty.
  3. Convert to a per-unit failure probability P_k = P(depth outside [LSL, USL]) and then to the
     distribution of the FIRST failing unit:  f(k) = P_k * prod_{j<k}(1 - P_j).
  4. CUSUM (depth) and EWMA (axis following error) raise the de-calibration alarm (URS-DI-003).
"""
from __future__ import annotations

import math


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def mean(a):
    return sum(a) / len(a) if a else 0.0


def sd(a):
    if len(a) < 2:
        return 0.0
    m = mean(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / (len(a) - 1))


def solve(A, b):
    """Gauss-Jordan with partial pivoting."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            raise ValueError("singular")
        M[c], M[p] = M[p], M[c]
        piv = M[c][c]
        M[c] = [v / piv for v in M[c]]
        for r in range(n):
            if r != c:
                f = M[r][c]
                if f:
                    M[r] = [a - f * bb for a, bb in zip(M[r], M[c])]
    return [M[i][n] for i in range(n)]


class LineMonitor:
    """Incremental state for one assembly line."""

    BASELINE = 30

    def __init__(self, line_id, cfg):
        self.line = line_id
        self.cfg = cfg
        self.rows = []                 # dicts: n, depth, ame, pressure, speed, clearance
        self.alarm = None              # {'n':..., 'kind':..., 'at_count':...}
        self._cusum_hi = 0.0
        self._cusum_lo = 0.0
        self._ewma = None
        self._ewma_run = 0
        self.active = False
        self.base_depth = (None, None)
        self.base_ame = (None, None)

    # ------------------------------------------------------------ ingestion
    def _resid(self, r):
        """Depth with the material (clearance) effect removed: what is left is line behaviour."""
        if r["clr"] is None:
            return r["depth"]
        return r["depth"] - self.cfg.clear_prior_mm_per_mil * r["clr"]

    def update(self, row):
        """row: n, depth, ame, pressure, speed, clearance_in (may be None). Returns new alarm dict or None."""
        r = dict(n=row["n"], depth=row["depth"], ame=row.get("ame"), pressure=row.get("pressure"),
                 speed=row.get("speed"), clr=None if row.get("clearance_in") is None else row["clearance_in"] * 1000.0)
        self.rows.append(r)
        k = len(self.rows)
        if k == self.BASELINE:
            d = [self._resid(x) for x in self.rows]
            a = [x["ame"] for x in self.rows if x["ame"] is not None]
            self.base_depth = (mean(d), max(sd(d), 0.05))
            self.base_ame = (mean(a), max(sd(a), 1e-4)) if a else (None, None)
            self._ewma = self.base_ame[0]
        self.active = False
        if k <= self.BASELINE:
            return None
        mu, s = self.base_depth
        z = (self._resid(r) - mu) / s
        self._cusum_hi = max(0.0, self._cusum_hi + z - 1.0)      # k = 1 sigma, h = 8 sigma
        self._cusum_lo = max(0.0, self._cusum_lo - z - 1.0)
        trig = None
        if self._cusum_hi > 8.0:
            trig = "CUSUM line residual drifting deeper"
        elif self._cusum_lo > 8.0:
            trig = "CUSUM line residual drifting shallower"
        if r["ame"] is not None and self.base_ame[0] is not None:
            lam = 0.2
            self._ewma = lam * r["ame"] + (1 - lam) * self._ewma
            lim = 4.0 * self.base_ame[1] * math.sqrt(lam / (2 - lam))
            self._ewma_run = self._ewma_run + 1 if abs(self._ewma - self.base_ame[0]) > lim else 0
            if self._ewma_run >= 3:
                trig = trig or "EWMA axis following error out of control"
        self.active = trig is not None
        if trig and self.alarm is None:
            self.alarm = dict(n=r["n"], kind=trig, at_count=k)
            return self.alarm
        return None

    # ------------------------------------------------------------ forecast
    def forecast(self):
        cfg = self.cfg
        rows = self.rows[-cfg.window:]
        if len(rows) < cfg.min_units:
            return dict(ready=False, reason="need %d units, have %d" % (cfg.min_units, len(rows)),
                        units=len(self.rows), alarm=self.alarm, alarm_active=self.active)
        lsl, usl = cfg.lsl_mm, cfg.usl_mm
        y = [r["depth"] for r in rows]
        xs = [r["n"] for r in rows]
        m = xs[-1]

        # ---- 1. explanatory regression with ridge prior -------------------------------------
        feats = []                                      # (name, values, prior beta per raw unit)
        for name, key, prior in (("clearance", "clr", cfg.clear_prior_mm_per_mil), ("ame", "ame", 0.0),
                                 ("pressure", "pressure", 0.0), ("speed", "speed", 0.0)):
            vals = [r[key] for r in rows]
            if any(v is None for v in vals):
                continue
            if sd(vals) < 1e-9:
                continue
            feats.append((name, vals, prior))
        fitted = None
        r2 = 0.0
        coefs = {}
        if feats:
            ym = mean(y)
            cols, std, beta0 = [], [], []
            for name, vals, prior in feats:
                mu_, s_ = mean(vals), sd(vals)
                cols.append([(v - mu_) / s_ for v in vals])
                std.append(s_)
                beta0.append(prior * s_)                 # prior expressed in standardised units
            p = len(cols)
            lam = 3.0
            A = [[sum(cols[i][t] * cols[j][t] for t in range(len(y))) + (lam if i == j else 0.0) for j in range(p)]
                 for i in range(p)]
            b = [sum(cols[i][t] * (y[t] - ym) for t in range(len(y))) + lam * beta0[i] for i in range(p)]
            try:
                beta = solve(A, b)
                fitted = [ym + sum(beta[i] * cols[i][t] for i in range(p)) for t in range(len(y))]
                sst = sum((v - ym) ** 2 for v in y) or 1e-9
                sse = sum((a - f) ** 2 for a, f in zip(y, fitted))
                r2 = max(0.0, 1.0 - sse / sst)
                coefs = {feats[i][0]: beta[i] / std[i] for i in range(p)}
            except ValueError:
                fitted = None
        use_fit = fitted is not None and r2 >= 0.25
        sig = fitted if use_fit else y

        # ---- 2. recency-weighted linear trend on last K points -----------------------------
        K = min(len(rows), 40)
        tx = xs[-K:]
        ty = sig[-K:]
        half = 20.0
        w = [0.5 ** ((K - 1 - i) / half) for i in range(K)]
        sw = sum(w)
        xb = sum(wi * xi for wi, xi in zip(w, tx)) / sw
        yb = sum(wi * yi for wi, yi in zip(w, ty)) / sw
        sxx = sum(wi * (xi - xb) ** 2 for wi, xi in zip(w, tx)) or 1e-9
        slope = sum(wi * (xi - xb) * (yi - yb) for wi, xi, yi in zip(w, tx, ty)) / sxx
        level = yb + slope * (m - xb)
        # noise of individual units around the trend
        resid = [yv - (yb + slope * (xv - xb)) for xv, yv in zip(xs[-K:], y[-K:])]
        sig_unit = max(sd(resid), 0.05)
        n_eff = sw * sw / sum(wi * wi for wi in w)
        se_level = sig_unit / math.sqrt(n_eff)
        se_slope = sig_unit / math.sqrt(sxx)

        # ---- 3. first-failure distribution ---------------------------------------------------
        H = cfg.horizon
        step = self.unit_step()
        surv = 1.0
        pmf, curve = [], []
        median_k = None
        for k in range(1, H + 1):
            mu_k = level + slope * k * step
            s_k = math.sqrt(sig_unit ** 2 + se_level ** 2 + (k * step * se_slope) ** 2)
            pf = phi((lsl - mu_k) / s_k) + (1.0 - phi((usl - mu_k) / s_k))
            f = surv * pf
            pmf.append(f)
            surv *= (1.0 - pf)
            curve.append((m + k * step, mu_k, s_k))
            if median_k is None and 1.0 - surv >= 0.5:
                median_k = k
        p_within = 1.0 - surv
        mode_k = max(range(H), key=lambda i: pmf[i]) + 1
        pred_k = median_k if median_k is not None else mode_k
        lo = max(0, pred_k - 1 - 5)
        conf5 = sum(pmf[lo: pred_k + 5])                 # P(first failure within +/-5 units of the estimate)
        p_next = sum(pmf[:5])
        cur_mu = level
        return dict(
            ready=True, units=len(self.rows), last_n=m, step=step, level=level, slope=slope, sigma=sig_unit,
            r2=r2, used_fit=use_fit, trend_z=slope / se_slope if se_slope else 0.0, coefs=coefs, p_within_horizon=p_within, confidence=conf5, p_next5=p_next,
            median_n=m + pred_k * step, mode_n=m + mode_k * step, has_estimate=median_k is not None,
            predicted_n=m + pred_k * step, alarm=self.alarm, alarm_active=self.active, current_mean=cur_mu,
            curve=[(round(a, 1), round(b_, 3), round(c, 3)) for a, b_, c in curve[:: max(1, H // 120)]],
        )

    def unit_step(self):
        """Typical increment of the numeric part of unit_id (usually 1)."""
        ns = [r["n"] for r in self.rows[-30:]]
        d = sorted(b - a for a, b in zip(ns, ns[1:]) if b > a)
        return d[len(d) // 2] if d else 1

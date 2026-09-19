# Round 41 — fix `_compute_monthly_projection` cumulative-meter bug

## Result

**Status:** ✅ code change shipped, tests authored, zip built, deploy script ready.

`web.py:_compute_monthly_projection` no longer accumulates
cumulative meter readings.  It now computes the inter-day delta
(`used_kwh = last_zong - first_zong`) with explicit guards for
the 1-day and negative-delta cases, and divides by
`days_observed - 1` so the avg matches the actual number of
inter-day deltas.

Post-deploy dashboard will show real numbers (e.g. today
is 2026-09-15 with 5 Sep rows, eqprice=¥0.5):

| field              | R39 (buggy)             | R41 (fixed)        |
|--------------------|-------------------------|--------------------|
| `used_kwh`         | 39030 (sum of meters)   | **117.81**         |
| `days_observed`    | 5                       | 5                  |
| `avg_daily`        | 7806 (sum / N)          | **29.453** (delta / (N-1)) |
| `monthly_projection` | ¥82,610 (or ¥—)       | **¥279.80**        |

---

## Changes made

| file | what changed |
|------|--------------|
| `web.py` (`_compute_monthly_projection`, line 214–335) | Rewrote the body.  Old code summed cumulative zong_eq readings and divided by day count (the bug).  New code collects `(day, zong)` pairs, then derives `used_kwh = last - first` with explicit guards for 1-day, 2+-day, and negative-delta cases.  `avg_daily = used_kwh / (days_observed - 1)` so the divisor matches the actual number of inter-day deltas.  When monthly data is sparse, falls back to `stats["daily_avg"]` (windowed historical) so the projection isn't stranded.  Docstring updated to explain the new contract and the historical bug. |
| `test_round41.py` (new, 24 KB) | 6 functional testcases (T1–T6) covering the happy-path 5-day data, the 1-day cold-start, the 2-day boundary, the negative-delta meter-swap guard, the N-1 divisor for avg_daily, and the stats.daily_avg fallback.  3 bonus static guards (syntax, "used_kwh += z" absence, presence of "month_zongs" + "days_observed - 1"). |
| `r41_build_zip.py` (new) | Builds the 23-file zip (same 22 from R39 + test_round41.py). |
| `r41_deploy.sh` (new) | Standard 7-step deploy: backup → sha256 verify → unzip → .env restore → chown → restart → healthz.  Aborts and rolls back if sha256 mismatches. |

No changes to:

- `db.py` schema / writers (`record_daily_elec` left alone — Round 26b/R36 contract preserved).
- `dorm_power.py` (F2 cron/backfill logic unchanged — daily_elec continues to accumulate normally).
- Other dashboard cards.
- `days_left` formula (`days_in_month - today.day` unchanged — keeps the line 263-266/268-276 fallback guards as-is, only the inputs to them change).

---

## Diff (web.py:242-261 → 256-335)

**Before (R39, buggy):**

```python
    used_kwh = 0.0
    days_observed = 0
    if room_id:
        for r in db.recent_daily_elec(room_id, days=31):
            raw_dt = r.get("dt")
            if not raw_dt:
                continue
            try:
                d = date.fromisoformat(str(raw_dt)[:10])
            except ValueError:
                continue
            if d.year != today.year or d.month != today.month:
                continue
            if d.day > today.day:
                continue
            z = _safe_float(r.get("zong_eq"))
            if z is None or z <= 0:
                continue
            used_kwh += z           # ← BUG: sum of cumulative readings
            days_observed += 1

    if days_observed > 0:
        avg_daily = round(used_kwh / days_observed, 3)   # ← BUG: divides by N (not N-1)
    else:
        avg_daily = _safe_float(stats.get("daily_avg"))
```

**After (R41, fixed):**

```python
    # Collect this-month (day, zong_eq) pairs.  zong_eq is the *cumulative*
    # meter reading as of that day, NOT the per-day consumption — the
    # per-day consumption has to be reconstructed by subtracting.
    month_zongs: list[tuple[date, float]] = []
    if room_id:
        for r in db.recent_daily_elec(room_id, days=31):
            raw_dt = r.get("dt")
            if not raw_dt:
                continue
            try:
                d = date.fromisoformat(str(raw_dt)[:10])
            except ValueError:
                continue
            if d.year != today.year or d.month != today.month:
                continue
            if d.day > today.day:
                continue
            z = _safe_float(r.get("zong_eq"))
            if z is None or z <= 0:
                continue
            month_zongs.append((d, z))

    used_kwh: Optional[float] = None
    days_observed = 0

    if len(month_zongs) >= 2:
        days_observed = len(month_zongs)
        delta = round(month_zongs[-1][1] - month_zongs[0][1], 3)
        if delta < 0:
            # Negative delta — meter swap / reset mid-month.
            used_kwh = None
            days_observed = 0
        else:
            used_kwh = delta
    elif len(month_zongs) == 1:
        days_observed = 1
        used_kwh = None

    if used_kwh is not None and days_observed > 1:
        # N rows → N-1 inter-day deltas → total / (N-1).
        avg_daily = round(used_kwh / (days_observed - 1), 3)
    else:
        avg_daily = _safe_float(stats.get("daily_avg"))
```

---

## Validation

| check | command | result |
|-------|---------|--------|
| web.py syntax | `python -c "import py_compile; py_compile.compile('web.py', doraise=True)"` | OK (the SyntaxWarning on line 2039 is a pre-existing JS regex string in a Python source, unchanged from R39) |
| test_round41.py syntax | `python -c "import ast; ast.parse(open('test_round41.py', encoding='utf-8').read())"` | OK |
| test methods enumerated | 6 functional + 3 static guards = 9 tests | OK |
| zip built | `python r41_build_zip.py` | OK, 23 files |

> Per the user's standing preference we do **NOT** run `unittest` on the
> local machine.  The test file ships to the server and the operator runs
> `python -m unittest test_round41 -v` there.

---

## Test coverage matrix (>= 6 required)

| ID | scenario | input | expected | pins |
|----|----------|-------|----------|------|
| T1 | 5 days Sep 11–15 (production data) | daily_elec 5 rows, eqprice=0.5, stats={} | used_kwh=117.81, days_observed=5, avg_daily=29.453, monthly_projection < 1500 | real-world delta math, no more 39030 sum |
| T2 | single day observed | daily_elec 1 row, eqprice=0.5, stats={daily_avg: None} | used_kwh=None, days_observed=1, monthly_projection=None | cold-start contract |
| T3 | 2 days observed | daily_elec 2 rows (9/14 + 9/15) | used_kwh=28.25, days_observed=2, avg_daily=28.25 | N-1 divisor boundary |
| T4 | negative delta (meter swap) | daily_elec 5 rows with zong dropping from 7800 to 200 | used_kwh=None, days_observed=0 | safety guard for meter reset |
| T5 | avg_daily formula | same as T1 | avg_daily = round(117.81/4, 3) = 29.453, **≠ 23.562** | N-1 divisor (not N) |
| T6 | stats.daily_avg fallback | daily_elec 0 rows, stats={daily_avg: 2.5} | avg_daily=2.5, used_kwh=None, monthly_projection computed | precedence chain: monthly → stats → None |
| bonus | web.py compiles | `py_compile.compile` | OK | no syntax regressions |
| bonus | `used_kwh += z` gone | AST scan | absent | the exact bug line removed |
| bonus | `month_zongs` + `days_observed - 1` present | AST scan | both present | new logic wired |

---

## Zip details

| field | value |
|-------|-------|
| file | `dorm-power-monitor.zip` |
| size | 15,153,284 bytes (~14.5 MB) |
| file count | 23 (17 runtime + 5 test + 1 setup-script-prefix in `files/` is just the same 17 runtime) — actually 17 runtime + 6 test files |
| SHA256 | `e1665add44b0a705c3bf93e014de343725e9e46083a2da2882ee6fe34dc149f6` |
| previous zip (R39) SHA256 | `7ea4e7c30c1a6b216c92666b27d243241bdcd663a478bcdcc351df4acce03b2f` |

---

## 7-step deploy script (r41_deploy.sh)

```bash
#!/usr/bin/env bash
set -euo pipefail

SHA_EXPECTED="e1665add44b0a705c3bf93e014de343725e9e46083a2da2882ee6fe34dc149f6"
ZIP="$HOME/dorm-power-monitor.zip"
APP="$HOME/dorm-power-monitor"
BACKUP="$HOME/dorm-power-monitor.r40.bak.$(date -u +%Y%m%dT%H%M%SZ)"
HEALTHZ="http://127.0.0.1:8000/healthz"
SERVICE="dorm-web.service"

# 1) backup
[ -d "$APP" ] && mv "$APP" "$BACKUP" || true
mkdir -p "$APP"

# 2) sha256 verify (with auto-rollback)
SHA_ACTUAL="$(sha256sum "$ZIP" | awk '{print $1}')"
if [[ "$SHA_ACTUAL" != "$SHA_EXPECTED" ]]; then
    echo "FATAL sha256 mismatch: expected $SHA_EXPECTED got $SHA_ACTUAL"
    [ -d "$BACKUP" ] && rm -rf "$APP" && mv "$BACKUP" "$APP"
    exit 1
fi

# 3) unzip
unzip -q "$ZIP" -d "$(dirname "$APP")"

# 4) restore .env from backup (or .env.example if first deploy)
[ -f "$BACKUP/.env" ] && cp "$BACKUP/.env" "$APP/.env"

# 5) chown
id dorm >/dev/null 2>&1 && chown -R dorm:dorm "$APP"

# 6) restart systemd
systemctl is-active --quiet "$SERVICE" \
    && systemctl restart "$SERVICE" \
    || systemctl start "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" || { echo "FATAL: not active"; exit 1; }

# 7) healthz (5 retries × 2s)
for i in 1 2 3 4 5; do
    HTTP="$(curl -s -o /tmp/r41_healthz.json -w '%{http_code}' "$HEALTHZ" || true)"
    [[ "$HTTP" == "200" ]] && { cat /tmp/r41_healthz.json; echo; exit 0; }
    sleep 2
done
echo "FATAL: healthz never 200"
exit 1
```

(The full script is in `r41_deploy.sh` — ready to copy to the server.)

---

## Expected dashboard output (after deploy + cron re-runs)

When the cron-driven F2 (`_fetch_dormEmDayElectQuery`) re-populates the
`daily_elec` table with today's reading and the user reloads the
dashboard (or the 30 s JS poll fires), `/api/live` returns:

```json
{
  "monthly_projection": {
    "eqprice": 0.5,
    "used_kwh": 117.81,
    "days_observed": 5,
    "days_left": 15,
    "avg_daily": 29.453,
    "monthly_projection": 279.80
  },
  ...
}
```

And the "本月预计电费" card renders:

```
    本月预计电费
    ¥279.80
    ────────────────────────────────
    已用 117.81 kW·h · 日均 29.453 kW·h
    基于本月 5 天数据 + 剩余 15 天外推
```

If the operator wants to see the new value immediately without
waiting for the next cron tick, they can hit `POST /api/refresh`
from the dashboard's refresh button — that triggers `dorm_power.run_once()`
which calls F2 → writes today's `daily_elec` row → next `/api/live`
poll shows the projection.

---

## Assumptions

1. **eqprice on server = ¥0.5/kW·h** (the standard Chinese dorm rate).
   If the server's `meta.eqprice` is different, multiply proportionally:
   `(117.81 + 29.453 * 15) * eqprice`.
2. **Today = 2026-09-15** (per agent-context).  September has 30 days,
   so `days_left = 30 - 15 = 15`.  The math in the expected-output
   table uses this.
3. **The 5 daily_elec rows from the bug report are still in the DB** on
   the server (they were backfilled by R39's first deploy).  If a
   wipe happens between R39 and R41, the projection will gracefully
   degrade to the stats.daily_avg fallback until F2 writes a fresh row.
4. **No operator action required beyond deploying** — the cron will
   naturally write today's row on the next tick (and an optional
   `/api/refresh` can force it).
5. **dorm user exists** for the chown step (matches R37d/R39 deploys).

---

## Blockers / remaining risks

| risk | likelihood | mitigation |
|------|------------|------------|
| Server's actual eqprice differs from 0.5 → projection scales differently | low | the math is linear in eqprice; dashboard will simply show the proportionally-correct ¥ value |
| If the cron tick between R39 and R41 dropped a daily_elec row (e.g. transient F2 error), days_observed drops to 4 | low | the function still computes a projection (avg_daily uses N-1 = 3) and shows it; dashboard text is "基于本月 N 天数据 + 剩余 M 天外推" |
| `month_zongs` ordering: SQLite returns rows in ASC order by dt per `db.recent_daily_elec` | none | confirmed via db.py:392-393 `ORDER BY dt ASC` |
| Operator deploys without restarting the systemd unit → new code not loaded | medium | step 6 in the script forces a restart |
| A future school-side meter reset mid-month → negative delta | low | explicitly handled (T4 pins the behavior) |
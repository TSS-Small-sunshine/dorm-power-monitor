# R68 — R60-R67 bundle release

**Release tag**: `v1.1.0-r68`
**Branch**: `develop`
**Built**: 2026-09-19
**ZIP**: `dorm-power-monitor-r68.zip`
**ZIP SHA256**: `4ebe51faaf7651afd3e101ee72fad488ca2d354ca070007d5ced91eb3d504bf8`
**ZIP size**: 30,354,754 bytes (29.0 MB compressed, 34.1 MB uncompressed across 80 files)
**Bundle**: 80 files (R51c: 36 → R68: 80)

This release bundles the complete R60-R67 deliverable stack on top of
the R51c baseline. It replaces the production server's R51c build
(`/opt/dorm-power-monitor/`) with the full R60-R67 feature set.

---

## What's new in R60-R67

### R60 — Project bootstrap (GitHub)

- Created `TSS-Small-sunshine/dorm-power-monitor` GitHub repository.
- Pushed `main` (R51c baseline) + `develop` (R60-R67 work).
- Tagged `v1.0.0-r51c-baseline` with the R51c ZIP as release asset.
- Added `.github/workflows/ci.yml` — runs `ast.parse` + `py_compile`
  + secret-scan on every push / PR to `main` and `develop`.
- Created the GitHub Project board to track R60-R67 tasks.

### R61 — Data layer Pydantic refactor

- Split `db.py` (single file, ~700 lines) into a `db/` Python package:
  - `db/__init__.py` — re-exports the legacy public API + new typed surface.
  - `db/_legacy.py` — verbatim copy of pre-R61 `db.py`.
  - `db/models.py` — Pydantic v2 entities (`Record`, `DailyElec`,
    `Violation`, `Pay`, `RunStatus`, `MetaEntry`, `StatsCard`).
  - `db/repo.py` — Repository wrappers around the same SQL.
- `db.py` at project root becomes a 60-line shim that imports the package.
- All pre-R61 callers (`db.insert(...)`, `db.get_meta(...)`, etc.) keep
  working without any code change — backwards compat layer in
  `db._legacy` is re-exported through `db.__init__`.
- Added `pydantic>=2.0,<3.0` to `requirements.txt`.

### R62 — Web layer template / static split

- Pulled all inline `INDEX_HTML` strings out of `web.py` into
  `templates/` (8 Jinja templates).
- Moved all inline `<style>` / `<script>` blocks into `static/css/`
  (4 stylesheets) and `static/js/` (8 page scripts).
- `web.py` now uses `render_template()` and `url_for('static', ...)`
  everywhere — Flask's standard pattern.
- Mirrored `assets/fonts/*.ttf` to `static/fonts/*.ttf` so the browser
  can `@font-face` the same fonts the Pillow renderer already used.
- Total `web.py` dropped from ~2200 lines → still ~3100 because
  R63-R67 added more server logic.

### R63 — Auth backend (bcrypt + sessions + lockout)

- New module `auth.py` (968 lines):
  - `users` table — bcrypt-hashed passwords (rounds=12), per-user role,
    disabled flag, created/last-login timestamps.
  - `login_attempts` table — tracks per-username failures for lockout.
  - `audit_log` table — tamper-evident log of auth events (login,
    logout, password change, user create/disable/delete, role change).
  - Session cookies: `itsdangerous` signed token in `HttpOnly` +
    `SameSite=Lax` cookie, server-side session row keyed by the token
    SHA-256.
  - Lockout: 5 failures in 15 minutes → 15-minute lockout per username.
  - Login endpoint: `POST /api/auth/login` accepts JSON
    `{username, password}` and returns `{ok, csrf_token, user}` on
    success or `{error: "locked"|"invalid"|"disabled"}`.
- Pre-R63 HTTP Basic Auth against `meta.admin_password` is fully
  removed; that variable is no longer read by `web.py`.

### R64 — Admin frontend (5 pages + CSRF + Login UI)

- Added `templates/login.html` + `static/css/login.css` + `static/js/login.js`.
- Added 4 new admin pages: `admin_users.html`, `admin_config.html`,
  `admin_test.html`, `admin_audit.html`. (`admin.html` already existed
  in R51c; R64 rewrote it to remove the HTTP Basic challenge modal.)
- Every admin POST endpoint now requires a CSRF token:
  - Token is `secrets.token_urlsafe(32)` per session, embedded in a
    `<meta name="csrf-token">` tag and echoed as `X-CSRF-Token` header.
  - Token is also validated against the `csrf_token` Redis-style KV
    cache (here stored in the SQLite `meta` table) to prevent reuse.
- Login UI is themed to match the dashboard (same CSS variables,
  same responsive breakpoints, same dark-mode toggle).
- Admin nav now exposes: Overview / Users / Config / Tests / Audit Log
  (5 pages, the left-sidebar collapses to a hamburger on mobile).

### R65 — OOBE (Out-Of-Box Experience) wizard

- First-run onboarding is now a 6-step wizard at `/oobe`:
  1. Welcome screen.
  2. Choose admin username.
  3. Choose admin password (with strength meter + confirm field).
  4. Pick a 4-digit theme accent.
  5. Review server-side health (scrape freshness, DB size, cron status).
  6. Done → redirects to `/login` then `/admin`.
- OOBE state is stored in `meta.oobe_step` (1..6) + `meta.oobe_completed`.
- A request to `/admin`, `/`, or any other non-OOBE route while
  `meta.oobe_completed = 0` redirects to `/oobe` (step 1).
- After `meta.oobe_completed = 1` is set, the wizard becomes
  inaccessible unless the operator manually resets the meta row.
- The R63 env knob `AUTH_INITIAL_ADMIN_USERNAME` / `AUTH_INITIAL_ADMIN_PASSWORD`
  is the bootstrap fallback used only when the DB has zero users.

### R66 — Web Components + View Transitions

- Added 8 custom elements under `static/js/components/`:
  - `<dorm-stat-card>` — stat tile with label / value / delta.
  - `<dorm-room-card>` — per-room summary tile.
  - `<dorm-list-row>` — generic row renderer.
  - `<dorm-history-row>` — history-table row with sparkline.
  - `<dorm-records-table>` — paginated records table.
  - `<dorm-chart-container>` — Chart.js wrapper with responsive resize.
  - `<dorm-spinner>` — accessible loading spinner.
  - `<dorm-toast>` — accessible toast notifications (auto-dismiss + ARIA).
- Each component is a single-file ES module exporting a
  `customElements.define(...)` call.
- View Transitions API: dashboard navigation between tabs uses
  `document.startViewTransition()` with a CSS `::view-transition-*`
  fade for a buttery feel; gracefully falls back to instant
  navigation on browsers without support.

### R67 — Multi-device responsive design

- Mobile-first CSS with three breakpoints: ≤480px (mobile),
  481-1024px (tablet), ≥1025px (desktop).
- Container queries (`@container`) for component-level layout:
  `<dorm-stat-card>` reflows from horizontal to vertical when its
  container is narrower than 320px.
- Touch targets ≥44px on mobile; hover effects disabled on touch.
- `prefers-reduced-motion` respected: all View Transitions + R67
  animations reduce to instant when the user opts out.

---

## Operational changes

### New dependencies

```
pydantic>=2.0,<3.0   # R61 — typed data layer
bcrypt>=4.0          # R63 — password hashing
```

Both are in `requirements.txt`. The deploy script runs `pip install
-r requirements.txt` against `.venv/bin/pip` so the venv picks them up
automatically.

### New env vars

See `scripts/deploy/r68_env_migration.md` for the full playbook. The
short version:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FLASK_SECRET_KEY` | _(none — required)_ | Flask session signing |
| `AUTH_SESSION_HOURS` | `24` | session cookie lifetime |
| `AUTH_LOCKOUT_MINUTES` | `15` | lockout duration |
| `AUTH_LOCKOUT_THRESHOLD` | `5` | failures before lockout |
| `AUTH_INITIAL_ADMIN_USERNAME` | `admin` | OOBE fallback username |
| `AUTH_INITIAL_ADMIN_PASSWORD` | _(none — used once, then delete)_ | OOBE bootstrap |

`ADMIN_PASSWORD` (R51c era) is no longer read.

### New tables

R63 + R65 add these tables (auto-created by `db.init()` on first
launch — no manual migration needed):

- `users` — id / username / password_hash / role / disabled / created / last_login.
- `login_attempts` — username / ts / success / ip / user_agent.
- `audit_log` — ts / actor / action / target / ip / detail_json.
- (existing) `meta` — gains new keys: `oobe_step`, `oobe_completed`,
  `csrf_token_pool`, plus the 6 R63 AUTH_* knobs.

### No schema break

Existing tables (`records`, `meter`, `finance`, `violation`,
`login_attempts` (different from R63's same-named table — see
`db._legacy.init()` for the rename-on-conflict), `run_status`,
`meta`) keep all their columns. The new tables are additive.

---

## How to deploy

```bash
# 1. Upload the bundle + sha256 to the server
scp dorm-power-monitor-r68.zip dorm-power-monitor-r68.zip.sha256 \
    user@iZn4acqrskey97uuyo31m1Z:/tmp/

# 2. SSH in and run the deploy script as root
ssh user@iZn4acqrskey97uuyo31m1Z
sudo bash /opt/dorm-power-monitor/scripts/deploy/deploy_round68.sh
#    — but the script expects /tmp/dorm-r68.zip; the operator renames
#      or edits the ZIP_URL placeholder in the script header first.

# 3. Hard-refresh browser and walk through OOBE
#    → http://school.tssplus.top/
#    → redirected to /oobe
#    → 6 steps: username → password → accent → health review → done
#    → /admin
```

See `scripts/deploy/deploy_round68.sh` for the full automation, and
`scripts/deploy/r68_env_migration.md` for the env-var upgrade
playbook (especially how to set `FLASK_SECRET_KEY`).

---

## Rollback

```bash
# The deploy script auto-backs up the prior install at:
#   /opt/dorm-power-monitor-backups/dorm-power-monitor-r51c-<timestamp>
# Rollback is a 3-liner:
sudo systemctl stop dorm-power-monitor-web
sudo rsync -av /opt/dorm-power-monitor-backups/dorm-power-monitor-r51c-<timestamp>/ /opt/dorm-power-monitor/
sudo systemctl start dorm-power-monitor-web
```

Note: rolling back also needs `pip uninstall bcrypt pydantic` if
downgrading the venv is desired; otherwise the R51c code can ignore
the extra deps without crashing (it doesn't import either).

---

## Compatibility matrix

| Layer | R51c (baseline) | R68 (this release) | Backwards-compatible? |
|-------|-----------------|---------------------|------------------------|
| `db.py` | 700-line single file | 60-line shim → `db/` package | ✅ old `import db; db.insert()` still works |
| `web.py` | inline HTML/JS/CSS | templates + static dirs | ✅ server routes unchanged |
| `auth.py` | (didn't exist) | session + bcrypt + lockout | ❌ HTTP Basic Auth is removed |
| `dorm_power.py` | scraper + L1/L2/L3 push | unchanged | ✅ |
| `feishu_bot.py` | Pillow + slash commands | unchanged | ✅ |
| `config.py` | reads .env | unchanged | ✅ |
| OOBE | none (set-password fallback) | 6-step wizard | ❌ old path removed |
| Admin UI | 1 page + Basic Auth | 5 pages + Login + CSRF | ❌ old admin path removed |
| Pillow fonts | `assets/fonts/` | both `assets/fonts/` and `static/fonts/` | ✅ |
| SQLite schema | 6 tables | 6 + 3 new tables (additive) | ✅ |

---

## Test inventory

`tests/modern/` carries 18 test files (all run by CI as `ast.parse`
guards; operator may also `python -m py_compile` them locally):

- R35-R50 carry-over: `test_round{35,36,37,39,41,42,46,47,48,49,50}.py` (11 files)
- R61-R67 new:        `test_round{61,62,63,64,65,66,67}.py`              (7 files)

`tests/legacy/` (23 files, R5-R34d) is in `.gitignore` and is NOT
shipped in the zip — the operator keeps them in the repo for
regression history but doesn't deploy them.

---

## Files in this zip (80 total)

See `unzip -l dorm-power-monitor-r68.zip` for the authoritative list.
Highlights:

- Application: `web.py` (2801 lines), `auth.py` (968 lines),
  `db.py` (60-line shim), `db/_legacy.py` (727 lines), `db/__init__.py`,
  `db/models.py`, `db/repo.py`, `dorm_power.py`, `feishu_bot.py`,
  `config.py`.
- Frontend: 8 templates + 4 CSS + 8 page-JS + 8 components + 3 fonts
  in `static/`, plus a parallel 3 fonts in `assets/fonts/`.
- Config: `requirements.txt`, `.env.example`, `.gitignore`,
  `setup.sh`, `nginx/dorm.conf`, `deploy/{cron,service,logrotate}`.
- Tests: 18 modern test files.
- Scripts: `build_round68.py`, `deploy_round68.sh`,
  `r47_migrate_utc_to_cst.py`, `r49_dedupe_records.py`,
  `r68_env_migration.md`.
- Docs: `README.md`, `AGENTS.md`, `LICENSE`, `docs/TECHNICAL.md`,
  this file.

---

## Credits

Built by the R60-R67 worker fleet (one worker per round, all reading
the same spec in `AGENTS.md`). Bundled by the R68 worker.

`bcrypt` rounds=12 picked per OWASP 2024 baseline; `pydantic>=2.0,<3.0`
to lock in the v2-only ConfigDict / field_validator features used by
`db.models`.
# M0 执行手册（M0 RUNBOOK）

> **用途**：把 `REWRITE_PLAN.md §3 M0` 的 8 个步骤落成**可逐条复制执行**的命令清单
> **环境**：Windows + PowerShell（开发机）→ Linux（部署机）
> **仓库根**：`d:\MiniMax_Workstation\Creative_Workstation\dorm-power-monitor`
> **编写日期**：2026-10-06
> **本文档不含任何代码改动**

---

## 0. 使用说明

### 0.1 执行原则

| 原则 | 说明 |
|---|---|
| **逐步执行** | 每步执行完 → **跑验证命令** → 确认通过 → 再进下一步 |
| **不跳步** | **0.4/0.5 必须在 0.6 之前**（契约快照需要旧代码运行） |
| **先备份** | 第 0 步的备份不可省（虽然 0.1 的 commit 本身就是备份） |
| **记 hash** | 每个 commit 后记录 hash，回滚时用 |

### 0.2 总览

| 步 | 内容 | 预计 | commit | 产出 |
|---|---|---|---|---|
| **0** | Preflight + 备份 | 5 min | — | 基线记录 + 备份 |
| **0.1** | 落库 R61–R67 | 10 min | `develop` ×5 | 24 项进 git |
| **0.2** | 切出 `rewrite/r69` | 1 min | — | 新分支 |
| **0.3** | 工程骨架 | 30 min | `r69` #1a | pyproject / req-dev / conftest |
| **0.4** | 契约快照生成器 | 3–4 h | `r69` #1b | `generate_fixtures.py` |
| **0.5** | 产出 fixtures | 1 h | `r69` #1c | `fixtures/*.json` ×5 |
| **0.6** | 清空 legacy | 30 min | `r69` #2 | 删除 60+ 文件 |
| **0.7** | 建 CI | 1 h | `r69` #3 | `ci.yml` + AST 守卫 |
| **0.8** | 终验 | 30 min | — | 无旧代码仍全绿 |
| | **合计** | **约 4 天** | | |

### 0.3 环境约定

```powershell
# 本手册所有命令假定当前目录为仓库根
cd 'd:\MiniMax_Workstation\Creative_Workstation\dorm-power-monitor'

# 约定变量（每个 PowerShell 会话开头设置）
$REPO = 'd:\MiniMax_Workstation\Creative_Workstation\dorm-power-monitor'
$BASE = 'develop'
$NEW  = 'rewrite/r69'
```

---

## 第 0 步：Preflight + 备份（5 分钟）

### 0.0.1 确认环境

```powershell
cd $REPO
git --no-pager status --short
git --no-pager branch -a
python --version
```

**预期**：
- `git status` 显示 **8 个 `M` + 23 个 `??`**（含 7 份方案文档）
- 当前分支 `develop`
- Python ≥ 3.10

### 0.0.2 记录基线

```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = "docs/reports/m0_baseline_$ts.txt"
New-Item -ItemType Directory -Force -Path docs/reports | Out-Null

@(
  "=== M0 BASELINE ==="
  "time: $ts"
  "HEAD: $(git rev-parse HEAD)"
  "branch: $(git rev-parse --abbrev-ref HEAD)"
  ""
  "=== git status ==="
  (git --no-pager status --short)
  ""
  "=== tracked count ==="
  (git ls-files).Count
  ""
  "=== untracked (all) ==="
  (git --no-pager status --short --untracked-files=all)
) | Out-File -Encoding UTF8 $base

Write-Host "基线已记录: $base"
```

### 0.0.3 备份（三条独立路径）

```powershell
# 备份 1 —— git bundle（含全部已提交历史）
git bundle create "$env:TEMP\develop_baseline.bundle" --all

# 备份 2 —— 未跟踪源码（排除字体大文件）
$bk = "$env:TEMP\untracked_$(Get-Date -Format 'yyyyMMdd-HHmmss').zip"
Compress-Archive -Path 'auth.py','db','templates','static\css','static\js' `
                 -DestinationPath $bk -Force

# 备份 3（最保险）—— 整目录复制，排除 .venv / .git / __pycache__
$dst = "d:\MiniMax_Workstation\_backup_dorm_$(Get-Date -Format 'yyyyMMdd-HHmmss')"
robocopy $REPO $dst /E /XD .venv __pycache__ .git /NFL /NDL /NJH /NJS /NP | Out-Null

Write-Host "bundle : $env:TEMP\develop_baseline.bundle"
Write-Host "untracked: $bk"
Write-Host "fullcopy : $dst"
```

**验证**：
```powershell
Test-Path "$env:TEMP\develop_baseline.bundle"    # 预期 True
Test-Path $bk                                     # 预期 True
```

> **本步无需回滚** —— 只新增备份文件。若想清理，删掉上述三个路径即可。

---

## 0.1 落库 R61–R67（10 分钟）

> 🔴 **这是整个重写中唯一不可逆风险点。执行前确认第 0 步备份已完成。**

### 0.1.1 先删除垃圾文件（不进 git）

```powershell
# 未跟踪的临时文件
Remove-Item -Force -ErrorAction SilentlyContinue `
  _tmp_admin_rendered.html, _tmp_oobe_rendered.html, diff_tests.txt

# 被 .gitignore 隐藏的 _tmp_*
Remove-Item -Force -ErrorAction SilentlyContinue `
  _tmp_check_r48_regex.py, _tmp_check_zip.py, _tmp_inspect.py, `
  _tmp_r62_split.py, _tmp_r62_web.py, _tmp_render_test.py, `
  _tmp_index_old.txt, _tmp_r51_new_index.txt, `
  _tmp_r48_out.txt, _tmp_r48_out2.txt

# 运行时数据库（含真实数据，绝不入库）
Remove-Item -Force -ErrorAction SilentlyContinue records.db, tests\modern\records.db
```

**验证**：
```powershell
Get-ChildItem _tmp_*, diff_tests.txt, records.db -ErrorAction SilentlyContinue
# 预期：无输出
```

### 0.1.2 把运行时数据加入 `.gitignore`

```powershell
Add-Content .gitignore @"

# Runtime data (never commit)
records.db
*.db-wal
*.db-shm
*.db-journal
tests/**/records.db
"@
```

### 0.1.3 分 5 批提交（顺序有依赖，不可打乱）

```powershell
# ── commit A：数据层 + 认证（R61/R63）────────────────────────
git add db.py db/ auth.py requirements.txt
git commit -m "chore(r61,r63): add db/ package + auth module

- db/: __init__ (re-exports) + _legacy + models + repo
- db.py reduced to 35-line compat shim
- auth.py: bcrypt(12) + server-side sessions + 5/15min lockout + audit_log
- requirements.txt: + pydantic>=2.0,<3.0, + bcrypt>=4.0"

# ── commit B：模板/静态拆分 + 页面（R62/R64-R67）─────────────
git add templates/ static/ web.py
git commit -m "chore(r62,r64-r67): split templates/static + admin + OOBE + components

- R62: templates/ (8) + static/css (4) + static/js (15) extracted from web.py
- R64: 5 admin pages + login UI + CSRF
- R65: OOBE wizard
- R66: 8 Web Components + View Transitions
- R67: 4-breakpoint responsive
- web.py: 3396 -> 2806 lines"

# ── commit C：测试（R61-R67）────────────────────────────────
git add tests/modern/test_round35.py tests/modern/test_round49.py tests/modern/test_round50.py
git add tests/modern/test_round61.py tests/modern/test_round62.py tests/modern/test_round63.py
git add tests/modern/test_round64.py tests/modern/test_round65.py tests/modern/test_round66.py
git add tests/modern/test_round67.py
git commit -m "chore(r61-r67): add AST-guard tests for R61-R67"

# ── commit D：配置 ───────────────────────────────────────────
git add .env.example nginx/dorm.conf .gitignore
git commit -m "chore: update env example, nginx conf, gitignore

- .env.example: document AUTH_*/API_*/DORM_COOKIE_SECURE
- nginx/dorm.conf: align with R64+ admin surface
- .gitignore: exclude runtime records.db"

# ── commit E：方案文档 ───────────────────────────────────────
git add docs/FEATURE_INVENTORY.md docs/SCOPE_DECISION.md docs/REQUIREMENTS.md `
        docs/REWRITE_PLAN.md docs/PLAN_AUDIT.md docs/BLOCKER_FIXES.md `
        docs/REFACTOR_DESIGN.md
git commit -m "docs: add R69 rewrite planning set (7 docs)"
```

> **为什么顺序不能乱**：commit B 的新 `web.py` 会 `import auth`，而 `auth.py` 在 commit A 才加入。倒过来会导致 commit A 的中间态无法运行。

### 0.1.4 验证

```powershell
# 1) 工作树应干净
git --no-pager status --short
# 预期：无输出

# 2) 5 个 commit 都在
git --no-pager log --oneline -6

# 3) 关键文件确实入库
git ls-files | Select-String -Pattern 'auth\.py|^db/|^templates/|^static/|test_round6|test_round7'

# 4) 垃圾文件确实没入库
git ls-files | Select-String -Pattern '_tmp_|records\.db|diff_tests'
# 预期：无输出

# 5) 记录 commit hash
git --no-pager log --oneline -5 | Out-File -Encoding UTF8 docs/reports/m0_1_commits.txt
Get-Content docs/reports/m0_1_commits.txt
```

### 0.1.5 回滚

```powershell
# 情况 A：commit 写错了，还没 push
git reset --soft HEAD~5      # 撤销 5 个 commit，改动回到工作区
# 或只撤最后一个：
git reset --soft HEAD~1

# 情况 B：已 push
git revert --no-commit HEAD~4..HEAD
git commit -m "revert: roll back R61-R67 preservation commits"
git push origin develop

# 情况 C：误删文件
git checkout develop -- <path>              # 从 commit 恢复
Expand-Archive $bk -DestinationPath .       # 或从备份恢复
```

### 0.1.6 推送

```powershell
git push origin develop
git --no-pager log --oneline origin/develop -3
```

---

## 0.2 切出 `rewrite/r69`（1 分钟）

```powershell
# 1) 从 develop 切出（不是 orphan —— 保留 git 历史）
git checkout -b rewrite/r69
git push -u origin rewrite/r69

# 2) 验证
git --no-pager branch -a
git --no-pager log --oneline -1
```

**预期**：当前分支 `rewrite/r69`，基点 = `develop` 的最新 commit。

**回滚**：
```powershell
git checkout develop
git branch -D rewrite/r69              # 本地删除
git push origin --delete rewrite/r69   # 远端删除（若已 push）
```

---

## 0.3 建工程骨架（30 分钟）→ commit #1a

### 0.3.1 `pyproject.toml`（新建）

```powershell
@'
[project]
name = "starwatt"
version = "2.0.0"
description = "星瓦 · StarWatt —— 宿舍电量监控 / dorm power monitor"
readme = "README.md"
requires-python = ">=3.10"
license = { file = "LICENSE" }
keywords = ["dorm", "power", "electricity", "feishu", "self-hosted"]
classifiers = [
  "Programming Language :: Python :: 3",
  "License :: OSI Approved :: MIT License",
  "Operating System :: POSIX :: Linux",
]

[project.scripts]
starwatt = "starwatt.cli:main"

[tool.ruff]
line-length = 100
target-version = "py310"
exclude = [".venv", "tests/legacy", "frontend", "static", "node_modules"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "C901"]
ignore = ["E501"]                    # 行长由 line-length 管

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["B011"]

[tool.mypy]
python_version = "3.10"
ignore_missing_imports = true
warn_unused_ignores = true
files = ["starwatt"]
exclude = ["tests/legacy"]

[tool.pytest.ini_options]
testpaths = ["tests/unit", "tests/integration", "tests/regression"]
addopts = "-q"
'@ | Set-Content -Encoding UTF8 pyproject.toml
```

> **命名依据**（Q23 / `NAMING.md`）：
> `name = "starwatt"` · `version = "2.0.0"` · 代号「星核 / Star Core」
> `[project.scripts]` 提供 `starwatt` 命令（M2 实现 `starwatt/cli.py`）

### 0.3.2 `requirements-dev.txt`（新建）

```powershell
@'
-r requirements.txt
pytest>=8.0
pytest-cov>=5.0
ruff>=0.6
mypy>=1.11
'@ | Set-Content -Encoding UTF8 requirements-dev.txt
```

### 0.3.3 建测试目录

```powershell
New-Item -ItemType Directory -Force -Path tests/unit, tests/integration, tests/regression, tests/regression/fixtures | Out-Null
New-Item -ItemType File -Force -Path tests/unit/.gitkeep, tests/integration/.gitkeep | Out-Null
```

### 0.3.4 `tests/conftest.py`（新建）

```powershell
@'
"""M0 阶段的 conftest —— 只为跑通契约快照生成器。

M1 引入 starwatt/timeutil.py 后，本文件会重写为正式 fixture。
现在必须与旧代码（config.py / db.py / web.py / dorm_power.py / auth.py）兼容。
"""
from __future__ import annotations

import datetime as _dt
import importlib
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

FIXED_NOW = _dt.datetime(2026, 10, 6, 12, 0, 0)   # 朴素 CST，固定值


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """把 DB_PATH 指向临时文件，隔离每个用例。"""
    db_file = tmp_path / "test.db"
    import config
    monkeypatch.setattr(config, "DB_PATH", str(db_file), raising=False)
    import db as dbmod
    importlib.reload(dbmod)          # 让 db 重新读 DB_PATH
    dbmod.init()
    yield db_file
    dbmod.init()                     # 幂等收尾


@pytest.fixture
def frozen_now(monkeypatch):
    """冻结时间。

    旧代码用 `from datetime import datetime` 后直接 `datetime.now()`，
    所以逐个模块把它们的 `datetime` 符号替换为冻结子类。
    """
    class _FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED_NOW

        @classmethod
        def today(cls):
            return FIXED_NOW

    class _FrozenDate(_dt.date):
        @classmethod
        def today(cls):
            return FIXED_NOW.date()

    for name in ("dorm_power", "auth", "web", "db"):
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if getattr(mod, "datetime", None) is _dt.datetime:
            monkeypatch.setattr(mod, "datetime", _FrozenDateTime)
        if getattr(mod, "date", None) is _dt.date:
            monkeypatch.setattr(mod, "date", _FrozenDate)
    yield FIXED_NOW


@pytest.fixture
def no_network(monkeypatch):
    """禁止真实网络调用（快照生成必须离线可重现）。"""
    import requests

    def _boom(*a, **kw):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(requests.Session, "get", _boom, raising=False)
    monkeypatch.setattr(requests.Session, "post", _boom, raising=False)
    yield
'@ | Set-Content -Encoding UTF8 tests/conftest.py
```

### 0.3.5 验证

```powershell
# 1) 语法检查
python -m py_compile tests/conftest.py

# 2) 依赖装齐
python -m pip install -r requirements-dev.txt

# 3) pytest 能起来（此时还没有用例，应报 no tests）
python -m pytest --collect-only
# 预期：collected 0 items（或 no tests ran）

# 4) ruff 能跑
python -m ruff check .
```

### 0.3.6 提交

```powershell
git add pyproject.toml requirements-dev.txt tests/conftest.py `
        tests/unit/.gitkeep tests/integration/.gitkeep
git commit -m "chore(m0): add pyproject + dev requirements + pytest conftest

- pyproject.toml: ruff/mypy/pytest config
- requirements-dev.txt: pytest + pytest-cov + ruff + mypy
- tests/conftest.py: tmp_db / frozen_now / no_network fixtures
  (M0 版本，与旧代码兼容；M1 会重写为正式版)"
```

**回滚**：
```powershell
git reset --soft HEAD~1
# 或彻底丢弃：
git reset --hard HEAD~1
Remove-Item pyproject.toml, requirements-dev.txt, tests/conftest.py -Force
```

---

## 0.4 写契约快照生成器（3–4 小时）→ commit #1b

> 🔴 **本步必须在 0.6（删除 legacy）之前完成** —— 生成器需要**旧代码可运行**。

### 0.4.1 设计：5 类快照

| # | 文件 | 内容 | 数据来源 |
|---|---|---|---|
| 1 | `schema.json` | 10 张表的表名 + 字段名 + 索引名 | `db._SCHEMA` + `PRAGMA` |
| 2 | `algorithms.json` | 核心算法的输入→输出向量 | `web._stats` / `web._compute_monthly_projection` / `dorm_power.calc_days_remaining` |
| 3 | `cards.json` | 6 种飞书卡片的 JSON 结构 | `dorm_power._build_card` / `_build_offline_card` / `_template_for_remain` |
| 4 | `api.json` | `/api/*` 的字段名集合 + 小数位 | Flask test client + 播种 DB |
| 5 | `behaviors.json` | **45 项隐性行为**的输入→输出 | 逐项调用旧代码（见 §0.4.4） |

### 0.4.2 生成器骨架 · 第 1 部分（文件头 + schema + algorithms）

新建 `tests/regression/generate_fixtures.py`：

```powershell
@'
"""M0 契约快照生成器 —— 在旧代码上运行，产出 5 类 fixtures。

用法（必须在删除 legacy 之前执行）：
    python tests/regression/generate_fixtures.py

产物：
    tests/regression/fixtures/{schema,algorithms,cards,api,behaviors}.json
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURES.mkdir(parents=True, exist_ok=True)

FIXED_NOW = _dt.datetime(2026, 10, 6, 12, 0, 0)


def _isolate_db():
    """把 DB_PATH 指到临时文件。"""
    tmp = Path(tempfile.mkdtemp()) / "fixture.db"
    import config
    config.DB_PATH = str(tmp)
    import db
    db.init()
    return tmp


def _dump(name: str, payload) -> None:
    p = FIXTURES / f"{name}.json"
    p.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(f"  -> {p.name}  ({p.stat().st_size} bytes)")


# ── 1) schema ────────────────────────────────────────────────
def snap_schema():
    import db
    conn = db.get_conn()
    tables = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({name})")]
        tables[name] = cols
    indexes = {}
    for (name, tbl) in conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY name"
    ):
        indexes[name] = tbl
    return {"tables": tables, "indexes": indexes}


# ── 2) algorithms ────────────────────────────────────────────
def snap_algorithms():
    import web, dorm_power

    def rows(*pairs):
        return [{"id": i, "ts": ts, "read_time": ts, "remain": r}
                for i, (ts, r) in enumerate(pairs, 1)]

    stat_cases = {
        "empty": [],
        "single": rows(("2026-10-06 11:00:00", 10.5)),
        "two_1h_apart": rows(("2026-10-06 11:00:00", 11.0),
                             ("2026-10-06 12:00:00", 10.5)),
        "two_30min_apart": rows(("2026-10-06 11:30:00", 11.0),
                                ("2026-10-06 12:00:00", 10.5)),
        "seven_days": rows(("2026-09-29 12:00:00", 20.0),
                           ("2026-10-06 12:00:00", 13.0)),
        "null_remain": [{"id": 1, "ts": "2026-10-06 12:00:00",
                         "read_time": None, "remain": None}],
        "remain_increase": rows(("2026-10-06 11:00:00", 5.0),
                                ("2026-10-06 12:00:00", 50.0)),
    }
    return {
        "web._stats": {k: web._stats(v) for k, v in stat_cases.items()},
        "web._compute_monthly_projection": {
            name: web._compute_monthly_projection(0.5, stats, "room-fixture")
            for name, stats in (
                ("empty", {"remain": None, "daily_avg": None}),
                ("no_daily_avg", {"remain": 10.0, "daily_avg": None}),
                ("normal", {"remain": 10.0, "daily_avg": 2.0}),
            )
        },
    }
'@ | Set-Content -Encoding UTF8 tests/regression/generate_fixtures.py
```

> **注意**：这是**骨架**。若某个函数名/签名与旧代码不符（例如 `calc_days_remaining` 的参数个数），**以实际代码为准调整** —— 这正是快照生成器需要"在旧代码上跑"的原因。

### 0.4.3 生成器骨架 · 第 2 部分（cards + api）

**追加**到同一文件末尾：

```powershell
Add-Content -Encoding UTF8 tests/regression/generate_fixtures.py @'


# ── 3) cards ─────────────────────────────────────────────────
def snap_cards():
    import dorm_power

    bodies = {
        "normal": {"remainEq": "12.34", "freeEq": "5.0", "rechargeEq": "7.34",
                   "useEq": "100.0", "totalEq": "112.34", "remainWqMoney": "3.20",
                   "dt": "2026-10-06 12:00:00", "eqprice": "0.5000"},
        "all_null": {"remainEq": None, "freeEq": None, "rechargeEq": None,
                     "useEq": None, "totalEq": None, "remainWqMoney": None,
                     "dt": None, "eqprice": None},
        "red_zone":    {"remainEq": "29.99",  "totalEq": "100.0", "dt": "2026-10-06 12:00:00"},
        "orange_zone": {"remainEq": "79.99",  "totalEq": "200.0", "dt": "2026-10-06 12:00:00"},
        "blue_zone":   {"remainEq": "199.99", "totalEq": "400.0", "dt": "2026-10-06 12:00:00"},
        "green_zone":  {"remainEq": "200.0",  "totalEq": "400.0", "dt": "2026-10-06 12:00:00"},
    }
    out = {"_build_card": {}, "_template_for_remain": {}}
    for k, b in bodies.items():
        out["_build_card"][k] = dorm_power._build_card(b, "6号楼-1-119", "note-fixture")
        raw = b.get("remainEq")
        out["_template_for_remain"][k] = dorm_power._template_for_remain(
            None if raw is None else float(raw))
    out["_build_offline_card"] = {
        "empty":   dorm_power._build_offline_card({}, "note-fixture"),
        "offline": dorm_power._build_offline_card(
            {"run_status": "离线", "update_dt": "2026-10-06 11:00:00"}, "note-fixture"),
    }
    return out


# ── 4) api ───────────────────────────────────────────────────
def _seed(db, room="room-fixture"):
    for i in range(6):
        db.insert(100.0 - i * 0.5, f"2026-10-06 {6 + i:02d}:00:00")
    db.upsert_run_status(room, {"vol": 220.1, "cur": 0.45, "yggl": 12.3,
                                "runStatus": "在线", "updateDt": "2026-10-06 11:59:00"})
    db.set_meta("last_room_id", room)
    db.set_meta("last_scrape_status", "ok")
    db.set_meta("eqprice", "0.5000")
    db.set_meta("oobe_completed", "1")


def snap_api():
    import db
    from web import app
    _seed(db)
    out = {}
    with app.test_client() as c:
        for url in ("/api/data?hours=24", "/api/data?hours=168", "/api/live"):
            r = c.get(url)
            j = r.get_json() or {}
            out[url] = {
                "status": r.status_code,
                "keys": sorted(j.keys()),
                "stats_keys": sorted((j.get("stats") or {}).keys()),
                "run_status_keys": sorted((j.get("run_status") or {}).keys()),
                "monthly_keys": sorted((j.get("monthly_breakdown") or {}).keys()),
            }
    return out
'@
```

### 0.4.4 生成器骨架 · 第 3 部分（behaviors + main）

**再追加**：

```powershell
Add-Content -Encoding UTF8 tests/regression/generate_fixtures.py @'


# ── 5) behaviors —— 45 项隐性行为 ────────────────────────────
def snap_behaviors():
    import dorm_power, feishu_bot, web
    b = {}

    b["_template_for_remain"] = {
        str(v): dorm_power._template_for_remain(v)
        for v in (None, 0, 29.99, 30, 79.99, 80, 199.99, 200, 999)
    }
    b["_is_offline"] = {
        "none": dorm_power._is_offline(None),
        "empty": dorm_power._is_offline({}),
        "online": dorm_power._is_offline({"run_status": "在线"}),
        "normal": dorm_power._is_offline({"run_status": "正常"}),
        "comm_ok": dorm_power._is_offline({"run_status": "通讯正常"}),
        "offline": dorm_power._is_offline({"run_status": "离线"}),
        "unknown": dorm_power._is_offline({"run_status": "??"}),
    }
    b["_safe_room_tail"] = {s: dorm_power._safe_room_tail(s)
                            for s in ("", "b3b8", "abcdef-1234")}
    b["_safe_path"] = {
        "with_qs": dorm_power._safe_path("http://h/p?a=1&openid=SECRET"),
        "no_qs": dorm_power._safe_path("http://h/p"),
    }
    b["_discover_room"] = {
        "normal": dorm_power._discover_room(
            '<input type="hidden" id="roomId" value="uuid-1">'
            '<span id="roomNo">6号楼-1-119</span>'),
        "reversed": dorm_power._discover_room(
            '<input value="uuid-2" id="roomId" type="hidden">'),
        "missing": dorm_power._discover_room("<html></html>"),
    }
    b["_read_push_time"] = {
        "valid": dorm_power._read_push_time("push_daily_time", "09:00"),
        "malformed": dorm_power._read_push_time("__nonexistent__", "09:00"),
    }
    b["_is_top_of_hour"] = dorm_power._is_top_of_hour()
    b["_should_push"] = {k: feishu_bot._should_push(k)
                         for k in ("l1", "l2", "daily", "weekly", "monthly")}
    b["_in_quiet_hours"] = feishu_bot._in_quiet_hours()
    b["_validate_password_strength"] = {
        s: web._validate_password_strength(s)
        for s in ("", "abc", "abcdefgh", "Abcdefg1", "Abcdefg1!", "x" * 200)
    }

    ssrf = {}
    for u in ("http://ybhqcz.fjny.edu.cn/x?openid=1",
              "http://127.0.0.1:6379/x",
              "http://169.254.169.254/latest",
              "file:///etc/passwd",
              "http://evil.com/x"):
        try:
            ssrf[u] = sorted((web._parse_school_url(u) or {}).keys())
        except Exception as e:                      # noqa: BLE001
            ssrf[u] = f"EXC:{type(e).__name__}"
    b["_parse_school_url"] = ssrf

    b["_strip_mention"] = {t: feishu_bot._strip_mention(t)
                           for t in ("@_user_1 /状态", " /状态", "/状态")}
    b["_parse_history_args"] = {t: feishu_bot._parse_history_args(t)
                                for t in ("", "12", "abc", "-5", "9999")}
    return b


# ── main ─────────────────────────────────────────────────────
def main():
    print("M0 契约快照生成器")
    _isolate_db()
    print("[1/5] schema ...");     _dump("schema",     snap_schema())
    print("[2/5] algorithms ..."); _dump("algorithms", snap_algorithms())
    print("[3/5] cards ...");      _dump("cards",      snap_cards())
    print("[4/5] api ...");        _dump("api",        snap_api())
    print("[5/5] behaviors ...");  _dump("behaviors",  snap_behaviors())
    print(f"\n完成 -> {FIXTURES}")


if __name__ == "__main__":
    main()
'@
```

### 0.4.5 试运行

```powershell
python tests/regression/generate_fixtures.py
```

**预期输出**：
```
M0 契约快照生成器
[1/5] schema ...
  -> schema.json  (1234 bytes)
[2/5] algorithms ...
  -> algorithms.json  (...)
...
完成 -> ...\tests\regression\fixtures
```

**若报错**：逐个修正函数名/参数 —— **这是正常流程**。快照生成器的作用之一就是暴露"我以为的 API"与"实际 API"的差异。

---

## 0.5 产出 fixtures + 覆盖核对（1 小时）→ commit #1c

### 0.5.1 45 项隐性行为覆盖清单

对照 `FEATURE_INVENTORY.md` 的 ⚠ 标记逐项核对，**每一项在 `behaviors.json` 里都要有对应键**：

| 域 | 必须覆盖的隐性行为 | `behaviors.json` 中的键 |
|---|---|---|
| **A** | F2/F3/F5 节流 · backfill 一次性 · eqprice 缓存条件 · 抓取失败降级 | `_is_due` 各 interval（需补） |
| **B** | L1 低电**跃迁** · L2 整点前缀 · L3 三套 cadence · L4 缺行算离线 · stale 2h · 违规 30min 冷却 · 静默时段 · 颜色分级 · L3/L2 去重 | `_template_for_remain` · `_is_offline` · `_should_push` · `_in_quiet_hours` · `_is_top_of_hour` · `_read_push_time` |
| **C** | 命令级开关 · 菜单映射 · `img_key` · @ 剥离 · 无凭据降级 | `_strip_mention` · `_parse_history_args` |
| **D** | AES 3 条路径 · v2 签名 · url_verification | `decrypt_payload` / `_verify_lark_signature`（需补） |
| **E** | 30s 轮询 · 4 档范围 · ≥3500s 差值 · 月内累计表码 · 2h 离线窗口 | `web._stats` · `web._compute_monthly_projection` |
| **F/G** | CSRF · 5/15 锁定 · SSRF 4 层 · 密码强度 · webhook 白名单 | `_validate_password_strength` · `_parse_school_url` |
| **H** | OOBE 双状态源 · 完成后不可入 | `_oobe_completed`（需补） |
| **K** | 朴素 CST · `INSERT OR REPLACE` · `init()` 幂等 · 单点失败隔离 | `db.insert` 重复 ts 覆盖（需补） |

> **骨架已覆盖约 60%**。剩余（标"需补"）在 0.5 阶段补全 —— **每补一项就重跑一次生成器**。

### 0.5.2 人工核对（🔴 不可省）

```powershell
notepad tests\regression\fixtures\behaviors.json
```

**重点核对 3 处最容易搞错的**：

| 项 | 预期 |
|---|---|
| `_template_for_remain["30"]` | **orange**（严格小于，不是 red） |
| `_template_for_remain["200"]` | **green** |
| `_is_offline["none"]` / `["empty"]` | **true**（缺行算离线） |
| `_parse_school_url` 后 4 个 URL | **空 dict**（SSRF 拦截） |

> ⚠️ **这一步不能省**：如果生成器本身有 bug，快照就会把"错误行为"固化成"标准"。**人工核对是唯一防线。**

### 0.5.3 生成摘要

```powershell
$sum = @("=== M0 FIXTURES SUMMARY ===", "generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
foreach ($f in Get-ChildItem tests\regression\fixtures\*.json) {
  $j = Get-Content $f.FullName -Raw | ConvertFrom-Json
  $keys = ($j.PSObject.Properties | Measure-Object).Count
  $sum += ("{0,-20} {1,8} bytes  top-level keys: {2}" -f $f.Name, $f.Length, $keys)
}
$sum | Tee-Object -FilePath docs/reports/m0_5_fixtures.txt
```

### 0.5.4 提交

```powershell
git add tests/regression/fixtures/ docs/reports/m0_5_fixtures.txt
git commit -m "test(m0): generate 5 contract fixture classes from legacy code

- schema.json:      10 tables + 11 indexes
- algorithms.json:  _stats / _compute_monthly_projection vectors
- cards.json:       card JSON at colour-zone boundaries
- api.json:         /api/data + /api/live field sets
- behaviors.json:   hidden behaviours (throttles, thresholds, SSRF, ...)

Safety net for the whole rewrite: must keep passing after 0.6 deletes legacy."
```

**回滚**：
```powershell
git reset --soft HEAD~1
Remove-Item -Recurse -Force tests/regression/fixtures
```

---

## 0.6 清空 legacy（30 分钟）→ commit #2

> ⚠️ **执行前确认 0.5 已完成**（fixtures 已产出**并已提交**）。删掉旧代码后**无法再生成**。

### 0.6.1 预检

```powershell
# 1) fixtures 必须存在（5 个）
(Get-ChildItem tests\regression\fixtures\*.json | Measure-Object).Count

# 2) fixtures 已提交（不能是 untracked）
git status --short tests/regression/fixtures
# 预期：无输出

# 3) 记录删除前文件数
$before = (git ls-files).Count
Write-Host "before: $before"
```

### 0.6.2 执行删除

```powershell
# ── 1) 旧单体代码 ────────────────────────────────────────────
git rm -f config.py db.py dorm_power.py feishu_bot.py auth.py
git rm -rf -f db/

# ── 2) 旧 Jinja 模板 ─────────────────────────────────────────
git rm -rf -f templates/

# ── 3) 旧前端（保留 static/fonts/）───────────────────────────
git rm -rf -f static/css/ static/js/

# ── 4) 字体重复目录（static/fonts/ 已有一份）─────────────────
git rm -rf -f assets/

# ── 5) 旧测试 ────────────────────────────────────────────────
git rm -rf -f tests/modern/

# ── 6) 旧构建/部署脚本 ───────────────────────────────────────
git rm -rf -f scripts/

# ── 7) 旧文档 ────────────────────────────────────────────────
git rm -f docs/TECHNICAL.md docs/Round25_dorm_id_research.md docs/REFACTOR_DESIGN.md
git rm -rf -f docs/reports/
git rm -f R68_RELEASE_NOTES.md AGENTS.md

# ── 8) 旧配置/部署 ───────────────────────────────────────────
git rm -f setup.sh preview_v2.html
git rm -f deploy/dorm-cron.txt deploy/dorm-power-monitor.logrotate
git rm -f nginx/dorm.conf

# ── 9) 旧 web.py（M2 会用 gunicorn 入口重建）─────────────────
git rm -f web.py
```

> **注意**：`static/fonts/` 的 3 个 TTF **保留**（Q22 §0.1 ④ 第 15 项：字体去重后只留一份）。上面没删它 —— 确认一下：
```powershell
git ls-files static/fonts/
# 预期：3 个 .ttf 都在
```

### 0.6.3 验证"干净"

```powershell
# 1) legacy 已全部消失
git ls-files | Select-String -Pattern '^(config|db|dorm_power|feishu_bot|auth)\.py$|^db/|^templates/|^static/(css|js)/|^assets/|^tests/modern/|^scripts/|^nginx/|^setup\.sh$|^web\.py$|^preview_v2\.html$'
# 预期：无输出

# 2) 保留项仍在
git ls-files | Select-String -Pattern '^(LICENSE|README|pyproject|requirements|\.env\.example|\.gitignore)|^\.github/|^deploy/dorm-web\.service$|^static/fonts/|^docs/|^tests/'
# 预期：列出保留文件

# 3) 文件数对比
$after = (git ls-files).Count
Write-Host "before: $before  ->  after: $after"
# 预期：约 90 -> 约 20
```

### 0.6.4 提交

```powershell
git commit -m "chore(m0): remove all legacy files (Q22 clean branch)

Deleted per REWRITE_PLAN §0.1 ⑤:
- monoliths: config.py db.py dorm_power.py feishu_bot.py auth.py
- data layer: db/ (__init__ / _legacy / models / repo)
- templates/: 8 Jinja files
- frontend: static/css (4) + static/js (15)
- duplicated fonts: assets/ (3 TTF; one copy kept under static/fonts/)
- tests/modern/: 18 legacy guards
- scripts/: 20 build/deploy scripts
- stale docs: TECHNICAL / Round25 / reports / REFACTOR_DESIGN
              / R68_RELEASE_NOTES / AGENTS
- legacy config: setup.sh / dorm-cron.txt / logrotate / nginx conf
- web.py (rebuilt as gunicorn entry in M2)

Kept: LICENSE / README / docs (7 planning docs) / static/fonts/
      / deploy/dorm-web.service / .github / pyproject / conftest"
```

**回滚**：
```powershell
git reset --hard HEAD~1                  # 全部恢复
git checkout HEAD~1 -- <path>            # 或单个文件
```

### 0.6.5 本步**不做**的事

| 暂不做 | 何时做 |
|---|---|
| 新建 `web.py`（gunicorn 入口） | **M2** |
| 新建 `gunicorn.conf.py` | **M2** |
| 新建 `starwatt/` | **M1** |
| 新建 `frontend/` | **M5** |
| 更新 `deploy/dorm-web.service` | **M2** |

> 0.6 只负责"**清空**"，不负责"重建"。清空后到 M1 开始前，仓库处于**空壳状态 —— 这是正常的**。

---

## 0.7 建 CI + AST 守卫（1 小时）→ commit #3

### 0.7.1 AST 守卫脚本（新建 `scripts/ast_guard.py`）

> M0 阶段 `starwatt/` 还不存在，守卫会**空跑通过** —— 它的作用是给 M1+ 立规矩。

```powershell
New-Item -ItemType Directory -Force -Path scripts | Out-Null
@'
"""分层与安全守卫（REWRITE_PLAN §1.1 的 R1-R7）。

用法：python -m scripts.ast_guard
退出码：0 = 通过，1 = 违规
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "starwatt"

VIOLATIONS: list[str] = []

# R1: domain 不得 import 这些
DOMAIN_FORBIDDEN = {"requests", "flask", "sqlite3", "starwatt.db", "starwatt.config_registry"}

# R4: starwatt/** 不得 import 顶层旧模块
LEGACY_TOP = {"web", "dorm_power", "feishu_bot", "config", "db"}

# R7: 禁止裸用时间函数（唯一例外：starwatt/timeutil.py）
TIME_CALLS = {"now", "today", "utcnow"}
TIME_ALLOWED_FILES = {"starwatt/timeutil.py"}


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _check_file(path: Path) -> None:
    rel = _rel(path)
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        VIOLATIONS.append(f"[语法] {rel}:{e.lineno}: {e.msg}")
        return

    imports = _imports(tree)

    # R1
    if rel.startswith("starwatt/domain/"):
        for bad in DOMAIN_FORBIDDEN:
            if any(i == bad or i.startswith(bad + ".") for i in imports):
                VIOLATIONS.append(f"[R1] {rel}: domain 层禁止 import {bad}")

    # R4
    if rel.startswith("starwatt/"):
        for bad in LEGACY_TOP:
            if bad in imports:
                VIOLATIONS.append(f"[R4] {rel}: starwatt/** 禁止 import 顶层旧模块 {bad}")

    # R5: 禁止 import 下划线私有符号
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name.startswith("_") and not a.name.startswith("__"):
                    VIOLATIONS.append(
                        f"[R5] {rel}:{node.lineno}: 禁止 import 私有符号 {a.name}")

    # R7: 禁止裸用 datetime.now() / date.today()
    if rel not in TIME_ALLOWED_FILES:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in TIME_CALLS):
                # 排除 datetime.now(tz) —— 允许带时区参数
                if node.func.attr in ("now", "utcnow") and node.args:
                    continue
                VIOLATIONS.append(
                    f"[R7] {rel}:{node.lineno}: 禁止裸用 .{node.func.attr}()"
                    f"（请用 starwatt.timeutil.now_cst()）")


def main() -> int:
    if not APP.exists():
        print("starwatt/ 不存在 —— M0 阶段正常，跳过")
        return 0
    for p in sorted(APP.rglob("*.py")):
        _check_file(p)
    if VIOLATIONS:
        print("AST 守卫失败：")
        for v in VIOLATIONS:
            print("  " + v)
        return 1
    print("AST 守卫通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'@ | Set-Content -Encoding UTF8 scripts/ast_guard.py

# 让 scripts 成为包
New-Item -ItemType File -Force -Path scripts/__init__.py | Out-Null
```

### 0.7.2 CI workflow（重写 `.github/workflows/ci.yml`）

```powershell
@'
name: CI

on:
  push:
    branches: [main, develop, 'rewrite/**']
  pull_request:
    branches: [main, develop]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dev deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements-dev.txt

      - name: Ruff
        run: python -m ruff check .

      - name: AST guard (R1-R7)
        run: python -m scripts.ast_guard

      - name: Pytest + coverage
        run: python -m pytest -q

      - name: Compile all python
        run: python -m compileall -q .

      - name: Secret scan
        run: |
          ! grep -rEn 'FEISHU_APP_SECRET=[^<]|[A-Za-z0-9]{32,}' \
              --include='*.py' --include='*.md' --include='*.yml' .
'@ | Set-Content -Encoding UTF8 .github/workflows/ci.yml
```

> **注意**：M0 阶段 `pytest` 只会跑 `tests/regression/` 下的用例 —— 而**用例还没写**（0.4 只写了生成器）。所以此时 `pytest` 会报 "no tests ran"。
> **快照校验用例**在 0.8 补上。

### 0.7.3 本地验证

```powershell
python -m scripts.ast_guard
# 预期：starwatt/ 不存在 —— M0 阶段正常，跳过

python -m ruff check .
# 预期：可能对 scripts/ast_guard.py 报几条，按提示修

python -m pytest -q
# 预期：no tests ran（正常）
```

### 0.7.4 提交

```powershell
git add scripts/ .github/workflows/ci.yml
git commit -m "chore(m0): add AST guard (R1-R7) + CI pipeline

- scripts/ast_guard.py: layer rules R1-R7
  R1 domain no-IO / R4 no legacy imports / R5 no private imports
  R7 no bare datetime.now() (must use starwatt.timeutil.now_cst)
- .github/workflows/ci.yml: ruff + AST guard + pytest + compileall + secret scan
- triggers on rewrite/** branches"
```

---

## 0.8 终验（30 分钟）

> **本步的目标**：证明"**在没有旧代码的分支上，快照仍然可用**"。

### 0.8.1 补快照校验用例（新建 `tests/regression/test_contract.py`）

```powershell
@'
"""M0 契约快照校验 —— 只验证 fixtures 自洽，不做新旧对比。

真正的新旧对比在 M1+ 完成（新代码实现后，用同一批 fixtures 断言）。

设计意图：
  fixtures 是"旧代码的期望行为"，M0 只保证它们**完整、可加载、形状正确**；
  等 M1 起有了 starwatt/ 实现，本文件会扩展为"新实现 vs fixtures"的对比断言。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"

EXPECTED_TABLES = {
    "records", "daily_elec", "violations", "pay_history",
    "run_status", "meta", "users", "sessions", "failed_attempts", "audit_log",
}


def _load(name: str) -> dict:
    p = FIXTURES / f"{name}.json"
    assert p.exists(), f"缺少 fixture: {p}"
    return json.loads(p.read_text(encoding="utf-8"))


# ── 5 类 fixtures 都存在 ─────────────────────────────────────
@pytest.mark.parametrize("name", ["schema", "algorithms", "cards", "api", "behaviors"])
def test_fixture_exists_and_loads(name: str) -> None:
    data = _load(name)
    assert isinstance(data, dict) and data, f"{name}.json 为空或非对象"


# ── schema：10 张表 + 索引 ──────────────────────────────────
def test_schema_has_10_tables() -> None:
    s = _load("schema")
    assert set(s["tables"]) == EXPECTED_TABLES, (
        f"表集合不符。多: {set(s['tables']) - EXPECTED_TABLES}  "
        f"少: {EXPECTED_TABLES - set(s['tables'])}"
    )


def test_schema_records_columns_frozen() -> None:
    s = _load("schema")
    assert s["tables"]["records"] == ["id", "ts", "read_time", "remain"]


def test_schema_indexes_present() -> None:
    s = _load("schema")
    for idx in ("idx_records_ts", "idx_sessions_token", "idx_audit_time"):
        assert idx in s["indexes"], f"缺少索引 {idx}"


# ── algorithms：关键边界 ────────────────────────────────────
def test_stats_empty_returns_nulls() -> None:
    a = _load("algorithms")["web._stats"]["empty"]
    assert a == {"remain": None, "hourly_used": None,
                 "read_time": None, "daily_avg": None}


def test_stats_hourly_used_requires_3500s_gap() -> None:
    a = _load("algorithms")["web._stats"]
    assert a["two_1h_apart"]["hourly_used"] is not None, "1 小时间隔应有值"
    assert a["two_30min_apart"]["hourly_used"] is None, "30 分钟不满足 3500s 门槛"


def test_stats_remain_increase_yields_no_hourly() -> None:
    a = _load("algorithms")["web._stats"]["remain_increase"]
    assert a["hourly_used"] is None, "电量上升时不应给出耗电"


# ── cards：颜色分级边界（严格小于）──────────────────────────
@pytest.mark.parametrize("remain,expected", [
    (29.99, "red"), (30, "orange"), (79.99, "orange"), (80, "blue"),
    (199.99, "blue"), (200, "green"),
])
def test_card_colour_boundaries(remain: float, expected: str) -> None:
    c = _load("cards")["_template_for_remain"]
    assert c[str(remain)] == expected, f"remain={remain} 应为 {expected}"


# ── behaviors：3 处最易搞错 ─────────────────────────────────
def test_offline_when_row_missing() -> None:
    b = _load("behaviors")["_is_offline"]
    assert b["none"] is True, "缺行必须算离线"
    assert b["empty"] is True, "空 dict 必须算离线"
    assert b["online"] is False


def test_ssrf_blocked_urls_return_empty() -> None:
    b = _load("behaviors")["_parse_school_url"]
    for u in ("http://127.0.0.1:6379/x",
              "http://169.254.169.254/latest",
              "file:///etc/passwd",
              "http://evil.com/x"):
        assert b[u] == [], f"SSRF 未拦截: {u}"


def test_safe_room_tail_masks() -> None:
    b = _load("behaviors")["_safe_room_tail"]
    assert b["abcdef-1234"] == "1234"
    assert b[""] == "????"


# ── api：字段集 ─────────────────────────────────────────────
def test_api_live_keys_frozen() -> None:
    a = _load("api")["/api/live"]
    for k in ("stats", "run_status", "latest_ts", "scrape_status",
              "stale", "eqprice", "monthly_projection", "monthly_breakdown"):
        assert k in a["keys"], f"/api/live 缺少字段 {k}"


def test_api_data_keys_frozen() -> None:
    a = _load("api")["/api/data?hours=24"]
    assert a["keys"] == ["hours", "rows", "stats"]
'@ | Set-Content -Encoding UTF8 tests/regression/test_contract.py
```

### 0.8.2 跑全套验证

```powershell
Write-Host "`n=== 1) AST 守卫 ===" -ForegroundColor Cyan
python -m scripts.ast_guard

Write-Host "`n=== 2) Ruff ===" -ForegroundColor Cyan
python -m ruff check .

Write-Host "`n=== 3) Pytest ===" -ForegroundColor Cyan
python -m pytest -q

Write-Host "`n=== 4) 编译 ===" -ForegroundColor Cyan
python -m compileall -q .

Write-Host "`n=== 5) 工作树干净 ===" -ForegroundColor Cyan
git --no-pager status --short

Write-Host "`n=== 6) 无 legacy 引用 ===" -ForegroundColor Cyan
$hits = Select-String -Path *.py, tests/**/*.py, scripts/*.py `
        -Pattern '^\s*(import|from)\s+(dorm_power|feishu_bot|config|db)\b' `
        -ErrorAction SilentlyContinue
if ($hits) { $hits } else { Write-Host "  OK - 无 legacy import" }
```

**全部预期**：

| 检查 | 预期 |
|---|---|
| AST 守卫 | `starwatt/ 不存在 —— M0 阶段正常，跳过` |
| Ruff | 0 error |
| **Pytest** | **全部通过**（约 20 个用例） |
| compileall | 无输出 |
| git status | 空 |
| legacy import | 无 |

### 0.8.3 🔴 关键验收：删掉旧代码后仍全绿

```powershell
python -m pytest tests/regression/ -v
```

**这一步通过 → 说明**：
1. ✅ fixtures 完整（覆盖 5 类契约）
2. ✅ fixtures **自包含**（不依赖已删除的旧代码）
3. ✅ 快照校验用例本身可跑
4. ✅ **重写有了安全网**

### 0.8.4 提交 + 打 tag

```powershell
git add tests/regression/test_contract.py
git commit -m "test(m0): add contract snapshot assertions

Verifies fixtures are complete, self-consistent and loadable WITHOUT the
legacy code. This is the M0 acceptance gate: proves the safety net stands
on its own after 0.6 deleted 60+ legacy files."

git tag -a r69-m0 -m "M0 complete: baseline preserved + clean branch + contract snapshots"
git push origin rewrite/r69 --tags
```

### 0.8.5 生成 M0 报告

```powershell
$rpt = "docs/reports/m0_REPORT.md"
@"
# M0 报告

- 日期：$(Get-Date -Format 'yyyy-MM-dd HH:mm')
- 分支：rewrite/r69
- develop 基线：$(git rev-parse develop)

## 产出

| 项 | 值 |
|---|---|
| develop 落库 commit | 5 |
| rewrite/r69 commit | 5 |
| fixtures | 5 类 |
| 删除 legacy | $($before - $after) 个 |
| 保留文件 | $after 个 |

## 验收

- [x] develop 上 24 项已落库
- [x] rewrite/r69 工作树只含新架构文件
- [x] tests/regression/fixtures/ 有完整 5 类快照
- [x] 删除 legacy 后 pytest 仍全绿
- [x] ruff check 零错误
- [x] AST 守卫 R1-R7 通过

## 下一步

M1 — 基础设施层（timeutil / config / db / auth / config_registry / flags / logging / protocols）
"@ | Set-Content -Encoding UTF8 $rpt

git add $rpt
git commit -m "docs(m0): add M0 report"
git push origin rewrite/r69
```

---

## 附录 A：一键回滚手册

| 想撤到 | 命令 |
|---|---|
| **撤 0.8** | `git reset --hard r69-m0` |
| **撤 0.7** | `git reset --hard HEAD~1` |
| **撤 0.6（恢复 legacy）** | `git reset --hard HEAD~1` |
| **撤 0.5（删 fixtures）** | `git reset --hard HEAD~1` |
| **撤 0.4** | `git reset --hard HEAD~1` |
| **撤 0.3** | `git reset --hard HEAD~1` |
| **撤 0.2（删分支）** | `git checkout develop; git branch -D rewrite/r69` |
| **撤 0.1（撤销 5 个 commit）** | `git reset --soft HEAD~5`（保留改动）<br>`git reset --hard HEAD~5`（丢弃） |
| **灾难恢复（全丢）** | 从 bundle 恢复（见 A.1） |

### A.1 从 bundle 恢复（最坏情况）

```powershell
$dst = "$env:TEMP\recovered"
git clone "$env:TEMP\develop_baseline.bundle" $dst
cd $dst
git --no-pager log --oneline -5
# 再把需要的文件复制回仓库
```

### A.2 单个文件恢复

```powershell
# 从 develop 取回任何被删的旧文件
git checkout develop -- web.py
git checkout develop -- db/_legacy.py
git checkout develop -- static/fonts/
```

---

## 附录 B：常见问题

| # | 问题 | 处理 |
|---|---|---|
| 1 | `git rm` 报 `did not match any files` | 该文件可能已删 → `git status` 确认后跳过 |
| 2 | 快照生成器报 `AttributeError: module has no attribute '_stats'` | 函数名与旧代码不符 → 打开 `web.py` 确认真实名 |
| 3 | `pytest` 报 `fixture 'tmp_db' not found` | `conftest.py` 未被识别 → 确认在 `tests/` 根且文件名正确 |
| 4 | `frozen_now` 不生效（时间仍是真的） | 旧代码可能用 `import datetime` 而非 `from datetime import datetime` → 补 `monkeypatch.setattr(mod.datetime, "datetime", _F)` |
| 5 | 快照里出现 `EXC:XXX` | 该函数在测试环境抛异常 → 检查是否需 mock（如网络） |
| 6 | `static/fonts/` 被误删 | `git checkout develop -- static/fonts/` |
| 7 | CI 报 ruff 失败 | 本地先跑 `python -m ruff check . --fix` |
| 8 | `git push` 被拒 | `git pull --rebase origin rewrite/r69` 后重推 |
| 9 | `records.db` 被误提交 | `git rm --cached records.db` + 确认 `.gitignore` 已含它 |
| 10 | **忘了备份就执行了 0.6** | 只要 0.1 的 commit 成功，`git checkout develop -- <path>` 就能取回**任何**旧文件 |
| 11 | `git rm -rf db/` 报 `pathspec did not match` | `db/` 是未跟踪目录 → 改用 `Remove-Item -Recurse -Force db/` |
| 12 | PowerShell 中文乱码 | 每个会话先跑 `chcp 65001` 或 `$OutputEncoding = [Text.Encoding]::UTF8` |

---

## 附录 C：M0 验收清单

> **全部勾选 = M0 完成，可进 M1**

### 基线保全
- [ ] `develop` 上有 5 个新 commit（A–E）
- [ ] `auth.py` / `db/` / `templates/` / `static/` 已在 `develop` 入库
- [ ] `git ls-files` 无 `_tmp_` / `records.db` / `diff_tests`
- [ ] `develop` 已 push

### 分支与骨架
- [ ] `rewrite/r69` 从 `develop` 切出
- [ ] `pyproject.toml` 存在且 ruff/mypy/pytest 可跑
- [ ] `requirements-dev.txt` 存在
- [ ] `tests/conftest.py` 三个 fixture 可用

### 契约快照
- [ ] `tests/regression/fixtures/` 有 **5 个** JSON
- [ ] `behaviors.json` 覆盖 `FEATURE_INVENTORY` 的全部 ⚠ 项
- [ ] **人工核对过** 3 处易错边界（30 / 200 / 缺行）
- [ ] fixtures 已提交（不是 untracked）

### 清空 legacy
- [ ] `git ls-files` 无 `config.py` / `db.py` / `web.py` / `dorm_power.py` / `feishu_bot.py` / `auth.py`
- [ ] `git ls-files` 无 `db/` / `templates/` / `static/css/` / `static/js/` / `assets/`
- [ ] `git ls-files` 无 `tests/modern/` / `scripts/build/`
- [ ] `static/fonts/` 仍有 3 个 TTF
- [ ] 文件数从 ~90 降到 ~20

### CI 与守卫
- [ ] `scripts/ast_guard.py` 存在且可跑
- [ ] `.github/workflows/ci.yml` 含 ruff + AST + pytest
- [ ] CI 在 `rewrite/r69` 上绿灯

### 终验（最重要）
- [ ] **删除 legacy 后 `pytest` 仍全绿**
- [ ] `ruff check .` 零错误
- [ ] `git status --short` 空
- [ ] 无 `import dorm_power|feishu_bot|config|db` 残留
- [ ] tag `r69-m0` 已打并 push

---

## 附录 D：M0 → M1 交接

M0 结束时仓库状态：

```
rewrite/r69
├── .github/workflows/ci.yml      ← 新
├── .gitignore                    ← 已加 records.db
├── LICENSE / README.md
├── pyproject.toml                ← 新
├── requirements.txt / requirements-dev.txt
├── .env.example
├── deploy/dorm-web.service       ← 待 M2 更新
├── static/fonts/                 ← 3 TTF（去重后一份）
├── scripts/
│   ├── __init__.py
│   └── ast_guard.py              ← 新
├── tests/
│   ├── conftest.py               ← 新
│   ├── regression/
│   │   ├── generate_fixtures.py  ← 新
│   │   ├── test_contract.py      ← 新
│   │   └── fixtures/*.json ×5    ← 新（安全网）
│   ├── unit/                     ← 空
│   └── integration/              ← 空
└── docs/
    ├── (8 份方案文档)
    └── reports/m0_*.txt/md       ← 新
```

**M1 要做的**：在这个空壳上建 `starwatt/`（timeutil / config / db / auth / config_registry / flags / logging_setup / protocols）。

---

## 附录 E：变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-06 | v1 | 初稿：8 步完整命令 + 5 类快照生成器 + 回滚手册 + FAQ + 验收清单 |

---

**执行手册结束** —— 本文档不含任何代码改动（所有代码块均为"**待执行**"的指令）。
"""M0 契约快照生成器 —— 在旧代码上运行，产出 5 类 fixtures。

⚠️ 必须在删除 legacy 之前执行（M0 步骤 0.4/0.5 早于 0.6）。

用法::

    python tests/regression/generate_fixtures.py

产物::

    tests/regression/fixtures/{schema,algorithms,cards,api,behaviors}.json

设计意图
========

这 5 份 fixtures 是**重写的行为基准**：它们由**旧代码真实运行**产出，
在 0.6 删掉旧代码之后，M1+ 的新实现必须与它们逐项一致。

这是「全面重写不丢功能」的唯一防线 —— 因为 105 项功能里有 45 项是隐性行为
（见 ``docs/FEATURE_INVENTORY.md`` 的 ⚠ 标记）。
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# 朴素 CST 固定时刻（与项目「全栈朴素本地时间」约定一致）
FIXED_NOW = _dt.datetime(2026, 10, 6, 12, 0, 0)


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
def _isolate_db() -> Path:
    """把 ``config.DB_PATH`` 指向临时文件，避免污染真实 records.db。"""
    tmp = Path(tempfile.mkdtemp()) / "fixture.db"
    import config

    config.DB_PATH = str(tmp)
    import db

    db.init()
    return tmp


def _dump(name: str, payload: object) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    p = FIXTURES / f"{name}.json"
    p.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(f"  -> {p.name}  ({p.stat().st_size} bytes)")


# ---------------------------------------------------------------------------
# 1) schema —— 10 张表 + 索引（字段名冻结）
# ---------------------------------------------------------------------------
def snap_schema() -> dict:
    import db

    conn = db.get_conn()
    tables: dict[str, list[str]] = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({name})")]
        tables[name] = cols

    indexes: dict[str, str] = {}
    for name, tbl in conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY name"
    ):
        indexes[name] = tbl

    return {"tables": tables, "indexes": indexes}


# ---------------------------------------------------------------------------
# 2) algorithms —— 核心算法的输入→输出向量
# ---------------------------------------------------------------------------
def snap_algorithms() -> dict:
    import dorm_power
    import web

    def rows(*pairs: tuple[str, float | None]) -> list[dict]:
        return [
            {"id": i, "ts": ts, "read_time": ts, "remain": r}
            for i, (ts, r) in enumerate(pairs, 1)
        ]

    stat_cases = {
        "empty": [],
        "single": rows(("2026-10-06 11:00:00", 10.5)),
        "two_1h_apart": rows(
            ("2026-10-06 11:00:00", 11.0),
            ("2026-10-06 12:00:00", 10.5),
        ),
        "two_30min_apart": rows(
            ("2026-10-06 11:30:00", 11.0),
            ("2026-10-06 12:00:00", 10.5),
        ),
        "seven_days": rows(
            ("2026-09-29 12:00:00", 20.0),
            ("2026-10-06 12:00:00", 13.0),
        ),
        "null_remain": [
            {"id": 1, "ts": "2026-10-06 12:00:00", "read_time": None, "remain": None}
        ],
        "remain_increase": rows(
            ("2026-10-06 11:00:00", 5.0),
            ("2026-10-06 12:00:00", 50.0),
        ),
        "no_read_time": [
            {"id": 1, "ts": "2026-10-06 11:00:00", "read_time": None, "remain": 11.0},
            {"id": 2, "ts": "2026-10-06 12:00:00", "read_time": None, "remain": 10.5},
        ],
    }

    proj_cases = {
        "empty_stats": {"remain": None, "daily_avg": None, "hourly_used": None},
        "no_daily_avg": {"remain": 10.0, "daily_avg": None, "hourly_used": 0.5},
        "normal": {"remain": 10.0, "daily_avg": 2.0, "hourly_used": 0.5},
    }

    return {
        "web._stats": {k: web._stats(v) for k, v in stat_cases.items()},
        "web._compute_monthly_projection": {
            k: web._compute_monthly_projection(0.5, v, "room-fixture")
            for k, v in proj_cases.items()
        },
        "dorm_power.calc_days_remaining": {
            f"remain={r},avg={a}": dorm_power.calc_days_remaining(r, a)
            for r, a in (
                (None, None),
                (None, 2.0),
                (10.0, None),
                (0.0, 2.0),
                (10.0, 0.0),
                (-1.0, 2.0),
                (10.0, 2.0),
                (100.0, 2.0),
            )
        },
    }


# ---------------------------------------------------------------------------
# 3) cards —— 飞书卡片 JSON 结构（颜色分区边界 + 离线卡）
# ---------------------------------------------------------------------------
_CARD_BODIES: dict[str, dict] = {
    "normal": {
        "remainEq": "12.34",
        "freeEq": "5.0",
        "rechargeEq": "7.34",
        "useEq": "100.0",
        "totalEq": "112.34",
        "remainWqMoney": "3.20",
        "dt": "2026-10-06 12:00:00",
        "eqprice": "0.5000",
    },
    "all_null": {
        "remainEq": None,
        "freeEq": None,
        "rechargeEq": None,
        "useEq": None,
        "totalEq": None,
        "remainWqMoney": None,
        "dt": None,
        "eqprice": None,
    },
    "red_zone": {"remainEq": "29.99", "totalEq": "100.0", "dt": "2026-10-06 12:00:00"},
    "orange_zone": {"remainEq": "79.99", "totalEq": "200.0", "dt": "2026-10-06 12:00:00"},
    "blue_zone": {"remainEq": "199.99", "totalEq": "400.0", "dt": "2026-10-06 12:00:00"},
    "green_zone": {"remainEq": "200.0", "totalEq": "400.0", "dt": "2026-10-06 12:00:00"},
    "no_dt": {"remainEq": "50.0", "totalEq": "200.0"},
}

# 颜色分区边界（严格小于 —— 见 _template_for_remain）
_TEMPLATE_BOUNDARIES = (None, 0, 29.99, 30, 79.99, 80, 199.99, 200, 999)


def snap_cards() -> dict:
    import dorm_power

    cards: dict[str, dict] = {}
    templates: dict[str, str] = {}
    for key, body in _CARD_BODIES.items():
        # top_of_hour=False 固定，避免依赖真实时钟
        cards[key] = dorm_power._build_card(
            body, "6号楼-1-119", "note-fixture", top_of_hour=False
        )
        raw = body.get("remainEq")
        templates[str(raw)] = dorm_power._template_for_remain(
            None if raw is None else float(raw)
        )

    return {
        "_build_card": cards,
        "_template_for_remain": templates,
        "_template_for_remain_boundaries": {
            str(v): dorm_power._template_for_remain(v) for v in _TEMPLATE_BOUNDARIES
        },
        "_build_offline_card": {
            "empty": dorm_power._build_offline_card({}, "note-fixture"),
            "offline": dorm_power._build_offline_card(
                {"run_status": "离线", "update_dt": "2026-10-06 11:00:00"},
                "note-fixture",
            ),
            "missing_label": dorm_power._build_offline_card(
                {"update_dt": "2026-10-06 11:00:00"}, "note-fixture"
            ),
        },
    }


# ---------------------------------------------------------------------------
# 4) api —— /api/* 的字段名集合
# ---------------------------------------------------------------------------
def _seed(db) -> None:
    """播种已知数据，让 /api/* 有稳定输出。

    ⚠ 不用 ``db.insert`` —— 它的 ``ts`` 来自 ``datetime.now()``，
    6 次连续调用会落在同一秒，被 ``INSERT OR REPLACE`` 合并成 1 行
    （实测确认）。这里直接写 SQL 指定 ``ts``，且用「相对当前时间」的
    偏移，保证永远落在 ``/api/data?hours=24`` 的查询窗口内。
    """
    room = "room-fixture"
    now = _dt.datetime.now()
    conn = db.get_conn()
    for i in range(6):
        ts = (now - _dt.timedelta(hours=5 - i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR REPLACE INTO records (ts, read_time, remain) VALUES (?, ?, ?)",
            (ts, ts, 100.0 - i * 0.5),
        )
    db.upsert_run_status(
        room,
        {
            "vol": 220.1,
            "cur": 0.45,
            "yggl": 12.3,
            "runStatus": "在线",
            "updateDt": "2026-10-06 11:59:00",
        },
    )
    db.set_meta("last_room_id", room)
    db.set_meta("last_scrape_status", "ok")
    db.set_meta("eqprice", "0.5000")
    db.set_meta("oobe_completed", "1")


def snap_api() -> dict:
    import db

    _seed(db)

    from web import app

    out: dict[str, dict] = {}
    with app.test_client() as client:
        for url in ("/api/data?hours=24", "/api/data?hours=168", "/api/live"):
            resp = client.get(url)
            payload = resp.get_json() or {}
            out[url] = {
                "status": resp.status_code,
                "keys": sorted(payload.keys()),
                "stats_keys": sorted((payload.get("stats") or {}).keys()),
                "run_status_keys": sorted((payload.get("run_status") or {}).keys()),
                "monthly_keys": sorted((payload.get("monthly_breakdown") or {}).keys()),
                "row_keys": (
                    sorted(payload["rows"][0].keys()) if payload.get("rows") else []
                ),
            }
    return out


# ---------------------------------------------------------------------------
# 5) behaviors —— 隐性行为（FEATURE_INVENTORY 的 ⚠ 标记）
# ---------------------------------------------------------------------------
def _frozen_modules():
    """把相关模块的 ``datetime`` 符号替换为冻结子类（生成期生效）。"""
    import contextlib

    import dorm_power
    import feishu_bot
    import web

    @contextlib.contextmanager
    def _ctx():
        class _FrozenDateTime(_dt.datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003
                return FIXED_NOW

            @classmethod
            def today(cls):
                return FIXED_NOW

        saved = {}
        for mod in (dorm_power, feishu_bot, web):
            if getattr(mod, "datetime", None) is _dt.datetime:
                saved[mod] = mod.datetime
                mod.datetime = _FrozenDateTime
        try:
            yield
        finally:
            for mod, orig in saved.items():
                mod.datetime = orig

    return _ctx()


def _offline():
    """阻止真实网络调用 —— fixture 必须离线可重现。

    ``web._parse_school_url`` 内部会 ``requests.get()`` 学校页面；
    若不拦截，生成的 fixture 会依赖网络与学校可用性。
    拦截后它会走 ``except requests.RequestException`` 分支，
    只返回 ``{openid, base_url}`` —— 这正是我们要固化的确定性行为。
    """
    from unittest import mock

    import requests

    return mock.patch.object(
        requests,
        "get",
        side_effect=requests.RequestException("network disabled during fixture generation"),
    )


def snap_behaviors() -> dict:
    import db
    import dorm_power
    import feishu_bot
    import web

    out: dict = {}

    with _frozen_modules():
        # ── 阈值 / 判定 ─────────────────────────────────────
        out["_template_for_remain"] = {
            str(v): dorm_power._template_for_remain(v) for v in _TEMPLATE_BOUNDARIES
        }

        out["_is_offline"] = {
            "none": dorm_power._is_offline(None),
            "empty": dorm_power._is_offline({}),
            "no_label": dorm_power._is_offline({"vol": 220.0}),
            "online": dorm_power._is_offline({"run_status": "在线"}),
            "normal": dorm_power._is_offline({"run_status": "正常"}),
            "comm_ok": dorm_power._is_offline({"run_status": "通讯正常"}),
            "offline": dorm_power._is_offline({"run_status": "离线"}),
            "unknown": dorm_power._is_offline({"run_status": "??"}),
        }

        # ── 脱敏 ───────────────────────────────────────────
        out["_safe_room_tail"] = {
            repr(s): dorm_power._safe_room_tail(s)
            for s in ("", "b3b8", "abcdef-1234", "12345")
        }
        out["_safe_path"] = {
            "with_qs": dorm_power._safe_path("http://h/p?a=1&openid=SECRET"),
            "no_qs": dorm_power._safe_path("http://h/p"),
            "empty": dorm_power._safe_path(""),
        }

        # ── 解析 ───────────────────────────────────────────
        # 注意：_ROOM_NO_RE 只匹配 <input ... id="roomNo" value="...">，
        # 不匹配 <span id="roomNo">…</span>（实测确认）。
        out["_discover_room"] = {
            "normal": list(
                dorm_power._discover_room(
                    '<input type="hidden" id="roomId" value="uuid-1">'
                    '<input type="hidden" id="roomNo" value="6号楼-1-119">'
                )
            ),
            "reversed_attrs": list(
                dorm_power._discover_room(
                    '<input value="uuid-2" id="roomId" type="hidden">'
                    '<input value="6号楼-2-220" id="roomNo" type="hidden">'
                )
            ),
            "span_not_matched": list(
                dorm_power._discover_room(
                    '<input type="hidden" id="roomId" value="uuid-3">'
                    '<span id="roomNo">6号楼-3-330</span>'
                )
            ),
            "missing": list(dorm_power._discover_room("<html></html>")),
        }

        # ── 时间 / 节流 ────────────────────────────────────
        out["_is_top_of_hour_frozen_1200"] = dorm_power._is_top_of_hour()
        out["_read_push_time"] = {
            "default_used": list(
                dorm_power._read_push_time("__nonexistent_key__", "09:00")
            ),
            "malformed_falls_back": list(
                dorm_power._read_push_time("__nonexistent_key__", "8:30")
            ),
        }
        out["_is_due_never_seen"] = dorm_power._is_due("__never_set__", 3600)
        db.set_meta("__fresh_stamp__", FIXED_NOW.strftime("%Y-%m-%d %H:%M:%S"))
        out["_is_due_just_stamped_3600"] = dorm_power._is_due("__fresh_stamp__", 3600)

        # ── 推送策略 ───────────────────────────────────────
        out["_should_push"] = {
            layer: feishu_bot._should_push(layer)
            for layer in ("l1", "l2", "daily", "weekly", "monthly")
        }
        out["_in_quiet_hours_frozen"] = feishu_bot._in_quiet_hours()

        # ── 飞书文本工具 ───────────────────────────────────
        out["_strip_mention"] = {
            repr(t): feishu_bot._strip_mention(t)
            for t in ("@_user_1 /状态", " /状态", "/状态", "@_bot_1   /电表")
        }
        out["_parse_history_args"] = {
            repr(t): feishu_bot._parse_history_args(t)
            for t in ("", "12", "abc", "-5", "9999", "720")
        }

        # ── 密码强度 ───────────────────────────────────────
        out["_validate_password_strength"] = {
            repr(s): web._validate_password_strength(s)
            for s in ("", "abc", "abcdefgh", "Abcdefg1", "Abcdefg1!", "x" * 200)
        }

        # ── SSRF 4 层防护（离线拦截，见 _offline）───────────
        with _offline():
            ssrf: dict[str, object] = {}
            for url in (
                "http://ybhqcz.fjny.edu.cn/x?openid=1",
                "http://127.0.0.1:6379/x?openid=1",
                "http://169.254.169.254/latest?openid=1",
                "file:///etc/passwd?openid=1",
                "http://evil.example.com/x?openid=1",
                "http://ybhqcz.fjny.edu.cn/x",
            ):
                try:
                    ssrf[url] = sorted((web._parse_school_url(url) or {}).keys())
                except Exception as exc:  # noqa: BLE001
                    ssrf[url] = f"EXC:{type(exc).__name__}"
            out["_parse_school_url"] = ssrf

    # ── DB 语义（冻结时间，验证 INSERT OR REPLACE）─────────
    with _frozen_modules():
        before = len(db.query(hours=24))
        db.insert(42.0, "2026-10-06 12:00:00")
        db.insert(43.0, "2026-10-06 12:00:01")   # 同一秒 → INSERT OR REPLACE
        after = len(db.query(hours=24))
        latest_row = db.latest()                 # sqlite3.Row | None
    out["db.insert_replace_semantics"] = {
        "rows_before": before,
        "rows_after_two_inserts": after,
        "delta": after - before,
        "latest_remain": latest_row["remain"] if latest_row else None,
    }

    # ── 本次生成发现的死代码体系（🔴 L21）───────────────────
    # 证据：push_if_enabled() 定义于 feishu_bot.py:2107，零调用；
    #       _should_push() 与 _in_quiet_hours() 只被它调用。
    #       所以 Admin UI 的推送开关 / 静默时段对行为**无影响**。
    out["_findings_dead_push_switches"] = {
        "_evidence": [
            "feishu_bot.py:2107  def push_if_enabled(layer, card)  -> 0 call sites",
            "feishu_bot.py:2113  _should_push(layer)  -> only called by push_if_enabled",
            "feishu_bot.py:2116  _in_quiet_hours()    -> only called by push_if_enabled",
            "web.py:2199/2205/2206  push_l1_enable / push_l2_enable written+returned but never read",
            "push_receivers_l1/l2/report/alert  -> 0 call sites",
        ],
        "DEAD": [
            "push_l1_enable",
            "push_l2_enable",
            "quiet_hours_start",
            "quiet_hours_end",
            "push_receivers_l1",
            "push_receivers_l2",
            "push_receivers_report",
            "push_receivers_alert",
        ],
        "ACTIVE": [
            "push_daily_enable  (read by dorm_power._l3_due)",
            "push_weekly_enable (read by dorm_power._l3_due)",
            "push_monthly_enable(read by dorm_power._l3_due)",
            "push_daily_time    (read by dorm_power._read_push_time)",
            "push_weekly_time   (read by dorm_power._read_push_time)",
            "push_monthly_time  (read by dorm_power._read_push_time)",
        ],
        "consequence": (
            "重写时 Q16 不是『把现有开关搬到 UI』，而是**从零实现真正的开关体系**；"
            "L1/L2/违规/离线/stale 五条推送路径当前都绕过 enable 检查直接 _post_feishu。"
        ),
    }

    # ── 待补项（M0 骨架未覆盖，见 FEATURE_INVENTORY 的 ⚠）───
    out["_TODO_not_yet_snapshotted"] = [
        "A: F2/F3/F5 节流间隔（需预置 meta 时间戳或 patch _is_due）",
        "A: backfill 一次性（F1+F2+F5 全成功才置 backfill_done）",
        "A: eqprice 缓存条件（仅非默认值才写回 meta）",
        "A: 抓取失败降级为 stale（需 mock _fetch_data 抛异常）",
        "B: L1 低电跃迁判定（需 mock _post_feishu + 预置 records）",
        "B: L3 三套 cadence（_l3_due daily/weekly/monthly）",
        "B: 违规 30min 冷却（_check_violation_alert）",
        "B: stale 2h 门槛（_check_stale_scrape）",
        "B: L3/L2 同分钟去重（skip_l2_top_of_hour）",
        "C: 9 个 slash 命令输出（需预置 DB + mock 上传）",
        "C: 菜单事件映射（MENU_KEYS）",
        "C: 无凭据降级文案",
        "D: AES 三条解密路径（decrypt_payload）",
        "D: v2 HMAC 签名校验（_verify_lark_signature）",
        "E: 前端契约（30s 轮询 / 4 档范围 / 2h 离线窗口）—— 非 Python，靠人工核对",
        "H: OOBE 双状态源 + 完成后不可入",
        "K: init() 幂等（重复调用无副作用）",
    ]

    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    print("M0 契约快照生成器（StarWatt）")
    print(f"  项目根  : {PROJ}")
    print(f"  产物目录: {FIXTURES}")
    tmp = _isolate_db()
    print(f"  隔离 DB : {tmp}")
    print()

    print("[1/5] schema ...")
    _dump("schema", snap_schema())
    print("[2/5] algorithms ...")
    _dump("algorithms", snap_algorithms())
    print("[3/5] cards ...")
    _dump("cards", snap_cards())
    print("[4/5] api ...")
    _dump("api", snap_api())
    print("[5/5] behaviors ...")
    _dump("behaviors", snap_behaviors())

    print(f"\n完成 -> {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



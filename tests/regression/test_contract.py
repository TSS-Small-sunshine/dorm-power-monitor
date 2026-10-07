"""M0 契约快照校验 —— 只验证 fixtures 自洽，不做新旧对比。

背景
====

``tests/regression/fixtures/*.json`` 由 ``generate_fixtures.py`` 在**旧代码**
上运行产出（M0 步骤 0.4/0.5，早于 0.6 的 legacy 清除）。它们固化了重写必须
保真的行为 —— 这是「全面重写不丢功能」的唯一防线（105 项功能里 45 项是隐性行为）。

M0 阶段本文件只保证 fixtures **完整、可加载、形状正确、关键边界值正确**；
等 M1 起有了 ``starwatt/`` 实现，会扩展为「新实现 vs fixtures」的对比断言。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"

EXPECTED_TABLES = {
    "audit_log",
    "daily_elec",
    "failed_attempts",
    "meta",
    "pay_history",
    "records",
    "run_status",
    "sessions",
    "users",
    "violations",
}

ALL_FIXTURES = ("schema", "algorithms", "cards", "api", "behaviors")


def _load(name: str) -> dict:
    path = FIXTURES / f"{name}.json"
    assert path.exists(), f"缺少 fixture: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# 0) 5 类 fixtures 都存在且可加载
# ===========================================================================
@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_fixture_exists_and_loads(name: str) -> None:
    data = _load(name)
    assert isinstance(data, dict) and data, f"{name}.json 为空或非对象"


# ===========================================================================
# 1) schema —— 10 表 + 索引（字段名冻结）
# ===========================================================================
def test_schema_has_exactly_10_tables() -> None:
    tables = set(_load("schema")["tables"])
    assert tables == EXPECTED_TABLES, (
        f"表集合不符。多: {tables - EXPECTED_TABLES}  少: {EXPECTED_TABLES - tables}"
    )


def test_schema_records_columns_frozen() -> None:
    assert _load("schema")["tables"]["records"] == ["id", "ts", "read_time", "remain"]


def test_schema_users_has_no_disabled_column() -> None:
    """记录 L18：R68 文档声称有 disabled 字段，实际没有（M0 基线如此）。"""
    cols = _load("schema")["tables"]["users"]
    assert cols == ["id", "username", "password_hash", "role", "created_at", "last_login_at"]
    assert "disabled" not in cols


def test_schema_indexes_present() -> None:
    indexes = _load("schema")["indexes"]
    for idx in ("idx_records_ts", "idx_sessions_token", "idx_audit_time"):
        assert idx in indexes, f"缺少索引 {idx}"
    assert len(indexes) >= 15, "索引数量异常偏少"


# ===========================================================================
# 2) algorithms —— 核心算法边界
# ===========================================================================
def test_stats_empty_returns_all_nulls() -> None:
    assert _load("algorithms")["web._stats"]["empty"] == {
        "remain": None,
        "hourly_used": None,
        "read_time": None,
        "daily_avg": None,
    }


def test_stats_hourly_used_requires_3500s_gap() -> None:
    """🔑 1 小时间隔有值；30 分钟间隔（<3500s）必须为 None。"""
    stats = _load("algorithms")["web._stats"]
    assert stats["two_1h_apart"]["hourly_used"] == 0.5
    assert stats["two_30min_apart"]["hourly_used"] is None


def test_stats_remain_increase_yields_no_hourly_used() -> None:
    """电量上升（充值）不算耗电。"""
    assert _load("algorithms")["web._stats"]["remain_increase"]["hourly_used"] is None


def test_stats_daily_avg_uses_oldest_minus_newest() -> None:
    """7 天窗口：(20.0 - 13.0) / 7 = 1.0"""
    assert _load("algorithms")["web._stats"]["seven_days"]["daily_avg"] == 1.0


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("remain=None,avg=None", None),
        ("remain=None,avg=2.0", None),
        ("remain=10.0,avg=None", None),
        ("remain=0.0,avg=2.0", 0.0),
        ("remain=10.0,avg=0.0", None),
        ("remain=-1.0,avg=2.0", 0.0),
        ("remain=10.0,avg=2.0", 5.0),
        ("remain=100.0,avg=2.0", 50.0),
    ],
)
def test_calc_days_remaining_boundaries(key: str, expected: float | None) -> None:
    assert _load("algorithms")["dorm_power.calc_days_remaining"][key] == expected


# ===========================================================================
# 3) cards —— 颜色分级边界（严格小于）
# ===========================================================================
@pytest.mark.parametrize(
    ("remain", "expected"),
    [
        ("0", "red"),
        ("29.99", "red"),
        ("30", "orange"),      # 🔑 严格小于：30 不是 red
        ("79.99", "orange"),
        ("80", "blue"),
        ("199.99", "blue"),
        ("200", "green"),      # 🔑 严格小于：200 是 green
        ("999", "green"),
        ("None", "blue"),      # 缺数据回退 blue
    ],
)
def test_card_colour_boundaries(remain: str, expected: str) -> None:
    boundaries = _load("cards")["_template_for_remain_boundaries"]
    assert boundaries[remain] == expected


def test_card_bodies_have_header_and_elements() -> None:
    cards = _load("cards")["_build_card"]
    assert set(cards) >= {"normal", "all_null", "red_zone", "green_zone"}
    for name, card in cards.items():
        assert card["msg_type"] == "interactive", f"{name}: msg_type 应为 interactive"
        header = card["card"]["header"]
        assert "title" in header and "template" in header, f"{name}: header 缺字段"
        assert isinstance(card["card"]["elements"], list), f"{name}: elements 应为 list"


# ===========================================================================
# 4) api —— 字段集冻结
# ===========================================================================
def test_api_data_keys_frozen() -> None:
    api = _load("api")
    for url in ("/api/data?hours=24", "/api/data?hours=168"):
        assert api[url]["keys"] == ["hours", "rows", "stats"]
        assert api[url]["status"] == 200
        assert api[url]["row_keys"] == ["id", "read_time", "remain", "ts"]


def test_api_live_keys_frozen() -> None:
    live = _load("api")["/api/live"]
    for key in (
        "stats",
        "run_status",
        "latest_ts",
        "scrape_status",
        "stale",
        "eqprice",
        "monthly_projection",
        "monthly_breakdown",
    ):
        assert key in live["keys"], f"/api/live 缺少字段 {key}"


def test_api_live_run_status_has_exactly_5_keys() -> None:
    assert _load("api")["/api/live"]["run_status_keys"] == [
        "cur",
        "run_status",
        "update_dt",
        "vol",
        "yggl",
    ]


# ===========================================================================
# 5) behaviors —— 隐性行为
# ===========================================================================
def test_offline_when_row_missing() -> None:
    """🔑 关键设计：缺行 / 空 dict / 无标签 都算离线（防静默失败）。"""
    offline = _load("behaviors")["_is_offline"]
    assert offline["none"] is True
    assert offline["empty"] is True
    assert offline["no_label"] is True
    assert offline["offline"] is True
    assert offline["unknown"] is True
    assert offline["online"] is False
    assert offline["normal"] is False
    assert offline["comm_ok"] is False


def test_ssrf_blocked_urls_are_rejected() -> None:
    """🔒 4 层防护：内网 / 元数据地址 / 非 http scheme / 外域 —— 全部不解析出 roomId。"""
    ssrf = _load("behaviors")["_parse_school_url"]
    for url in (
        "http://127.0.0.1:6379/x?openid=1",
        "http://169.254.169.254/latest?openid=1",
        "file:///etc/passwd?openid=1",
        "http://evil.example.com/x?openid=1",
    ):
        assert ssrf[url] == ["base_url", "openid"], f"SSRF 未拦截: {url}"


def test_safe_room_tail_masks_to_last4() -> None:
    tails = _load("behaviors")["_safe_room_tail"]
    assert tails["''"] == "????"
    assert tails["'b3b8'"] == "b3b8"
    assert tails["'abcdef-1234'"] == "1234"
    assert tails["'12345'"] == "2345"


def test_safe_path_strips_query_string() -> None:
    """🔒 openid 在 query string 里，日志只能出现 path。"""
    paths = _load("behaviors")["_safe_path"]
    assert paths["with_qs"] == "/p"
    assert paths["no_qs"] == "/p"
    assert paths["empty"] == "/"


def test_discover_room_parses_input_tag() -> None:
    """_ROOM_NO_RE 只匹配 <input id="roomNo">，不匹配 <span>（已固化该限制）。"""
    rooms = _load("behaviors")["_discover_room"]
    assert rooms["normal"] == ["uuid-1", "6号楼-1-119"]
    assert rooms["reversed_attrs"] == ["uuid-2", "6号楼-2-220"]
    assert rooms["span_not_matched"] == ["uuid-3", None]
    assert rooms["missing"] == [None, None]


def test_read_push_time_falls_back_on_missing_meta() -> None:
    push_time = _load("behaviors")["_read_push_time"]
    assert push_time["default_used"] == [9, 0]
    assert push_time["malformed_falls_back"] == [8, 30]


def test_is_due_semantics() -> None:
    behaviors = _load("behaviors")
    assert behaviors["_is_due_never_seen"] is True
    assert behaviors["_is_due_just_stamped_3600"] is False


def test_should_push_defaults() -> None:
    """🔑 L1 默认关闭，其余默认开启（_PUSH_DEFAULTS）。"""
    should = _load("behaviors")["_should_push"]
    assert should == {
        "l1": False,
        "l2": True,
        "daily": True,
        "weekly": True,
        "monthly": True,
    }


def test_strip_mention_and_history_args() -> None:
    behaviors = _load("behaviors")
    assert behaviors["_strip_mention"]["'@_user_1 /状态'"] == "/状态"
    assert behaviors["_strip_mention"]["'@_bot_1   /电表'"] == "/电表"
    args = behaviors["_parse_history_args"]
    assert args["''"] == 24          # 默认
    assert args["'12'"] == 12
    assert args["'720'"] == 720      # 上限内
    assert args["'9999'"] == 24      # 超上限 → 回落默认
    assert args["'abc'"] == 24       # 非数字 → 回落默认
    assert args["'-5'"] == 24        # 负数 → 回落默认


def test_password_strength_rules() -> None:
    pw = _load("behaviors")["_validate_password_strength"]
    assert pw["''"] == "password required"
    assert pw["'abc'"] == "password must be at least 8 characters"
    assert pw["'abcdefgh'"] == "password must contain an uppercase letter"
    assert pw["'Abcdefg1'"] == "password must contain a special character"
    assert pw["'Abcdefg1!'"] is None
    assert pw["'" + "x" * 200 + "'"] == "password must be 128 characters or less"


def test_db_insert_replace_semantics() -> None:
    """🔑 records.ts UNIQUE + INSERT OR REPLACE：同秒两次插入不增行，值被替换。"""
    sem = _load("behaviors")["db.insert_replace_semantics"]
    assert sem["delta"] == 0, "同秒插入不应新增行"
    assert sem["latest_remain"] == 43.0, "第二次插入的值应覆盖第一次"


# ===========================================================================
# 6) 死代码证据（L21）—— 防止重写时误以为开关已生效
# ===========================================================================
def test_dead_push_switches_are_documented() -> None:
    findings = _load("behaviors")["_findings_dead_push_switches"]
    assert set(findings["DEAD"]) == {
        "push_l1_enable",
        "push_l2_enable",
        "quiet_hours_start",
        "quiet_hours_end",
        "push_receivers_l1",
        "push_receivers_l2",
        "push_receivers_report",
        "push_receivers_alert",
    }
    assert len(findings["ACTIVE"]) == 6
    assert findings["_evidence"], "必须保留证据链"


def test_todo_list_is_tracked() -> None:
    """M0 骨架未覆盖的行为必须显式列出，不能静默漏掉。"""
    todo = _load("behaviors")["_TODO_not_yet_snapshotted"]
    assert len(todo) >= 15
    joined = " ".join(todo)
    for domain in ("A:", "B:", "C:", "D:", "E:", "H:", "K:"):
        assert domain in joined, f"TODO 缺少 {domain} 域"



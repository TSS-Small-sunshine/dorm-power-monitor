"""M7 生产切换自检 —— 把 9 项验收里**能自动化**的部分跑完。

用法
====

    # 对着一个正在运行的新实例跑（推荐先用旧库副本旁路起一套）
    python -m scripts.cutover_check --base http://127.0.0.1:5001 \
        --user admin --password '你的密码'

    # 顺带体检数据库（会自动跳过会写库的检查）
    python -m scripts.cutover_check --db /var/lib/dorm-power-monitor/records.db \
        --user admin --password '你的密码'

    # 允许真的触发一次抓取（默认不触发 —— 它会去请求学校接口）
    python -m scripts.cutover_check --refresh --user admin --password '...'

    # 密码也可以走环境变量，避免出现在 shell 历史里
    CUTOVER_PASSWORD='...' python -m scripts.cutover_check --user admin

为什么要有它
============

`docs/M7_CUTOVER.md` 的第 2 步列了 9 项验收。人工点一遍容易漏，而且
「看起来正常」和「真的正常」差别很大（比如调度器没起来时界面照样能开）。
这个脚本把能问的都问一遍，并打印**证据**（行数、时间戳、项目数），
剩下的（机器人命令、卡片、等一个抓取周期）明确列成手动清单，不假装验过。

退出码：0 = 自动项全过；1 = 有失败项。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover - 运行时依赖，理论上不会缺
    print("需要 requests（pip install -r requirements.txt）", file=sys.stderr)
    raise SystemExit(1) from None

CSRF_HEADER = "X-CSRF-Token"
DEFAULT_BASE = "http://127.0.0.1:5000"

#: 抓取新鲜度阈值（秒）：默认抓取间隔是 600 秒，超过这个数就值得警惕
STALE_AFTER_SEC = 30 * 60


class Report:
    """收集结果并决定退出码。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.passed = 0
        self.warned = 0
        self.failed = 0
        self.manual: list[str] = []

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(title)

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        self.lines.append(f"  ✅ {label}" + (f" —— {detail}" if detail else ""))

    def warn(self, label: str, detail: str = "") -> None:
        self.warned += 1
        self.lines.append(f"  ⚠️  {label}" + (f" —— {detail}" if detail else ""))

    def bad(self, label: str, detail: str = "") -> None:
        self.failed += 1
        self.lines.append(f"  ❌ {label}" + (f" —— {detail}" if detail else ""))

    def note(self, text: str) -> None:
        self.lines.append(f"     ℹ️  {text}")

    def add_manual(self, text: str) -> None:
        self.manual.append(text)

    def render(self) -> str:
        out = list(self.lines)
        if self.manual:
            out.append("")
            out.append("需要你手动确认（脚本替代不了）")
            out.extend(f"  □ {item}" for item in self.manual)
        out.append("")
        if self.failed:
            out.append(
                f"结论：❌ {self.passed} 项通过，{self.failed} 项失败，"
                f"{self.warned} 项待观察"
            )
        else:
            out.append(
                f"结论：✅ {self.passed} 项通过，0 项失败，{self.warned} 项待观察"
            )
        return "\n".join(out)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------
def _get(session, base: str, path: str) -> tuple[int, dict | list | None]:
    """GET，返回 ``(状态码, JSON)``；解析失败时 JSON 为 ``None``。"""
    try:
        resp = session.get(base + path, timeout=session.timeout)  # type: ignore[attr-defined]
    except requests.RequestException as exc:
        return 0, {"error": str(exc)}
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _post(session, base: str, path: str, payload: dict) -> tuple[int, dict | None]:
    try:
        resp = session.post(base + path, json=payload, timeout=session.timeout)  # type: ignore[attr-defined]
    except requests.RequestException as exc:
        return 0, {"error": str(exc)}
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _age_text(stamp: str | None) -> tuple[str, float | None]:
    """把 ``YYYY-MM-DD HH:MM:SS`` 变成「N 分钟前」与秒数。"""
    if not stamp:
        return "无时间戳", None
    try:
        moment = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stamp, None
    delta = (datetime.now() - moment).total_seconds()
    if delta < 0:
        return f"{stamp}（时间在未来？检查时区）", delta
    if delta < 90:
        return f"{stamp}（{int(delta)} 秒前）", delta
    if delta < 5400:
        return f"{stamp}（{int(delta / 60)} 分钟前）", delta
    return f"{stamp}（{delta / 3600:.1f} 小时前）", delta


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------
def check_health(report: Report, session, base: str) -> bool:
    report.section("① 探活")
    status, body = _get(session, base, "/healthz")
    if status != 200 or not isinstance(body, dict):
        report.bad("/healthz 不通", f"HTTP {status} {body}")
        return False
    report.ok("/healthz 200", f"version={body.get('version', '?')}")
    return True


def login(report: Report, session, base: str, user: str, password: str) -> bool:
    """登录并读 ``/api/auth/me``；顺带判断「首登强制改密」状态。"""
    report.section("② 登录与首登强制改密")
    status, body = _post(session, base, "/api/auth/login", {"username": user, "password": password})
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        report.bad(
            f"{user} 登录失败",
            f"HTTP {status} {body.get('error') if isinstance(body, dict) else body}",
        )
        return False
    report.ok(f"{user} 登录成功")

    status, me = _get(session, base, "/api/auth/me")
    if status != 200 or not isinstance(me, dict):
        report.bad("/api/auth/me 读不到", f"HTTP {status}")
        return False
    who = me.get("user") or {}
    must_change = bool(who.get("must_change_password"))
    if must_change:
        report.warn(
            "这个账号还处于「首登强制改密」状态",
            "除改密/登出/me 外的接口都会 403 must_change_password",
        )
        report.note("先去网页改一次密码，再重跑本脚本；下面的管理项会被跳过。")
        return False
    report.ok("已通过强制改密", f"角色={who.get('role', '?')}")
    return True


def check_data(report: Report, session, base: str) -> None:
    """③ 四段数据 —— 这是「零迁移」最直观的证据。

    ⚠️ 房间相关的三段（历史 / 违规 / 缴费）与电表都由 ``last_room_id`` 过滤，
    而那个 state 键**由成功的抓取发布**。所以旧库切过来后、还没抓过一次时，
    它们会是空的 —— 这时报 ⚠️ 并给出原因，而不是当成通过（否则会给出假阴性）。
    """
    report.section("③ 四段数据（概览 / 历史 / 违规 / 电表）")

    status, site = _get(session, base, "/api/site")
    if status == 200 and isinstance(site, dict):
        report.ok("站点信息 /api/site", f"site_name={site.get('site_name', '?')}")
    else:
        report.warn("站点信息读不到", f"HTTP {status}")

    status, data = _get(session, base, "/api/data?hours=24")
    if status != 200 or not isinstance(data, dict):
        report.bad("概览 /api/data 读不到", f"HTTP {status} {data}")
    else:
        rows = data.get("rows") or []
        newest = rows[-1].get("ts") if rows else None
        age_text, age = _age_text(newest)
        if not rows:
            report.warn("概览没有任何记录", "还没抓过数据？先点一次「刷新」")
        elif age is not None and age > STALE_AFTER_SEC:
            report.warn("概览数据偏旧", f"最新 {age_text}（超过 {STALE_AFTER_SEC // 60} 分钟）")
        else:
            report.ok("概览 /api/data", f"{len(rows)} 条，最新 {age_text}")

    status, live = _get(session, base, "/api/live")
    if status != 200 or not isinstance(live, dict):
        report.bad("电表 /api/live 读不到", f"HTTP {status}")
    else:
        # ⚠️ LivePayload 里**没有** room_id（房间号在历史/违规/缴费那三个里），
        #    所以这里用 latest_ts 判断「抓过没有」（踩过）。
        run = live.get("run_status") or {}
        latest = live.get("latest_ts")
        if not latest:
            report.warn(
                "还没抓到过实时电表数据",
                "点一次「刷新」或等一个周期（首次抓取成功后才会有）",
            )
        elif live.get("stale"):
            report.warn("电表数据被标记为陈旧", f"latest_ts={latest}")
        else:
            report.ok(
                "电表 /api/live",
                f"抄表时间={latest} 电压={run.get('vol', '?')} 电流={run.get('cur', '?')} "
                f"功率={run.get('yggl', '?')}",
            )

    for label, path, unit in (
        ("历史", "/api/daily?days=30", "天"),
        ("违规", "/api/violations?days=30", "条"),
        ("缴费", "/api/payments?days=90", "笔"),
    ):
        _check_room_section(report, session, base, label, path, unit)


def _check_room_section(
    report: Report, session, base: str, label: str, path: str, unit: str
) -> None:
    """按房间过滤的一段（历史 / 违规 / 缴费）。

    空结果要分清两种情况：房间号还没确定（等抓取）vs 房间号有了但确实没数据。
    前者是正常过渡态，后者才值得怀疑。
    """
    status, body = _get(session, base, path)
    if status != 200 or not isinstance(body, dict):
        report.warn(f"{label} {path} 读不到", f"HTTP {status}")
        return
    rows = body.get("rows") or []
    room = str(body.get("room_id") or "")
    if rows:
        report.ok(f"{label} {path}", f"{len(rows)} {unit}")
    elif not room:
        report.warn(
            f"{label} 暂时为空",
            "房间号还没确定（见上）—— 抓取成功后旧数据会自动出现",
        )
    else:
        report.warn(f"{label} 为空", f"房间={room} 查不到记录（旧库确实没有？）")


def check_admin(report: Report, session, base: str) -> None:
    """④ 管理接口 —— 配置中心 / 开关 / 日志 / 用户 / 审计。"""
    report.section("④ 管理接口")

    status, schema = _get(session, base, "/api/admin/config/schema")
    if status == 200 and isinstance(schema, dict):
        total = schema.get("total") or 0
        groups = len(schema.get("groups") or [])
        if total >= 90 and groups == 9:
            report.ok("配置中心 schema", f"{total} 项 / {groups} 组")
        else:
            report.warn("配置中心 schema 数量异常", f"{total} 项 / {groups} 组（预期 94 / 9）")
    else:
        report.bad("配置中心 schema 读不到", f"HTTP {status} {schema}")

    status, flags = _get(session, base, "/api/admin/flags")
    if status == 200 and isinstance(flags, dict):
        items = flags.get("flags") or []
        kinds: dict[str, int] = {}
        for item in items:
            kind = str(item.get("kind", "?"))
            kinds[kind] = kinds.get(kind, 0) + 1
        detail = " + ".join(f"{k} {v} 个" for k, v in sorted(kinds.items()))
        if len(items) == 18:
            report.ok("功能开关", f"{len(items)} 个（{detail}）")
        else:
            report.warn("功能开关数量异常", f"{len(items)} 个（预期 18）")
        if flags.get("quiet_hours"):
            report.note(f"静默时段：{flags['quiet_hours']}")
    else:
        report.bad("功能开关读不到", f"HTTP {status} {flags}")

    status, logging_state = _get(session, base, "/api/admin/logging")
    if status == 200 and isinstance(logging_state, dict):
        levels = logging_state.get("levels") or []
        categories = logging_state.get("categories") or []
        if len(levels) == 7 and len(categories) == 8:
            report.ok("日志设置", f"{len(levels)} 级 × {len(categories)} 类")
        else:
            report.warn("日志设置数量异常", f"{len(levels)} 级 × {len(categories)} 类（预期 7 × 8）")
        report.note(
            f"全局级别={logging_state.get('global_level')}，格式={logging_state.get('format')}"
        )
    else:
        report.bad("日志设置读不到", f"HTTP {status} {logging_state}")


def check_admin_users(report: Report, session, base: str) -> None:
    """④b 用户与审计（拆出来是为了让每个函数都保持可读的复杂度）。"""
    status, users = _get(session, base, "/api/admin/users")
    if status == 200 and isinstance(users, dict):
        report.ok("用户列表", f"{len(users.get('users') or [])} 个")
    else:
        report.warn("用户列表读不到", f"HTTP {status}")

    status, audit = _get(session, base, "/api/admin/audit?limit=5")
    if status == 200:
        rows = audit.get("rows") if isinstance(audit, dict) else audit
        report.ok("审计日志", f"最近 {len(rows or [])} 条可读")
    else:
        report.warn("审计日志读不到", f"HTTP {status}")


def check_daily_report(report: Report, db_path: Path | None = None) -> None:
    """⑤ L3 日报 —— 直接读 ``meta.last_daily_report_date``。

    为什么不走 HTTP：``/api/admin/config`` 只返回**配置项**（实测顶层键就是
    ``{flags, values}``），而 ``last_daily_report_date`` 是 **state** 键，不在里面。
    所以只有给了 ``--db`` 才能自动判断；否则老实列成手动项。
    """
    report.section("⑤ L3 日报（默认 09:00）")
    if db_path is None or not db_path.is_file():
        report.note("没给 --db，无法自动判断日报状态 → 见下面的手动项")
        report.add_manual("群里收到过 L3 日报卡片（或等到 09:00 后确认）")
        return

    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_daily_report_date'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        report.warn("读不到日报状态", str(exc))
        return

    today = datetime.now().strftime("%Y-%m-%d")
    stamp = row["value"] if row else None
    if stamp is None:
        report.warn("从没发过日报", "last_daily_report_date 是空的")
        report.note("日报默认 09:00 触发；若刚部署、还没到时间点，这是正常的。")
    elif str(stamp) == today:
        report.ok("今天已发过日报", f"last_daily_report_date={stamp}")
    else:
        report.warn("今天还没发日报", f"last_daily_report_date={stamp}，现在是 {today}")
        report.note("日报默认 09:00 触发；刚部署还没到时间点的话，这是正常的。")
    report.add_manual("群里收到过 L3 日报卡片（或等到 09:00 后确认）")


def check_db(report: Report, db_path: Path) -> None:
    """⑥ 数据库兼容性（复用 check_db，只读）。"""
    report.section("⑥ 数据库")
    from scripts import check_db

    inner = check_db.Report()
    check_db.inspect(db_path, inner)
    if inner.fatal:
        report.bad("数据库自检失败", inner.fatal)
        return
    if inner.missing_tables or inner.missing_columns:
        report.warn(
            "数据库比当前代码旧（启动时会自动补齐）",
            f"缺表 {inner.missing_tables}，缺列 {inner.missing_columns}",
        )
    else:
        report.ok("数据库结构与当前代码一致", f"schema_version={inner.schema_version}")
def check_refresh(report: Report, session, base: str, csrf: str) -> None:
    """⑦ 手动触发一次抓取（只有显式 ``--refresh`` 才跑）。"""
    report.section("⑦ 触发一次抓取（--refresh）")
    try:
        resp = session.post(
            base + "/api/refresh",
            headers={CSRF_HEADER: csrf},
            timeout=session.timeout,  # type: ignore[attr-defined]
        )
    except requests.RequestException as exc:
        report.bad("触发抓取失败", str(exc))
        return
    if resp.status_code != 200:
        report.bad("触发抓取失败", f"HTTP {resp.status_code} {resp.text[:120]}")
        return
    body = resp.json() if resp.content else {}
    report.ok("已触发抓取", f"{body}"[:120])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
MANUAL_ITEMS = (
    "浏览器里用新账号点一遍：首登应**直接跳到改密页**，进不了仪表盘",
    "飞书私聊逐个发这 9 个命令，都要有回复："
    "/状态 /剩余 /电表 /电表状态 /今日 /历史 /缴费 /违规 /帮助",
    "群里收到过一张卡片（或在「配置中心」手动触发一次推送验证）",
    "等一个抓取周期（默认 10 分钟），首页「最后更新」时间会变",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.cutover_check",
        description="M7 切换自检：把 9 项验收里能自动化的部分跑完",
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"实例地址（默认 {DEFAULT_BASE}）")
    parser.add_argument(
        "--user", default=os.environ.get("CUTOVER_USER", "admin"), help="管理员用户名"
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("CUTOVER_PASSWORD", ""),
        help="密码（也可用环境变量 CUTOVER_PASSWORD，避免留在 shell 历史里）",
    )
    parser.add_argument("--db", help="顺带体检这个 records.db（只读）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数")
    parser.add_argument(
        "--refresh", action="store_true", help="允许真的触发一次抓取（会请求学校接口）"
    )
    args = parser.parse_args(argv)

    base = args.base.rstrip("/")
    report = Report()
    session = requests.Session()
    session.timeout = args.timeout  # type: ignore[attr-defined]
    session.headers["User-Agent"] = "starwatt-cutover-check"

    print("StarWatt 星瓦 · 切换自检（M7）")
    print("=" * 46)
    print(f"目标：{base}")
    print(f"账号：{args.user}")

    if not check_health(report, session, base):
        print(report.render())
        return report.exit_code

    logged_in = login(report, session, base, args.user, args.password)
    csrf = ""
    if logged_in:
        status, me = _get(session, base, "/api/auth/me")
        if isinstance(me, dict):
            csrf = str(me.get("csrf_token") or "")
        check_data(report, session, base)
        check_admin(report, session, base)
        check_admin_users(report, session, base)
        check_daily_report(report, Path(args.db) if args.db else None)
        if args.refresh and csrf:
            check_refresh(report, session, base, csrf)
    else:
        report.note("没登录成功 → 数据与管理项跳过（只跑了探活）")

    if args.db:
        check_db(report, Path(args.db))

    for item in MANUAL_ITEMS:
        report.add_manual(item)

    print(report.render())
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())

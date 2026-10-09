"""``records.db`` 兼容性自检 —— 「我手上这个是哪一版、能不能直接用？」

用法
====

    # 1) 只读体检（**不会改你的库**）
    python -m scripts.check_db /path/to/records.db

    # 2) 在**副本**上真的跑一遍升级，看结果是否干净
    python -m scripts.check_db /path/to/records.db --fix

    # 3) 只看结论（退出码 0=可直接用 / 1=需人工 / 2=不是本项目的库）
    python -m scripts.check_db /path/to/records.db --quiet

为什么需要它
============

需求里写的是「零迁移」（Q9/Q10）：**老库不用搬数据**。但「不用搬」不等于
「表结构一定对得上」—— 升级前没法知道手上那个 ``records.db`` 是哪一版代码
建出来的。这个工具就是回答那个问题的：

* 拿**新代码的期望结构**（现建一个空库做基准）去比对目标库
* 报告缺哪些表、缺哪些列、多哪些列（多列无害）、每张表多少行
* 需要时在**副本**上跑一次真实的 ``init()``，验证「补完之后数据还在、
  结构和新库一致」，然后把替换步骤打印出来

三条安全约束（改这个脚本前先读）
================================

1. **默认只读** —— 用 ``file:...?mode=ro`` 打开，物理上写不进去
2. **``--fix`` 也不动原库** —— 复制到临时目录再跑，原文件一个字节都不变
3. **不猜** —— 认不出的库直接说「不是本项目的库」，不尝试「修复」
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_NEEDS_WORK = 1
EXIT_NOT_OURS = 2

#: 期望的表（与 starwatt.db.schema.TABLE_NAMES 对齐）。写死一份是为了
#: 「体检本身不依赖运行环境」，实际比对用的是现场建出来的空库结构。
_TABLE_HINT = (
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
)


class Report:
    """体检结果收集器（顺便决定退出码）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.path = ""
        self.missing_tables: list[str] = []
        self.missing_columns: dict[str, list[str]] = {}
        self.extra_columns: dict[str, list[str]] = {}
        self.unknown_tables: list[str] = []
        self.counts: dict[str, int] = {}
        self.schema_version = 0
        self.fatal = ""
        #: ``fatal`` 非空时的退出码（显式设置，**不要**靠匹配错误文案 —— 试过，
        #: 「认不出这是本项目的库」里并没有「不是」两个字，于是被判成「需人工」）
        self.fatal_code = EXIT_NEEDS_WORK

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def usable(self) -> bool:
        """能不能直接起服务（缺表可建、缺列由 migration 补，都算能用）。"""
        return not self.fatal

    @property
    def exit_code(self) -> int:
        return self.fatal_code if self.fatal else EXIT_OK


def _read_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """``{表名: [列名]}``（跳过 sqlite 内部表）。"""
    schema: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for row in rows:
        table = row["name"]
        columns = [info["name"] for info in conn.execute(f"PRAGMA table_info({table})")]
        schema[table] = columns
    return schema


def _open_readonly(path: Path) -> sqlite3.Connection:
    """只读打开（写操作会被 SQLite 自己拒绝）。"""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class _settings_scope:
    """临时把全局 Settings 指向某个库，退出时恢复。

    ⚠️ **不能靠改 ``os.environ``**：``db_path()`` 读的是
    ``config.get_settings()`` 这个**懒加载单例**，环境变量在它第一次被取用
    之后就再也影响不了它了。踩过的坑：本脚本最初用 ``os.environ["DB_PATH"]``
    来建基准库，结果 ``init()`` 会打到**用户真实的库**上 —— 直接违背
    「默认只读」。这里用与测试相同的 ``override_settings`` 机制。
    """

    def __init__(self, data_dir: Path, db_file: Path) -> None:
        self._data_dir = data_dir
        self._db_file = db_file
        self._previous = None

    def __enter__(self):
        from starwatt import config as cfg

        self._previous = cfg.get_settings()  # 先物化，避免恢复到 None
        cfg.override_settings(
            cfg.load_settings(
                {
                    "DORM_DATA_DIR": str(self._data_dir),
                    "DB_PATH": str(self._db_file),
                    # 给个假密钥：避免任何代码路径以为密钥缺失而写真实 .env
                    "FLASK_SECRET_KEY": "check-db-not-a-real-key",
                }
            )
        )
        return self

    def __exit__(self, *exc_info) -> None:
        from starwatt import config as cfg
        from starwatt.config_registry import store as cfg_store

        cfg_store.invalidate()  # 配置缓存是进程级全局，换库必须清
        cfg.override_settings(self._previous)


def _expected_schema() -> dict[str, list[str]]:
    """新代码期望的表 → 列（现场建一个空库来取，不复制 DDL 以免两边漂移）。"""
    tmp = tempfile.mkdtemp(prefix="starwatt_expected_")
    fresh = Path(tmp) / "fresh.db"
    try:
        with _settings_scope(Path(tmp), fresh):
            from starwatt.db.connection import init

            init()
            conn = sqlite3.connect(str(fresh))
            conn.row_factory = sqlite3.Row
            try:
                return _read_schema(conn)
            finally:
                conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------
def _read_version(conn: sqlite3.Connection) -> int:
    """读 ``meta.schema_version``；表不存在/值为空 → 0（= 老库）。"""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    if row is None or not row["value"]:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _read_target(path: Path, report: Report) -> dict[str, list[str]] | None:
    """打开并读取目标库结构；失败时写 ``report.fatal`` 并返回 ``None``。"""
    report.path = str(path)
    if not path.is_file():
        report.fatal = f"文件不存在：{path}"
        return None

    report.say(f"文件          ：{path}")
    report.say(f"大小          ：{path.stat().st_size / 1024:,.1f} KB")

    try:
        conn = _open_readonly(path)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        report.fatal = f"不是 SQLite 数据库（或已损坏）：{exc}"
        report.fatal_code = EXIT_NOT_OURS
        return None

    try:
        actual = _read_schema(conn)
        report.schema_version = _read_version(conn)
    finally:
        conn.close()

    if not actual:
        report.fatal = "库里没有任何表（空的）—— 不是本项目的库"
        report.fatal_code = EXIT_NOT_OURS
        return None
    if not (set(actual) & set(_TABLE_HINT)):
        report.fatal = (
            f"认不出这是本项目的库（{len(_TABLE_HINT)} 张预期表一张都没有）—— "
            f"实际表：{', '.join(sorted(actual))}"
        )
        report.fatal_code = EXIT_NOT_OURS
        return None
    return actual


def _diff_schema(actual: dict[str, list[str]], report: Report) -> None:
    """把「缺表 / 缺列 / 多列 / 不认得的表」填进 report。"""
    expected = _expected_schema()
    for table in sorted(expected):
        if table not in actual:
            report.missing_tables.append(table)
            continue
        missing = [c for c in expected[table] if c not in actual[table]]
        extra = [c for c in actual[table] if c not in expected[table]]
        if missing:
            report.missing_columns[table] = missing
        if extra:
            report.extra_columns[table] = extra
    report.unknown_tables = sorted(set(actual) - set(expected))


def _count_rows(path: Path, actual: dict[str, list[str]], report: Report) -> None:
    """统计关键表的行数（给用户一个「数据还在」的直观感受）。"""
    try:
        conn = _open_readonly(path)
        for table in ("records", "daily_elec", "violations", "pay_history", "run_status"):
            if table in actual:
                report.counts[table] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"]
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()


def inspect(path: Path, report: Report) -> None:
    """读一遍目标库，把差异填进 ``report``。**不写任何东西**。"""
    actual = _read_target(path, report)
    if actual is None:
        return
    _diff_schema(actual, report)
    _count_rows(path, actual, report)


def _version_line(version: int, target: int) -> str:
    """把 schema_version 翻译成一句人话。"""
    if version == 0:
        desc = "老库（没有 schema_version 记录：v1 基线或更早）"
    elif version == target:
        desc = f"已是最新（v{version}）"
    elif version < target:
        desc = f"较旧（v{version} → 需升到 v{target}）"
    else:
        desc = f"⚠️ 比当前代码还新（v{version} > v{target}）—— 你可能在用旧代码读新库"
    return f"schema_version：{version}（{desc}）"


def _table_lines(report: Report) -> list[str]:
    """每张表一行：状态 / 行数 / 差异说明。"""
    lines: list[str] = []
    for table in sorted(set(_TABLE_HINT) | set(report.missing_tables)):
        if table in report.missing_tables:
            lines.append(f"  ❌ {table:<15} 整张表缺失 —— 启动时会自动创建")
            continue
        notes = []
        if table in report.missing_columns:
            notes.append(f"缺列 {', '.join(report.missing_columns[table])}")
        if table in report.extra_columns:
            notes.append(f"多列 {', '.join(report.extra_columns[table])}（无害）")
        count = report.counts.get(table)
        rows = f"{count:,} 行" if count is not None else ""
        mark = "⚠ " if table in report.missing_columns else "✅"
        lines.append(f"  {mark} {table:<15} {rows:<12} {'；'.join(notes)}")
    if report.unknown_tables:
        lines.append("")
        lines.append(f"  ℹ️  新代码不认的表（不影响运行）：{', '.join(report.unknown_tables)}")
    return lines


def _verdict_lines(report: Report, target: int) -> list[str]:
    """结论 + （需要时）启动时会自动做哪些事。"""
    if not report.missing_tables and not report.missing_columns:
        return ["结论：✅ 可以直接用。结构与当前代码一致，启动不会改动任何数据。"]

    lines = ["结论：✅ 可以直接用。启动时 init() 会自动补齐，**不动你的数据**："]
    step = 1
    if report.missing_tables:
        lines.append(f"  {step}. 建缺失的表（CREATE TABLE IF NOT EXISTS）")
        step += 1
    if report.missing_columns:
        pairs = "、".join(
            f"{table}.{col}"
            for table, cols in report.missing_columns.items()
            for col in cols
        )
        lines.append(f"  {step}. 给这些表加列（ALTER TABLE ADD COLUMN）：{pairs}")
        step += 1
    lines.append(f"  {step}. 把 meta.schema_version 写成 {target}")
    lines.append("")
    lines.append("建议先备份，再在副本上验证一遍：")
    lines.append(f"  python -m scripts.check_db {report.path} --fix")
    return lines


def render(report: Report) -> str:
    """把体检结果变成给人看的文本。"""
    if report.fatal:
        return f"结论：❌ {report.fatal}"

    from starwatt.db import schema as _schema

    target = _schema.SCHEMA_VERSION
    report.say(_version_line(report.schema_version, target))
    report.say()
    report.say("表与列：")
    report.lines.extend(_table_lines(report))
    report.say()
    report.lines.extend(_verdict_lines(report, target))
    return "\n".join(report.lines)


# ---------------------------------------------------------------------------
# --fix：在副本上真跑一遍升级（原库不动）
# ---------------------------------------------------------------------------
def fix_on_copy(path: Path) -> tuple[int, str]:
    """复制一份 → 在副本上跑真实 ``init()`` → 比对数据与结构。

    返回 ``(退出码, 报告文本)``。**绝不写原库。**
    """
    lines: list[str] = []
    tmp = tempfile.mkdtemp(prefix="starwatt_fix_")
    copy = Path(tmp) / path.name
    shutil.copy2(path, copy)
    # SQLite 的 WAL 边车文件也要一起带，否则副本可能看不到最近的写入
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, copy.with_name(copy.name + suffix))

    before = Report()
    inspect(copy, before)
    if before.fatal:
        shutil.rmtree(tmp, ignore_errors=True)
        return EXIT_NOT_OURS, f"副本体检就失败了：{before.fatal}"

    counts_before = dict(before.counts)

    # 让 app 指向副本，跑真实的启动路径（建表 + migration）
    try:
        with _settings_scope(Path(tmp), copy):
            from starwatt.db.connection import init

            init()
    except Exception as exc:  # noqa: BLE001 - 报告里要原样带上原因
        shutil.rmtree(tmp, ignore_errors=True)
        return EXIT_NEEDS_WORK, f"升级副本时出错：{type(exc).__name__}: {exc}"

    after = Report()
    inspect(copy, after)

    lines.append(f"在副本上跑了一遍 init()（原库未改动）：{copy}")
    lines.append(f"  schema_version：{before.schema_version} → {after.schema_version}")
    if before.missing_tables:
        lines.append(f"  新建的表：{', '.join(before.missing_tables)}")
    if before.missing_columns:
        pairs = "、".join(
            f"{t}.{c}" for t, cols in before.missing_columns.items() for c in cols
        )
        lines.append(f"  补上的列：{pairs}")

    lines.append("  行数对比（升级前 → 升级后）：")
    changed = False
    for table, old in counts_before.items():
        new = after.counts.get(table, 0)
        flag = "✅" if old == new else "❌"
        if old != new:
            changed = True
        lines.append(f"    {flag} {table:<12} {old:,} → {new:,}")

    remaining = after.missing_tables + [
        f"{t}.{c}" for t, cols in after.missing_columns.items() for c in cols
    ]
    if remaining or changed:
        lines.append(f"结论：❌ 还有问题，需要人工看：{remaining or '行数发生变化'}")
        code = EXIT_NEEDS_WORK
    else:
        lines.append("结论：✅ 副本升级干净 —— 数据一行不少、结构已与当前代码一致。")
        lines.append("       你**不需要**手动替换：直接拿原库启动服务，init() 会做同样的事。")
        lines.append("       （要稳妥的话：先备份原库，再启动。）")
        code = EXIT_OK

    shutil.rmtree(tmp, ignore_errors=True)
    return code, "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.check_db",
        description="检查 records.db 与当前代码的表结构是否兼容（默认只读）",
    )
    parser.add_argument("db", nargs="?", help="数据库路径（默认取环境变量 DB_PATH）")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="在**副本**上跑一遍真实的建表与迁移，验证升级是否干净（原库不动）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args(argv)

    raw = args.db or os.environ.get("DB_PATH") or ""
    if not raw:
        print("用法：python -m scripts.check_db <records.db 的路径>", file=sys.stderr)
        return EXIT_NEEDS_WORK
    path = Path(raw).expanduser().resolve()

    report = Report()
    inspect(path, report)
    text = render(report)

    if args.quiet:
        for line in text.splitlines():
            if line.startswith("结论"):
                print(line)
                break
        else:
            print(text.splitlines()[-1])
    else:
        print("StarWatt 星瓦 · 数据库兼容性自检")
        print("=" * 46)
        print(text)

    code = report.exit_code
    if args.fix and report.usable:
        print()
        print("-" * 46)
        code, fix_text = fix_on_copy(path)
        print(fix_text)
    return code


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())

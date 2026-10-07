"""分层与安全守卫（REWRITE_PLAN §1.1 的 R1-R7）。

用法::

    python -m scripts.ast_guard

退出码：0 = 通过，1 = 违规。

M0 阶段 ``starwatt/`` 尚不存在，本脚本会**空跑通过** —— 它的作用是从
M1 起为分层规则立规矩。

每条规则一个函数，便于单独测试与扩展（R6 行长由 ruff 负责，不在此重复）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "starwatt"

VIOLATIONS: list[str] = []

# R1: domain 层禁止 import 这些
DOMAIN_FORBIDDEN = {"requests", "flask", "sqlite3", "starwatt.db", "starwatt.config_registry"}

# R4: starwatt/** 禁止 import 顶层旧模块
LEGACY_TOP = {"web", "dorm_power", "feishu_bot", "config", "db"}

# R7: 禁止裸用时间函数（唯一例外：starwatt/timeutil.py）
TIME_CALLS = {"now", "today", "utcnow"}
TIME_ALLOWED_FILES = {"starwatt/timeutil.py"}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


# ---------------------------------------------------------------------------
# R1 —— domain 层零 IO
# ---------------------------------------------------------------------------
def _check_r1(rel: str, imports: set[str]) -> None:
    if not rel.startswith("starwatt/domain/"):
        return
    for bad in DOMAIN_FORBIDDEN:
        if any(i == bad or i.startswith(bad + ".") for i in imports):
            VIOLATIONS.append(f"[R1] {rel}: domain 层禁止 import {bad}")


# ---------------------------------------------------------------------------
# R4 —— 不得回引旧顶层模块
# ---------------------------------------------------------------------------
def _check_r4(rel: str, imports: set[str]) -> None:
    if not rel.startswith("starwatt/"):
        return
    for bad in LEGACY_TOP:
        if bad in imports:
            VIOLATIONS.append(f"[R4] {rel}: 禁止 import 顶层旧模块 {bad}")


# ---------------------------------------------------------------------------
# R5 —— 禁止跨模块 import 下划线私有符号
# ---------------------------------------------------------------------------
def _check_r5(rel: str, tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name.startswith("_") and not alias.name.startswith("__"):
                VIOLATIONS.append(
                    f"[R5] {rel}:{node.lineno}: 禁止 import 私有符号 {alias.name}"
                )


# ---------------------------------------------------------------------------
# R7 —— 禁止裸用 datetime.now() / date.today()
# ---------------------------------------------------------------------------
def _check_r7(rel: str, tree: ast.AST) -> None:
    if rel in TIME_ALLOWED_FILES:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in TIME_CALLS:
            continue
        # 允许 datetime.now(tz) —— 显式带时区参数
        if func.attr in ("now", "utcnow") and node.args:
            continue
        VIOLATIONS.append(
            f"[R7] {rel}:{node.lineno}: 禁止裸用 .{func.attr}()"
            f"（请用 starwatt.timeutil.now_cst()）"
        )


def _check_file(path: Path) -> None:
    rel = _rel(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        VIOLATIONS.append(f"[syntax] {rel}:{exc.lineno}: {exc.msg}")
        return

    imports = _imports(tree)
    _check_r1(rel, imports)
    _check_r4(rel, imports)
    _check_r5(rel, tree)
    _check_r7(rel, tree)


def main() -> int:
    if not PKG.exists():
        print("starwatt/ 不存在 —— M0 阶段正常，跳过")
        return 0

    for path in sorted(PKG.rglob("*.py")):
        _check_file(path)

    if VIOLATIONS:
        print("AST 守卫失败：")
        for item in VIOLATIONS:
            print("  " + item)
        return 1

    print("AST 守卫通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

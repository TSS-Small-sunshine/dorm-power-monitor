"""分层与安全守卫（REWRITE_PLAN §1.1 的 R1-R7 + M1 追加的 R8）。

用法::

    python -m scripts.ast_guard

退出码：0 = 通过，1 = 违规。

规则一览
========

==== ==========================================================
R1   ``starwatt/domain/`` 零 IO（禁止 requests / flask / sqlite3 …）
R4   ``starwatt/**`` 禁止回引旧顶层模块（web / db / feishu_bot …）
R5   禁止跨模块 import 下划线私有符号
R7   禁止裸用 ``datetime.now()`` / ``date.today()``
     （唯一例外 ``starwatt/timeutil.py``；**统一走 ``timeutil.now_cst()``**，
     这样 conftest 的 ``frozen_now`` 才能一处 patch 全局生效）
R8   **禁止 UTF-8 BOM** —— Python 3 会因它直接 ``SyntaxError``
==== ==========================================================

R8 是 M1 实战教训：Windows 上 ``Set-Content -Encoding UTF8`` 会静默加 BOM，
本地 ``pytest`` 报出莫名其妙的 ``invalid non-printable character U+FEFF``。
现在它在 CI 就会被拦下。

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

#: UTF-8 BOM —— Python 3 会因它抛 SyntaxError（R8）
BOM = b"\xef\xbb\xbf"

#: R8 扫描范围：整个仓库的 .py，但跳过这些目录
BOM_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".ruff_cache"}


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
    raw = path.read_bytes()

    # R8 —— UTF-8 BOM 会让 Python 3 直接 SyntaxError
    # （Windows 的 `Set-Content -Encoding UTF8` / 某些编辑器会自动加上）
    if raw.startswith(BOM):
        VIOLATIONS.append(
            f"[R8] {rel}:1: 文件带 UTF-8 BOM —— 请保存为无 BOM 的 UTF-8"
        )
        raw = raw[len(BOM) :]

    try:
        tree = ast.parse(raw.decode("utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        line = getattr(exc, "lineno", 0) or 0
        msg = getattr(exc, "msg", None) or str(exc)
        VIOLATIONS.append(f"[syntax] {rel}:{line}: {msg}")
        return

    imports = _imports(tree)
    _check_r1(rel, imports)
    _check_r4(rel, imports)
    _check_r5(rel, tree)
    _check_r7(rel, tree)


def _check_bom_everywhere() -> None:
    """R8 —— 扫描仓库内**所有** ``.py``（含 ``tests/`` 与 ``scripts/``）。

    ``starwatt/`` 之外的脚本同样会被 Python 导入执行，BOM 一样致命。
    """
    for path in sorted(ROOT.rglob("*.py")):
        parts = set(path.relative_to(ROOT).parts)
        if parts & BOM_SKIP_DIRS:
            continue
        rel = _rel(path)
        if rel.startswith("starwatt/"):  # 已由 _check_file 覆盖
            continue
        if path.read_bytes().startswith(BOM):
            VIOLATIONS.append(
                f"[R8] {rel}:1: 文件带 UTF-8 BOM —— 请保存为无 BOM 的 UTF-8"
            )


def main() -> int:
    _check_bom_everywhere()

    if not PKG.exists():
        print("starwatt/ 不存在 —— M0 阶段正常，跳过")
        return _report()

    for path in sorted(PKG.rglob("*.py")):
        _check_file(path)

    return _report()


def _report() -> int:
    if VIOLATIONS:
        print("AST 守卫失败：")
        for item in VIOLATIONS:
            print("  " + item)
        return 1

    print("AST 守卫通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

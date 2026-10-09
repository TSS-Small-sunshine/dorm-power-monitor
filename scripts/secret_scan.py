"""凭据泄漏扫描 —— 找「看起来像真凭据」的赋值。

用法::

    python -m scripts.secret_scan

退出码：0 = 干净，1 = 疑似泄漏（CI 会因此失败）。

为什么不用 ``grep -rE '[A-Za-z0-9]{32,}'``
=========================================

那条规则会对**任何**长字符串告警 —— 测试里的假哈希、base64 样例、
示例 UUID 全都会命中。噪音一大，人就学会直接忽略这条 CI 步骤，
守卫就死了。

本脚本只匹配**赋值形态**：``<敏感键名> = <24 位以上值>``。
这样既能抓到真的硬编码凭据，又不会对测试数据吠叫。

已知盲区
========

* Python **字典字面量**里的凭据（``{"API_KEY": "sk-..."}``）不会被抓到 ——
  为了不对 ``{"FLASK_SECRET_KEY": "test-..."}`` 这类测试夹具误报，
  分隔符**不允许**被引号包住。硬编码凭据基本都写成 ``KEY = "value"``，
  所以这个取舍是划算的。
* 高熵但无关键词的凭据（裸 token 变量名）抓不到 —— 本脚本是**绊线**，
  不是保证。真正的防线是「凭据只存 DB + AES-GCM 加密」（Q15/Q17）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 敏感键名（大小写敏感，覆盖本项目与常见约定）
KEY_NAMES = (
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "API_?KEY",
    "APP_ID",
    "OPENID",
    "PRIVATE_KEY",
    "ACCESS_KEY",
)

#: 值至少要这么长才可能是真凭据（占位符如 `changeme` 不会命中）
MIN_VALUE_LEN = 24

#: 赋值形态：KEY [= 或 :] 可选引号 + 长值。捕获组 = 值本身
_PATTERN = re.compile(
    r"(?:" + "|".join(KEY_NAMES) + r")[A-Za-z_]*"
    r"[ \t]*[:=][ \t]*[\"']?"
    r"([A-Za-z0-9+/=_-]{" + str(MIN_VALUE_LEN) + r",})"
)

#: 值**本身**是全大写标识符 → 它是「环境变量键名常量」，不是凭据。
#: 例如 ``ENV_BOOTSTRAP_PASSWORD = "BOOTSTRAP_ADMIN_PASSWORD"``。
_IDENTIFIER_LIKE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: 扫描这些后缀
#: ⚠️ 含 ``.env`` —— 注意 ``Path(".env").suffix`` 是 **空串**（点文件没有后缀），
#: 所以下面用 :func:`_effective_suffix` 按「第一个点之后」判断，
#: 否则最该被扫的文件反而会被跳过（本脚本第一版就漏了 ``.env``）。
SUFFIXES = {
    ".py",
    ".md",
    ".yml",
    ".yaml",
    ".json",
    ".env",
    ".env.example",
    ".sh",
    ".example",
    ".txt",
}


#: 按**文件名**整名跳过。
#:
#: ``.env`` 是 `.gitignore` 里的本地文件，**按设计**就装着真凭据 ——
#: 每次扫描都告警纯属噪音，会训练人忽略这个检查。
#: 而 ``.env.example``（会提交进仓库）**仍然扫描**，见 SUFFIXES。
SKIP_NAMES = {".env"}


def _effective_suffix(name: str) -> str:
    """``.env`` → ``.env``；``.env.example`` → ``.env.example``；``x.py`` → ``.py``。

    与 :attr:`Path.suffix` 的差别：点文件（``.env``）也能正确返回后缀。
    """
    dot = name.find(".")
    return name[dot:] if dot != -1 else ""

#: 跳过这些目录
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
}

#: 允许出现的例外（正则匹配到这些行视为正常）
ALLOWLIST = (
    # 自举密钥自测用的假值
    "test-secret-key-not-for-production",
    # 文档里教用户怎么生成密钥
    "secrets.token_hex",
)


def scan(root: Path = ROOT) -> list[str]:
    """返回 ``["相对路径:行号: 行内容", ...]``，空列表表示干净。"""
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in SKIP_NAMES:
            continue
        if _effective_suffix(path.name) not in SUFFIXES:
            continue
        parts = set(path.relative_to(root).parts)
        if parts & SKIP_DIRS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            match = _PATTERN.search(line)
            if match is None:
                continue
            # 值是全大写标识符 → 键名常量，不是凭据
            if _IDENTIFIER_LIKE.match(match.group(1)):
                continue
            if any(ok in line for ok in ALLOWLIST):
                continue
            rel = path.relative_to(root).as_posix()
            findings.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    return findings


def main() -> int:
    # Windows 控制台默认 GBK，若报告内容含 BOM / emoji 会 UnicodeEncodeError 崩掉。
    # 扫描工具**绝不能**因为「报告里有奇怪字符」而失败。
    # 注意：只放宽 errors，**不**改 encoding —— 强行改 UTF-8 会让中文在
    # GBK 控制台上变乱码（本脚本第一版就踩了这个坑）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    findings = scan()
    if findings:
        print("疑似凭据泄漏：")
        for item in findings:
            print("  " + item)
        return 1
    print("凭据扫描通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

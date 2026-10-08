"""自助配置（N4）—— 用户粘贴 H5 URL，自动解析 + 验证 + 落库。

流程
====

::

    用户粘贴 H5 URL
       ↓
    parse_url(url)   ← SSRF 4 层防护（复用 scraper.ssrf）+ 参数提取
       ↓  {openid, room_id, room_no, eqprice, base_url}
    verify(...)      ← 用解析结果**真跑一次 F1**，验证凭据可用
       ↓
    commit(...)      ← 写入配置（dorm_openid / dorm_room_id / eqprice）

安全约束（**沿用 legacy R37 B5 防护，不得简化**）
================================================

1. **scheme 白名单**：只允许 ``http`` / ``https``（:func:`starwatt.scraper.ssrf.check_scheme`）
2. **host 形态校验**：拒绝畸形主机名（``check_host_shape``）
3. **IP 字面量拒绝**：内网 / 回环 / 链路本地一律拒绝（``is_blocked_ip``）
4. **DNS 解析后逐 IP 复查** + 重定向由 ``SchoolClient`` 限制（``assert_public_url``）

再加一条本项目特有的白名单：

5. **host 必须与已配置的 ``dorm_base_url`` 同源**（除非后者还是出厂占位值）
   —— 否则「粘贴一个 URL 就把凭据发到别人服务器」会成为钓鱼入口。
   首次 OOBE 时 ``dorm_base_url`` 仍是占位值，此时**采纳**粘贴 URL 的 host。

📌 ``verify`` **不写任何配置**（验证失败不该留下半截状态）；``commit`` 才落库。
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from starwatt.config_registry import get_bool, get_str, set_state
from starwatt.scraper.client import FINDUSER_PATH, ScrapeError
from starwatt.scraper.endpoints import F1Live, discover_room
from starwatt.scraper.ssrf import SsrfError, assert_public_url

logger = logging.getLogger("starwatt.setup")

__all__ = [
    "DEFAULT_BASE_URL",
    "commit",
    "parse_url",
    "verify",
]

#: 出厂占位地址（与注册表 ``dorm_base_url`` 的 default 一致）
DEFAULT_BASE_URL = "https://xydf.xxx.edu.cn"

#: URL 查询参数 → 规范名（学校的参数名大小写 / 拼写都不统一）
_PARAM_ALIASES: dict[str, str] = {
    "openid": "openid",
    "roomid": "room_id",
    "roomno": "room_no",
    "eqprice": "eqprice",
    "roomlabel": "room_label",
}


def _pick(query: dict[str, list[str]], name: str) -> str:
    """按别名取第一个非空参数值。"""
    for raw_key, values in query.items():
        if raw_key.lower() != name:
            continue
        for value in values:
            if value and value.strip():
                return value.strip()
    return ""


def _check_host_whitelist(host: str) -> None:
    """host 白名单（约束 5）。

    Raises:
        ValueError: 与已配置的 ``dorm_base_url`` 不同源。
    """
    if not host:
        return  # 交给 SSRF 层给出「缺少主机名」这类更准确的报错
    configured = get_str("dorm_base_url", DEFAULT_BASE_URL).strip()
    if not configured or configured == DEFAULT_BASE_URL:
        return  # 还没配过（或仍是出厂占位值）→ 采纳粘贴 URL 的 host
    configured_host = urlparse(configured).hostname or ""
    if configured_host and configured_host.lower() != host.lower():
        raise ValueError(
            f"这个 URL 指向 {host}，但当前配置的学校地址是 {configured_host} —— "
            "为避免凭据被发往其它服务器，请先在配置里改「学校接口地址」"
        )


def parse_url(url: str) -> dict[str, Any]:
    """解析用户粘贴的 H5 URL。

    Args:
        url: 形如 ``https://xydf.xxx.edu.cn/...?openid=xxx&roomId=yyy`` 的地址。

    Returns:
        ``{"openid", "room_id", "room_no", "room_label", "eqprice", "base_url"}``
        （未出现的字段为空串）。

    Raises:
        ValueError: 地址非法 / 被 SSRF 防护拒绝 / host 不在白名单内。
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("请粘贴学校 H5 页面的完整地址")
    if "://" not in raw:
        raw = "https://" + raw  # 用户常常只粘 ``xydf.xxx.edu.cn/...``

    # 白名单先判（纯本地判断）：错误信息更准，也不必为此做 DNS 查询
    _check_host_whitelist((urlparse(raw).hostname or "").lower())

    try:
        # 4 层 SSRF 防护（scheme / host 形态 / IP 字面量 / DNS 解析）
        assert_public_url(raw, allow_private=get_bool("allow_private_hosts", False))
    except SsrfError as exc:
        raise ValueError(f"地址被安全策略拒绝：{exc}") from exc

    parsed = urlparse(raw)
    host = parsed.hostname or ""

    query = parse_qs(parsed.query, keep_blank_values=False)
    canonical: dict[str, str] = {}
    for key, values in query.items():
        name = _PARAM_ALIASES.get(key.lower().replace("_", ""))
        if name is None:
            continue
        for value in values:
            if value and value.strip():
                canonical.setdefault(name, value.strip())

    eqprice_raw = canonical.get("eqprice", "")
    try:
        eqprice = float(eqprice_raw) if eqprice_raw else None
    except ValueError:
        eqprice = None

    result = {
        "openid": canonical.get("openid", ""),
        "room_id": canonical.get("room_id", ""),
        "room_no": canonical.get("room_no", ""),
        "room_label": canonical.get("room_label", ""),
        "eqprice": eqprice,
        "base_url": f"{parsed.scheme}://{parsed.netloc}",
    }
    logger.info(
        "解析学校 URL：host=%s room_id=%s openid=%s",
        host,
        result["room_id"] or "-",
        "有" if result["openid"] else "无",
    )
    return result


# ---------------------------------------------------------------------------
# 验证（真跑一次 F1，不写任何配置）
# ---------------------------------------------------------------------------
def verify(
    *,
    openid: str = "",
    room_id: str = "",
    base_url: str = "",
    client=None,
) -> dict[str, Any]:
    """用给定凭据真跑一次 F1，确认「能连上 + 能拿到剩余电量」。

    Args:
        openid: 登录凭据；空则取当前配置。
        room_id: 房间 ID；空则先走 A1 房间自动发现（``finduser`` 页面）。
        base_url: 学校根地址；空则取当前配置。
        client: 注入的 HTTP 客户端（测试用）。

    Returns:
        ``{"ok", "room_id", "room_label", "remain", "read_time", "error"}``
        —— **绝不**回显 openid（Q20）。
    """
    from starwatt.scraper.service import ScrapeContext, build_client

    credential = (openid or get_str("dorm_openid", "")).strip()
    if not credential:
        return {
            "ok": False,
            "room_id": "",
            "room_label": None,
            "remain": None,
            "read_time": "",
            "error": "缺少登录凭据 openid",
        }

    root = (base_url or get_str("dorm_base_url", DEFAULT_BASE_URL)).strip()
    try:
        assert_public_url(root, allow_private=get_bool("allow_private_hosts", False))
    except SsrfError as exc:
        return {
            "ok": False,
            "room_id": "",
            "room_label": None,
            "remain": None,
            "read_time": "",
            "error": f"学校地址被安全策略拒绝：{exc}",
        }

    owns_client = client is None
    http = client if client is not None else build_client()
    try:
        ctx = ScrapeContext(client=http, openid=credential, room_id=(room_id or "").strip())
        label: str | None = None
        if not ctx.room_id:
            info = discover_room(ctx.get(FINDUSER_PATH))
            if not info.room_id:
                return {
                    "ok": False,
                    "room_id": "",
                    "room_label": None,
                    "remain": None,
                    "read_time": "",
                    "error": "凭据无效，或学校页面里找不到房间号（可手工填写房间 ID）",
                }
            ctx.room_id, label = info.room_id, info.room_label

        rows = F1Live().fetch(ctx)
        row = rows[0] if rows else {}
        return {
            "ok": True,
            "room_id": ctx.room_id,
            "room_label": label,
            "remain": row.get("remain"),
            "read_time": row.get("read_time") or "",
            "error": None,
        }
    except ScrapeError as exc:
        return {
            "ok": False,
            "room_id": "",
            "room_label": None,
            "remain": None,
            "read_time": "",
            "error": _sanitize(str(exc), credential),
        }
    except Exception as exc:  # noqa: BLE001 —— 网络层各种异常统一成「验证失败」
        logger.warning("自助配置验证失败：%s", type(exc).__name__)
        return {
            "ok": False,
            "room_id": "",
            "room_label": None,
            "remain": None,
            "read_time": "",
            "error": _sanitize(f"{type(exc).__name__}: {exc}", credential),
        }
    finally:
        if owns_client:
            http.close()


def _sanitize(text: str, secret: str) -> str:
    """把 openid 从消息里抹掉（Q20：secret 不得进 API 响应 / 日志）。"""
    return text.replace(secret, "***") if secret else text


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def commit(
    *,
    openid: str,
    room_id: str = "",
    eqprice: float | None = None,
    base_url: str = "",
) -> dict[str, Any]:
    """把解析 / 验证结果写进配置（经注册表校验 + 审计）。

    Returns:
        :func:`starwatt.services.admin_service.config_update` 的产物，
        另加 ``{"room_id": ...}``。
    """
    from starwatt.services import admin_service

    payload: dict[str, Any] = {}
    if openid:
        payload["dorm_openid"] = openid
    if room_id:
        payload["dorm_room_id"] = room_id
    if eqprice is not None:
        payload["eqprice"] = eqprice
    if base_url:
        payload["dorm_base_url"] = base_url

    result = admin_service.config_update(payload)
    if room_id and "dorm_room_id" not in result["errors"]:
        set_state("last_room_id", room_id)
    result["room_id"] = room_id
    logger.info(
        "自助配置已保存：%d 项（%s）",
        len(result["applied"]),
        "、".join(result["applied"]) or "-",
    )
    return result

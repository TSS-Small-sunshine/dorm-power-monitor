"""推送出口 —— **唯一**的飞书发送实现（消除 L4/L5）。

legacy 有**两份** ``_post_feishu``（``dorm_power`` 与 ``feishu_bot``），
``web.py`` 还直接 ``from dorm_power import _post_feishu`` —— 三处入口、
签名参数不一致、错误处理不一致。重写后全项目只有本模块发请求。

设计要点
========

* **签名**按飞书规范：``sig = base64(HMAC-SHA256(key=ts+"\\n"+secret, msg=b""))``
  —— 官方文档就是把待签串当 **key**、消息体留空，照抄别自作聪明
* **失败不抛**（默认）：一次 webhook 抖动不该影响抓取主流程（K7）
  —— 需要把错误暴露给用户时（OOBE「发送测试消息」）显式传
  ``raise_on_error=True``（B2/B3 的教训：测试按钮曾经永远显示成功）
* **未配置 webhook** → 记 ``NOTICE`` 并返回 ``False``，**不再 print 到 stdout**
  （容器里 stdout 已有结构化日志；旧行为的 print 每 10 分钟刷一次 JSON）
* ``session`` 可注入 → 测试完全不碰网络

``Notifier`` 协议
=================

:class:`WebhookNotifier` 实现 :class:`starwatt.protocols.Notifier`
（``name`` / ``enabled`` / ``send_card`` / ``send_text``）。协议约定：
``send_*()`` **不得向上抛**（失败记 ERROR 日志）—— 单点失败不影响主流程。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any

import requests

from starwatt.config_registry import get_str

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "WebhookNotifier",
    "post_card",
    "post_text",
    "resolve_secret",
    "resolve_webhook",
    "sign",
    "webhook_notifier",
]

#: 单次 webhook 请求超时（秒）—— 飞书侧偶发慢，但绝不该拖住抓取
DEFAULT_TIMEOUT_SEC = 15


def sign(secret: str, *, timestamp: int | None = None) -> tuple[str, str]:
    """飞书自定义机器人的签名。

    Returns:
        ``(timestamp, sign)`` —— 直接塞进 payload 的 ``timestamp`` / ``sign`` 字段。
    """
    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return ts, base64.b64encode(digest).decode("utf-8")


def resolve_webhook() -> str:
    """群机器人 webhook（配置注册表 ``feishu_webhook_url``，已解密）。"""
    return get_str("feishu_webhook_url", "")


def resolve_secret() -> str:
    """群机器人签名密钥（配置注册表 ``feishu_secret``，已解密）；未开启签名为空串。"""
    return get_str("feishu_secret", "")


def _group_push_enabled() -> bool:
    """群推送总开关（``push_group_enabled``）是否打开。

    为什么在**出口处**再判一次
    --------------------------

    :func:`starwatt.flags.should_push` 已经在每条告警路径上判过（总开关 +
    分项开关 + 静默时段）。这里是**第二道**防线：将来有人新加一条推送路径却
    忘了 ``should_push``，总开关仍然生效 —— 这正是 legacy 缺陷 **L21** 的成因
    （开关存在、但某个调用点漏判，于是「关了还在发」）。

    延迟导入 ``flags``：本模块被 ``policies`` / ``channels`` 依赖，保持导入图
    单向、避免任何循环风险。
    """
    from starwatt import flags

    return flags.is_enabled("push_group_enabled")


def _post(
    payload: dict[str, Any],
    *,
    webhook_url: str | None = None,
    secret: str | None = None,
    session: requests.Session | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    raise_on_error: bool = False,
    respect_flags: bool = True,
) -> bool:
    """POST 一个 webhook payload；返回是否成功。

    ``respect_flags=False`` 只给**显式动作**用（管理页「发送测试消息」）：
    用户主动点按钮就是为了验证凭据，此时总开关不该把它拦掉。
    """
    if respect_flags and not _group_push_enabled():
        logger.log(25, "群推送总开关已关闭 —— 静默跳过推送")  # NOTICE
        return False

    webhook = webhook_url if webhook_url is not None else resolve_webhook()
    if not webhook:
        logger.log(  # NOTICE：用户还没配，不是异常
            25,
            "未配置 feishu_webhook_url —— 跳过推送（内容：%s）",
            json.dumps(payload, ensure_ascii=False)[:500],
        )
        return False

    body: dict[str, Any] = dict(payload)
    signing_secret = secret if secret is not None else resolve_secret()
    if signing_secret:
        ts, sig = sign(signing_secret)
        body["timestamp"] = ts
        body["sign"] = sig

    http = session or requests
    try:
        response = http.post(webhook, json=body, timeout=timeout_sec)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("飞书推送失败：%s", exc)
        if raise_on_error:
            raise
        return False

    logger.info("飞书推送成功（msg_type=%s）", payload.get("msg_type", "?"))
    return True


def post_card(
    card: dict[str, Any],
    *,
    webhook_url: str | None = None,
    secret: str | None = None,
    session: requests.Session | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    raise_on_error: bool = False,
    respect_flags: bool = True,
) -> bool:
    """发送交互卡片（**全项目唯一出口**）。

    ``card`` 既可以是 ``{"msg_type": "interactive", "card": {...}}`` 的完整
    payload，也可以是裸的 ``{"header": ..., "elements": ...}``。

    总开关（``push_group_enabled``）关闭时**静默返回 False**（记 NOTICE）——
    这是 Q16「关闭 ≠ 报错」在出口处的兜底，见 :func:`_group_push_enabled`。
    """
    if "msg_type" in card and "card" in card:
        payload = card
    else:
        payload = {"msg_type": "interactive", "card": card}
    return _post(
        payload,
        webhook_url=webhook_url,
        secret=secret,
        session=session,
        timeout_sec=timeout_sec,
        raise_on_error=raise_on_error,
        respect_flags=respect_flags,
    )


def post_text(
    text: str,
    *,
    webhook_url: str | None = None,
    secret: str | None = None,
    session: requests.Session | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    raise_on_error: bool = False,
    respect_flags: bool = True,
) -> bool:
    """发送纯文本（webhook 支持的第二种 msg_type）。"""
    return _post(
        {"msg_type": "text", "content": {"text": text}},
        webhook_url=webhook_url,
        secret=secret,
        session=session,
        timeout_sec=timeout_sec,
        raise_on_error=raise_on_error,
        respect_flags=respect_flags,
    )


class WebhookNotifier:
    """群机器人渠道 —— 实现 :class:`starwatt.protocols.Notifier`。

    ``enabled()`` 读**缓存**（配置注册表 + 开关注册表），不发网络请求；
    因此可以在每次推送前无成本地调用。
    """

    name = "feishu_group"

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session

    def enabled(self) -> bool:
        """总开关打开 **且** webhook 已配置。"""
        from starwatt import flags

        if not flags.is_enabled("push_group_enabled"):
            return False
        return bool(resolve_webhook())

    def send_card(self, card: dict[str, Any]) -> None:
        """发送卡片；失败只记日志（协议约定：不向上抛）。"""
        try:
            post_card(card, session=self._session)
        except Exception:  # noqa: BLE001 —— 见协议：单点失败不得影响主流程
            logger.exception("send_card 失败（已忽略）")

    def send_text(self, text: str) -> None:
        """发送文本；失败只记日志。"""
        try:
            post_text(text, session=self._session)
        except Exception:  # noqa: BLE001
            logger.exception("send_text 失败（已忽略）")


def webhook_notifier(session: requests.Session | None = None) -> WebhookNotifier:
    """构造群机器人渠道（``session`` 仅测试注入）。"""
    return WebhookNotifier(session=session)


# ---------------------------------------------------------------------------
# 飞书应用机器人 API（私聊回复用）—— 与群 webhook 是两套凭据
# ---------------------------------------------------------------------------
#: 飞书 OpenAPI 根地址
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

#: 凭证提前刷新余量（秒）
TOKEN_REFRESH_MARGIN_SEC = 60

#: 应用凭证默认有效期（秒）
DEFAULT_TENANT_TTL = 7200

_token_lock = threading.RLock()
_token_cache: dict[str, Any] = {"value": "", "expires_at": 0.0}


def clear_token_cache() -> None:
    """清空 tenant_access_token 缓存（测试与「改完配置立即生效」用）。"""
    with _token_lock:
        _token_cache["value"] = ""
        _token_cache["expires_at"] = 0.0


def tenant_access_token(*, session: requests.Session | None = None) -> str:
    """取 ``tenant_access_token``（带缓存，过期前 60 秒刷新）。

    Returns:
        凭证；未配置 ``feishu_app_id`` / ``feishu_app_secret`` 时返回空串
        （调用方按「机器人未配置」处理，**不抛**）。
    """
    app_id = get_str("feishu_app_id", "")
    app_secret = get_str("feishu_app_secret", "")
    if not app_id or not app_secret:
        logger.debug("未配置 feishu_app_id / feishu_app_secret")
        return ""

    now = time.time()
    with _token_lock:
        if _token_cache["value"] and now < _token_cache["expires_at"]:
            return str(_token_cache["value"])

    http = session or requests
    try:
        response = http.post(
            f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("取 tenant_access_token 失败：%s", exc)
        return ""

    if not isinstance(payload, dict) or payload.get("code") != 0:
        logger.error("tenant_access_token 业务错误：%s", payload)
        return ""

    token = str(payload.get("tenant_access_token") or "")
    try:
        ttl = int(payload.get("expire") or DEFAULT_TENANT_TTL)
    except (TypeError, ValueError):
        ttl = DEFAULT_TENANT_TTL
    with _token_lock:
        _token_cache["value"] = token
        _token_cache["expires_at"] = now + max(ttl - TOKEN_REFRESH_MARGIN_SEC, 60)
    logger.info("tenant_access_token 已刷新（%d 秒有效）", ttl)
    return token


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def build_image_card(image_key: str, title: str, *, footer: str = "") -> dict:
    """把已上传的图片包成飞书卡片。

    ⚠️ 上传接口返回的字段叫 ``image_key``，但卡片 ``img`` 元素的字段是
    **``img_key``** —— 写错会被飞书以 ``11310 img element must contain
    img_key`` 拒绝（legacy Round 19 踩过的坑）。
    """
    elements: list[dict] = [{"tag": "img", "img_key": image_key}]
    if footer:
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]}
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }


def build_text_card(title: str, text: str, *, footer: str = "") -> dict:
    """纯文本卡片（图片渲染失败时的降级形态之一）。"""
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": text}}
    ]
    if footer:
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]}
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }


def upload_image(png_bytes: bytes, *, session: requests.Session | None = None) -> str:
    """上传 PNG 到 ``im/v1/images``，返回 ``image_key``（失败返回空串）。

    ⚠️ ``image_type`` 必须是 **multipart 表单字段**，不能放 query string
    （legacy Round 15/17 的结论：放 query 会被拒，报缺字段）。
    """
    token = tenant_access_token(session=session)
    if not token or not png_bytes:
        return ""

    http = session or requests
    try:
        response = http.post(
            f"{FEISHU_API_BASE}/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            files={"image": ("card.png", png_bytes, "image/png")},
            data={"image_type": "message"},
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("图片上传失败：%s", exc)
        return ""

    if not isinstance(payload, dict) or payload.get("code") != 0:
        logger.error("图片上传业务错误：%s", payload)
        return ""
    return str((payload.get("data") or {}).get("image_key") or "")


def _send_message(
    receive_id: str,
    msg_type: str,
    content: str,
    *,
    session: requests.Session | None = None,
) -> bool:
    """发一条消息给某个 ``open_id``（``im/v1/messages``）。"""
    token = tenant_access_token(session=session)
    if not token or not receive_id:
        return False

    http = session or requests
    try:
        response = http.post(
            f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type=open_id",
            headers=_auth_headers(token),
            json={"receive_id": receive_id, "msg_type": msg_type, "content": content},
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("发消息失败（%s）：%s", msg_type, exc)
        return False

    if not isinstance(payload, dict) or payload.get("code") != 0:
        logger.error("发消息业务错误（%s）：%s", msg_type, payload)
        return False
    return True


def reply_text(receive_id: str, text: str, *, session: requests.Session | None = None) -> bool:
    """给用户发纯文本。

    ⚠️ ``content`` 必须是**序列化后的 JSON 字符串**（``{"text": ...}``），
    传 dict 会被飞书以 ``230001 Invalid request param`` 拒绝。
    """
    return _send_message(
        receive_id,
        "text",
        json.dumps({"text": text}, ensure_ascii=False),
        session=session,
    )


def reply_card(receive_id: str, card: dict, *, session: requests.Session | None = None) -> bool:
    """给用户发交互卡片（``content`` 同样是 JSON 字符串）。"""
    return _send_message(
        receive_id,
        "interactive",
        json.dumps(card, ensure_ascii=False),
        session=session,
    )

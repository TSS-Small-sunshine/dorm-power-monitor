"""QQ 官方机器人渠道（QQ 开放平台 · 新渠道，Q18 扩展 SOP 的第二个实现）。

为什么能加得这么干净
====================

``REWRITE_PLAN`` §2.12 定的扩展 SOP 是「① 实现 ``Notifier`` ② 注册
③ ``registry.py`` 加凭据项」，本文件正好就是那条路：

1. 实现 :class:`QQNotifier`（``name`` / ``enabled`` / ``send_card`` / ``send_text``）
2. 在 :mod:`starwatt.notify.channels` 注册
3. 凭据项进注册表（``qq_app_id`` / ``qq_app_secret`` / ``qq_bot_secret``）

命令层（:mod:`starwatt.notify.commands`）完全复用 —— QQ 和飞书发同样的
``/状态``，回复同一段文本。

QQ 侧的三条硬规则（都来自官方文档，不是猜的）
===========================================

**1. 取凭证**：``POST https://api.bot.qq.com/app/getAppAccessToken``
``{"appId", "clientSecret"}`` → ``{"access_token", "expires_in"}``；
之后所有调用带 ``Authorization: QQBot {access_token}``。

⚠️ 失败时 **HTTP 仍是 200**，错误在 body 的 ``code`` 里 —— 所以判成功要看
``code``，不能只看状态码（官方文档明确警告过）。

**2. 回调签名（Ed25519）**：入站请求带 ``X-Signature-Ed25519``（hex）与
``X-Signature-Timestamp``，签名体是 ``timestamp + body``（**原始字节**），
公钥由 ``qq_bot_secret`` 按 QQ 的重复规则派生（见 :mod:`.ed25519`）。
URL 验证事件 ``op=13`` 需要我们回 ``{"plain_token", "signature"}``，
其中签名体是 ``event_ts + plain_token``。

**3. 发消息**：``POST /v2/users/{openid}/messages``（单聊）或
``POST /v2/groups/{group_openid}/messages``（群聊）；
被动回复必须带 ``msg_id``（5 分钟内、最多 5 次），``msg_seq`` 用于区分
同一条消息的多次回复（重复的 ``msg_id + msg_seq`` 会被去重而失败）。

富媒体（PNG）走 ``POST .../files`` 拿 ``file_info``，再用 ``msg_type=7`` 发。
本模块的 :meth:`QQNotifier.send_card` 会**先尝试渲染 PNG**，失败才降级成
文本 —— 渲染器（``renderer.py``）就绪后 QQ 侧自动升级为图片，无需改这里。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from starwatt.config_registry import get_str
from starwatt.db.coerce import coerce_str
from starwatt.notify import commands, ed25519

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "DEFAULT_TOKEN_TTL",
    "EVENT_C2C",
    "EVENT_GROUP_AT",
    "OP_DISPATCH",
    "OP_VALIDATION",
    "QQ_API_BASE",
    "QQNotifier",
    "SIGNATURE_HEADERS",
    "Target",
    "access_token",
    "card_to_text",
    "clear_token_cache",
    "handle_event",
    "qq_notifier",
    "remember_target",
    "resolve_target",
    "send_image",
    "send_text",
    "upload_image",
    "validation_response",
    "verify_signature",
]

#: OpenAPI 根地址（官方文档示例用 ``api.bot.qq.com``）
QQ_API_BASE = "https://api.bot.qq.com"

#: 取凭证的路径
TOKEN_PATH = "/app/getAppAccessToken"

#: 凭证默认有效期（秒）—— 官方文档：7200
DEFAULT_TOKEN_TTL = 7200

#: 提前刷新的余量（秒）—— 官方建议过期前 60 秒内刷新
REFRESH_MARGIN_SEC = 60

#: 事件 op 码
OP_DISPATCH = 0
OP_VALIDATION = 13

#: 我们处理的两类消息事件
EVENT_C2C = "C2C_MESSAGE_CREATE"  # 单聊
EVENT_GROUP_AT = "GROUP_AT_MESSAGE_CREATE"  # 群聊 @机器人

#: 签名头
SIGNATURE_HEADER = "X-Signature-Ed25519"
TIMESTAMP_HEADER = "X-Signature-Timestamp"

#: 入站签名相关的头（顺序固定，路由层按它统一读取）
SIGNATURE_HEADERS: tuple[str, str] = (SIGNATURE_HEADER, TIMESTAMP_HEADER)

#: 纯文本消息的 ``msg_type``（0=文本，2=Markdown，7=富媒体）
MSG_TEXT = 0
MSG_MEDIA = 7

#: 富媒体类型（1=图片）
FILE_TYPE_IMAGE = 1

#: 单次请求超时（秒）
DEFAULT_TIMEOUT_SEC = 15

_token_lock = threading.RLock()
_token_cache: dict[str, Any] = {"value": "", "expires_at": 0.0}


@dataclass(frozen=True, slots=True)
class Target:
    """消息目标：单聊（``c2c``）或群聊（``group``）。

    ``msg_id`` 有值时是**被动回复**（5 分钟窗口内最多 5 次）；
    为空则是主动推送（受频控：每群 20/分钟）。
    """

    kind: str
    openid: str
    msg_id: str = ""

    @property
    def path(self) -> str:
        """消息接口路径（``/v2/users/...`` 或 ``/v2/groups/...``）。"""
        scope = "users" if self.kind == "c2c" else "groups"
        return f"/v2/{scope}/{self.openid}"


def clear_token_cache() -> None:
    """清空凭证缓存（测试与「改完配置立即生效」用）。"""
    with _token_lock:
        _token_cache["value"] = ""
        _token_cache["expires_at"] = 0.0


# ---------------------------------------------------------------------------
# 凭证
# ---------------------------------------------------------------------------
def _api_base() -> str:
    """OpenAPI 根地址（可配；沙箱 / 私有化部署指到别处）。"""
    return (get_str("qq_api_base", "") or QQ_API_BASE).rstrip("/")


def access_token(*, session: requests.Session | None = None, force: bool = False) -> str:
    """取 ``access_token``（带缓存，过期前 60 秒自动刷新）。

    Returns:
        凭证字符串；未配置 ``qq_app_id`` / ``qq_app_secret`` 时返回空串
        （调用方按「渠道未配置」处理，**不抛**）。
    """
    app_id = get_str("qq_app_id", "")
    client_secret = get_str("qq_app_secret", "")
    if not app_id or not client_secret:
        logger.debug("QQ 渠道未配置 app_id / app_secret")
        return ""

    now = time.time()
    with _token_lock:
        if not force and _token_cache["value"] and now < _token_cache["expires_at"]:
            return str(_token_cache["value"])

    http = session or requests
    try:
        response = http.post(
            f"{_api_base()}{TOKEN_PATH}",
            json={"appId": app_id, "clientSecret": client_secret},
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("QQ 取凭证失败：%s", exc)
        return ""

    # ⚠️ 业务错误也返回 HTTP 200 —— 必须看 code
    if not isinstance(payload, dict) or payload.get("code"):
        logger.error(
            "QQ 取凭证业务错误：code=%s message=%s",
            payload.get("code") if isinstance(payload, dict) else "?",
            payload.get("message") if isinstance(payload, dict) else "?",
        )
        return ""

    token = str(payload.get("access_token") or "")
    try:
        ttl = int(payload.get("expires_in") or DEFAULT_TOKEN_TTL)
    except (TypeError, ValueError):
        ttl = DEFAULT_TOKEN_TTL
    with _token_lock:
        _token_cache["value"] = token
        _token_cache["expires_at"] = now + max(ttl - REFRESH_MARGIN_SEC, 60)
    logger.info("QQ 凭证已刷新（%d 秒有效）", ttl)
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}


def _post(
    path: str,
    payload: dict[str, Any],
    *,
    session: requests.Session | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """带鉴权的 POST（失败默认**不抛**，与飞书渠道一致）。"""
    token = access_token(session=session)
    if not token:
        return {}

    http = session or requests
    try:
        response = http.post(
            f"{_api_base()}{path}",
            json=payload,
            headers=_headers(token),
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("QQ 接口调用失败（%s）：%s", path, exc)
        if raise_on_error:
            raise
        return {}

    if isinstance(body, dict) and body.get("code"):
        logger.error(
            "QQ 接口业务错误（%s）：code=%s message=%s",
            path,
            body.get("code"),
            body.get("message"),
        )
        if raise_on_error:
            raise RuntimeError(f"QQ API error {body.get('code')}: {body.get('message')}")
        return {}
    return body if isinstance(body, dict) else {}


def send_text(
    target: Target,
    text: str,
    *,
    msg_seq: int = 1,
    session: requests.Session | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """发纯文本消息（``msg_type=0``）。

    ``target.msg_id`` 非空 → 被动回复（必须带 ``msg_id``，否则 5 分钟后
    就不能回；重复的 ``msg_id + msg_seq`` 会被 QQ 去重而失败）。
    """
    payload: dict[str, Any] = {"msg_type": MSG_TEXT, "content": text}
    if target.msg_id:
        payload["msg_id"] = target.msg_id
        payload["msg_seq"] = msg_seq
    return _post(
        f"{target.path}/messages",
        payload,
        session=session,
        raise_on_error=raise_on_error,
    )


def upload_image(
    target: Target,
    image_bytes: bytes,
    *,
    session: requests.Session | None = None,
) -> str:
    """上传图片 → ``file_info``（供 ``msg_type=7`` 使用）。

    Returns:
        ``file_info`` 字符串；失败返回空串。
    """
    token = access_token(session=session)
    if not token:
        return ""

    http = session or requests
    try:
        response = http.post(
            f"{_api_base()}{target.path}/files",
            files={"file_data": ("card.png", image_bytes, "image/png")},
            data={"file_type": FILE_TYPE_IMAGE},
            headers={"Authorization": f"QQBot {token}"},
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("QQ 图片上传失败：%s", exc)
        return ""

    if not isinstance(body, dict) or body.get("code"):
        logger.error("QQ 图片上传业务错误：%s", body)
        return ""
    file_info = str(body.get("file_info") or body.get("file_uuid") or "")
    if not file_info:
        logger.warning("QQ 图片上传返回里没有 file_info：%s", sorted(body))
    return file_info


def send_image(
    target: Target,
    file_info: str,
    *,
    msg_seq: int = 2,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """发富媒体消息（``msg_type=7``）。

    ``msg_seq`` 默认 2：同一条被动消息通常先回文本再补图片，用不同的
    ``msg_seq`` 避免被 QQ 去重（40054005）。
    """
    if not file_info:
        return {}
    payload: dict[str, Any] = {"msg_type": MSG_MEDIA, "media": {"file_info": file_info}}
    if target.msg_id:
        payload["msg_id"] = target.msg_id
        payload["msg_seq"] = msg_seq
    return _post(f"{target.path}/messages", payload, session=session)


# ---------------------------------------------------------------------------
# 入站：签名 / 验证 / 事件
# ---------------------------------------------------------------------------
def verify_signature(
    headers: dict[str, str], body: bytes, *, bot_secret: str = ""
) -> bool:
    """校验入站请求的 Ed25519 签名（**fail-closed**）。

    * 签名体 = ``X-Signature-Timestamp`` + **原始 body 字节**
    * 公钥 = 由 ``qq_bot_secret`` 按 QQ 的重复规则派生

    未配置 ``qq_bot_secret`` 或缺少签名头 → **拒绝**（与飞书渠道同一条原则：
    宁可不回消息，也不能让伪造事件进来）。
    """
    secret = bot_secret or get_str("qq_bot_secret", "")
    if not secret:
        logger.warning("未配置 qq_bot_secret —— 拒绝入站事件（fail-closed）")
        return False

    signature = (headers or {}).get(SIGNATURE_HEADER, "") or ""
    timestamp = (headers or {}).get(TIMESTAMP_HEADER, "") or ""
    if not (signature and timestamp):
        logger.warning("QQ 事件缺少签名头 —— 拒绝（fail-closed）")
        return False

    try:
        raw_signature = bytes.fromhex(signature)
    except ValueError:
        logger.warning("QQ 签名不是合法 hex —— 拒绝")
        return False

    public = ed25519.public_key_from_seed(ed25519.seed_from_secret(secret))
    return ed25519.verify(public, timestamp.encode("utf-8") + body, raw_signature)


def validation_response(body: Any, *, bot_secret: str = "") -> dict[str, Any] | None:
    """``op=13`` 回调地址验证：返回 ``{"plain_token", "signature"}``。

    签名体 = ``event_ts + plain_token``（官方文档的回调验证流程）。
    不是验证事件 / 缺字段 / 未配置密钥 → ``None``。
    """
    if not isinstance(body, dict) or body.get("op") != OP_VALIDATION:
        return None
    data = body.get("d") or {}
    plain_token = str(data.get("plain_token") or "")
    event_ts = str(data.get("event_ts") or "")
    if not plain_token or not event_ts:
        return None

    secret = bot_secret or get_str("qq_bot_secret", "")
    if not secret:
        logger.error("收到回调验证但未配置 qq_bot_secret —— 无法应答")
        return None

    signature = ed25519.sign(
        ed25519.seed_from_secret(secret), (event_ts + plain_token).encode("utf-8")
    ).hex()
    return {"plain_token": plain_token, "signature": signature}


def card_to_text(card: dict[str, Any]) -> str:
    """把飞书卡片降级成纯文本（QQ 没有卡片消息，只有文本/Markdown/富媒体）。

    顺带处理 QQ 的一条硬限制：**群消息不允许包含 URL**（错误码 40054010），
    所以 Markdown 链接会被压成纯文字标签。
    """
    payload = card
    if isinstance(card, dict) and isinstance(card.get("card"), dict):
        payload = card["card"]
    if not isinstance(payload, dict):
        return ""

    lines: list[str] = []
    header = payload.get("header") or {}
    for key in ("title", "subtitle"):
        text = _node_text(header.get(key))
        if text:
            lines.append(text)
    for element in payload.get("elements") or []:
        text = _element_text(element)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _node_text(node: Any) -> str:
    if isinstance(node, dict):
        return str(node.get("content") or "")
    return str(node or "")


def _element_text(element: Any) -> str:
    if not isinstance(element, dict):
        return ""
    tag = element.get("tag")
    if tag == "div":
        return _plain_text(_node_text(element.get("text")))
    if tag == "note":
        return " · ".join(
            _plain_text(_node_text(item)) for item in element.get("elements") or []
        )
    if tag == "column_set":
        cells = []
        for column in element.get("columns") or []:
            for child in (column or {}).get("elements") or []:
                text = _element_text(child)
                if text:
                    cells.append(text.replace("\n", " "))
        return "　".join(cells)
    return ""


def _plain_text(text: str) -> str:
    """去掉 lark_md 的粗体标记，并把链接压成纯文字（QQ 群禁用 URL）。"""
    if not text:
        return ""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).replace("**", "")


# ---------------------------------------------------------------------------
# 目标解析（主动推送需要知道发给谁）
# ---------------------------------------------------------------------------
def _explicit_target() -> Target | None:
    """显式配置的目标（``qq_target_openid`` + ``qq_target_kind``）。"""
    openid = coerce_str(get_str("qq_target_openid", ""))
    if not openid:
        return None
    kind = coerce_str(get_str("qq_target_kind", "")) or "group"
    return Target(kind="c2c" if kind == "c2c" else "group", openid=openid)


def _learned_target() -> Target | None:
    """从**最近一次收到的消息**里学到的目标（``last_qq_target`` 状态键）。

    📌 为什么需要它：QQ 的机器人**不能凭空主动私聊**某个用户 —— 必须先用
    户说过话（平台侧要求）。所以这里「第一次交互即记住会话」，之后 L1/L3
    这些主动告警才有地方可发。用户也可以显式配置 ``qq_target_openid`` 覆盖。
    """
    raw = get_str("last_qq_target", "")
    if not raw or ":" not in raw:
        return None
    kind, _, openid = raw.partition(":")
    if not openid:
        return None
    return Target(kind="c2c" if kind == "c2c" else "group", openid=openid)


def resolve_target() -> Target | None:
    """主动推送的目标：显式配置优先，其次用学到的会话。"""
    return _explicit_target() or _learned_target()


def remember_target(target: Target) -> None:
    """记住一个会话（供后续主动推送）。"""
    from starwatt.config_registry import set_state

    try:
        set_state("last_qq_target", f"{target.kind}:{target.openid}")
    except Exception:  # noqa: BLE001 —— 记不住不影响本次回复
        logger.debug("记录 QQ 会话目标失败", exc_info=True)


# ---------------------------------------------------------------------------
# Notifier 实现
# ---------------------------------------------------------------------------
class QQNotifier:
    """QQ 官方机器人渠道 —— 实现 :class:`starwatt.protocols.Notifier`。

    * ``send_text`` → ``msg_type=0`` 文本消息
    * ``send_card`` → 先尝试渲染 PNG（``renderer``）→ ``msg_type=7`` 富媒体；
      渲染或上传失败则降级成**文本**（:func:`card_to_text`）

    协议约定：``send_*`` **不向上抛**（失败只记 ERROR）。
    """

    name = "qq"

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session

    def enabled(self) -> bool:
        """开关打开 **且** 凭据齐备（读缓存，不发网络请求）。"""
        from starwatt import flags

        if not flags.is_enabled("qq_bot_enabled"):
            return False
        return bool(get_str("qq_app_id", "") and get_str("qq_app_secret", ""))

    def send_text(self, text: str) -> None:
        try:
            target = resolve_target()
            if target is None or not text:
                logger.debug("QQ 渠道没有可用目标（先在 QQ 里跟机器人说句话）")
                return
            send_text(target, text, session=self._session)
        except Exception:  # noqa: BLE001 —— 协议约定：不向上抛
            logger.exception("QQ send_text 失败（已忽略）")

    def send_card(self, card: dict[str, Any]) -> None:
        try:
            target = resolve_target()
            if target is None:
                logger.debug("QQ 渠道没有可用目标，跳过卡片推送")
                return
            image = _render_card(card)
            if image:
                file_info = upload_image(target, image, session=self._session)
                if file_info:
                    send_image(target, file_info, session=self._session)
                    return
            text = card_to_text(card)
            if text:
                send_text(target, text, session=self._session)
        except Exception:  # noqa: BLE001
            logger.exception("QQ send_card 失败（已忽略）")


def _render_card(card: dict[str, Any]) -> bytes | None:
    """把卡片渲染成 PNG 字节；渲染器不可用 / 失败 → ``None``（降级文本）。

    延迟导入：``renderer`` 依赖 Pillow，抓取路径不该被它拖慢。
    """
    try:
        from starwatt.notify import renderer
    except Exception:  # noqa: BLE001 —— 渲染器还没落地或 Pillow 缺失
        return None
    try:
        return renderer.render_card(card)
    except Exception:  # noqa: BLE001
        logger.warning("卡片渲染失败，降级为文本", exc_info=True)
        return None


def qq_notifier(session: requests.Session | None = None) -> QQNotifier:
    """构造 QQ 渠道（``session`` 仅测试注入）。"""
    return QQNotifier(session=session)


# ---------------------------------------------------------------------------
# 事件入口
# ---------------------------------------------------------------------------
def handle_event(
    body: Any,
    *,
    room_id: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """处理一个**已验签**的 QQ 事件，返回给 QQ 的应答体。

    调用方（HTTP 路由）负责：读原始 body → :func:`verify_signature` → 解析
    JSON → 调本函数。这里只做业务分发。

    Returns:
        ``{"code": 0}`` = 正常应答；``{"code": 1, ...}`` = 拒绝 / 无法处理。
    """
    from starwatt import flags

    if not isinstance(body, dict):
        return {"code": 1, "message": "invalid body"}

    op = body.get("op")
    if op == OP_VALIDATION:
        response = validation_response(body)
        return response if response is not None else {"code": 1, "message": "bad token"}

    if op != OP_DISPATCH:
        return {"code": 0}  # 心跳 / 其它 op → 空应答

    event_type = str(body.get("t") or "")
    if event_type not in (EVENT_C2C, EVENT_GROUP_AT):
        return {"code": 0}

    data = body.get("d") or {}
    content = str(data.get("content") or "")
    openid = str(data.get("group_openid") or data.get("user_openid") or "")
    target = Target(
        kind="group" if event_type == EVENT_GROUP_AT else "c2c",
        openid=openid,
        msg_id=str(data.get("id") or ""),
    )
    if not target.openid:
        return {"code": 1, "message": "no openid"}

    if not flags.is_enabled("qq_bot_enabled"):
        logger.log(25, "QQ 机器人总开关已关闭 —— 静默忽略事件")  # NOTICE
        return {"code": 0}

    remember_target(target)

    try:
        reply = commands.dispatch_text(content, room_id or commands.current_room_id())
    except Exception:  # noqa: BLE001 —— 命令层出错不能让 QQ 收到 5xx（它会重试）
        logger.exception("QQ 命令处理失败")
        send_text(target, "⚠ 服务暂时不可用，请稍后再试。", session=session)
        return {"code": 0}

    if reply.is_empty:
        return {"code": 0}

    send_text(target, reply.text, session=session)
    return {"code": 0}

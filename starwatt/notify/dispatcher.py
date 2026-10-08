"""飞书私聊机器人 —— 事件订阅（解密 / 验签 / 命令 / 图片回复）。

一次入站请求的完整链路
======================

::

    POST /feishu/event
      ↓ ① 读**原始 body 字节**（签名体必须是原文）
      ↓ ② 验签（fail-closed）—— 见 :func:`verify_request`
      ↓ ③ 加密事件？→ 解密（:func:`decrypt_body`）
      ↓ ④ url_verification？→ 回 challenge
      ↓ ⑤ 提取消息（文本 / 菜单）→ :mod:`starwatt.notify.commands`
      ↓ ⑥ 回复：**PNG 图片卡片** → 文本卡片 → 纯文本（逐级降级）
      ↓ 200 + {"code": 0, "msg": "ok"}

为什么要三级降级
================

legacy Round 16 的教训：图片链路（Pillow 渲染 → 上传 → 发卡片）任何一步
失败，用户都会**完全收不到回复**，看起来像机器人挂了。所以：

1. 图片卡片（最好看）
2. 文本卡片（图片挂了还能有排版）
3. 纯文本（前两者都挂时的保命路径）

三层全失败才记 ERROR —— 而且此时仍返回 200，因为让飞书重试也不会更好。

事件形状（v2 / v1）
===================

==============================  ==========================================
事件                             关键字段
==============================  ==========================================
``im.message.receive_v1``        ``event.message.content``（**JSON 字符串**
                                 里的 ``text``）、``event.message.message_type``
``application.bot.menu_v6``      ``event.event_key``（对应 ``commands.MENU_KEYS``）
``url_verification``（v1）        ``challenge`` / ``token``
==============================  ==========================================

⚠️ v2 事件**没有**顶层 ``text`` 字段 —— 文本在 ``message.content`` 里且是
JSON 编码的。legacy 曾经读 ``msg.get("text")`` 导致「命令永远解析不到」
（Round 12 修复），这里按正确形状实现。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from starwatt import flags
from starwatt.config_registry import get_str
from starwatt.notify import commands, crypto, renderer, transport

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "MENU_EVENT",
    "MESSAGE_EVENT",
    "Message",
    "decrypt_body",
    "extract_message",
    "handle_event",
    "handle_url_verification",
    "reply",
    "verify_request",
]

#: v2 文本消息事件
MESSAGE_EVENT = "im.message.receive_v1"

#: 自定义菜单点击事件
MENU_EVENT = "application.bot.menu_v6"

#: 保命文案（三级降级都失败时给用户一个交代）
FALLBACK_TEXT = "⚠ 服务暂时不可用，请稍后再试。"


@dataclass(frozen=True, slots=True)
class Message:
    """从事件里提取出的「一条待回复的消息」。"""

    open_id: str
    text: str = ""
    event_type: str = ""
    menu_key: str = ""

    @property
    def is_menu(self) -> bool:
        return self.event_type == MENU_EVENT


# ---------------------------------------------------------------------------
# 验签 / 解密
# ---------------------------------------------------------------------------
def verify_request(
    headers: dict[str, str],
    raw_body: bytes,
    *,
    encrypt_key: str = "",
    verification_token: str = "",
    allow_unsigned: bool = False,
) -> bool:
    """校验入站请求的签名（**fail-closed**，见 ``crypto``）。

    密钥留空时从配置注册表读（``feishu_encrypt_key`` /
    ``feishu_verification_token``）。
    """
    return crypto.verify_lark_signature(
        headers,
        raw_body,
        encrypt_key=encrypt_key or get_str("feishu_encrypt_key", ""),
        verification_token=verification_token or get_str("feishu_verification_token", ""),
        allow_unsigned=allow_unsigned,
    )


def decrypt_body(body: object, *, encrypt_key: str = "") -> dict:
    """加密事件 → 明文事件体；非加密 / 解密失败 → 原样返回空 dict。

    ⚠️ 解密失败**不抛**：宁可当作「无法处理的事件」返回 200，
    也不要给飞书一个 500（它会重试，而重试同样解不开）。
    """
    if not isinstance(body, dict):
        return {}
    if not crypto.is_encrypted(body):
        return body
    key = encrypt_key or get_str("feishu_encrypt_key", "")
    try:
        return crypto.decrypt_payload(str(body.get("encrypt") or ""), key)
    except crypto.CryptoError as exc:
        logger.error("事件解密失败：%s", exc)
        return {}


def handle_url_verification(body: dict, *, verification_token: str = "") -> dict | None:
    """``url_verification`` 握手：返回 ``{"challenge": ...}``。

    不是握手事件 → ``None``；token 不匹配 → ``{"code": 401, ...}``。
    """
    if not isinstance(body, dict) or body.get("type") != "url_verification":
        return None
    token = verification_token or get_str("feishu_verification_token", "")
    if not crypto.verify_token(body, verification_token=token):
        logger.warning("url_verification token 不匹配 —— 拒绝")
        return {"code": 401, "msg": "invalid token"}
    return {"challenge": body.get("challenge", "")}


# ---------------------------------------------------------------------------
# 事件 → 消息
# ---------------------------------------------------------------------------
def _parse_text_content(content: object) -> str:
    """``message.content`` 是 JSON 字符串（``{"text": "..."}``）。"""
    if not isinstance(content, str) or not content:
        return ""
    try:
        parsed = json.loads(content)
    except ValueError:
        return ""
    if isinstance(parsed, dict):
        return str(parsed.get("text") or "")
    return ""


def extract_message(body: dict) -> Message | None:
    """从事件体里提取消息；不是我们处理的事件 → ``None``。"""
    if not isinstance(body, dict):
        return None
    event = body.get("event") or {}
    header = body.get("header") or {}
    event_type = str(header.get("event_type") or event.get("type") or "")
    sender = ((event.get("sender") or {}).get("sender_id") or {})
    open_id = str(sender.get("open_id") or "")

    if event_type == MESSAGE_EVENT:
        message = event.get("message") or {}
        if message.get("message_type") != "text":
            return None  # 图片 / 文件 / 表情包一律不处理
        return Message(
            open_id=open_id,
            text=_parse_text_content(message.get("content")),
            event_type=event_type,
        )

    if event_type == MENU_EVENT:
        return Message(
            open_id=open_id,
            event_type=event_type,
            menu_key=str(event.get("event_key") or ""),
        )

    return None


# ---------------------------------------------------------------------------
# 回复（三级降级）
# ---------------------------------------------------------------------------
def reply(
    message: Message,
    reply_body: commands.Reply,
    *,
    session=None,
) -> bool:
    """把一条 :class:`commands.Reply` 回给用户。

    Returns:
        ``True`` = 至少有一级成功送达。
    """
    if not message.open_id or reply_body.is_empty:
        return False

    if reply_body.title:
        # ① 图片卡片
        try:
            image = renderer.render_text(reply_body.text, title=reply_body.title)
            image_key = transport.upload_image(image, session=session)
            if image_key:
                card = transport.build_image_card(
                    image_key,
                    reply_body.title,
                    footer=f"PNG {len(image)} bytes",
                )
                if transport.reply_card(message.open_id, card, session=session):
                    return True
        except Exception:  # noqa: BLE001 —— 渲染/上传失败要能降级，不能冒泡
            logger.warning("图片卡片链路失败，降级为文本卡片", exc_info=True)

        # ② 文本卡片
        try:
            card = transport.build_text_card(reply_body.title, reply_body.text)
            if transport.reply_card(message.open_id, card, session=session):
                return True
        except Exception:  # noqa: BLE001
            logger.warning("文本卡片失败，降级为纯文本", exc_info=True)

    # ③ 纯文本（保命路径）
    return transport.reply_text(message.open_id, reply_body.text, session=session)


def handle_event(
    body: object,
    *,
    room_id: str | None = None,
    session=None,
) -> dict:
    """处理一个**已验签**的飞书事件，返回应答体。

    调用方（HTTP 路由）负责：读原始 body → :func:`verify_request` →
    解析 JSON → :func:`decrypt_body` → 调本函数。
    """
    if not isinstance(body, dict):
        return {"code": 1, "msg": "invalid body"}

    challenge = handle_url_verification(body)
    if challenge is not None:
        return challenge

    if not flags.is_enabled(flags.BOT_MASTER):
        logger.log(25, "私聊机器人总开关已关闭 —— 静默忽略事件")  # NOTICE
        return {"code": 0, "msg": "ok"}

    message = extract_message(body)
    if message is None:
        return {"code": 0, "msg": "ok"}

    room = room_id or commands.current_room_id()
    try:
        if message.is_menu:
            reply_body = commands.dispatch_menu(message.menu_key, room)
        else:
            reply_body = commands.dispatch_text(message.text, room)
    except Exception:  # noqa: BLE001 —— 命令层出错不能给飞书 5xx
        logger.exception("飞书命令处理失败")
        transport.reply_text(message.open_id, FALLBACK_TEXT, session=session)
        return {"code": 0, "msg": "ok"}

    if reply_body.is_empty:
        return {"code": 0, "msg": "ok"}

    try:
        reply(message, reply_body, session=session)
    except Exception:  # noqa: BLE001 —— 三层降级都失败时给用户一句交代
        logger.exception("飞书回复全部失败")
        try:
            transport.reply_text(message.open_id, FALLBACK_TEXT, session=session)
        except Exception:  # noqa: BLE001
            logger.exception("保命回复也失败了（放弃）")

    return {"code": 0, "msg": "ok"}

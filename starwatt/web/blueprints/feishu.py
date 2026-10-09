"""平台事件订阅入口（M4 §4.2）—— 飞书 ``/feishu/event`` + QQ ``/qq/events``。

为什么两家共用一个蓝图
======================

``REWRITE_PLAN`` §1.2 的七蓝图清单里这一格叫 ``feishu``，但飞书与 QQ 的回调
**在结构上是同一件事**：

* 都是「平台推给我们」—— 不走会话 Cookie，认证靠**平台签名**
* 都要求 **几秒内回 200**，否则平台重试（重试风暴）
* 都必须 **fail-closed**：验签失败一律 401，绝不「先处理再补验」

所以放同一个蓝图，共用同一套「读原始字节 → 验签 → 解密 → 分发」流程；
QQ 是 M3 追加的第二个平台（自带 ``QQ`` 配置分组与 ``qq_bot_enabled`` 开关）。

路由只做四件事（都在毫秒级）
============================

1. 读**原始 body 字节**（签名体必须是原文，不能用重新序列化的 JSON）
2. 验签（**fail-closed**）—— 验不过就 401
3. 加密事件先解密（飞书 AES；解密失败返回 ``{}`` 交给分发器兜底）
4. 交给 ``handle_event`` 做业务分发

⚠️ 命令处理（读库、渲染 PNG）是同步的。如果将来它变慢，应该改成
「先回 200，再异步回复」，而不是让平台等 —— 这条留在注释里提醒后来人。

⚠️ 机器人总开关关闭时**仍返回 200**（静默降级，见 ``REWRITE_PLAN`` §Q16）：
飞书侧看到 5xx/4xx 会重试并报警，而「机器人关了」是我们的内部状态。
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, jsonify, request

from starwatt.notify import dispatcher, qq

logger = logging.getLogger("starwatt.notify")

bp = Blueprint("feishu", __name__)

__all__ = ["FEISHU_EVENTS_PATH", "QQ_EVENTS_PATH", "bp"]

#: QQ 回调路径（在 QQ 开放平台里填 ``https://<你的域名>/qq/events``）
QQ_EVENTS_PATH = "/qq/events"

#: 飞书事件订阅路径（在飞书开放平台「事件订阅」里填 ``https://<你的域名>/feishu/event``）
FEISHU_EVENTS_PATH = "/feishu/event"


def _signature_headers(names: tuple[str, ...]) -> dict[str, str]:
    """按平台约定的头名取值（缺头返回空串 → 验签必然失败 → 401）。"""
    return {name: request.headers.get(name, "") for name in names}


@bp.post(QQ_EVENTS_PATH)
def qq_events():
    raw = request.get_data() or b""  # 🔑 原始字节，先取再解析
    headers = _signature_headers(qq.SIGNATURE_HEADERS)

    if not qq.verify_signature(headers, raw):
        logger.warning("QQ 回调验签失败 —— 拒绝（%s）", request.remote_addr)
        return jsonify({"code": 1, "message": "invalid signature"}), 401

    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        logger.warning("QQ 回调 body 不是合法 JSON")
        return jsonify({"code": 1, "message": "invalid json"}), 400

    try:
        return jsonify(qq.handle_event(body))
    except Exception:  # noqa: BLE001 —— 绝不让 QQ 收到 5xx（它会重试）
        logger.exception("QQ 事件处理异常")
        return jsonify({"code": 0})


@bp.post(FEISHU_EVENTS_PATH)
def feishu_events():
    raw = request.get_data() or b""
    headers = _signature_headers(dispatcher.crypto.SIGNATURE_HEADERS)

    if not dispatcher.verify_request(headers, raw):
        logger.warning("飞书事件验签失败 —— 拒绝（%s）", request.remote_addr)
        return jsonify({"code": 401, "msg": "invalid signature"}), 401

    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        logger.warning("飞书事件 body 不是合法 JSON")
        return jsonify({"code": 1, "msg": "invalid json"}), 400

    body = dispatcher.decrypt_body(body)  # 加密事件 → 明文（失败返回 {}）

    try:
        return jsonify(dispatcher.handle_event(body))
    except Exception:  # noqa: BLE001
        logger.exception("飞书事件处理异常")
        return jsonify({"code": 0, "msg": "ok"})

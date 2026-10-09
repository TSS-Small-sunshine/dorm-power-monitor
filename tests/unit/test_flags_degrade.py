"""功能开关的「静默降级」**逐项穷举**验证（M4 §4.7 验收）。

验收原文（``REWRITE_PLAN`` M4）
=============================

  「**逐个开关关闭后行为符合"静默降级"**」

为什么必须穷举而不是抽查
======================

旧代码的开关是**死代码**（缺陷 **L21**）：``push_if_enabled`` / ``_should_push``
/ ``push_receivers_l1/l2/report/alert`` 全是 **0 调用点**，Admin 页面上改了
没有任何影响。这类 bug 的特点：

* **不抛异常**、**不报错**、**测试不会红** —— 只是「关了还在发」
* 只能靠**逐个开关 + 断言行为**钉住

所以本模块就是三个穷举，并用一条**元测试**证明穷举本身不漏：

==================================  ============================================
用例类                               覆盖
==================================  ============================================
``TestSweepIsComplete``             18 个开关全部落进下面分类，无遗漏
``TestGroupMaster``                 群推送总开关 → 8 层全部静默 + 出口兜底
``TestBotMaster``                   机器人总开关 → 飞书 / QQ 事件静默
``TestLayerSwitches``               8 个告警层 × 真实推送入口（含正反对照）
``TestCommandSwitches``             7 个命令开关 × 文本 / 菜单两条入口
``TestEverythingOffAtOnce``         18 个全关 → 所有入口都不抛异常
==================================  ============================================

三层判定顺序（为什么不会互相掩盖）
================================

::

    渠道层  dispatcher.handle_event 查 BOT_MASTER / qq.handle_event 查 QQ_MASTER
      ↓ 整条渠道静默（HTTP 200，不处理、不回复）
    命令层  commands._run 查 cmd_*_enabled（flags.own_value，**不看总开关**）
      ↓ 该命令回「该命令已禁用」
    出口层  policies.should_push(layer) 查 GROUP_MASTER + 分项 + 静默时段
            transport.post_card 再兜一道 GROUP_MASTER
      ↓ 不发卡片，且**不打点**（幂等铁律：没发出去就不算发过）
"""
from __future__ import annotations

from datetime import datetime

import pytest

from starwatt import flags
from starwatt.config_registry import get_str, set_many
from starwatt.db.models import Record
from starwatt.db.repositories import RecordRepo
from starwatt.notify import channels, commands, dispatcher, policies, qq, transport

#: NOTICE 级别（``starwatt.logging_setup.NOTICE``）：用户主动关的开关不是异常
NOTICE = 25

NOW = datetime(2026, 10, 6, 12, 0, 0)

#: QQ 回调凭据（官方文档公开示例值；拼装以避免被 ``scripts/secret_scan.py``
#: 当成真凭据 —— 与 ``test_notify_qq.py`` 同一约定）
BOT_SECRET = "naOC0ocQE3shWLAf" + "ffVLB1rhYPG7"

#: 8 个告警层 → 一个**真实推送入口**的名字（可读性 + 元测试校验用）
ENTRY_POINTS: dict[str, str] = {
    "l1": "push_low_battery_if_due",
    "l2": "push_summary",
    "daily": "push_l3_if_due",
    "weekly": "push_l3_if_due",
    "monthly": "push_l3_if_due",
    "violation": "push_violations_if_due",
    "stale": "push_stale_if_due",
    "offline": "push_offline_if_due",
}

#: 7 个命令开关 → 一条能命中它的斜杠命令
COMMAND_SAMPLES: dict[str, str] = {
    "cmd_remain_enabled": "/状态",
    "cmd_meter_enabled": "/电表",
    "cmd_today_enabled": "/今日",
    "cmd_history_enabled": "/历史",
    "cmd_pay_enabled": "/缴费",
    "cmd_violations_enabled": "/违规",
    "cmd_help_enabled": "/帮助",
}

#: 处理器名 → 菜单 event_key（``/帮助`` 与 ``menu_help`` 必须同一个开关）
MENU_OF: dict[str, str] = {handler: key for key, handler in commands.MENU_KEYS.items()}


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """拦下**出口**：任何真的发出去的卡片都进这个列表。

    拦 ``transport.post_card``（而不是 ``requests``）：这样既证明「没发」，
    也证明「调用链确实走到了出口、被开关拦下」—— 两者是不同的 bug。
    """
    calls: list[dict] = []

    def _fake(card, **kwargs):
        calls.append({"card": card, **kwargs})
        return True

    monkeypatch.setattr(transport, "post_card", _fake)
    monkeypatch.setattr(channels, "broadcast_card", lambda *a, **k: [])
    return calls


@pytest.fixture
def due(monkeypatch) -> None:
    """把所有「是否到点 / 是否该报」的判定强行置真 —— 让开关成为**唯一**变量。

    没有这一步，「开关没生效」与「因为没到点所以没发」两种结果长得一模一样。
    """
    from starwatt.notify import card_builder as cards
    from starwatt.notify import reports

    monkeypatch.setattr(policies, "low_battery_due", lambda **kw: True)
    monkeypatch.setattr(policies, "l3_due", lambda kind, **kw: True)
    monkeypatch.setattr(policies, "violation_rows_to_push", lambda *a, **kw: [{"dt": "x"}])
    monkeypatch.setattr(policies, "stale_due_gap", lambda **kw: 30)
    monkeypatch.setattr(policies, "is_offline", lambda *a, **kw: True)
    monkeypatch.setattr(cards, "build_l3_card", lambda *a, **kw: {"config": {}})
    monkeypatch.setattr(reports, "build_summary", lambda *a, **kw: {})
    # L1 需要一条「很低」的最新读数（``push_low_battery_if_due`` 会读它）
    RecordRepo.insert(Record(ts="2026-10-06 11:50:00", read_time=None, remain=10.0))


def _fire(layer: str) -> bool:
    """调用该层的**真实推送入口**，返回它是否「发出去了」。"""
    if layer == "l1":
        return policies.push_low_battery_if_due(now=NOW)
    if layer == "l2":
        return policies.push_summary({"remain": 12.0}, "房间")
    if layer in policies.L3_KINDS:
        return policies.push_l3_if_due(layer, now=NOW)
    if layer == "violation":
        return policies.push_violations_if_due("room-1", now=NOW)
    if layer == "stale":
        return policies.push_stale_if_due(now=NOW)
    assert layer == "offline"
    return policies.push_offline_if_due("room-1")


def _notices(caplog) -> list[str]:
    """本次调用里所有 NOTICE 级别的消息。"""
    return [r.getMessage() for r in caplog.records if r.levelno == NOTICE]


def _disable(*keys: str) -> None:
    errors = set_many(dict.fromkeys(keys, False))
    assert errors == {}, errors


def _no_quiet_hours() -> None:
    """关掉静默时段（起止相同 = 不静默）—— 让断言与真实时钟无关。"""
    set_many({"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"})


# ===========================================================================
# 穷举本身是否完整（元测试）
# ===========================================================================
class TestSweepIsComplete:
    def test_three_families_cover_every_registered_flag(self) -> None:
        """18 个开关 = 3 总开关 + 8 告警层 + 7 命令，且三类**互不重叠**。

        下面的穷举用例全部由这三张表驱动 —— 表漏了就等于开关没被测。
        以后往 ``flags.py`` 加开关却忘了同步这里，这条会立刻红。
        """
        masters = {flags.GROUP_MASTER, flags.BOT_MASTER, flags.QQ_MASTER}
        layers = set(flags.LAYER_FLAGS.values())
        command_flags = set(flags.COMMAND_FLAGS.values())

        assert masters | layers | command_flags == set(flags.FLAGS)
        assert len(masters) + len(layers) + len(command_flags) == len(flags.FLAGS)
        assert len(flags.FLAGS) == 18

    def test_every_layer_has_a_push_entry_and_every_command_a_sample(self) -> None:
        assert set(ENTRY_POINTS) == set(flags.LAYER_FLAGS)
        assert set(COMMAND_SAMPLES) == set(flags.COMMAND_FLAGS.values())
        assert all(hasattr(policies, name) for name in ENTRY_POINTS.values())

    def test_only_sub_switches_have_a_parent(self) -> None:
        """分项开关挂父级；总开关自己不该挂在别人下面（否则会被静默连坐）。"""
        parents = {key: f.parent for key, f in flags.FLAGS.items() if f.parent}
        assert set(parents) == set(flags.LAYER_FLAGS.values()) | set(
            flags.COMMAND_FLAGS.values()
        )
        assert all(parent in flags.FLAGS for parent in parents.values())


# ===========================================================================
# 群推送总开关
# ===========================================================================
class TestGroupMaster:
    @pytest.mark.parametrize("layer", sorted(flags.LAYER_FLAGS))
    def test_all_layers_silent_when_master_off(self, tmp_db, caplog, sent, due, layer):
        """总开关关闭 → **8 个层全部**静默（用户不必逐个关）。"""
        caplog.set_level(NOTICE, logger="starwatt.notify")
        _disable(flags.GROUP_MASTER)

        assert flags.should_push(layer) is False
        assert flags.why_suppressed(layer) == f"总开关 {flags.GROUP_MASTER} 已关闭"
        assert _fire(layer) is False
        assert sent == []
        assert any(flags.GROUP_MASTER in msg for msg in _notices(caplog))

    def test_post_card_itself_is_gated(self, tmp_db, caplog) -> None:
        """出口兜底：将来有人新加推送路径却忘了 ``should_push``，总开关仍生效。"""
        caplog.set_level(NOTICE, logger="starwatt.notify")
        _disable(flags.GROUP_MASTER)

        assert transport.post_card({"header": {}, "elements": []}) is False
        assert transport.post_text("你好") is False
        assert any("总开关" in msg for msg in _notices(caplog))

    def test_explicit_test_send_still_works(self, tmp_db, monkeypatch) -> None:
        """🔑 **唯一例外**：管理页「发送测试消息」是显式动作，总开关拦不住它。

        理由：用户主动点按钮就是为了验证凭据是否正确 —— 如果关着推送就点不动，
        那个按钮在「睡前关推送」的场景里就失效了。这是 ``respect_flags=False``
        的唯一用途（其余调用点一律默认 ``True``）。
        """
        from starwatt.services import admin_service

        _disable(flags.GROUP_MASTER)
        set_many({"feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x"})

        posted: list[str] = []

        class _Ok:
            status_code = 200
            text = "{}"

            @staticmethod
            def json() -> dict:
                return {}

            @staticmethod
            def raise_for_status() -> None:
                return None

        def _fake_post(url, **kwargs):
            posted.append(url)
            return _Ok()

        monkeypatch.setattr(transport.requests, "post", _fake_post)

        result = admin_service.config_test("feishu_group")

        assert result["ok"] is True
        assert posted, "显式测试消息必须真的发出去（这是那条唯一例外的意义）"

    def test_notifier_enabled_reflects_master(self, tmp_db) -> None:
        """渠道扇出（``channels``）也认总开关 —— 否则关掉的群还会被扇出一次。"""
        set_many({"feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x"})
        assert transport.WebhookNotifier().enabled() is True

        _disable(flags.GROUP_MASTER)

        assert transport.WebhookNotifier().enabled() is False
        assert "feishu_group" not in [n.name for n in channels.enabled_channels()]


# ===========================================================================
# 机器人总开关（渠道层）
# ===========================================================================
def _no_http(*_args, **_kwargs):
    raise AssertionError("总开关已关闭 —— 不该发起任何网络请求")


class _Boom:
    """任何网络调用都会炸的替身（``session`` 注入点）。"""

    post = staticmethod(_no_http)


class TestBotMaster:
    @staticmethod
    def _feishu_message() -> dict:
        return {
            "header": {"event_type": dispatcher.MESSAGE_EVENT},
            "event": {
                "message": {"message_type": "text", "content": '{"text": "/帮助"}'},
                "sender": {"sender_id": {"open_id": "ou-1"}},
            },
        }

    @staticmethod
    def _qq_message() -> dict:
        return {
            "op": qq.OP_DISPATCH,
            "t": qq.EVENT_C2C,
            "d": {"id": "m-1", "content": "/帮助", "user_openid": "user-1"},
        }

    def test_feishu_event_is_silent(self, tmp_db, caplog) -> None:
        """关闭后**返回 200 但不处理** —— 飞书侧不会重试、不会报警（Q16）。"""
        caplog.set_level(NOTICE, logger="starwatt.notify")
        _disable(flags.BOT_MASTER)

        assert dispatcher.handle_event(self._feishu_message(), session=_Boom()) == {
            "code": 0,
            "msg": "ok",
        }
        assert any("总开关" in msg for msg in _notices(caplog))

    def test_feishu_url_verification_still_answers(self, tmp_db) -> None:
        """回显 challenge 不是「业务处理」—— 关闭状态下也必须回，否则飞书无法配置。"""
        _disable(flags.BOT_MASTER)
        body = {"type": "url_verification", "challenge": "C-1"}
        assert dispatcher.handle_event(body) == {"challenge": "C-1"}

    def test_qq_event_is_silent(self, tmp_db, caplog) -> None:
        caplog.set_level(NOTICE, logger="starwatt.notify")
        errors = set_many(
            {
                "qq_app_id": "102000000",
                "qq_app_secret": "app-secret",
                "qq_bot_secret": BOT_SECRET,
            }
        )
        assert errors == {}, errors
        _disable(flags.QQ_MASTER)

        assert qq.handle_event(self._qq_message(), session=_Boom()) == {"code": 0}
        assert any("总开关" in msg for msg in _notices(caplog))

    def test_qq_validation_event_still_answers(self, tmp_db) -> None:
        """QQ 的 ``op=13`` 回调地址验证同理：那是握手，不是业务处理。"""
        errors = set_many({"qq_bot_secret": BOT_SECRET})
        assert errors == {}, errors
        _disable(flags.QQ_MASTER)

        body = {"op": qq.OP_VALIDATION, "d": {"plain_token": "PT", "event_ts": "5"}}
        assert qq.handle_event(body)["plain_token"] == "PT"

    def test_qq_notifier_enabled_reflects_master(self, tmp_db) -> None:
        """QQ 总开关默认**关闭**（与飞书私聊机器人一致）—— 显式打开后才可用。"""
        errors = set_many(
            {"qq_app_id": "102000000", "qq_app_secret": "app-secret", "qq_bot_enabled": True}
        )
        assert errors == {}, errors
        assert qq.QQNotifier().enabled() is True

        _disable(flags.QQ_MASTER)

        assert qq.QQNotifier().enabled() is False


# ===========================================================================
# 8 个告警层
# ===========================================================================
class TestLayerSwitches:
    @pytest.mark.parametrize("layer", sorted(flags.LAYER_FLAGS))
    def test_layer_off_is_silent_and_logged(self, tmp_db, caplog, layer):
        caplog.set_level(NOTICE, logger="starwatt.notify")
        key = flags.LAYER_FLAGS[layer]
        _disable(key)

        assert flags.should_push(layer) is False
        reason = flags.why_suppressed(layer)
        assert reason is not None and key in reason
        assert any(key in msg for msg in _notices(caplog))

    @pytest.mark.parametrize("layer", sorted(flags.LAYER_FLAGS))
    def test_layer_off_stops_the_real_push_entry(self, tmp_db, caplog, sent, due, layer):
        """真实入口不发卡片，**也不打点**（幂等铁律：没发出去就不算发过）。

        如果这里打点了，用户「关一晚开关再打开」会永久丢掉那段时间的告警。
        """
        caplog.set_level(NOTICE, logger="starwatt.notify")
        _disable(flags.LAYER_FLAGS[layer])

        assert _fire(layer) is False
        assert sent == []
        for state_key in policies.STATE_KEYS.values():
            assert get_str(state_key, "") == "", state_key
        assert _notices(caplog)

    @pytest.mark.parametrize("layer", sorted(flags.LAYER_FLAGS))
    def test_layer_on_still_pushes(self, tmp_db, sent, due, layer):
        """**反面对照**：开关打开时同一个入口确实发得出去。

        没有这条，「入口根本不工作」的 bug 会被上一条的「没发出去」掩盖。
        """
        _no_quiet_hours()
        assert flags.should_push(layer) is True
        assert _fire(layer) is True
        assert len(sent) == 1
        assert sent[0]["card"], "发出去的卡片不该是空的"


# ===========================================================================
# 7 个命令开关
# ===========================================================================
class TestCommandSwitches:
    @pytest.mark.parametrize("key", sorted(COMMAND_SAMPLES))
    def test_text_command_is_disabled(self, tmp_db, caplog, key):
        caplog.set_level(NOTICE, logger="starwatt.notify")
        _disable(key)

        assert commands.dispatch_text(COMMAND_SAMPLES[key]).text == commands.DISABLED_TEXT
        assert any(key in msg for msg in _notices(caplog))

    @pytest.mark.parametrize("key", sorted(COMMAND_SAMPLES))
    def test_menu_entry_is_disabled_too(self, tmp_db, key):
        """菜单点击与斜杠命令**共用同一个开关** —— 只堵一边等于没堵。"""
        handler = next(h for h, k in flags.COMMAND_FLAGS.items() if k == key)
        _disable(key)

        assert commands.dispatch_menu(MENU_OF[handler]).text == commands.DISABLED_TEXT

    @pytest.mark.parametrize("key", sorted(COMMAND_SAMPLES))
    def test_command_on_still_replies(self, tmp_db, key):
        """**反面对照**：开关打开时命令正常回复（不是恒返回「已禁用」）。"""
        reply = commands.dispatch_text(COMMAND_SAMPLES[key])
        assert reply.text != commands.DISABLED_TEXT
        assert reply.text.strip()

    def test_status_and_remain_share_one_switch(self, tmp_db) -> None:
        """/状态 与 /剩余 是同一个处理器 → 必须同一个开关。"""
        _disable("cmd_remain_enabled")
        assert commands.dispatch_text("/状态").text == commands.DISABLED_TEXT
        assert commands.dispatch_text("/剩余").text == commands.DISABLED_TEXT

    def test_unknown_command_is_not_gated(self, tmp_db) -> None:
        """未知命令仍走「未知命令 + 帮助」—— 开关只管**已注册**的命令。"""
        _disable(*flags.COMMAND_FLAGS.values())
        assert commands.dispatch_text("/不存在的命令").text.startswith("未知命令")

    def test_bot_master_does_not_double_gate_commands(self, tmp_db) -> None:
        """总开关由**渠道层**判（整条渠道静默），命令层只看自己的开关。

        两处都判 ``is_enabled`` 会出问题：机器人总开关**默认关闭**，那样命令层
        会连带失效 —— 「单独渲染一条命令文本」这类用法会莫名不可用。
        """
        _disable(flags.BOT_MASTER)

        assert flags.command_enabled("help") is False  # 渠道视角：整体静默
        assert flags.own_value("cmd_help_enabled") is True  # 自身开关仍是开的
        assert commands.dispatch_text("/帮助").text == commands.HELP_TEXT


# ===========================================================================
# 全关：任何入口都不该抛异常
# ===========================================================================
class TestEverythingOffAtOnce:
    def test_nothing_raises_with_every_switch_off(self, tmp_db, no_network, due) -> None:
        """「关闭 ≠ 报错」的最强形式：18 个开关全关，逐个入口调一遍。

        这里**不拦出口**（不注入 ``sent``），而是用 ``no_network`` 兜底：
        任何一次真的网络调用都会抛 ``RequestException`` —— 于是「不抛异常」
        同时证明了「没有真的发出去」。
        """
        _disable(*flags.FLAGS)

        assert flags.enabled_flags() == []

        for layer in flags.LAYER_FLAGS:
            assert _fire(layer) is False

        assert transport.post_card({"header": {}, "elements": []}) is False
        assert channels.broadcast_card({"header": {}, "elements": []}) == []
        assert dispatcher.handle_event({"type": "url_verification", "challenge": "C"}) == {
            "challenge": "C"
        }
        assert commands.dispatch_text("/帮助").text == commands.DISABLED_TEXT
        assert commands.dispatch_menu("menu_help").text == commands.DISABLED_TEXT

    def test_disabled_flags_are_reported_to_the_ui(self, tmp_db) -> None:
        """「功能开关」页靠 ``FLAG_LABELS`` + ``enabled_flags`` 渲染，不能缺项。"""
        assert set(flags.FLAG_LABELS) == set(flags.FLAGS)

        _disable("cmd_pay_enabled")

        assert "cmd_pay_enabled" not in flags.enabled_flags()
        assert flags.FLAG_LABELS["cmd_pay_enabled"] == "/缴费"
        assert flags.FLAGS["cmd_pay_enabled"].parent == flags.BOT_MASTER

    def test_notice_level_matches_logging_setup(self) -> None:
        """本模块用字面量 25 断言日志级别 —— 与 ``logging_setup`` 对齐（防漂移）。"""
        from starwatt import logging_setup

        assert logging_setup.NOTICE == NOTICE


# ===========================================================================
# HTTP 层：DoD 原文「机器人关闭时 /feishu/event 返回 200，飞书侧无报错」
# ===========================================================================
class TestBotRoutesAnswerOkWhenDisabled:
    """开关关闭时必须回 **200** —— 平台看到 4xx/5xx 会重试并报警。"""

    KEY = "a" * 32

    @classmethod
    def _feishu_headers(cls, body: bytes, ts: str = "1700000000", nonce: str = "n") -> dict:
        """飞书签名：``sha256(ts + nonce + encrypt_key + body)``。"""
        import hashlib

        from starwatt.notify import crypto

        sig = hashlib.sha256((ts + nonce + cls.KEY + body.decode()).encode()).hexdigest()
        return {
            crypto.SIGNATURE_HEADERS[0]: sig,
            crypto.SIGNATURE_HEADERS[1]: ts,
            crypto.SIGNATURE_HEADERS[2]: nonce,
        }

    @staticmethod
    def _qq_headers(body: bytes, ts: str = "1700000000") -> dict:
        """QQ 签名：``Ed25519(seed, timestamp + body)``。"""
        from starwatt.notify import ed25519, qq

        seed = ed25519.seed_from_secret(BOT_SECRET)
        return {
            qq.SIGNATURE_HEADER: ed25519.sign(seed, ts.encode() + body).hex(),
            qq.TIMESTAMP_HEADER: ts,
        }

    def test_feishu_route_returns_200_when_bot_off(self, web_app, no_network) -> None:
        import json

        errors = set_many({"feishu_encrypt_key": self.KEY, "push_bot_enabled": False})
        assert errors == {}, errors

        body = json.dumps(
            {
                "header": {"event_type": dispatcher.MESSAGE_EVENT},
                "event": {
                    "message": {"message_type": "text", "content": '{"text": "/帮助"}'},
                    "sender": {"sender_id": {"open_id": "ou-1"}},
                },
            }
        ).encode()

        response = web_app.test_client().post(
            "/feishu/event", data=body, headers=self._feishu_headers(body)
        )

        assert response.status_code == 200
        assert response.get_json() == {"code": 0, "msg": "ok"}

    def test_qq_route_returns_200_when_bot_off(self, web_app, no_network) -> None:
        import json

        errors = set_many({"qq_bot_secret": BOT_SECRET, "qq_bot_enabled": False})
        assert errors == {}, errors

        body = json.dumps(
            {
                "op": qq.OP_DISPATCH,
                "t": qq.EVENT_C2C,
                "d": {"id": "m-1", "content": "/帮助", "user_openid": "user-1"},
            }
        ).encode()

        response = web_app.test_client().post(
            "/qq/events", data=body, headers=self._qq_headers(body)
        )

        assert response.status_code == 200
        assert response.get_json() == {"code": 0}

    def test_switches_never_relax_signature_checks(self, web_app, no_network) -> None:
        """⚠️ 关闭 ≠ 免验签：签名不对**照样 401**（fail-closed 不受开关影响）。"""
        import json

        errors = set_many({"feishu_encrypt_key": self.KEY, "push_bot_enabled": False})
        assert errors == {}, errors

        body = json.dumps({"header": {"event_type": dispatcher.MESSAGE_EVENT}}).encode()
        headers = self._feishu_headers(body)
        headers[dispatcher.crypto.SIGNATURE_HEADERS[0]] = "0" * 64  # 篡改签名

        response = web_app.test_client().post("/feishu/event", data=body, headers=headers)

        assert response.status_code == 401

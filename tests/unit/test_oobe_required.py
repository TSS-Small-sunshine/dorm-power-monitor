"""OOBE 必需项门禁 —— ``required`` 非空的步不许「留空走完」。

``advance()`` 原先只做逐键校验（``admin_service.config_update``），从不看
``step.required``：用户一次都没填 ``dorm_openid`` 也能一路「下一步」走完
5 步，服务端还把 ``oobe_completed`` 置 True —— 调度器随后拿着空 openid
永远抓不到数据，界面却显示「已完成」。

📌 只有 ``school`` 步声明了 ``required``（其余步「可跳过」是设计，别改），
所以断言与 ``STEPS`` 里的声明绑定；文案用注册表 label，不另立一套。
"""
from __future__ import annotations

import pytest

from starwatt.config_registry import get_bool, get_int, get_str
from starwatt.services import oobe_service

#: ``STEPS[1]`` = school，``required=("dorm_openid",)``
SCHOOL_INDEX = 1
SCHOOL_KEY = "school"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_db):
    """配置层有进程级缓存 + 真库 —— 每个用例都要自己的空库。"""
    return tmp_db


def _goto_school() -> dict:
    """从欢迎步前进到「学校接入」（welcome 没有 required ⇒ 一定放行）。"""
    body = oobe_service.advance(direction="next")
    assert body["step"] == SCHOOL_INDEX, body["saved"]
    assert body["current"]["key"] == SCHOOL_KEY
    return body


class TestRequiredGate:
    def test_school_step_blocks_submitted_empty_openid(self) -> None:
        """提交了空值 → 失败 + 停在 school + 不置 completed。"""
        _goto_school()

        body = oobe_service.advance(direction="next", values={"dorm_openid": ""})

        # 空值本身会被 ``set_many`` 写进去（既有语义，不归本门禁管）——
        # 门禁只管「这一项还是空的」⇒ 不许前进。
        assert "dorm_openid" in body["saved"]["errors"]
        assert body["step"] == SCHOOL_INDEX
        assert body["current"]["key"] == SCHOOL_KEY
        assert body["completed"] is False
        assert get_bool("oobe_completed", False) is False
        assert get_int("oobe_step", -1) == SCHOOL_INDEX

    def test_bare_next_is_blocked_too(self) -> None:
        """一个 ``values`` 都不带也必须拦住 —— 前端「下一步」走的就是这条路径。"""
        _goto_school()

        body = oobe_service.advance(direction="next")

        assert body["step"] == SCHOOL_INDEX
        assert body["current"]["key"] == SCHOOL_KEY
        assert "dorm_openid" in body["saved"]["errors"]
        assert body["completed"] is False
        assert get_bool("oobe_completed", False) is False

    def test_error_message_says_what_to_fill(self) -> None:
        """错误文案要说明「该填什么」（取自注册表 label，中文）。"""
        _goto_school()

        errors = oobe_service.advance(direction="next")["saved"]["errors"]

        assert "登录凭据 openid" in errors["dorm_openid"]

    def test_filled_openid_advances(self) -> None:
        _goto_school()

        body = oobe_service.advance(direction="next", values={"dorm_openid": "o-1"})

        assert body["saved"]["errors"] == {}
        assert body["saved"]["applied"] == ["dorm_openid"]
        assert get_str("dorm_openid") == "o-1"
        assert body["step"] == SCHOOL_INDEX + 1
        assert body["current"]["key"] == "feishu_group"

    def test_already_configured_openid_advances_without_resubmitting(self) -> None:
        """库里已配好 openid → 不必每步重填（判定用**已落库**的值）。"""
        _goto_school()
        oobe_service.advance(direction="next", values={"dorm_openid": "o-1"})

        back = oobe_service.advance(direction="prev")
        assert back["step"] == SCHOOL_INDEX

        body = oobe_service.advance(direction="next")
        assert body["saved"]["errors"] == {}
        assert body["step"] == SCHOOL_INDEX + 1

    def test_prev_is_not_blocked_by_required(self) -> None:
        """后退不受 required 限制 —— 用户必须能退回去改。"""
        _goto_school()

        body = oobe_service.advance(direction="prev")

        assert body["step"] == 0
        assert body["current"]["key"] == "welcome"
        assert body["saved"]["errors"] == {}

    def test_skip_still_moves_without_openid(self) -> None:
        """显式 ``skip`` 仍然放行（前端对 required 非空的步不显示该按钮）。"""
        _goto_school()

        body = oobe_service.advance(direction="next", skip=True)

        assert body["step"] == SCHOOL_INDEX + 1
        assert body["saved"]["applied"] == []
        assert body["completed"] is False

    def test_last_step_still_completes(self) -> None:
        """既有行为不变：走到最后一步 = 完成（B5 把 ``/complete`` 并进 advance）。"""
        _goto_school()
        oobe_service.advance(direction="next", values={"dorm_openid": "o-1"})  # → 2
        oobe_service.advance(direction="next")  # → 3

        body = oobe_service.advance(direction="next")  # → 4 == LAST_INDEX

        assert body["step"] == oobe_service.LAST_INDEX
        assert body["completed"] is True
        assert get_bool("oobe_completed", False) is True

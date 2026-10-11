"""房间名回填（H3）：``verify()`` 是 UI 实际走的路径，``commit()`` 是备用路径。

链路：``scraper.endpoints.discover_room`` 从页面 ``id="roomNo"`` 取房间名
→ ``verify()`` 返回 ``room_label`` 并回填配置（``commit()`` 也同样会写）
→ ``notify.policies.room_label()`` → 推送卡片标题
（``starwatt/notify/card_builder.py:162``，为空则回落「宿舍电量监控」）。

为什么两条路都要覆盖：前端**没有任何地方**调用 ``/api/setup/commit``
（``frontend/src/api/client.ts`` 只有定义），只调 ``/api/setup/parse-url``
与 ``/api/setup/verify``；``verify()`` 因此在发现阶段就把房间名落库，
``commit()`` 的回填留给将来的接线（端点存在，行为已固化在这里）。
"""
from __future__ import annotations

import pytest

from starwatt.config_registry import get_str, set_many
from starwatt.notify import policies
from starwatt.services import setup_service

LABEL = "6号楼-1-119"


def test_commit_writes_discovered_room_label(tmp_db) -> None:
    """发现到房间名 → 落库，且推送标题链路读得到。"""
    assert policies.room_label() is None

    result = setup_service.commit(openid="o-1", room_id="room-9", room_label=LABEL)

    assert result["errors"] == {}
    assert "room_label" in result["applied"]
    assert get_str("room_label") == LABEL
    assert policies.room_label() == LABEL


def test_commit_keeps_user_edited_room_label(tmp_db) -> None:
    """用户手填过的显示名优先（与 ``ensure_defaults`` 同一条约定）。"""
    assert set_many({"room_label": "我手填的名字"}) == {}

    result = setup_service.commit(openid="o-1", room_id="room-9", room_label=LABEL)

    assert result["errors"] == {}
    assert "room_label" not in result["applied"]
    assert get_str("room_label") == "我手填的名字"
    assert policies.room_label() == "我手填的名字"


@pytest.mark.parametrize("label", [None, "", "   "])
def test_commit_without_label_writes_nothing(tmp_db, label) -> None:
    """本次没拿到房间名（发现是 best-effort）→ 不写、不报错。

    ``None`` 这一档同时覆盖「调用方根本没传 ``room_label``」的默认路径。
    """
    result = setup_service.commit(openid="o-1", room_id="room-9", room_label=label)

    assert result["errors"] == {}
    assert "room_label" not in result["applied"]
    assert get_str("room_label") == ""
    assert policies.room_label() is None


def test_overlong_label_does_not_fail_the_commit(tmp_db) -> None:
    """房间名超 ``len:0..32`` 只记日志 —— 展示信息不该拖垮整次提交。"""
    result = setup_service.commit(openid="o-1", room_id="room-9", room_label="房" * 40)

    assert result["errors"] == {}
    assert get_str("room_label") == ""
    assert get_str("dorm_openid") == "o-1"
    assert get_str("dorm_room_id") == "room-9"


# ===========================================================================
# verify() —— 前端真正调用的路径（``/api/setup/verify``）
# ===========================================================================
ROOM_ID = "room-uuid-1"
BASE = "https://xydf.example.edu.cn"
#: ``roomId`` 为空 = 站点认为凭据失效（发现失败）
EMPTY_ROOM_ID_HTML = '<input type="hidden" id="roomId" value="">'


def _finduser_html(room_id: str, room_no: str = "") -> str:
    """真实站点的 ``finduser`` 页面片段（``roomNo`` 是房间**显示名**）。"""
    return (
        f'<input type="hidden" id="roomId" value="{room_id}">\n'
        f'<input type="hidden" id="roomNo" value="{room_no}">'
    )


class _FakeClient:
    """``SchoolClient`` 替身：按页面内容应答，绝不联网。"""

    def __init__(self, finduser_html: str) -> None:
        self.finduser_html = finduser_html
        self.calls: list[str] = []

    def get(self, path: str, **_kwargs) -> str:
        self.calls.append(path)
        return self.finduser_html

    def post_form(self, path: str, data: dict) -> dict:
        self.calls.append(path)
        return {"status": 0, "remainEq": 129.96, "dt": "2026-10-06 12:00:00"}

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """SSRF 的 DNS 固定到公网 IP —— 用例不做真实 DNS 查询。"""
    from starwatt.scraper import ssrf

    monkeypatch.setattr(ssrf, "resolve_ips", lambda _host: ["93.184.216.34"])


def _verify(client) -> dict:
    """走发现流程（``room_id=""``）—— 与前端调用形态一致。"""
    return setup_service.verify(openid="o-1", room_id="", base_url=BASE, client=client)


def test_verify_backfills_discovered_room_label(tmp_db) -> None:
    """发现到房间名 → 不必等 commit，推送标题链路立刻读得到。"""
    assert policies.room_label() is None

    result = _verify(_FakeClient(_finduser_html(ROOM_ID, LABEL)))

    assert result["ok"] is True
    assert result["room_label"] == LABEL
    assert get_str("room_label") == LABEL
    assert policies.room_label() == LABEL


def test_verify_keeps_user_edited_room_label(tmp_db) -> None:
    """用户手填过的显示名优先（与 ``ensure_defaults`` 同一条约定）。"""
    assert set_many({"room_label": "我手填的名字"}) == {}

    result = _verify(_FakeClient(_finduser_html(ROOM_ID, LABEL)))

    assert result["ok"] is True
    assert get_str("room_label") == "我手填的名字"
    assert policies.room_label() == "我手填的名字"


def test_verify_without_discovered_label_writes_nothing(tmp_db) -> None:
    """发现是 best-effort：页面里没有 ``roomNo`` → 不写、不报错。"""
    result = _verify(_FakeClient(_finduser_html(ROOM_ID)))

    assert result["ok"] is True
    assert result["room_label"] is None
    assert get_str("room_label") == ""
    assert policies.room_label() is None


def test_verify_failure_writes_nothing(tmp_db) -> None:
    """验证失败（openid 失效）→ **不**写 ``room_label``。"""
    result = _verify(_FakeClient(EMPTY_ROOM_ID_HTML))

    assert result["ok"] is False
    assert get_str("room_label") == ""
    assert policies.room_label() is None


def test_verify_return_shape_unchanged_by_backfill(tmp_db) -> None:
    """回填是副作用：返回字段与 ``ok`` 语义不变（前端契约）。"""
    result = _verify(_FakeClient(_finduser_html(ROOM_ID, LABEL)))

    assert set(result) == {
        "ok",
        "room_id",
        "room_label",
        "remain",
        "read_time",
        "error",
    }
    assert result["ok"] is True
    assert result["room_id"] == ROOM_ID


def test_verify_still_ok_when_label_rejected(tmp_db) -> None:
    """房间名超 ``len:0..32``：只记日志，``verify`` 仍然 ``ok``。"""
    result = _verify(_FakeClient(_finduser_html(ROOM_ID, "房" * 40)))

    assert result["ok"] is True
    assert get_str("room_label") == ""
    assert policies.room_label() is None

"""领域实体 —— 用 **dataclass** 而非 Pydantic（D1 / B1）。

为什么不用 Pydantic
===================

Q7 原本选了 Pydantic v2，但 B1 修复（D1）决定剔除它 —— 因为
``pydantic-core`` 是 **Rust** 扩展，**没有 armv7 wheel**，而 Q3 要求覆盖
全架构（见 ``docs/BLOCKER_FIXES.md §1``）。

本模块用标准库 ``dataclass(frozen=True)`` 实现等价能力：

| Pydantic 能力 | 本模块替代 |
|---|---|
| 类型标注 | dataclass 字段注解 |
| ``from_attributes`` | :meth:`_Base.from_row` |
| ``model_dump()`` | :meth:`_Base.as_dict` |
| 校验 / 强制转换 | ``from_row`` 内经 :mod:`starwatt.db.coerce` 转换 |
| 不可变 | ``frozen=True`` |

约定
====

* 字段名与 SQL 列名**保持一致**（``roomId`` 与 ``wg_reason`` 混用是历史包袱，
  但**冻结**，见 AGENTS.md 硬约束 #6）
* ``from_row()`` 接受 ``sqlite3.Row`` / ``dict`` / 任何支持 ``[]`` 的对象
* 数值字段一律经 :mod:`starwatt.db.coerce` 转换，**不抛异常**
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, ClassVar

from starwatt.db.coerce import coerce_float, coerce_int

__all__ = [
    "AuditLog",
    "DailyElec",
    "MetaEntry",
    "Pay",
    "Record",
    "RunStatus",
    "StatsCard",
    "User",
    "Violation",
]


class _Base:
    """所有实体的公共行为。

    ``_FLOAT_FIELDS`` / ``_INT_FIELDS`` 声明需要强制转换的字段名 ——
    门户返回的 JSON 类型不稳定（``"12.34"`` / ``12.34`` / ``null`` 都出现过）。
    """

    _FLOAT_FIELDS: ClassVar[tuple[str, ...]] = ()
    _INT_FIELDS: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_row(cls, row: Any) -> Any:
        """从 ``sqlite3.Row`` / ``dict`` 构造实体。

        只取本类声明的字段（忽略 ``SELECT *`` 的额外列），
        数值字段做强制转换，缺失字段走 dataclass 默认值。
        """
        data: dict[str, Any] = {}
        for field in fields(cls):  # type: ignore[arg-type]
            name = field.name
            if name.startswith("_"):
                continue
            try:
                raw = row[name]
            except (KeyError, IndexError, TypeError):
                continue
            if name in cls._FLOAT_FIELDS:
                data[name] = coerce_float(raw)
            elif name in cls._INT_FIELDS:
                data[name] = coerce_int(raw)
            else:
                data[name] = raw
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        """转成普通 dict（``model_dump()`` 的等价物）。"""
        return asdict(self)  # type: ignore[call-overload]

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """本实体的字段名元组（顺序与声明一致）。"""
        return tuple(f.name for f in fields(cls))  # type: ignore[arg-type]


# ===========================================================================
# records —— F1 抓取快照（R1 建立，R49 加 UNIQUE(ts)）
# ===========================================================================
@dataclass(frozen=True)
class Record(_Base):
    """一次抓取快照。

    * ``ts``        —— 服务端**朴素 CST** 写入时间，``YYYY-MM-DD HH:MM:SS``，
                       由 :mod:`starwatt.timeutil` 产出（Q10）
    * ``read_time`` —— 门户报告的抄表时间；未抄表时可能为 ``None``
    * ``remain``    —— 剩余电量（kW·h）；解析失败为 ``None``
    * ``id``        —— SQLite rowid；新建记录时为 ``None``
    """

    ts: str
    read_time: str | None = None
    remain: float | None = None
    id: int | None = None

    _FLOAT_FIELDS: ClassVar[tuple[str, ...]] = ("remain",)
    _INT_FIELDS: ClassVar[tuple[str, ...]] = ("id",)


# ===========================================================================
# daily_elec —— F2 每日用电（R2）
# ===========================================================================
@dataclass(frozen=True)
class DailyElec(_Base):
    """一行每日用电。``zong_eq`` 是**累积表码**，不是当日用量。"""

    roomId: str
    dt: str
    total_eq: float | None = None
    esbm: float | None = None
    eebm: float | None = None
    zong_eq: float | None = None

    _FLOAT_FIELDS: ClassVar[tuple[str, ...]] = ("total_eq", "esbm", "eebm", "zong_eq")


# ===========================================================================
# violations —— F3 违规记录（R2）
# ===========================================================================
@dataclass(frozen=True)
class Violation(_Base):
    """一行违规。

    ⚠️ ``wg_power`` 的单位是 **kW（瞬时功率）**，不是 kW·h。
    旧代码在仪表盘显示 ``W``、飞书显示 ``kW``（L7），重写统一为 **kW**。
    """

    roomId: str
    dt: str
    wg_reason: str
    wg_power: float | None = None

    _FLOAT_FIELDS: ClassVar[tuple[str, ...]] = ("wg_power",)


# ===========================================================================
# pay_history —— F5 缴费历史（R2）
# ===========================================================================
@dataclass(frozen=True)
class Pay(_Base):
    """一行缴费。

    门户 JSON 用 ``payTypeLabel`` / ``feeTypeLabel``；SQL 列名是
    ``pay_type`` / ``fee_type``。为兼容两种来源，提供 :attr:`kind` /
    :attr:`amount` 别名属性（沿用旧 ``db.models.Pay`` 契约）。
    """

    dt: str
    roomId: str | None = None
    pay_type: str | None = None
    fee_type: str | None = None
    money: float | None = None

    _FLOAT_FIELDS: ClassVar[tuple[str, ...]] = ("money",)

    @property
    def kind(self) -> str | None:
        """``pay_type`` 的别名。"""
        return self.pay_type

    @property
    def amount(self) -> float | None:
        """``money`` 的别名。"""
        return self.money


# ===========================================================================
# run_status —— F4 电表实时状态（R2）；每房间一行
# ===========================================================================
@dataclass(frozen=True)
class RunStatus(_Base):
    """电表实时快照。

    ``run_status`` 的取值见 ``starwatt.domain.thresholds``：
    ``在线`` / ``正常`` / ``通讯正常`` 视为在线，**其余（含缺失）都算离线**
    （防静默失败 —— 见 ``tests/regression/fixtures/behaviors.json`` 的 ``_is_offline``）。
    """

    roomId: str
    meter_no: str | None = None
    dt: str | None = None
    run_status: str | None = None
    work_status: str | None = None
    update_dt: str | None = None
    vol: float | None = None
    cur: float | None = None
    yggl: float | None = None
    stop_reason: str | None = None

    _FLOAT_FIELDS: ClassVar[tuple[str, ...]] = ("vol", "cur", "yggl")


# ===========================================================================
# meta —— 通用键值（R2 / R34A）
# ===========================================================================
@dataclass(frozen=True)
class MetaEntry(_Base):
    """一行 ``meta``。``value`` 恒为 TEXT；类型转换由 ``config_registry`` 负责。"""

    key: str
    value: str | None = None


# ===========================================================================
# users / sessions / audit_log —— R63 认证（R69 加 2 列，D2）
# ===========================================================================
@dataclass(frozen=True)
class User(_Base):
    """一个用户。

    * ``password_hash`` —— 格式 ``scrypt$n$r$p$salt_b64$dk_b64``
      （D1：stdlib ``hashlib.scrypt`` 替代 bcrypt）
    * ``disabled``      —— R69 新增列（D2）；1 = 禁用，无法登录
    * ``must_change_password`` —— R69 新增列（Q19）；1 = 强制首登改密
    """

    id: int
    username: str
    password_hash: str
    role: str
    created_at: str
    last_login_at: str | None = None
    disabled: int = 0
    must_change_password: int = 0

    _INT_FIELDS: ClassVar[tuple[str, ...]] = ("id", "disabled", "must_change_password")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        """``disabled`` 为 0 才可登录。"""
        return not self.disabled

    @property
    def needs_password_change(self) -> bool:
        return bool(self.must_change_password)


@dataclass(frozen=True)
class Session(_Base):
    """一行服务端会话。

    ⚠️ ``token`` 存的是 **``sha256(token)`` 的十六进制串**（L17 修复），
    不是原始 token —— DB 泄露也无法直接盗用会话。列名保持 ``token`` 不变。
    """

    id: int
    user_id: int
    token: str
    expires_at: str
    created_at: str
    ip: str | None = None
    user_agent: str | None = None

    _INT_FIELDS: ClassVar[tuple[str, ...]] = ("id", "user_id")


@dataclass(frozen=True)
class AuditLog(_Base):
    """一行审计日志。

    不可篡改语义：**只追加，不更新、不删除**（与可轮转的运行日志分离，Q20）。
    ``details`` 是 JSON 字符串；**绝不含密码 / token / openid**。
    """

    action: str
    created_at: str
    id: int | None = None
    user_id: int | None = None
    target: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    details: str | None = None

    _INT_FIELDS: ClassVar[tuple[str, ...]] = ("id", "user_id")


# ===========================================================================
# StatsCard —— 仪表盘 stat card 的虚拟形状（无 SQL 表）
# ===========================================================================
@dataclass(frozen=True)
class StatsCard(_Base):
    """仪表盘卡片的数据契约。

    不落库 —— 由 ``starwatt.domain.metrics`` 计算、``dashboard_service`` 装配。
    定义成实体是为了让「前后端字段契约」有单一出处。
    """

    remain: float | None = None
    hourly_used: float | None = None
    daily_avg: float | None = None
    read_time: str | None = None
    monthly_projection: float | None = None
    eqprice: float | None = None
    last_scrape_status: str | None = None
    last_scrape_at: str | None = None
    last_room_id: str | None = None

    _FLOAT_FIELDS: ClassVar[tuple[str, ...]] = (
        "remain",
        "hourly_used",
        "daily_avg",
        "monthly_projection",
        "eqprice",
    )


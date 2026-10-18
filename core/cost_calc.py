# -*- coding: utf-8 -*-
"""花费计价与报表的纯逻辑（不导入任何 ``astrbot`` 模块）。

分成「取数」和「算钱」两半，是为了让算钱这半能被完整测试：数据库那一侧的
时区、SQL、async generator 都很容易出错，而**把 token 折成钱**同样容易出错，
后者不该只能靠真机验证。

金额一律用 ``float`` 表示「元」，并且：
- 单价的口径统一是**元 / 百万 token**（与模型厂商的报价单一致）；
- 缓存命中的输入 token 有自己的价（一般比普通输入便宜一个数量级），
  不做区分会把成本高估几倍——这是各家账单里最大的一个坑。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

#: 计价分档的键，与 ``ProviderStat`` 的列一一对应。
TOKEN_KINDS = ("input_other", "input_cached", "output")

#: 给用户看的中文名。
TOKEN_LABELS = {
    "input_other": "输入",
    "input_cached": "缓存输入",
    "output": "输出",
}


def _finite(value: Any, fallback: float = 0.0) -> float:
    """把任意输入变成有限 float；坏值一律回落，绝不抛异常。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(number) or math.isinf(number):
        return fallback
    return number


def _count(value: Any) -> int:
    """token 计数的归一化：负数与坏值算 0。"""

    return max(0, int(_finite(value, 0.0)))


# ------------------------------------------------------------------ 计价


@dataclass(frozen=True)
class Prices:
    """一套全局单价（元 / 百万 token）。

    ``cached_input`` 单独一档：缓存命中的输入通常只有普通输入的 10% 甚至更低，
    混在一起算会让「输入很多但大部分命中缓存」的账号被严重高估。
    """

    input_per_mtok: float = 0.0
    cached_input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    @property
    def configured(self) -> bool:
        """是否有任何一档单价被设置过（全 0 = 没配价，金额无意义）。"""

        return any(
            _finite(price, 0.0) > 0.0
            for price in (
                self.input_per_mtok,
                self.cached_input_per_mtok,
                self.output_per_mtok,
            )
        )

    def cost_of(self, tokens: Mapping[str, Any]) -> float:
        """把一组 token 计数折成金额（元）。"""

        total = 0.0
        for kind, price in (
            ("input_other", self.input_per_mtok),
            ("input_cached", self.cached_input_per_mtok),
            ("output", self.output_per_mtok),
        ):
            rate = _finite(price, 0.0)
            if rate <= 0.0:
                continue
            total += _count(tokens.get(kind)) * rate / 1_000_000.0
        return total


@dataclass
class Usage:
    """一组 token 计数（可累加）。"""

    input_other: int = 0
    input_cached: int = 0
    output: int = 0

    def add(self, other: Mapping[str, Any] | None) -> None:
        """累加一行数据。``None`` / 非映射 / 坏值一律当作 0，不抛异常。"""

        if not isinstance(other, Mapping):
            return
        self.input_other += _count(other.get("input_other"))
        self.input_cached += _count(other.get("input_cached"))
        self.output += _count(other.get("output"))

    @property
    def total_tokens(self) -> int:
        """输入侧 + 输出侧的总 token（缓存输入也算输入，不重复计）。"""

        return self.input_other + self.input_cached + self.output

    @property
    def total_input(self) -> int:
        return self.input_other + self.input_cached

    def cost(self, prices: Prices) -> float:
        return prices.cost_of(
            {
                "input_other": self.input_other,
                "input_cached": self.input_cached,
                "output": self.output,
            }
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_other": self.input_other,
            "input_cached": self.input_cached,
            "output": self.output,
        }


# ------------------------------------------------------------------ 时间


def local_now(now: float, *, tz_offset_minutes: int = 480) -> datetime:
    """把「UTC 时间戳」换成**本地**的 naive datetime。"""

    # 用 fromtimestamp(..., timezone.utc) 而不是 utcfromtimestamp：后者在 3.12
    # 已弃用。取到 aware 之后立刻去掉 tzinfo，因为数据库列是 naive 的。
    return datetime.fromtimestamp(_finite(now, 0.0), tz=timezone.utc).replace(
        tzinfo=None
    ) + timedelta(minutes=int(tz_offset_minutes))


def to_utc_naive(local: datetime, *, tz_offset_minutes: int = 480) -> datetime:
    """把本地 naive datetime 换回**数据库里存的** UTC naive datetime。

    ``ProviderStat.created_at`` 的列类型是 ``DateTime(timezone=False)``，写入时
    存的是 UTC 值。所以查询边界必须去掉时区再比较，带 tzinfo 去比会整体错开
    一个时区偏移（东八区就是差 8 小时，跨天统计全错）。
    """

    return local - timedelta(minutes=int(tz_offset_minutes))


def day_range(
    now: float, *, tz_offset_minutes: int = 480
) -> tuple[datetime, datetime]:
    """今天的 ``[起, 止)``，返回**数据库用的 UTC naive** 边界。"""

    start = local_now(now, tz_offset_minutes=tz_offset_minutes).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return to_utc_naive(start, tz_offset_minutes=tz_offset_minutes), to_utc_naive(
        start + timedelta(days=1), tz_offset_minutes=tz_offset_minutes
    )


def month_range(
    now: float, *, tz_offset_minutes: int = 480
) -> tuple[datetime, datetime]:
    """本月的 ``[起, 止)``，返回**数据库用的 UTC naive** 边界。"""

    start = local_now(now, tz_offset_minutes=tz_offset_minutes).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return to_utc_naive(start, tz_offset_minutes=tz_offset_minutes), to_utc_naive(
        end, tz_offset_minutes=tz_offset_minutes
    )


def days_in_month(now: float, *, tz_offset_minutes: int = 480) -> int:
    """本月有几天（用于日均与预算推算）。"""

    local = local_now(now, tz_offset_minutes=tz_offset_minutes)
    start = local.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return (next_month - start).days


def month_label(now: float, *, tz_offset_minutes: int = 480) -> str:
    """``2025-10`` 形式的月份标签。"""

    local = local_now(now, tz_offset_minutes=tz_offset_minutes)
    return f"{local.year:04d}-{local.month:02d}"


def day_label(now: float, *, tz_offset_minutes: int = 480) -> str:
    local = local_now(now, tz_offset_minutes=tz_offset_minutes)
    return f"{local.month:02d}-{local.day:02d}"


def progressed_days(now: float, *, tz_offset_minutes: int = 480) -> float:
    """本月已经过去多少天（含小数），用于算日均。最小值 1e-6 防除零。"""

    local = local_now(now, tz_offset_minutes=tz_offset_minutes)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(1e-6, (local - start).total_seconds() / 86400.0)


# ------------------------------------------------------------------ 报表


@dataclass(frozen=True)
class PeriodCost:
    """一段时间的花费汇总。"""

    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    #: 这一段时间里有多少次模型调用
    calls: int = 0


@dataclass(frozen=True)
class ModelCost:
    """按模型拆分的花费。"""

    model: str
    provider_id: str
    usage: Usage
    cost: float
    calls: int


@dataclass(frozen=True)
class ChatCost:
    """按会话拆分的花费。"""

    umo: str
    usage: Usage
    cost: float
    calls: int


@dataclass(frozen=True)
class DailyCost:
    """按天拆分的花费（日期是**本地**日期 ``MM-DD``）。"""

    day: str
    usage: Usage
    cost: float
    calls: int


@dataclass
class Report:
    """一次查询的完整结果。

    ``ok=False`` 时 ``error`` 说明原因，其余字段不具有意义。
    """

    ok: bool = True
    error: str = ""
    now: float = 0.0
    #: 月份标签，形如 ``2025-10``
    month: str = ""
    prices: Prices = field(default_factory=Prices)
    monthly_budget: float = 0.0
    today: PeriodCost = field(default_factory=PeriodCost)
    this_month: PeriodCost = field(default_factory=PeriodCost)
    total: PeriodCost = field(default_factory=PeriodCost)
    by_model: list[ModelCost] = field(default_factory=list)
    by_chat: list[ChatCost] = field(default_factory=list)
    daily: list[DailyCost] = field(default_factory=list)

    @property
    def priced(self) -> bool:
        """是否配了单价（没配时金额全是 0，只能看 token 数）。"""

        return self.prices.configured

    @property
    def remaining(self) -> float:
        """本月预算还剩多少；没设预算返回 0。"""

        if self.monthly_budget <= 0:
            return 0.0
        return self.monthly_budget - self.this_month.cost

    @property
    def remaining_ratio(self) -> float:
        if self.monthly_budget <= 0:
            return 0.0
        return max(0.0, min(1.0, self.remaining / self.monthly_budget))

    def projected_month_cost(self, *, tz_offset_minutes: int = 480) -> float:
        """按本月日均推算的**整月**花费。

        刻意用「日均 × 本月天数」而不是「日均 × 30」：月长不同，推算值会跟着变，
        用户拿去和月度账单对时不会莫名其妙差一天。
        """

        days = progressed_days(self.now, tz_offset_minutes=tz_offset_minutes)
        per_day = self.this_month.cost / days
        return per_day * days_in_month(self.now, tz_offset_minutes=tz_offset_minutes)

    def daily_average(self, *, tz_offset_minutes: int = 480) -> float:
        return self.this_month.cost / progressed_days(
            self.now, tz_offset_minutes=tz_offset_minutes
        )


def _top(rows: list, limit: int) -> list:
    """按花费降序取前 ``limit`` 条；``limit <= 0`` 表示全部。"""

    ordered = sorted(rows, key=lambda row: (-row.cost, -row.usage.total_tokens))
    return ordered if limit <= 0 else ordered[:limit]


def build_report(
    *,
    now: float,
    prices: Prices,
    monthly_budget: float,
    today_rows: list[Mapping[str, Any]],
    month_rows: list[Mapping[str, Any]],
    total_rows: list[Mapping[str, Any]],
    by_model_rows: list[Mapping[str, Any]],
    by_chat_rows: list[Mapping[str, Any]],
    daily_rows: list[Mapping[str, Any]],
    tz_offset_minutes: int = 480,
    top_models: int = 8,
    top_chats: int = 8,
) -> Report:
    """把各组聚合行组装成交给渲染层的 ``Report``。

    每一行的形状由取数层保证：

    - ``today_rows`` / ``month_rows`` / ``total_rows``：单行 ``{input_other,
      input_cached, output, calls}``；
    - ``by_model_rows``：``{provider_id, provider_model, input_other,
      input_cached, output, calls}``；
    - ``by_chat_rows``：``{umo, input_other, input_cached, output, calls}``；
    - ``daily_rows``：``{created_at: date|datetime, input_other,
      input_cached, output, calls}``。

    这里只做「加总 + 排序 + 折钱」，任何一行数据坏了都只会让那一行变成 0，
    不会让整张报表失败。
    """

    def period(rows: list[Mapping[str, Any]]) -> PeriodCost:
        usage = Usage()
        calls = 0
        for row in rows or []:
            usage.add(row)
            if isinstance(row, Mapping):
                calls += _count(row.get("calls"))
        return PeriodCost(usage=usage, cost=usage.cost(prices), calls=calls)

    report = Report(
        ok=True,
        now=_finite(now, 0.0),
        month=month_label(now, tz_offset_minutes=tz_offset_minutes),
        prices=prices,
        monthly_budget=max(0.0, _finite(monthly_budget, 0.0)),
        today=period(today_rows),
        this_month=period(month_rows),
        total=period(total_rows),
    )

    models: list[ModelCost] = []
    for row in by_model_rows or []:
        usage = Usage()
        usage.add(row)
        model = str(row.get("provider_model") or "").strip() or "(未知模型)"
        models.append(
            ModelCost(
                model=model,
                provider_id=str(row.get("provider_id") or "").strip(),
                usage=usage,
                cost=usage.cost(prices),
                calls=_count(row.get("calls")),
            )
        )
    report.by_model = _top(models, int(top_models))

    chats: list[ChatCost] = []
    for row in by_chat_rows or []:
        usage = Usage()
        usage.add(row)
        chats.append(
            ChatCost(
                umo=str(row.get("umo") or "").strip() or "(未知会话)",
                usage=usage,
                cost=usage.cost(prices),
                calls=_count(row.get("calls")),
            )
        )
    report.by_chat = _top(chats, int(top_chats))

    days: list[DailyCost] = []
    for row in daily_rows or []:
        usage = Usage()
        usage.add(row)
        days.append(
            DailyCost(
                day=_day_key(row.get("created_at"), tz_offset_minutes),
                usage=usage,
                cost=usage.cost(prices),
                calls=_count(row.get("calls")),
            )
        )
    report.daily = sorted(days, key=lambda d: d.day)
    return report


def _day_key(value: Any, tz_offset_minutes: int) -> str:
    """把聚合出来的日期列变成 ``MM-DD``。

    SQLite 的 ``func.date()`` 可能给 ``str``，别的方言可能给 ``date``；两者都接受。
    它已经是**按 UTC 边界分组**的结果，这里不再做时区换算（取数层负责让边界落在
    本地日界上）。
    """

    if isinstance(value, datetime):
        return f"{value.month:02d}-{value.day:02d}"
    if hasattr(value, "month") and hasattr(value, "day"):
        return f"{int(value.month):02d}-{int(value.day):02d}"
    text = str(value or "").strip()
    # 形如 "2025-10-18" / "2025-10-18 00:00:00"
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return f"{text[5:7]}-{text[8:10]}"
    return text or "(未知日期)"

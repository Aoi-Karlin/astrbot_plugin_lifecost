# -*- coding: utf-8 -*-
"""从 AstrBot 的 ``provider_stats`` 表取数（唯一接触数据库的地方）。

为什么单独一层：这一层里三个坑都不是「逻辑」而是「宿主契约」——

1. ``context.get_db()`` 返回的是 ``BaseDatabase``，真正的 session 要
   ``async with db.get_db() as session`` 才拿得到（一个 **async generator**）；
2. ``ProviderStat.created_at`` 是 ``DateTime(timezone=False)``，写进去的是
   **UTC naive**，所以查询边界也必须去掉 tzinfo，否则整体错开一个时区；
3. 只统计 ``agent_type == "internal"``，与生活插件同口径，数字才和它的
   「本月花费」对得上。

任何一条出错都只让查询失败并给出可读原因，绝不让宿主崩掉。
"""

from __future__ import annotations

from typing import Any

from .cost_calc import Report, Usage, build_report, day_range, month_range


class QueryError(RuntimeError):
    """取数失败（连不上库、表不存在、驱动不支持等）。"""


#: 只统计内部 agent 的调用（插件自己发的 llm_generate 不写这张表）
AGENT_TYPE = "internal"


async def _run_select(context: Any, statements: list[Any]) -> list[Any]:
    """在同一个 session 里依次执行若干 ``select``，返回各自的结果行。"""

    db = context.get_db()
    if db is None:
        raise QueryError("拿不到 AstrBot 的数据库句柄")
    rows: list[Any] = []
    async with db.get_db() as session:
        for statement in statements:
            # execute() 是协程，必须 await；漏了会拿到 ChunkedIteratorResult
            result = await session.execute(statement)
            rows.append(list(result.all()))
    return rows


def _build_statements(
    *,
    today_start: Any,
    today_end: Any,
    month_start: Any,
    month_end: Any,
) -> list[Any]:
    """构造全部查询语句。

    拆出来是为了让「查询长什么样」能被单独检查（列名、过滤条件、分组键），
    不必真的连一个数据库。
    """

    from sqlalchemy import func, select

    from astrbot.core.db.po import ProviderStat as PS

    def _sums() -> tuple[Any, Any, Any, Any]:
        return (
            func.sum(PS.token_input_other),
            func.sum(PS.token_input_cached),
            func.sum(PS.token_output),
            func.count(PS.id),
        )

    def _period_range(start: Any, end: Any) -> list[Any]:
        return [
            PS.created_at >= start,
            PS.created_at < end,
            PS.agent_type == AGENT_TYPE,
        ]

    total_cols = _sums()

    today = select(*total_cols).where(*_period_range(today_start, today_end))
    month = select(*total_cols).where(*_period_range(month_start, month_end))
    overall = select(*total_cols).where(PS.agent_type == AGENT_TYPE)

    by_model = (
        select(PS.provider_id, PS.provider_model, *total_cols)
        .where(*_period_range(month_start, month_end))
        .group_by(PS.provider_id, PS.provider_model)
    )

    # 按天分组：SQLite 的 date() 直接截 UTC 日期，而我们要的是**本地**日期，
    # 所以先把边界整体平移回本地再分组，最后在 Python 侧只取 MM-DD。
    daily = (
        select(func.date(PS.created_at, "+480 minutes").label("d"), *total_cols)
        .where(*_period_range(month_start, month_end))
        .group_by("d")
        .order_by("d")
    )

    return [today, month, overall, by_model, daily]


_TAIL = ("input_other", "input_cached", "output", "calls")


def _normalize(
    rows: list[Any], *, keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    """按位置把行转成 ``{键..., input_other, input_cached, output, calls}``。"""

    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for index, key in enumerate(keys):
            item[key] = row[index] if index < len(row) else None
        for index, key in enumerate(_TAIL):
            position = len(keys) + index
            item[key] = row[position] if position < len(row) else 0
        out.append(item)
    return out


async def fetch_report(
    context: Any,
    *,
    now: float,
    prices: Any,
    monthly_budget: float,
    tz_offset_minutes: int = 480,
    top_models: int = 8,
) -> Report:
    """查一次完整报表。失败时返回 ``ok=False`` 的 ``Report``，不抛异常。"""

    today_start, today_end = day_range(now, tz_offset_minutes=tz_offset_minutes)
    month_start, month_end = month_range(now, tz_offset_minutes=tz_offset_minutes)

    try:
        statements = _build_statements(
            today_start=today_start,
            today_end=today_end,
            month_start=month_start,
            month_end=month_end,
        )
        today_rows, month_rows, total_rows, model_rows, daily_rows = (
            await _run_select(context, statements)
        )
    except QueryError as exc:
        return Report(ok=False, error=str(exc), now=now, prices=prices)
    except Exception as exc:  # noqa: BLE001 —— 统计坏了不该影响宿主
        return Report(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            now=now,
            prices=prices,
        )

    return build_report(
        now=now,
        prices=prices,
        monthly_budget=monthly_budget,
        today_rows=_normalize(today_rows, keys=()),
        month_rows=_normalize(month_rows, keys=()),
        total_rows=_normalize(total_rows, keys=()),
        by_model_rows=_normalize(model_rows, keys=("provider_id", "provider_model")),
        daily_rows=_normalize(daily_rows, keys=("created_at",)),
        tz_offset_minutes=tz_offset_minutes,
        top_models=top_models,
    )


def totals_of(rows: list[dict[str, Any]]) -> Usage:
    """把一组归一化行加总成 ``Usage``（供测试与调试）。"""

    usage = Usage()
    for row in rows:
        usage.add(row)
    return usage

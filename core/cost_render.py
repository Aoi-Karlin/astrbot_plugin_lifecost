# -*- coding: utf-8 -*-
"""把 ``Report`` 渲染成聊天消息（纯函数，便于测试）。

金额格式刻意分档：小额（< 1 元）保留 4 位小数。用两位小数显示 ``0.00``
会让「今天花了 0.37 元」这种真实数据看起来像没花钱，用户会以为统计坏了。
"""

from __future__ import annotations

from typing import Sequence

from .cost_calc import (
    DailyCost,
    ModelCost,
    PeriodCost,
    Report,
    Usage,
    days_in_month,
    progressed_days,
)

#: 条形图最长多少个字符
BAR_WIDTH = 12


def money(amount: float) -> str:
    """金额格式化：大额两位小数，小额四位小数。"""

    value = float(amount or 0.0)
    if value == 0:
        return "0.00 元"
    if abs(value) < 1:
        return f"{value:.4f} 元"
    return f"{value:.2f} 元"


def tokens(count: int) -> str:
    """token 数：上万用「万」，上亿用「亿」，否则千分位。"""

    value = int(count or 0)
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if value >= 10_000:
        return f"{value / 10_000:.2f} 万"
    return f"{value:,}"


def _bar(ratio: float, width: int = BAR_WIDTH) -> str:
    ratio = max(0.0, min(1.0, float(ratio or 0.0)))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _usage_line(usage: Usage) -> str:
    return (
        f"输入 {tokens(usage.input_other)} / "
        f"缓存 {tokens(usage.input_cached)} / "
        f"输出 {tokens(usage.output)}"
    )


def _period_block(title: str, period: PeriodCost, *, priced: bool) -> list[str]:
    lines = [f"{title}：{money(period.cost) if priced else '未配单价'}"]
    lines.append(f"  {_usage_line(period.usage)}　共 {tokens(period.usage.total_tokens)}")
    lines.append(f"  {period.calls} 次调用")
    return lines


def summary_card(report: Report, *, tz_offset_minutes: int = 480) -> str:
    """``/花费``：一屏看完今天、本月、预算与按模型/会话的拆分。"""

    if not report.ok:
        return f"💸 花费查询失败\n\n{report.error or '未知错误'}"

    lines = [f"💸 花费（{report.month}）", ""]
    lines += _period_block("今天", report.today, priced=report.priced)
    lines.append("")
    lines += _period_block("本月", report.this_month, priced=report.priced)

    if not report.priced:
        lines.append("")
        lines.append("⚠ 还没配单价：金额一律为 0，只能看 token 数。")
        lines.append("　去「单价」里填 输入 / 缓存输入 / 输出 的 元每百万 token。")

    if report.monthly_budget > 0:
        lines.append("")
        lines.append(
            f"本月预算：{money(report.monthly_budget)}　"
            f"剩余 {money(report.remaining)}"
        )
        lines.append(
            f"  已用 {_bar(1.0 - report.remaining_ratio)} "
            f"{(1.0 - report.remaining_ratio) * 100:.0f}%"
        )

    if report.priced and report.this_month.cost > 0:
        per_day = report.daily_average(tz_offset_minutes=tz_offset_minutes)
        projected = report.projected_month_cost(tz_offset_minutes=tz_offset_minutes)
        days = days_in_month(report.now, tz_offset_minutes=tz_offset_minutes)
        elapsed = progressed_days(report.now, tz_offset_minutes=tz_offset_minutes)
        lines.append("")
        lines.append(
            f"日均 {money(per_day)}（已过 {elapsed:.1f} 天）"
            f"，按此推算整月 {money(projected)}（{days} 天）"
        )

    if report.by_model:
        lines.append("")
        lines.append("按模型：")
        for model in report.by_model:
            name = model.model
            if len(name) > 32:
                name = name[:32] + "…"
            lines.append(f"  {name}：{money(model.cost) if report.priced else tokens(model.usage.total_tokens)}")

    if report.by_chat:
        lines.append("")
        lines.append("按会话（前几条）：")
        for chat in report.by_chat:
            lines.append(
                f"  {_short_umo(chat.umo)}："
                f"{money(chat.cost) if report.priced else tokens(chat.usage.total_tokens)}"
            )

    return "\n".join(lines)


def daily_card(report: Report, *, limit: int = 14) -> str:
    """``/花费 每天``：最近若干天的花费，带条形图。"""

    if not report.ok:
        return f"💸 花费查询失败\n\n{report.error or '未知错误'}"

    if not report.daily:
        return "💸 本月还没有调用记录。"

    rows = report.daily[-limit:] if limit > 0 else report.daily
    peak = max((row.cost for row in rows), default=0.0)
    if peak <= 0:
        # 没配单价时按 token 数画条：至少还能看出哪天用得多
        peak = max((row.usage.total_tokens for row in rows), default=0) or 1
        scale = lambda row: row.usage.total_tokens / peak  # noqa: E731
    else:
        scale = lambda row: row.cost / peak  # noqa: E731

    lines = [f"💸 每日花费（{report.month}）", ""]
    for row in rows:
        value = money(row.cost) if report.priced else tokens(row.usage.total_tokens)
        lines.append(f"{row.day}  {_bar(scale(row))}  {value}")
    if len(report.daily) > len(rows):
        lines.append(f"（只显示最近 {len(rows)} 天，共 {len(report.daily)} 天有记录）")
    return "\n".join(lines)


def _short_umo(umo: str) -> str:
    """把 ``platform:type:session`` 压成好认的短标签。"""

    text = str(umo or "").strip()
    if not text:
        return "(未知会话)"
    parts = text.split(":", 2)
    if len(parts) < 3:
        return text
    platform, mtype, session = parts
    kind = "群" if mtype == "GroupMessage" else ("私聊" if mtype == "FriendMessage" else mtype)
    return f"{platform} {kind} {session}"

# -*- coding: utf-8 -*-
"""花费查询插件：按 AstrBot 的 ``provider_stats`` 统计花了多少钱。

数据口径与「生活频率」插件一致（只统计 ``agent_type="internal"``），所以两个
插件的本月金额能对上。

设计原则：**只读**。这个插件从不写数据库、不改任何状态，查询失败也只回一条
可读的错误消息，不影响宿主。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core import cost_calc as C
from .core import cost_query as Q
from .core import cost_render as R

PLUGIN_NAME = "astrbot_plugin_lifecost"
TZ_OFFSET_MINUTES = 480  # 东八区；AstrBot 侧时间是 UTC，展示按本地日界


def _now() -> float:
    return time.time()


def _section(conf: dict[str, Any], name: str) -> dict[str, Any]:
    """取一个配置分组；类型不对时返回空 dict（WebUI 可能存成任何东西）。"""

    value = (conf or {}).get(name)
    return value if isinstance(value, dict) else {}


def _num(sec: dict[str, Any], key: str, default: float = 0.0) -> float:
    """读一个数字配置，坏值回落默认值。"""

    try:
        value = float(sec.get(key, default))
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
        return default
    return value


def _flag(sec: dict[str, Any], key: str, default: bool = False) -> bool:
    value = sec.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(value)


@register(
    PLUGIN_NAME,
    "AstralSight",
    "按 AstrBot 的 provider_stats 统计模型花费：今天、本月、按模型、按天。",
    "1.0.0",
)
class LifeCostPlugin(Star):
    """花费查询。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.conf: dict[str, Any] = config if isinstance(config, dict) else {}
        self._warned: set[str] = set()

    async def initialize(self) -> None:
        """启动时打一条口径说明，方便事后核对数字。"""
        logger.info(
            f"[{PLUGIN_NAME}] 已启动：只统计 agent_type=internal 的调用，"
            f"按本地 UTC+{TZ_OFFSET_MINUTES // 60} 划分日界。"
        )

    # ------------------------------------------------------------ 配置读取

    def _prices(self) -> C.Prices:
        sec = _section(self.conf, "pricing")
        return C.Prices(
            input_per_mtok=_num(sec, "input_per_mtok"),
            cached_input_per_mtok=_num(sec, "cached_input_per_mtok"),
            output_per_mtok=_num(sec, "output_per_mtok"),
        )

    def _budget(self) -> float:
        return max(0.0, _num(_section(self.conf, "budget"), "monthly_budget"))

    def _top_n(self, sec: dict[str, Any], key: str, default: int) -> int:
        return max(0, int(_num(sec, key, float(default))))

    def _admin_only(self) -> bool:
        return _flag(_section(self.conf, "access"), "admin_only", True)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """是否管理员：``event.is_admin()`` 或全局 ``admins_id`` 里有他。"""

        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            admins = (self.context.get_config() or {}).get("admins_id", []) or []
        except Exception:  # noqa: BLE001
            admins = []
        sender = str(event.get_sender_id() or "")
        return sender in {str(item) for item in admins}

    def _check_access(self, event: AstrMessageEvent) -> bool:
        """是否允许查询。``admin_only=false`` 时人人可查。"""

        if not self._admin_only():
            return True
        return self._is_admin(event)

    # ------------------------------------------------------------ 查询

    async def _report(self) -> C.Report:
        """查一次报表；任何异常都变成 ``ok=False``，不抛给宿主。"""

        try:
            return await Q.fetch_report(
                self.context,
                now=_now(),
                prices=self._prices(),
                monthly_budget=self._budget(),
                tz_offset_minutes=TZ_OFFSET_MINUTES,
                top_models=self._top_n(_section(self.conf, "display"), "top_models", 8),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 查询花费失败")
            return C.Report(
                ok=False, error=f"{type(exc).__name__}: {exc}", now=_now()
            )

    # ------------------------------------------------------------ 指令

    @filter.command_group("花费")
    def cost_group(self):
        """花费相关指令。"""
        pass

    @cost_group.command("查询")
    async def cost_summary(self, event: AstrMessageEvent):
        """查看今天与本月的花费、预算余量、按模型的拆分。

        Args:
            event: 消息事件。
        """
        if not self._check_access(event):
            yield event.plain_result("只有管理员能查花费。")
            return
        report = await self._report()
        yield event.plain_result(R.summary_card(report, tz_offset_minutes=TZ_OFFSET_MINUTES))

    @cost_group.command("每天")
    async def cost_daily(self, event: AstrMessageEvent):
        """查看本月每天的花费（带条形图）。

        Args:
            event: 消息事件。
        """
        if not self._check_access(event):
            yield event.plain_result("只有管理员能查花费。")
            return
        report = await self._report()
        limit = self._top_n(_section(self.conf, "display"), "daily_days", 14)
        yield event.plain_result(R.daily_card(report, limit=limit))

    @cost_group.command("模型")
    async def cost_models(self, event: AstrMessageEvent):
        """查看本月按模型拆分的花费明细。

        Args:
            event: 消息事件。
        """
        if not self._check_access(event):
            yield event.plain_result("只有管理员能查花费。")
            return
        report = await self._report()
        if not report.ok:
            yield event.plain_result(f"💸 花费查询失败\n\n{report.error}")
            return
        if not report.by_model:
            yield event.plain_result("本月还没有调用记录。")
            return
        lines = [f"💸 按模型（{report.month}）", ""]
        for item in report.by_model:
            lines.append(f"{item.model}")
            lines.append(
                f"  花费 {R.money(item.cost) if report.priced else '未配单价'}"
                f"　调用 {item.calls} 次"
            )
            lines.append(
                f"  输入 {R.tokens(item.usage.input_other)}"
                f" / 缓存 {R.tokens(item.usage.input_cached)}"
                f" / 输出 {R.tokens(item.usage.output)}"
            )
        yield event.plain_result("\n".join(lines))


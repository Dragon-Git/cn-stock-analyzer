"""
神华分析系统 - A 股交易日判断
=============================
简单的 A 股交易日判断：先看星期几，再调用 akshare 的交易日历接口
确认。如果 akshare 失败就只看星期。
交易日历通过 src.cache 跨 run 缓存 1 周 (一年只变一次)。
"""
from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Set

logger = logging.getLogger(__name__)

# 内存缓存 (本 run 内, 避免每次 is_trading_day 都查)
_TRADING_DAYS_CACHE: Set[str] = set()
_CACHE_FETCHED = False


def _fetch_trading_days() -> Set[str]:
    """调用 akshare 拉取近 1 年 A 股交易日历 (优先用 cache)"""
    # 跨 run 复用 (TTL 1 周, 交易日历一年只变一次)
    try:
        from . import cache
        cached = cache.get("trading_days_v1")
        if cached is not None:
            logger.info("交易日历命中缓存 (%d 天)", len(cached))
            return set(cached)
    except Exception:  # noqa: BLE001
        pass

    # lazy import akshare (trading_calendar 这个模块被 analyzer 强制 import,
    # 但 akshare 实际只在 fetch_trading_days 调用时才需要)
    try:
        from . import cache as _c
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            return set()
        days = {str(d) for d in df["trade_date"].astype(str)}
        # 写 cache
        _c.set_("trading_days_v1", list(days), _c.TTL_TRADING_CALENDAR)
        return days
    except Exception as e:  # noqa: BLE001
        logger.warning("拉取交易日历失败: %s", e)
        return set()


def is_trading_day(d: date = None) -> bool:
    """判断指定日期是否为 A 股交易日"""
    global _CACHE_FETCHED
    d = d or date.today()
    # 周末直接返回 False
    if d.weekday() >= 5:
        return False

    if not _CACHE_FETCHED:
        days = _fetch_trading_days()
        if days:
            _TRADING_DAYS_CACHE.update(days)
        _CACHE_FETCHED = True

    # 如果缓存为空（akshare 拉失败），回退到只看星期
    if not _TRADING_DAYS_CACHE:
        return d.weekday() < 5

    return d.strftime("%Y-%m-%d") in _TRADING_DAYS_CACHE


if __name__ == "__main__":
    today = date.today()
    print(f"今天 {today} ({today.strftime('%A')}) 是 A 股交易日: {is_trading_day(today)}")

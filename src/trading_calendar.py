"""
神华分析系统 - A 股交易日判断
=============================
简单的 A 股交易日判断：先看星期几，再调用 akshare 的交易日历接口
确认。如果 akshare 失败就只看星期。
"""
from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Set

logger = logging.getLogger(__name__)

# 缓存 (模块内)
_TRADING_DAYS_CACHE: Set[str] = set()
_CACHE_FETCHED = False


def _fetch_trading_days() -> Set[str]:
    """调用 akshare 拉取近 1 年 A 股交易日历"""
    import akshare as ak
    global _CACHE_FETCHED
    try:
        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            return set()
        # 列为 trade_date
        return {str(d) for d in df["trade_date"].astype(str)}
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

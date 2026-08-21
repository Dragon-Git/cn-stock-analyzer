"""
神华分析系统 - 配置
==================
集中管理股票代码、分析时段、akshare 字段映射等。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


# ===== 标的配置 =====

@dataclass
class StockConfig:
    """单只股票的配置"""
    symbol: str  # 6 位代码, 如 601088
    name: str    # 中文名
    market: str  # sh / sz / bj
    ak_prefix: str  # akshare 用前缀 (sh/sz/bj)
    sina_symbol: str  # 新浪行情 symbol, 如 sh601088

    @property
    def ak_daily_symbol(self) -> str:
        return f"{self.ak_prefix}{self.symbol}"


# 中国神华 A 股
SHENHUA = StockConfig(
    symbol="601088",
    name="中国神华",
    market="sh",
    ak_prefix="sh",
    sina_symbol="sh601088",
)


# ===== 时段配置 =====
# 一天 5 个分析时段（按 A 股交易日）。GitHub Actions cron 在 UTC 触发，
# 字段是 "分钟 小时 * * 周" 格式。下表时区: UTC，参见 README。
TIME_SLOTS: List[Dict] = [
    {
        "id": "pre_market",
        "label": "盘前",
        "cron_utc": "0 0 * * 1-5",       # UTC 00:00 = 北京 08:00
        "focus": "隔夜美股、昨日 K 线复盘、关键位预判、竞价预期",
    },
    {
        "id": "post_auction",
        "label": "竞价结束",
        "cron_utc": "35 1 * * 1-5",      # UTC 01:35 = 北京 09:35
        "focus": "集合竞价结果、开盘缺口、量能初判、早盘策略",
    },
    {
        "id": "noon",
        "label": "午间",
        "cron_utc": "35 3 * * 1-5",      # UTC 03:35 = 北京 11:35
        "focus": "上午 K 线、上午量能、午后预判、KDJ/MACD 状态",
    },
    {
        "id": "post_close",
        "label": "收盘后",
        "cron_utc": "10 7 * * 1-5",      # UTC 07:10 = 北京 15:10
        "focus": "全天 K 线、MACD/RSI/布林全天态、量价总结",
    },
    {
        "id": "evening",
        "label": "盘后深度",
        "cron_utc": "35 7 * * 1-5",      # UTC 07:35 = 北京 15:35
        "focus": "资金流向、龙虎榜（如有）、估值与基本面综合",
    },
]


def get_slot(slot_id: str) -> Dict:
    """按 id 获取时段配置"""
    for s in TIME_SLOTS:
        if s["id"] == slot_id:
            return s
    raise KeyError(f"Unknown slot id: {slot_id}")


# ===== A 股交易时间（仅用于报告中语义描述） =====
A_SHARE_SESSIONS = [
    ("集合竞价", "09:15-09:25"),
    ("连续竞价上午", "09:30-11:30"),
    ("午间休市", "11:30-13:00"),
    ("连续竞价下午", "13:00-15:00"),
    ("盘后固定价格交易", "15:05-15:30"),
]

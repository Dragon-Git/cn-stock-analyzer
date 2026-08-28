"""
神华分析系统 - 配置
=====================
股票池和时段的统一配置。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List

# 中国 A 股时区 (UTC+8, 不含夏令时)
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_beijing() -> datetime:
    """返回当前 Asia/Shanghai 时区的 datetime (带 tzinfo)"""
    return datetime.now(BEIJING_TZ)


@dataclass
class StockConfig:
    """单只股票的配置"""
    symbol: str          # 6 位代码, 如 601088
    name: str            # 中文名
    market: str          # sh / sz / bj
    ak_prefix: str       # akshare 用前缀 (sh/sz/bj)
    sina_symbol: str     # 新浪行情 symbol, 如 sh601088
    industry: str = ""   # 行业关键词, 用于 cache key / 行业对比

    @property
    def ak_daily_symbol(self) -> str:
        return f"{self.ak_prefix}{self.symbol}"


# A 股监控池 — 蓝筹/白马/科技代表
# 改这里增加/删除股票
STOCKS: List[StockConfig] = [
    StockConfig(
        symbol="601088",
        name="中国神华",
        market="sh",
        ak_prefix="sh",
        sina_symbol="sh601088",
        industry="煤炭",
    ),
    StockConfig(
        symbol="600900",
        name="长江电力",
        market="sh",
        ak_prefix="sh",
        sina_symbol="sh600900",
        industry="电力",
    ),
    StockConfig(
        symbol="002371",
        name="北方华创",
        market="sz",
        ak_prefix="sz",
        sina_symbol="sz002371",
        industry="半导体",
    ),
    StockConfig(
        symbol="601988",
        name="中国银行",
        market="sh",
        ak_prefix="sh",
        sina_symbol="sh601988",
        industry="银行",
    ),
    StockConfig(
        symbol="600026",
        name="中远海能",
        market="sh",
        ak_prefix="sh",
        sina_symbol="sh600026",
        industry="航运",
    ),
    StockConfig(
        symbol="600584",
        name="长电科技",
        market="sh",
        ak_prefix="sh",
        sina_symbol="sh600584",
        industry="半导体",
    ),
]


# 兼容旧名字, 部分 import 还在用
SHENHUA = STOCKS[0]


# 4 个时段定义 (北京时间)
# 9:15 集合竞价开盘 / 9:35 开盘后 5 分钟 / 12:00 午间 / 15:00 收盘
TIME_SLOTS = [
    {
        "id": "pre_open",          # 集合竞价盘前
        "label": "盘前",
        "cron_utc": "15 1 * * 1-5",  # 09:15 北京
        "focus": "集合竞价 + 外盘 + 昨 K 趋势",
    },
    {
        "id": "post_auction",      # 集合竞价后 / 开盘 5 分钟
        "label": "开盘",
        "cron_utc": "35 1 * * 1-5",  # 09:35 北京
        "focus": "开盘价 + 集合竞价强度 + 板块异动",
    },
    {
        "id": "noon",              # 午间
        "label": "午间",
        "cron_utc": "0 4 * * 1-5",   # 12:00 北京
        "focus": "上午量价 + 板块轮动 + 北向",
    },
    {
        "id": "post_close",        # 收盘
        "label": "收盘",
        "cron_utc": "0 7 * * 1-5",   # 15:00 北京
        "focus": "全天 K 线 + 技术指标 + 量价总结",
    },
]


def get_slot(slot_id: str) -> dict:
    for s in TIME_SLOTS:
        if s["id"] == slot_id:
            return s
    raise ValueError(f"Unknown slot: {slot_id}")


# 报告输出目录
REPORTS_DIR_NAME = "reports"

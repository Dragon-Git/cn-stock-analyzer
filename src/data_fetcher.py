"""
神华分析系统 - 数据拉取
======================
封装 akshare 拉取：历史 K 线、实时行情、个股基本信息、估值、财务摘要、
主力资金流、北向资金、融资融券。所有 akshare 接口都做重试 + 失败容错，
任一接口失败不阻塞整体报告生成。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import akshare as ak
import pandas as pd

from .config import SHENHUA, StockConfig

logger = logging.getLogger(__name__)


# ===== 工具 =====
def _retry(fn, *args, retries: int = 2, sleep: float = 1.0, **kwargs):
    """简单重试包装"""
    last_err: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("akshare call %s failed (%d/%d): %s",
                           fn.__name__, i + 1, retries + 1, e)
            if i < retries:
                time.sleep(sleep)
    raise last_err  # type: ignore[misc]


def _safe_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _safe_str(x: Any, default: str = "N/A") -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return default
    s = str(x).strip()
    return s if s else default


# ===== 数据类 =====
@dataclass
class KLineBundle:
    """K 线 + 衍生量能字段"""
    df: pd.DataFrame              # 原始历史 K 线 (date, open, high, low, close, volume, amount)
    today: Optional[pd.Series]    # 最新一个交易日 (含当日盘中数据，由实时接口补充)


@dataclass
class MarketSnapshot:
    """实时行情快照"""
    price: float = 0.0            # 当前价
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    pre_close: float = 0.0        # 昨收
    change: float = 0.0           # 涨跌额
    change_pct: float = 0.0       # 涨跌幅 %
    volume: float = 0.0           # 成交量 (手)
    amount: float = 0.0           # 成交额 (元)
    turnover_pct: float = 0.0     # 换手率 %
    pe: Optional[float] = None    # 滚动 PE
    pb: Optional[float] = None    # PB
    total_mv: Optional[float] = None  # 总市值 (亿)
    circ_mv: Optional[float] = None   # 流通市值 (亿)
    as_of: str = ""               # 快照时间 ISO


@dataclass
class FundFlowSnapshot:
    """资金面快照"""
    main_net_inflow: Optional[float] = None     # 主力净流入 (元)
    super_net_inflow: Optional[float] = None    # 超大单净流入
    big_net_inflow: Optional[float] = None      # 大单净流入
    mid_net_inflow: Optional[float] = None      # 中单净流入
    small_net_inflow: Optional[float] = None    # 小单净流入
    north_net_inflow: Optional[float] = None    # 北向资金净流入 (元)
    margin_balance: Optional[float] = None     # 融资余额 (元)
    as_of: str = ""


@dataclass
class FinancialSnapshot:
    """基本面快照"""
    industry: str = "N/A"
    main_business: str = "N/A"
    list_date: str = "N/A"
    total_shares: Optional[float] = None
    circ_shares: Optional[float] = None
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    ps_ttm: Optional[float] = None
    pcf: Optional[float] = None
    roe: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    debt_ratio: Optional[float] = None
    revenue_latest: Optional[float] = None       # 最新报告期营收 (元)
    revenue_yoy: Optional[float] = None
    net_profit_latest: Optional[float] = None
    net_profit_yoy: Optional[float] = None
    eps_latest: Optional[float] = None
    div_yield: Optional[float] = None            # 股息率 %
    report_date: str = "N/A"


# ===== 核心拉取 =====
def fetch_history_kline(stock: StockConfig = SHENHUA,
                        days: int = 400) -> pd.DataFrame:
    """
    拉取历史日 K 线（含近 1 年数据，足够算 MA60/布林/RSI 等）
    使用 akshare 的东方财富接口，比新浪更全。
    """
    end = datetime.now()
    start = end - timedelta(days=days)
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    logger.info("拉取 %s K 线 %s -> %s", stock.name, start_str, end_str)
    df = _retry(
        ak.stock_zh_a_hist,
        symbol=stock.symbol,
        period="daily",
        start_date=start_str,
        end_date=end_str,
        adjust="qfq",  # 前复权
    )
    if df is None or df.empty:
        return pd.DataFrame()

    # 统一列名
    rename = {
        "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume", "成交额": "amount",
        "振幅": "amplitude", "涨跌幅": "change_pct", "涨跌额": "change",
        "换手率": "turnover_pct",
    }
    df = df.rename(columns=rename)
    keep = ["date", "open", "high", "low", "close", "volume", "amount",
            "amplitude", "change_pct", "change", "turnover_pct"]
    cols = [c for c in keep if c in df.columns]
    df = df[cols].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def fetch_realtime_snapshot(stock: StockConfig = SHENHUA) -> MarketSnapshot:
    """
    拉取实时行情快照 (东方财富接口，全市场一次拉，本地过滤)
    """
    snap = MarketSnapshot()
    snap.as_of = datetime.now().isoformat(timespec="seconds")
    try:
        df = _retry(ak.stock_zh_a_spot_em)
    except Exception as e:  # noqa: BLE001
        logger.error("拉取实时行情失败: %s", e)
        return snap
    if df is None or df.empty:
        return snap

    row = df[df["代码"] == stock.symbol]
    if row.empty:
        logger.warning("实时行情里没找到 %s", stock.symbol)
        return snap
    r = row.iloc[0]
    snap.price = _safe_float(r.get("最新价"), 0.0) or 0.0
    snap.open = _safe_float(r.get("今日开盘价"), 0.0) or 0.0
    snap.high = _safe_float(r.get("最高价"), 0.0) or 0.0
    snap.low = _safe_float(r.get("最低价"), 0.0) or 0.0
    snap.pre_close = _safe_float(r.get("昨收"), 0.0) or 0.0
    snap.change = _safe_float(r.get("涨跌额"), 0.0) or 0.0
    snap.change_pct = _safe_float(r.get("涨跌幅"), 0.0) or 0.0
    snap.volume = _safe_float(r.get("成交量"), 0.0) or 0.0
    snap.amount = _safe_float(r.get("成交额"), 0.0) or 0.0
    snap.turnover_pct = _safe_float(r.get("换手率"), 0.0) or 0.0
    snap.pe = _safe_float(r.get("市盈率-动态"))
    snap.pb = _safe_float(r.get("市净率"))
    snap.total_mv = _safe_float(r.get("总市值"))   # 单位: 元
    snap.circ_mv = _safe_float(r.get("流通市值"))  # 单位: 元
    if snap.total_mv is not None:
        snap.total_mv = snap.total_mv / 1e8  # 转亿
    if snap.circ_mv is not None:
        snap.circ_mv = snap.circ_mv / 1e8
    return snap


def fetch_individual_info(stock: StockConfig = SHENHUA) -> FinancialSnapshot:
    """拉取个股基本信息 + 估值/财务摘要"""
    fs = FinancialSnapshot()
    try:
        df = _retry(ak.stock_individual_info_em, symbol=stock.symbol)
        if df is not None and not df.empty:
            kv = dict(zip(df["item"], df["value"]))
            fs.industry = _safe_str(kv.get("行业"), "N/A")
            fs.main_business = _safe_str(kv.get("主营业务"), "N/A")
            fs.list_date = _safe_str(kv.get("上市时间"), "N/A")
            fs.total_shares = _safe_float(kv.get("总股本"))
            fs.circ_shares = _safe_float(kv.get("流通股本"))
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_individual_info_em 失败: %s", e)

    # 财务摘要 - 东方财富
    try:
        df = _retry(ak.stock_financial_abstract, symbol=stock.symbol)
        if df is not None and not df.empty:
            # 列: 指标, 2025-06-30, 2024-12-31, ...
            rmap = {
                "基本每股收益": "eps_latest",
                "每股净资产": "_skip",
                "加权净资产收益率": "roe",
                "毛利率": "gross_margin",
                "净利率": "net_margin",
                "资产负债率": "debt_ratio",
                "营业总收入": "revenue_latest",
                "营业总收入同比": "revenue_yoy",
                "归属母公司净利润": "net_profit_latest",
                "归属母公司净利润同比": "net_profit_yoy",
            }
            for _, row in df.iterrows():
                key = _safe_str(row.iloc[0])
                if key in rmap and rmap[key] != "_skip":
                    val = _safe_float(row.iloc[1])
                    if val is not None:
                        setattr(fs, rmap[key], val)
            # 报告期 = 第一列
            if len(df.columns) > 1:
                fs.report_date = _safe_str(df.columns[1])
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_financial_abstract 失败: %s", e)

    return fs


def fetch_industry_panel(stock: StockConfig = SHENHUA) -> Dict[str, float]:
    """
    拉取煤炭板块当日表现，与个股做对比。
    返回: {"sector_name": str, "sector_change_pct": float, "sh_index_change_pct": float}
    """
    out: Dict[str, float] = {
        "sector_name": "N/A",
        "sector_change_pct": 0.0,
        "sh_index_change_pct": 0.0,
    }
    try:
        df = _retry(ak.stock_board_industry_name_em)
        if df is not None and not df.empty:
            # 找煤炭
            mask = df["板块名称"].astype(str).str.contains("煤炭", na=False)
            if mask.any():
                row = df[mask].iloc[0]
                out["sector_name"] = str(row.get("板块名称", "N/A"))
                out["sector_change_pct"] = _safe_float(row.get("涨跌幅"), 0.0) or 0.0
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_board_industry_name_em 失败: %s", e)
    try:
        df = _retry(ak.stock_zh_index_spot_em, symbol="上证指数")
        if df is not None and not df.empty:
            out["sh_index_change_pct"] = _safe_float(df.iloc[0].get("涨跌幅"), 0.0) or 0.0
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_zh_index_spot_em 失败: %s", e)
    return out


def fetch_fund_flow(stock: StockConfig = SHENHUA) -> FundFlowSnapshot:
    """拉取当日主力资金流 + 北向 + 融资余额"""
    fs = FundFlowSnapshot()
    fs.as_of = datetime.now().isoformat(timespec="seconds")
    fs.as_of = datetime.now().isoformat(timespec="seconds")
    # 主力资金流 (个股)
    try:
        df = _retry(ak.stock_individual_fund_flow, stock=stock.symbol,
                    market=stock.market)
        if df is not None and not df.empty:
            r = df.iloc[0]
            fs.main_net_inflow = _safe_float(r.get("主力净流入-净额"))
            fs.super_net_inflow = _safe_float(r.get("超大单净流入-净额"))
            fs.big_net_inflow = _safe_float(r.get("大单净流入-净额"))
            fs.mid_net_inflow = _safe_float(r.get("中单净流入-净额"))
            fs.small_net_inflow = _safe_float(r.get("小单净流入-净额"))
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_individual_fund_flow 失败: %s", e)

    # 北向资金持股 - 取最新一日的"今日持股变化"作为净流入代理
    try:
        df = _retry(ak.stock_hsgt_individual_em, symbol=stock.sina_symbol)
        if df is not None and not df.empty:
            # df 列: 持股日期, 当日收盘价, 持股数量, 持股市值, 持股变化 ...
            r = df.iloc[0]
            chg = _safe_float(r.get("持股变化"))
            price = _safe_float(r.get("当日收盘价"))
            if chg is not None and price is not None:
                fs.north_net_inflow = chg * price
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_hsgt_individual_em 失败: %s", e)

    # 融资融券
    try:
        df = _retry(ak.stock_margin_underlying_info_szse if stock.market == "sz"
                    else ak.stock_margin_underlying_info_sse, symbol=stock.symbol)
        if df is not None and not df.empty:
            r = df.iloc[0]
            fs.margin_balance = _safe_float(r.get("融资余额"))
    except Exception as e:  # noqa: BLE001
        logger.warning("融资融券 失败: %s", e)

    return fs

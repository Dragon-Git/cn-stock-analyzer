"""
神华分析系统 - 数据拉取
======================
封装 akshare 拉取：历史 K 线、实时行情、个股基本信息、估值、财务摘要、
主力资金流、北向资金、融资融券。所有 akshare 接口都做重试 + 失败容错，
任一接口失败不阻塞整体报告生成。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import akshare as ak
import pandas as pd
import requests

from .config import SHENHUA, StockConfig
from . import cache

logger = logging.getLogger(__name__)


# ===== 工具 =====
def _retry(fn, *args, retries: int = 1, sleep: float = 0.1, **kwargs):
    """简单重试包装（默认 1 次重试，避免长时间阻塞 Action）"""
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


# ===== 腾讯直连 =====
def _tencent_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """
    直连腾讯财经 API 拉取单只股票实时行情，作为最稳兜底。
    https://qt.gtimg.cn/q=sh601088
    返回 v_sh601088="1~中国神华~601088~46.55~46.90~46.34~4606998~..."
    """
    if not symbol.startswith(("sh", "sz", "bj")):
        symbol = f"sh{symbol}"
    url = f"https://qt.gtimg.cn/q={symbol}"
    try:
        r = requests.get(url, timeout=8,
                        headers={"User-Agent": "Mozilla/5.0"})
        text = r.text.strip()
        if "=" not in text or '""' in text:
            return None
        # 提取 v_sh601088="..." 中的内容
        val = text.split('="', 1)[1].rstrip('";')
        parts = val.split("~")
        if len(parts) < 40:
            return None
        # 字段顺序参考 https://stock.gtimg.cn/data/index.php
        # 0: 未知 1: 名称 2: 代码 3: 当前价 4: 昨收 5: 今开 6: 成交量(手)
        # 30: 时间戳 31: 涨跌额 32: 涨跌幅(%) 33: 最高 34: 最低
        # 38: 换手率(%) 39: PE 44: 流通市值(亿) 45: 总市值(亿) 41/42: 涨停/跌停价
        return {
            "name": parts[1],
            "symbol": parts[2],
            "price": float(parts[3]) if parts[3] else None,
            "pre_close": float(parts[4]) if parts[4] else None,
            "open": float(parts[5]) if parts[5] else None,
            "volume_hand": int(float(parts[6])) if parts[6] else 0,  # 手
            "high": float(parts[33]) if len(parts) > 33 and parts[33] else None,
            "low": float(parts[34]) if len(parts) > 34 and parts[34] else None,
            "change_pct": float(parts[32]) if len(parts) > 32 and parts[32] else None,
            "change": float(parts[31]) if len(parts) > 31 and parts[31] else None,
            "turnover_pct": float(parts[38]) if len(parts) > 38 and parts[38] else None,
            "pe": float(parts[39]) if len(parts) > 39 and parts[39] else None,
            "circ_mv_yi": float(parts[44]) if len(parts) > 44 and parts[44] else None,
            "total_mv_yi": float(parts[45]) if len(parts) > 45 and parts[45] else None,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("腾讯直连 %s 失败: %s", symbol, e)
        return None


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
    industry: str = ""
    main_business: str = ""
    list_date: str = ""
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
    report_date: str = ""


# ===== 核心拉取 =====
def fetch_history_kline(stock: StockConfig = SHENHUA,
                        days: int = 400) -> pd.DataFrame:
    """
    拉取历史日 K 线。多源 fallback：
      1) 新浪 (ak.stock_zh_a_daily, qfq 前复权) — 最稳
      2) 东方财富 (ak.stock_zh_a_hist) — 数据最全但易被封
    结果缓存 6 小时，避免每个时段重复拉。
    """
    # === cache check ===
    cache_key = cache.kline_key(stock.symbol, days, "qfq")
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("K 线命中缓存 (%d 条, key=%s)", len(cached), cache_key)
        df_cached = pd.DataFrame(cached)
        if "date" in df_cached.columns:
            df_cached["date"] = pd.to_datetime(df_cached["date"])
        return df_cached

    end = datetime.now()
    start = end - timedelta(days=days)
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    logger.info("拉取 %s K 线 %s -> %s", stock.name, start_str, end_str)

    df = pd.DataFrame()
    source: Optional[str] = None  # 数据源标识, 决定 volume 单位是否需要转换

    # 1) 新浪: stock_zh_a_daily(symbol="sh601088", qfq=True)
    try:
        df = _retry(
            ak.stock_zh_a_daily,
            symbol=stock.sina_symbol,
            start_date=start_str,
            end_date=end_str,
            adjust="qfq",
        )
        if df is not None and not df.empty:
            source = "sina"
            logger.info("新浪源获取 K 线 %d 条", len(df))
    except Exception as e:  # noqa: BLE001
        logger.warning("新浪源失败: %s", e)

    # 2) 东方财富 fallback
    if df is None or df.empty:
        try:
            df = _retry(
                ak.stock_zh_a_hist,
                symbol=stock.symbol,
                period="daily",
                start_date=start_str,
                end_date=end_str,
                adjust="qfq",
            )
            if df is not None and not df.empty:
                source = "em"
                logger.info("东方财富源获取 K 线 %d 条", len(df))
        except Exception as e:  # noqa: BLE001
            logger.warning("东方财富源失败: %s", e)

    if df is None or df.empty:
        return pd.DataFrame()

    # 统一列名（两个源的列名略有差异）
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
    # volume 单位:
    #   - 新浪 (ak.stock_zh_a_daily) 返回的是"股", 需 /100 转为"手"
    #   - 东财 (ak.stock_zh_a_hist) 返回的是"手", 不动
    # 之前用 avg_vol > 1e7 启发式判断源, 对中小盘 (北方华创/长电科技/中远海能)
    # 给出错误单位, 已被替换为 source 显式判断。
    if "volume" in df.columns and source == "sina":
        df["volume"] = df["volume"] / 100.0
    # === cache write ===
    try:
        # 转 dict 时把 Timestamp/date 序列化为 ISO 字符串
        df_for_cache = df.copy()
        if "date" in df_for_cache.columns and pd.api.types.is_datetime64_any_dtype(
            df_for_cache["date"]
        ):
            df_for_cache["date"] = df_for_cache["date"].dt.strftime("%Y-%m-%d")
        records = df_for_cache.to_dict(orient="records")
        cache.set_(cache_key, records, cache.TTL_HISTORY_KLINE)
        logger.info("K 线已缓存 (%d 条, TTL=%ds)", len(records), cache.TTL_HISTORY_KLINE)
    except Exception as e:  # noqa: BLE001
        logger.warning("K 线缓存失败: %s", e)
    return df


def fetch_realtime_snapshot(stock: StockConfig = SHENHUA) -> MarketSnapshot:
    """
    拉取实时行情快照。多源 fallback：**腾讯直连优先** (单 symbol 直连, 0.2s) →
    新浪 → 东方财富 (都拉全市场 5000+ 股票, 30s+)。
    """
    snap = MarketSnapshot()
    snap.as_of = datetime.now().isoformat(timespec="seconds")
    # 0) 腾讯直连 (首选) - 单 symbol, 0.2s, 最稳
    try:
        q = _tencent_quote(stock.sina_symbol)
        if q and q.get("price") is not None:
            snap.price = q["price"]
            snap.open = q.get("open") or 0.0
            snap.high = q.get("high") or 0.0
            snap.low = q.get("low") or 0.0
            snap.pre_close = q.get("pre_close") or 0.0
            snap.change = q.get("change") or 0.0
            snap.change_pct = q.get("change_pct") or 0.0
            snap.volume = float(q.get("volume_hand") or 0)
            snap.turnover_pct = q.get("turnover_pct") or 0.0
            snap.pe = q.get("pe")
            snap.total_mv = q.get("total_mv_yi")  # 腾讯直接给亿
            snap.circ_mv = q.get("circ_mv_yi")
            logger.info("腾讯直连获取 %s 行情成功 (优先路径)", stock.sina_symbol)
            return snap
    except Exception as e:  # noqa: BLE001
        logger.warning("腾讯直连 失败: %s", e)
    # 1) 新浪 (备选) - stock_zh_a_spot 全市场, 慢
    try:
        df = _retry(ak.stock_zh_a_spot, retries=1, sleep=0.3)
        if df is not None and not df.empty:
            row = df[df["代码"] == stock.symbol]
            if not row.empty:
                return _parse_sina_spot(snap, row.iloc[0])
    except Exception as e:  # noqa: BLE001
        logger.warning("新浪 spot 失败: %s", e)
    # 2) 东方财富 (最后兜底)
    try:
        df = _retry(ak.stock_zh_a_spot_em, retries=1, sleep=0.3)
        if df is not None and not df.empty:
            row = df[df["代码"] == stock.symbol]
            if not row.empty:
                return _parse_em_spot(snap, row.iloc[0])
    except Exception as e:  # noqa: BLE001
        logger.warning("东财 spot 失败: %s", e)
    return snap


def _parse_em_spot(snap: MarketSnapshot, r) -> MarketSnapshot:
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
    snap.total_mv = _safe_float(r.get("总市值"))
    snap.circ_mv = _safe_float(r.get("流通市值"))
    if snap.total_mv is not None:
        snap.total_mv /= 1e8
    if snap.circ_mv is not None:
        snap.circ_mv /= 1e8
    return snap


def _parse_sina_spot(snap: MarketSnapshot, r) -> MarketSnapshot:
    """新浪源字段名跟东财不一样，做个映射"""
    # 新浪 stock_zh_a_spot 的列：代码, 名称, 最新价, 涨跌额, 涨跌幅, 买入, 卖出,
    # 昨收, 今开, 最高, 最低, 成交量, 成交额, ...
    snap.price = _safe_float(r.get("最新价"), 0.0) or 0.0
    snap.open = _safe_float(r.get("今开"), 0.0) or 0.0
    snap.high = _safe_float(r.get("最高"), 0.0) or 0.0
    snap.low = _safe_float(r.get("最低"), 0.0) or 0.0
    snap.pre_close = _safe_float(r.get("昨收"), 0.0) or 0.0
    snap.change = _safe_float(r.get("涨跌额"), 0.0) or 0.0
    snap.change_pct = _safe_float(r.get("涨跌幅"), 0.0) or 0.0
    snap.volume = _safe_float(r.get("成交量"), 0.0) or 0.0
    snap.amount = _safe_float(r.get("成交额"), 0.0) or 0.0
    # 新浪没换手率/PE/PB，留空
    return snap


def fetch_individual_info(stock: StockConfig = SHENHUA) -> FinancialSnapshot:
    """拉取个股基本信息 + 估值/财务摘要（多源 fallback）。缓存 1 周。"""
    cache_key = cache.info_key(stock.symbol)
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("个股基本面命中缓存: %s", cache_key)
        return FinancialSnapshot(**cached)
    fs = FinancialSnapshot()
    # 1) 个股基本信息 - 同花顺
    try:
        df = _retry(ak.stock_zyjs_ths, symbol=stock.symbol)
        if df is not None and not df.empty:
            # df 列: 主营业务, ...
            for _, row in df.iterrows():
                k, v = str(row.iloc[0]), str(row.iloc[1]) if len(row) > 1 else ""
                if "主营" in k or "业务" in k:
                    fs.main_business = v[:200]
                elif "行业" in k:
                    fs.industry = v
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_zyjs_ths 失败: %s", e)

    # 2) 个股基本信息 - 雪球 (备选)
    if not fs.industry:
        try:
            df = _retry(ak.stock_individual_basic_info_xq, symbol=stock.sina_symbol)
            if df is not None and not df.empty:
                kv = dict(zip(df["item"], df["value"]))
                fs.industry = _safe_str(kv.get("所属行业"), fs.industry)
                fs.list_date = _safe_str(kv.get("上市日期"), fs.list_date)
                fs.total_shares = _safe_float(kv.get("总股本"), fs.total_shares)
                fs.circ_shares = _safe_float(kv.get("流通股本"), fs.circ_shares)
        except Exception as e:  # noqa: BLE001
            logger.warning("stock_individual_basic_info_xq 失败: %s", e)

    # 3) 财务摘要 - 雪球
    if hasattr(ak, "stock_financial_report_sina"):
        try:
            df = _retry(ak.stock_financial_report_sina, stock=stock.symbol)
            if df is not None and not df.empty:
                # 新浪财务: 字段包括 报告日期, 每股收益, 每股净资产, ROE, ...
                rmap = {
                    "每股收益": "eps_latest",
                    "净资产收益率": "roe",
                    "毛利率": "gross_margin",
                    "净利率": "net_margin",
                    "资产负债率": "debt_ratio",
                    "营业总收入": "revenue_latest",
                    "营业收入": "revenue_latest",
                    "净利润": "net_profit_latest",
                    "归属母公司净利润": "net_profit_latest",
                }
                for col in df.columns:
                    val = df.iloc[0][col] if len(df) > 0 else None
                    if col in rmap:
                        v = _safe_float(val)
                        if v is not None and getattr(fs, rmap[col], None) is None:
                            setattr(fs, rmap[col], v)
                # 报告期
                if "报告日期" in df.columns:
                    fs.report_date = _safe_str(df.iloc[0]["报告日期"])
        except Exception as e:  # noqa: BLE001
            logger.warning("stock_financial_report_sina 失败: %s", e)

    # === cache write ===
    try:
        cache.set_(cache_key, asdict(fs), cache.TTL_INDIVIDUAL_INFO)
    except Exception as e:  # noqa: BLE001
        logger.warning("基本面缓存失败: %s", e)
    return fs


def _tencent_index(symbol: str) -> Optional[Dict[str, Any]]:
    """
    腾讯指数直连。symbol 形如 sh000001 (上证) / sz399001 (深成)。
    """
    if not symbol.startswith(("sh", "sz")):
        symbol = f"sh{symbol}"
    url = f"https://qt.gtimg.cn/q={symbol}"
    try:
        r = requests.get(url, timeout=8,
                        headers={"User-Agent": "Mozilla/5.0"})
        text = r.text.strip()
        if "=" not in text or '""' in text:
            return None
        val = text.split('="', 1)[1].rstrip('";')
        parts = val.split("~")
        if len(parts) < 40:
            return None
        return {
            "name": parts[1],
            "price": float(parts[3]) if parts[3] else None,
            "change_pct": float(parts[32]) if len(parts) > 32 and parts[32] else None,
            "change": float(parts[31]) if len(parts) > 31 and parts[31] else None,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("腾讯指数 %s 失败: %s", symbol, e)
        return None


def fetch_industry_panel(stock: StockConfig = SHENHUA) -> Dict[str, float]:
    """
    拉取煤炭板块当日表现，与个股做对比。多源 fallback。
    缓存 30 分钟（板块涨跌变化较快，但 5 个时段内不必每次都拉）。
    """
    cache_key = cache.industry_key(stock.industry or "default")
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("行业板块命中缓存: %s", cached)
        return cached
    out: Dict[str, float] = {
        "sector_name": "N/A",
        "sector_change_pct": 0.0,
        "sh_index_change_pct": 0.0,
    }
    # 1) 煤炭板块 - 申万煤炭指数 sh000820
    try:
        q = _tencent_index("sh000820")
        if q and q.get("change_pct") is not None:
            out["sector_name"] = q.get("name", "煤炭指数")
            out["sector_change_pct"] = q["change_pct"]
    except Exception as e:  # noqa: BLE001
        logger.warning("腾讯煤炭板块 失败: %s", e)
    # 2) 东财板块 fallback
    if out["sector_change_pct"] == 0.0:
        try:
            df = _retry(ak.stock_board_industry_name_em, retries=1, sleep=0.1)
            if df is not None and not df.empty:
                mask = df["板块名称"].astype(str).str.contains("煤炭", na=False)
                if mask.any():
                    row = df[mask].iloc[0]
                    out["sector_name"] = str(row.get("板块名称", "煤炭开采"))
                    out["sector_change_pct"] = _safe_float(row.get("涨跌幅"), 0.0) or 0.0
        except Exception as e:  # noqa: BLE001
            logger.warning("板块 (东财) 失败: %s", e)

    # 3) 上证指数 - 腾讯首选
    try:
        q = _tencent_index("sh000001")
        if q and q.get("change_pct") is not None:
            out["sh_index_change_pct"] = q["change_pct"]
    except Exception as e:  # noqa: BLE001
        logger.warning("腾讯上证 失败: %s", e)
    # 4) 上证指数 fallback
    if out["sh_index_change_pct"] == 0.0:
        try:
            df = _retry(ak.stock_zh_index_spot_em, symbol="上证指数", retries=1, sleep=0.1)
            if df is not None and not df.empty:
                out["sh_index_change_pct"] = _safe_float(df.iloc[0].get("涨跌幅"), 0.0) or 0.0
        except Exception as e:  # noqa: BLE001
            logger.warning("东财上证 失败: %s", e)
    # === cache write ===
    try:
        cache.set_(cache_key, out, cache.TTL_INDUSTRY_INDEX)
    except Exception as e:  # noqa: BLE001
        logger.warning("行业缓存失败: %s", e)
    return out


def fetch_fund_flow(stock: StockConfig = SHENHUA) -> FundFlowSnapshot:
    """拉取当日主力资金流 + 北向 + 融资余额。多源 fallback。缓存 6 小时。"""
    cache_key = cache.fund_flow_key(stock.symbol)
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("资金流命中缓存: %s", cache_key)
        fs = FundFlowSnapshot(**cached)
        return fs
    fs = FundFlowSnapshot()
    fs.as_of = datetime.now().isoformat(timespec="seconds")
    # 主力资金流 (个股) - 多源
    flow_fns = [
        lambda: ak.stock_individual_fund_flow(stock=stock.symbol, market=stock.market),
    ]
    if hasattr(ak, "stock_individual_fund_flow_xq"):
        flow_fns.append(lambda: ak.stock_individual_fund_flow_xq(symbol=stock.sina_symbol))
    for fn in flow_fns:
        try:
            df = _retry(fn)
            if df is not None and not df.empty:
                r = df.iloc[0]
                fs.main_net_inflow = _safe_float(r.get("主力净流入-净额"))
                fs.super_net_inflow = _safe_float(r.get("超大单净流入-净额"))
                fs.big_net_inflow = _safe_float(r.get("大单净流入-净额"))
                fs.mid_net_inflow = _safe_float(r.get("中单净流入-净额"))
                fs.small_net_inflow = _safe_float(r.get("小单净流入-净额"))
                if fs.main_net_inflow is not None:
                    break
        except Exception as e:  # noqa: BLE001
            logger.warning("fund_flow source failed: %s", e)

    # 北向资金持股
    try:
        df = _retry(ak.stock_hsgt_individual_em, symbol=stock.sina_symbol)
        if df is not None and not df.empty:
            r = df.iloc[0]
            chg = _safe_float(r.get("持股变化"))
            price = _safe_float(r.get("当日收盘价"))
            if chg is not None and price is not None:
                fs.north_net_inflow = chg * price
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_hsgt_individual_em 失败: %s", e)

    # 融资融券
    try:
        df_fn = (ak.stock_margin_underlying_info_szse if stock.market == "sz"
                 else ak.stock_margin_underlying_info_sse)
        df = _retry(df_fn, symbol=stock.symbol)
        if df is not None and not df.empty:
            r = df.iloc[0]
            fs.margin_balance = _safe_float(r.get("融资余额"))
    except Exception as e:  # noqa: BLE001
        logger.warning("融资融券 失败: %s", e)

    # === cache write ===
    try:
        cache.set_(cache_key, asdict(fs), cache.TTL_FUND_FLOW)
    except Exception as e:  # noqa: BLE001
        logger.warning("资金流缓存失败: %s", e)
    return fs

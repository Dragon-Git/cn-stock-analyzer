"""
神华分析系统 - 报告生成
======================
根据不同时段生成侧重点不同的 Markdown 报告，并把原始数据保存为 JSON
供后续回溯。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .config import STOCKS, StockConfig, get_slot
from .data_fetcher import (FinancialSnapshot, FundFlowSnapshot,
                            KLineBundle, MarketSnapshot)

logger = logging.getLogger(__name__)


# Per-slot lock for the shared `latest_{slot}.md` file.
# 6 stocks all call save_report() in parallel; without this, the
# read-modify-write inside the function lets one thread's write overwrite
# another's. We hold the lock only across the latest-md section, since
# per-stock files (md/json with timestamp) are independent.
_LATEST_LOCKS: Dict[str, threading.Lock] = {}
_LATEST_LOCKS_GUARD = threading.Lock()


def _get_latest_lock(slot_id: str) -> threading.Lock:
    """Return a process-wide lock unique to this slot id."""
    with _LATEST_LOCKS_GUARD:
        lock = _LATEST_LOCKS.get(slot_id)
        if lock is None:
            lock = threading.Lock()
            _LATEST_LOCKS[slot_id] = lock
        return lock


# ===== 格式化辅助 =====
def _fmt(v: Any, digits: int = 2, suffix: str = "",
         na: str = "N/A") -> str:
    if v is None:
        return na
    if isinstance(v, str):
        return v
    if isinstance(v, float):
        if v != v:  # NaN
            return na
        if abs(v) >= 1e8:
            return f"{v/1e8:.{digits}f}亿{suffix}"
        if abs(v) >= 1e4 and suffix == "元":
            return f"{v/1e4:.{digits}f}万{suffix}"
        return f"{v:.{digits}f}{suffix}"
    return str(v)


def _pct(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "N/A"
    return f"{v:+.{digits}f}%"


def _sign_arrow(v: Optional[float]) -> str:
    if v is None:
        return "·"
    if v > 0:
        return "▲"
    if v < 0:
        return "▼"
    return "—"


# ===== 交易信号判断 =====
def judge_macd(m: Dict[str, Optional[float]]) -> str:
    dif, dea, hist = m.get("DIF"), m.get("DEA"), m.get("MACD")
    if dif is None or dea is None:
        return "数据不足"
    if dif > dea and hist > 0:
        return "多头 (DIF>DEA, 柱状为正)"
    if dif < dea and hist < 0:
        return "空头 (DIF<DEA, 柱状为负)"
    if dif > dea and hist < 0:
        return "金叉但绿柱 (动能偏弱)"
    if dif < dea and hist > 0:
        return "死叉但红柱 (动能偏强)"
    return "缠绕"


def judge_rsi(rsi: Dict[str, Optional[float]]) -> str:
    r12 = rsi.get("RSI12")
    if r12 is None:
        return "N/A"
    if r12 >= 80:
        return "超买"
    if r12 >= 70:
        return "偏强"
    if r12 <= 20:
        return "超卖"
    if r12 <= 30:
        return "偏弱"
    return "中性"


def judge_kdj(kdj: Dict[str, Optional[float]]) -> str:
    k, d, j = kdj.get("K"), kdj.get("D"), kdj.get("J")
    if k is None or d is None or j is None:
        return "N/A"
    if j > 100:
        return f"超买 (J={j:.1f})"
    if j < 0:
        return f"超卖 (J={j:.1f})"
    if k > d:
        return f"金叉 (K={k:.1f}>D={d:.1f})"
    return f"死叉 (K={k:.1f}<D={d:.1f})"


def judge_boll(boll: Dict[str, Optional[float]],
               price: float) -> str:
    u, m, d = boll.get("BOLL_UP"), boll.get("BOLL_MID"), boll.get("BOLL_DN")
    if u is None or d is None or price <= 0:
        return "N/A"
    if price > u:
        return f"突破上轨 ({u:.2f})"
    if price < d:
        return f"跌破下轨 ({d:.2f})"
    pos = (price - d) / (u - d) * 100 if u > d else 50
    return f"轨道内 ({pos:.0f}%)"


def judge_trend(mas: Dict[str, Optional[float]], price: float) -> str:
    """判断均线多头/空头排列"""
    keys = ["MA5", "MA10", "MA20", "MA60"]
    vals = [mas.get(k) for k in keys]
    if any(v is None for v in vals) or price <= 0:
        return "N/A"
    # 价格 > MA5 > MA10 > MA20 > MA60
    if price > vals[0] > vals[1] > vals[2] > vals[3]:
        return "完美多头排列"
    if price < vals[0] < vals[1] < vals[2] < vals[3]:
        return "完美空头排列"
    if price > vals[0] > vals[1]:
        return "短期偏多"
    if price < vals[0] < vals[1]:
        return "短期偏空"
    return "缠绕/震荡"


# ===== 报告生成 =====
def _header(slot_id: str, snapshot: MarketSnapshot, stock_name: str, stock_symbol: str) -> str:
    slot = get_slot(slot_id)
    lines = [
        f"# {stock_name}（{stock_symbol}）— {slot['label']}分析报告",
        "",
        f"> **报告时点**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)  ",
        f"> **时段侧重**: {slot['focus']}  ",
        f"> **当前价**: ¥{snapshot.price:.2f}  |  涨跌 {_sign_arrow(snapshot.change)} {_pct(snapshot.change_pct)}  |  昨收 ¥{snapshot.pre_close:.2f}",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def _section_market(snapshot: MarketSnapshot) -> str:
    return "\n".join([
        "## 一、行情快照",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 最新价 | ¥{snapshot.price:.2f} |",
        f"| 今日开盘 | ¥{snapshot.open:.2f} |",
        f"| 今日最高 | ¥{snapshot.high:.2f} |",
        f"| 今日最低 | ¥{snapshot.low:.2f} |",
        f"| 昨收 | ¥{snapshot.pre_close:.2f} |",
        f"| 涨跌额 | ¥{snapshot.change:+.2f} |",
        f"| 涨跌幅 | {_pct(snapshot.change_pct)} |",
        f"| 成交量 | {_fmt(snapshot.volume, 0, '手')} |",
        f"| 成交额 | {_fmt(snapshot.amount, 2, '元')} |",
        f"| 换手率 | {snapshot.turnover_pct:.2f}% |",
        f"| 总市值 | {_fmt(snapshot.total_mv, 2, '')} |",
        f"| 流通市值 | {_fmt(snapshot.circ_mv, 2, '')} |",
        f"| 滚动PE | {_fmt(snapshot.pe)} |",
        f"| 市净率PB | {_fmt(snapshot.pb)} |",
        "",
    ])


def _section_technical(indicators: Dict[str, Dict[str, Optional[float]]],
                        price: float) -> str:
    trend = indicators.get("trend", {})
    osc = indicators.get("oscillator", {})
    vol = indicators.get("volatility", {})
    vol_q = indicators.get("volume", {})
    mom = indicators.get("momentum", {})

    ma_signal = judge_trend(trend, price)
    macd_signal = judge_macd(trend)
    rsi_signal = judge_rsi(osc)
    kdj_signal = judge_kdj(osc)
    boll_signal = judge_boll(osc, price)

    lines = [
        "## 二、技术面分析（基于 akquant 内置指标）",
        "",
        "### 2.1 趋势指标",
        "",
        "| 指标 | 数值 | 解读 |",
        "|---|---|---|",
        f"| MA5 | {_fmt(trend.get('MA5'))} | 短期成本 |",
        f"| MA10 | {_fmt(trend.get('MA10'))} | 短期成本 |",
        f"| MA20 | {_fmt(trend.get('MA20'))} | 月线 |",
        f"| MA60 | {_fmt(trend.get('MA60'))} | 季线 |",
        f"| MA120 | {_fmt(trend.get('MA120'))} | 半年线 |",
        f"| MA250 | {_fmt(trend.get('MA250'))} | 年线 |",
        f"| EMA12 | {_fmt(trend.get('EMA12'))} | 快线 |",
        f"| EMA26 | {_fmt(trend.get('EMA26'))} | 慢线 |",
        f"| **均线排列** | **{ma_signal}** | |",
        "",
        "### 2.2 MACD (12, 26, 9)",
        "",
        "| DIF | DEA | MACD柱 | 状态 |",
        "|---|---|---|---|",
        f"| {_fmt(trend.get('DIF'), 4)} | {_fmt(trend.get('DEA'), 4)} | "
        f"{_fmt(trend.get('MACD'), 4)} | {macd_signal} |",
        "",
        "### 2.3 震荡指标",
        "",
        "| 指标 | 数值 | 解读 |",
        "|---|---|---|",
        f"| RSI6 | {_fmt(osc.get('RSI6'))} | 短期超买超卖 |",
        f"| RSI12 | {_fmt(osc.get('RSI12'))} | 中期强弱 |",
        f"| RSI24 | {_fmt(osc.get('RSI24'))} | 长期强弱 |",
        f"| **RSI12 信号** | **{rsi_signal}** | |",
        f"| KDJ-K | {_fmt(osc.get('K'))} | |",
        f"| KDJ-D | {_fmt(osc.get('D'))} | |",
        f"| KDJ-J | {_fmt(osc.get('J'))} | |",
        f"| **KDJ 状态** | **{kdj_signal}** | |",
        f"| 布林上轨 | {_fmt(osc.get('BOLL_UP'))} | |",
        f"| 布林中轨 | {_fmt(osc.get('BOLL_MID'))} | |",
        f"| 布林下轨 | {_fmt(osc.get('BOLL_DN'))} | |",
        f"| **布林位置** | **{boll_signal}** | |",
        f"| WR14 | {_fmt(osc.get('WR14'))} | <20 超买, >80 超卖 |",
        f"| CCI14 | {_fmt(osc.get('CCI14'))} | >100 超买, <-100 超卖 |",
        "",
        "### 2.4 波动 / 趋势强度",
        "",
        "| 指标 | 数值 | 解读 |",
        "|---|---|---|",
        f"| ATR14 | {_fmt(vol.get('ATR14'), 4)} | 真实波幅 (绝对) |",
        f"| NATR14 | {_fmt(vol.get('NATR14'), 3, '%')} | 归一化波幅 |",
        f"| ADX14 | {_fmt(vol.get('ADX14'))} | >25 趋势明显, <20 盘整 |",
        f"| +DI | {_fmt(vol.get('PLUS_DI'))} | 多向力量 |",
        f"| -DI | {_fmt(vol.get('MINUS_DI'))} | 空向力量 |",
        "",
        "### 2.5 量能指标",
        "",
        "| 指标 | 数值 | 解读 |",
        "|---|---|---|",
        f"| OBV | {_fmt(vol_q.get('OBV'), 0)} | 能量潮 |",
        f"| OBV MA10 | {_fmt(vol_q.get('OBV_MA10'), 0)} | 资金均线 |",
        f"| MFI14 | {_fmt(vol_q.get('MFI14'))} | >80 超买, <20 超卖 |",
        "",
        "### 2.6 动量指标",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| ROC12 | {_fmt(mom.get('ROC12'), 3, '%')} |",
        f"| MOM10 | {_fmt(mom.get('MOM10'), 4, '')} |",
        f"| TRIX14 | {_fmt(mom.get('TRIX14'), 4)} |",
        f"| BIAS5 | {_fmt(osc.get('BIAS5'), 2, '%')} |",
        f"| BIAS10 | {_fmt(osc.get('BIAS10'), 2, '%')} |",
        f"| BIAS20 | {_fmt(osc.get('BIAS20'), 2, '%')} |",
        "",
    ]
    return "\n".join(lines)


def _section_kline_overview(kline: pd.DataFrame) -> str:
    """最近 N 日 K 线概览"""
    if kline is None or kline.empty:
        return "## 三、最近 K 线\n\n数据缺失\n\n"
    last = kline.tail(10).copy()
    lines = [
        "## 三、最近 10 日 K 线",
        "",
        "| 日期 | 开盘 | 最高 | 最低 | 收盘 | 成交量(手) | 涨跌% | 换手% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _, r in last.iterrows():
        d = r["date"].strftime("%Y-%m-%d")
        vol = f"{r['volume']:,.0f}" if pd.notna(r.get("volume")) else "—"
        tp = r.get("turnover_pct")
        tp_s = f"{tp:.2f}" if tp is not None and pd.notna(tp) else "—"
        lines.append(
            f"| {d} | {r['open']:.2f} | {r['high']:.2f} | {r['low']:.2f} | "
            f"{r['close']:.2f} | {vol} | "
            f"{_pct(r.get('change_pct'))} | {tp_s} |"
        )
    lines.append("")
    return "\n".join(lines)


def _section_fundamental(fin: FinancialSnapshot) -> str:
    # 如果什么都没拉到，就不渲染这一节
    if (not fin.industry and not fin.main_business and
            fin.roe is None and fin.revenue_latest is None):
        return ""
    return "\n".join([
        "## 四、基本面（财务摘要）",
        "",
        f"> 报告期: {fin.report_date}",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 所属行业 | {fin.industry} |",
        f"| 主营业务 | {fin.main_business} |",
        f"| 上市日期 | {fin.list_date} |",
        f"| 总股本 | {_fmt(fin.total_shares, 2, '股')} |",
        f"| 流通股本 | {_fmt(fin.circ_shares, 2, '股')} |",
        f"| 滚动PE (TTM) | {_fmt(fin.pe_ttm)} |",
        f"| 市净率PB | {_fmt(fin.pb)} |",
        f"| 市销率PS (TTM) | {_fmt(fin.ps_ttm)} |",
        f"| 滚动PCF | {_fmt(fin.pcf)} |",
        f"| **加权 ROE** | **{_fmt(fin.roe, 2, '%')}** |",
        f"| 毛利率 | {_fmt(fin.gross_margin, 2, '%')} |",
        f"| 净利率 | {_fmt(fin.net_margin, 2, '%')} |",
        f"| 资产负债率 | {_fmt(fin.debt_ratio, 2, '%')} |",
        f"| 基本每股收益 | {_fmt(fin.eps_latest, 4, '元')} |",
        f"| 营业总收入 | {_fmt(fin.revenue_latest, 2, '元')} |",
        f"| 营收同比 | {_pct(fin.revenue_yoy)} |",
        f"| 归母净利润 | {_fmt(fin.net_profit_latest, 2, '元')} |",
        f"| 净利同比 | {_pct(fin.net_profit_yoy)} |",
        f"| 股息率 | {_fmt(fin.div_yield, 2, '%')} |",
        "",
    ])


def _section_industry(panel: Dict[str, float],
                       stock_change: float,
                       stock_name: str = "个股") -> str:
    sec = panel.get("sector_change_pct", 0.0)
    idx = panel.get("sh_index_change_pct", 0.0)
    rel_sec = stock_change - sec
    rel_idx = stock_change - idx
    return "\n".join([
        "## 五、行业 / 板块对比",
        "",
        f"| 维度 | 涨跌幅 | 相对个股 |",
        "|---|---|---|",
        f"| {stock_name}（个股） | {_pct(stock_change)} | — |",
        f"| {panel.get('sector_name', '所属板块')} | {_pct(sec)} | "
        f"{'强于板块' if rel_sec > 0 else '弱于板块'} {_pct(rel_sec)} |",
        f"| 上证指数 | {_pct(idx)} | "
        f"{'强于大盘' if rel_idx > 0 else '弱于大盘'} {_pct(rel_idx)} |",
        "",
    ])


def _section_fund_flow(flow: FundFlowSnapshot) -> str:
    # 全部 None 时跳过
    if all(v is None for v in (flow.main_net_inflow, flow.super_net_inflow,
                                 flow.big_net_inflow, flow.mid_net_inflow,
                                 flow.small_net_inflow, flow.north_net_inflow,
                                 flow.margin_balance)):
        return ""
    def colored(v):
        if v is None:
            return "N/A"
        arrow = _sign_arrow(v)
        return f"{arrow} {v/1e4:,.2f}万"
    return "\n".join([
        "## 六、资金流向",
        "",
        "| 资金类型 | 净额 (元) | 等价 (万元) |",
        "|---|---|---|",
        f"| 主力净流入 | {flow.main_net_inflow or 0:,.0f} | {colored(flow.main_net_inflow)} |",
        f"| 超大单净流入 | {flow.super_net_inflow or 0:,.0f} | {colored(flow.super_net_inflow)} |",
        f"| 大单净流入 | {flow.big_net_inflow or 0:,.0f} | {colored(flow.big_net_inflow)} |",
        f"| 中单净流入 | {flow.mid_net_inflow or 0:,.0f} | {colored(flow.mid_net_inflow)} |",
        f"| 小单净流入 | {flow.small_net_inflow or 0:,.0f} | {colored(flow.small_net_inflow)} |",
        f"| 北向资金净流入 (估算) | {flow.north_net_inflow or 0:,.0f} | {colored(flow.north_net_inflow)} |",
        f"| 融资余额 | {flow.margin_balance or 0:,.0f} | {colored(flow.margin_balance)} |",
        "",
    ])


def _section_summary(slot_id: str, snapshot: MarketSnapshot,
                     indicators: Dict[str, Dict[str, Optional[float]]],
                     fin: FinancialSnapshot,
                     flow: FundFlowSnapshot) -> str:
    """每时段不同的简短结论"""
    lines = ["## 七、综合研判", ""]
    price = snapshot.price
    trend = indicators.get("trend", {})
    osc = indicators.get("oscillator", {})

    ma5, ma10, ma20, ma60 = (trend.get(k) for k in ("MA5", "MA10", "MA20", "MA60"))
    rsi12 = osc.get("RSI12")
    kdj_j = osc.get("J")
    macd_h = trend.get("MACD")
    adx = indicators.get("volatility", {}).get("ADX14")

    # 共用结论片段
    trend_word = judge_trend(trend, price)
    lines.append(f"- **趋势**: {trend_word}（当前价 ¥{price:.2f}）")

    if rsi12 is not None:
        lines.append(f"- **RSI12** = {rsi12:.1f}，{judge_rsi(osc)}")
    if kdj_j is not None:
        lines.append(f"- **KDJ-J** = {kdj_j:.1f}，{judge_kdj(osc)}")
    if macd_h is not None:
        lines.append(f"- **MACD 柱** = {macd_h:.4f}，{judge_macd(trend)}")
    if adx is not None:
        if adx > 25:
            lines.append(f"- **ADX14** = {adx:.1f}，趋势性较强")
        elif adx < 20:
            lines.append(f"- **ADX14** = {adx:.1f}，盘整格局")
        else:
            lines.append(f"- **ADX14** = {adx:.1f}，趋势中等")

    main_in = flow.main_net_inflow
    if main_in is not None:
        direction = "流入" if main_in > 0 else "流出"
        amount_wan = main_in / 1e4
        lines.append(f"- **主力资金** {direction} {amount_wan:,.0f} 万元")
    elif flow.main_net_inflow is None and all(
        v is None for v in (flow.super_net_inflow, flow.big_net_inflow,
                            flow.north_net_inflow, flow.margin_balance)
    ):
        # 资金数据整体缺失，加个提示
        lines.append("- ℹ️ 资金流数据源 (东方财富) 暂不可用，跳过资金面分析")

    if fin.roe is not None:
        lines.append(f"- **ROE** = {fin.roe:.2f}% (报告期 {fin.report_date})")
    elif fin.industry:
        lines.append(f"- 行业: {fin.industry}")
    else:
        lines.append("- ℹ️ 基本面数据源 (东方财富/同花顺) 暂不可用，跳过基本面分析")

    # 时段特定结论
    lines.append("")
    lines.append(f"### {get_slot(slot_id)['label']} 关注重点")
    lines.append("")
    if slot_id == "pre_open":
        # 09:15 集合竞价刚开始, 今日 K 线尚未生成, 看外盘 + 昨 K
        lines.append("- 集合竞价进行中 (09:15-09:25), 9:20 后不可撤单段为真实情绪窗口")
        lines.append("- 09:25 撮合结果将决定今日开盘缺口")
        if ma20 and price:
            lines.append(f"- 参考：MA20 = {ma20:.2f}，MA60 = {ma60:.2f}")
    elif slot_id == "post_auction":
        gap_pct = ((snapshot.open - snapshot.pre_close) / snapshot.pre_close * 100
                   if snapshot.pre_close else 0)
        lines.append(f"- **开盘缺口**: {gap_pct:+.2f}%")
        lines.append("- 集合竞价结果对早盘情绪有指导意义")
        lines.append("- 关注 09:30-09:45 第一根 15 分钟 K 线的方向")
    elif slot_id == "noon":
        lines.append("- 上午已走完，关注午后是否突破上午高点 / 跌破上午低点")
        if rsi12 is not None and rsi12 > 70:
            lines.append("- ⚠️ RSI 偏超买，午后谨防回吐")
        if rsi12 is not None and rsi12 < 30:
            lines.append("- 💡 RSI 偏超卖，午后留意技术性反弹")
        lines.append("- 13:00 开盘量能是关键")
    elif slot_id == "post_close":
        lines.append("- 全日 K 线已收定，技术形态明确")
        lines.append("- 明日开盘关注今日最高 / 最低的支撑压力作用")
        lines.append("- 收盘价相对 MA5 的位置决定短期强弱")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> ⚠️ **免责声明**: 本报告由量化系统自动生成，仅供研究参考，")
    lines.append("> 不构成任何投资建议。投资有风险，决策需谨慎。")
    return "\n".join(lines)


# ===== 主入口 =====
def build_report(slot_id: str,
                 snapshot: MarketSnapshot,
                 kline: pd.DataFrame,
                 indicators: Dict[str, Dict[str, Optional[float]]],
                 fin: FinancialSnapshot,
                 flow: FundFlowSnapshot,
                 stock: StockConfig,
                 panel: Optional[Dict[str, float]] = None) -> str:
    """组装完整报告，自动跳过空 section"""
    parts = [
        _header(slot_id, snapshot, stock.name, stock.symbol),
        _section_market(snapshot),
        _section_technical(indicators, snapshot.price),
        _section_kline_overview(kline),
    ]
    fund_text = _section_fundamental(fin)
    if fund_text:
        parts.append(fund_text)
    if panel is not None and panel.get("sector_name") != "N/A":
        parts.append(_section_industry(panel, snapshot.change_pct, stock.name))
    flow_text = _section_fund_flow(flow)
    if flow_text:
        parts.append(flow_text)
    parts.append(_section_summary(slot_id, snapshot, indicators, fin, flow))
    return "\n".join(p for p in parts if p)


def save_report(report_md: str, slot_id: str,
                snapshot: MarketSnapshot, kline: pd.DataFrame,
                indicators: Dict[str, Dict[str, Optional[float]]],
                fin: FinancialSnapshot, flow: FundFlowSnapshot,
                stock: StockConfig,
                out_dir: Path,
                panel: Optional[Dict[str, float]] = None) -> Dict[str, str]:
    """
    保存报告和 JSON 数据。
    - 每只股票单独文件 {date}_{slot}_{symbol}_{ts}.md (回溯用)
    - latest_{slot}.md 单文件包含全部股票, 用 ## 📊 标题分隔
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    ts = datetime.now().strftime("%H%M%S")

    # 单只股票的时间戳文件 (供回溯)
    md_path = out_dir / f"{today}_{slot_id}_{stock.symbol}_{ts}.md"
    md_path.write_text(report_md, encoding="utf-8")

    # 原始数据 JSON
    raw = {
        "slot": slot_id,
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "stock": {"symbol": stock.symbol, "name": stock.name},
        "snapshot": asdict(snapshot),
        "kline": (kline.tail(60).assign(date=kline["date"].dt.strftime("%Y-%m-%d"))
                  ).to_dict(orient="records") if kline is not None and not kline.empty else [],
        "indicators": indicators,
        "financial": asdict(fin),
        "fund_flow": asdict(flow),
        "industry_panel": panel or {},
    }

    def _default(o):
        if hasattr(o, "item"):
            try:
                return o.item()
            except Exception:
                pass
        return str(o)

    json_path = out_dir / f"{today}_{slot_id}_{stock.symbol}_{ts}.json"
    json_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=_default),
                         encoding="utf-8")

    # 单文件: latest_{slot}.md 包含所有股票
    # 结构:
    #   # 多股票 {slot} 时段报告          (总标题, 仅在新建时)
    #   > 报告时点 / 股票池              (总 metadata, 仅在新建时)
    #   ## 📊 {name}（{symbol}）         (每只股票的 wrapper)
    #   ### {原 H1 标题}                  (原 H1 降级为 H3, 因为 wrapper 占了 H2)
    #   ## 一、行情快照                   (原 H2 不变, 因为 H2 没被占)
    #   ...
    # 6 stocks call this in parallel; serialize the read-modify-write of
    # the shared file via a per-slot lock to avoid lost sections.
    _write_latest_md(out_dir, slot_id, stock, report_md)

    return {"md": str(md_path), "json": str(json_path), "latest": str(out_dir / f"latest_{slot_id}.md")}


def _write_latest_md(out_dir: Path, slot_id: str,
                     stock: StockConfig, report_md: str) -> None:
    """
    Append/replace one stock's section in the shared `latest_{slot}.md`.

    Serialized via a per-slot lock so the read-modify-write of the file is
    atomic across parallel stock analyses.
    """
    latest_md = out_dir / f"latest_{slot_id}.md"
    section_title = f"## 📊 {stock.name}（{stock.symbol}）"
    # 把原报告的 H1 升级为 H3 (因为 ## 已经被 ## 📊 占)
    # H2 不动 (一、二、三、)
    body = report_md
    body = re.sub(r"^# (.+)$", rf"### \1", body, count=1, flags=re.MULTILINE)
    stock_section = f"{section_title}\n\n{body}"

    with _get_latest_lock(slot_id):
        if latest_md.exists():
            existing = latest_md.read_text(encoding="utf-8")
            # 旧格式 (无 ## 📊 marker, 是单只股票 H1 报告) → 删掉重建为多股票格式
            if "## 📊" not in existing:
                logger.info("[%s] latest_%s.md 是旧单股票格式, 重建为多股票格式",
                            stock.symbol, slot_id)
                existing = ""
            if not existing:
                # 多股票 header (首次创建)
                header = (
                    f"# 多股票 {slot_id} 时段报告\n\n"
                    f"> **报告时点**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)  \n"
                    f"> **股票池**: {' / '.join(f'{s.name}({s.symbol})' for s in STOCKS)}  \n\n"
                    "---\n\n"
                )
                latest_md.write_text(header + stock_section, encoding="utf-8")
            elif section_title in existing:
                # 替换该股票段落 (idempotent, e.g. 同一天内重跑)
                pattern = re.compile(
                    rf"## 📊 {re.escape(stock.name)}（{re.escape(stock.symbol)}）.*?"
                    rf"(?=\n## 📊 |\Z)",
                    re.DOTALL,
                )
                new_content = pattern.sub("", existing).rstrip()
                # pattern may leave a trailing "\n\n" before the next ## 📊; clean it
                new_content = re.sub(r"\n{3,}", "\n\n", new_content).rstrip()
                latest_md.write_text(new_content + "\n\n" + stock_section,
                                     encoding="utf-8")
            else:
                # 在已有内容末尾追加新股票段落
                existing = existing.rstrip()
                # Avoid stacking multiple "---" separators if a previous run
                # left the file ending with one
                if not existing.endswith("---"):
                    latest_md.write_text(existing + "\n\n---\n\n" + stock_section,
                                         encoding="utf-8")
                else:
                    latest_md.write_text(existing + "\n\n" + stock_section,
                                         encoding="utf-8")
        else:
            header = (
                f"# 多股票 {slot_id} 时段报告\n\n"
                f"> **报告时点**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)  \n"
                f"> **股票池**: {' / '.join(f'{s.name}({s.symbol})' for s in STOCKS)}  \n\n"
                "---\n\n"
            )
            latest_md.write_text(header + stock_section, encoding="utf-8")

"""
中国 A 股多股票分析系统 - 主入口
=================================
对指定时段并行分析所有监控股票，每只股票生成独立报告，合并到
单文件 `latest_{slot}.md`。多只股票共享同一个 SQLite cache (key 含
symbol 区分)。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from . import data_fetcher as df_mod
from . import indicators as ind_mod
from . import report_generator as rg
from . import trading_calendar as tc
from .config import STOCKS, StockConfig, TIME_SLOTS, get_slot, now_beijing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cn-stock")

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def analyze_one_stock(stock: StockConfig, slot_id: str) -> None:
    """
    分析单只股票在指定时段的全部数据并写报告。
    失败不抛异常，单只股票失败不影响其他股票。
    """
    logger.info("[%s] 开始分析...", stock.symbol)
    try:
        # 1. K 线 (cache 命中后秒级)
        kline = df_mod.fetch_history_kline(stock, days=400)
    except Exception as e:  # noqa: BLE001
        logger.error("[%s] K 线拉取失败: %s", stock.symbol, e)
        kline = pd.DataFrame()

    if kline.empty:
        logger.warning("[%s] K 线空, 生成 placeholder 报告", stock.symbol)
        snap = df_mod.MarketSnapshot()
        snap.as_of = now_beijing().isoformat(timespec="seconds")
        fin = df_mod.FinancialSnapshot()
        flow = df_mod.FundFlowSnapshot()
        panel = {"sector_name": "N/A", "sector_change_pct": 0.0,
                 "sh_index_change_pct": 0.0}
        report_md = (
            f"# {stock.name}（{stock.symbol}）— {get_slot(slot_id)['label']}分析报告\n\n"
            f"> **报告时点**: {now_beijing().strftime('%Y-%m-%d %H:%M:%S')} (Asia/Shanghai)  \n"
            f"> **时段**: {get_slot(slot_id)['label']}\n\n"
            f"## ⚠️ 数据获取失败\n\n"
            f"本时段未能拉取 {stock.name} 的历史 K 线。\n"
            "可能原因：网络问题、交易日为节假日、akshare 接口变更。\n"
        )
        rg.save_report(report_md, slot_id, snap, kline, {}, fin, flow, stock,
                       REPORTS_DIR, panel)
        return

    logger.info("[%s] K 线 %d 条, 最新 %s, 收盘 %.2f",
                stock.symbol, len(kline),
                kline["date"].iloc[-1].strftime("%Y-%m-%d"),
                kline["close"].iloc[-1])

    # 2. 实时行情
    snapshot = df_mod.fetch_realtime_snapshot(stock)
    if snapshot.price <= 0:
        last = kline.iloc[-1]
        snapshot.price = float(last["close"])
        snapshot.open = float(last["open"])
        snapshot.high = float(last["high"])
        snapshot.low = float(last["low"])
        snapshot.pre_close = float(last.get("close", 0))
        logger.warning("[%s] 实时行情为空, 用 K 线最后一日兜底", stock.symbol)
    else:
        kline.loc[kline.index[-1], "close"] = snapshot.price
        if snapshot.high > 0:
            kline.loc[kline.index[-1], "high"] = max(
                float(kline.loc[kline.index[-1], "high"]), snapshot.high
            )
        if snapshot.low > 0:
            kline.loc[kline.index[-1], "low"] = min(
                float(kline.loc[kline.index[-1], "low"]), snapshot.low
            )
        if snapshot.turnover_pct > 0:
            kline.loc[kline.index[-1], "turnover_pct"] = snapshot.turnover_pct
        if snapshot.change_pct != 0:
            kline.loc[kline.index[-1], "change_pct"] = snapshot.change_pct
            kline.loc[kline.index[-1], "change"] = snapshot.change

    # 自行计算 K 线中缺的 change_pct / change
    if "change_pct" not in kline.columns or kline["change_pct"].isna().all():
        kline["change_pct"] = kline["close"].pct_change() * 100
    if "change" not in kline.columns or kline["change"].isna().all():
        kline["change"] = kline["close"].diff()
    if "turnover_pct" not in kline.columns:
        kline["turnover_pct"] = None

    # 3. 基本面 + 资金流 + 行业 (并行)
    logger.info("[%s] 并行拉取 基本面/资金流/行业...", stock.symbol)
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_fin = ex.submit(df_mod.fetch_individual_info, stock)
        f_flow = ex.submit(df_mod.fetch_fund_flow, stock)
        f_panel = ex.submit(df_mod.fetch_industry_panel, stock)
        fin = f_fin.result()
        flow = f_flow.result()
        panel = f_panel.result()

    # 4. 技术指标
    indicators = ind_mod.compute_all(kline)

    # 5. 写报告 (append 到 latest_{slot}.md)
    report_md = rg.build_report(slot_id, snapshot, kline, indicators, fin, flow,
                                 stock, panel)
    rg.save_report(report_md, slot_id, snapshot, kline, indicators, fin, flow,
                   stock, REPORTS_DIR, panel)
    logger.info("[%s] 报告已生成", stock.symbol)


def run_slot(slot_id: str) -> int:
    """跑一个时段: 并行分析所有股票"""
    slot = get_slot(slot_id)
    logger.info("=" * 60)
    logger.info("开始 [%s] 报告 (时段: %s), 监控池: %d 只",
                slot_id, slot["label"], len(STOCKS))
    logger.info("=" * 60)

    # 6 只股票并行 (网络/akshare I/O bound, 线程池 6 路)
    with ThreadPoolExecutor(max_workers=len(STOCKS)) as ex:
        futures = {ex.submit(analyze_one_stock, s, slot_id): s for s in STOCKS}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                logger.error("[%s] 分析失败: %s", s.symbol, e)

    logger.info("[%s] 全部 %d 只股票完成, 报告: reports/latest_%s.md",
                slot_id, len(STOCKS), slot_id)
    return 0


def list_slots() -> None:
    print("可用时段:")
    for s in TIME_SLOTS:
        print(f"  {s['id']:14s}  {s['label']:6s}  cron(UTC)={s['cron_utc']:18s}  {s['focus']}")


def list_stocks() -> None:
    print("监控股票池:")
    for s in STOCKS:
        print(f"  {s.symbol}  {s.name:6s}  {s.industry}")


def main(argv: Optional[list] = None) -> int:
    global REPORTS_DIR
    p = argparse.ArgumentParser(description="A 股多股票智能分析")
    p.add_argument("--slot", "-s", choices=[s["id"] for s in TIME_SLOTS] + ["all"],
                   default="post_close", help="分析时段, all 表示跑全部 4 个")
    p.add_argument("--list", "-l", action="store_true", help="列出所有时段和股票")
    p.add_argument("--out", "-o", default=str(REPORTS_DIR), help="报告输出目录")
    p.add_argument("--stock", help="只分析指定股票 (6 位代码), 调试用")
    args = p.parse_args(argv)

    if args.list:
        list_slots()
        print()
        list_stocks()
        return 0

    REPORTS_DIR = Path(args.out)

    if args.stock:
        # 单股票调试
        s = next((x for x in STOCKS if x.symbol == args.stock), None)
        if not s:
            print(f"Unknown stock: {args.stock}")
            return 1
        if args.slot == "all":
            for slot in TIME_SLOTS:
                analyze_one_stock(s, slot["id"])
        else:
            analyze_one_stock(s, args.slot)
        return 0

    if args.slot == "all":
        rc = 0
        for s in TIME_SLOTS:
            r = run_slot(s["id"])
            rc = rc or r
        return rc
    return run_slot(args.slot)


if __name__ == "__main__":
    sys.exit(main())

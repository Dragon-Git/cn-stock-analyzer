"""
神华分析系统 - 主入口
======================
对指定时段执行完整分析流程：
  1. 拉取历史 K 线（akshare）
  2. 拉取实时行情
  3. 拉取基本面 / 财务摘要
  4. 拉取资金流向
  5. 用 akquant 计算技术指标
  6. 生成报告并落盘
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from . import data_fetcher as df_mod
from . import indicators as ind_mod
from . import report_generator as rg
from . import trading_calendar as tc
from .config import SHENHUA, TIME_SLOTS, get_slot
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("shenhua")


REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def run(slot_id: str, out_dir: Path = REPORTS_DIR) -> int:
    """
    执行一个时段的完整分析。
    返回 0 成功，非 0 失败。
    """
    # 交易日判断
    if not tc.is_trading_day():
        logger.info("今天 (%s) 不是 A 股交易日，跳过本次分析", date.today())
        return 0

    slot = get_slot(slot_id)
    logger.info("=" * 60)
    logger.info("开始 [%s] 报告生成 (时段: %s)", slot_id, slot["label"])
    logger.info("=" * 60)

    # 1. 历史 K 线
    try:
        kline = df_mod.fetch_history_kline(SHENHUA, days=400)
    except Exception as e:  # noqa: BLE001
        logger.error("拉取历史 K 线失败: %s", e)
        kline = pd.DataFrame()
    if kline.empty:
        logger.warning("K 线数据缺失，生成空数据 placeholder 报告")
        # 用零数据生成一份"数据获取失败"提示报告，保留 commit 行为
        snap = df_mod.MarketSnapshot()
        snap.as_of = datetime.now().isoformat(timespec="seconds")
        fin = df_mod.FinancialSnapshot()
        flow = df_mod.FundFlowSnapshot()
        indicators = {}
        panel = {"sector_name": "N/A", "sector_change_pct": 0.0,
                 "sh_index_change_pct": 0.0}
        report_md = (
            f"# {SHENHUA.name}（{SHENHUA.symbol}）— {slot['label']}分析报告\n\n"
            f"> **报告时点**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n"
            f"> **时段**: {slot['label']}\n\n"
            "## ⚠️ 数据获取失败\n\n"
            f"本时段未能从 akshare 拉取到 {SHENHUA.name} 的历史 K 线。\n"
            "可能原因：网络问题、交易日为节假日、akshare 接口变更。\n"
            "请查看上方日志或稍后重试。\n"
        )
        paths = rg.save_report(
            report_md, slot_id, snap, kline, indicators, fin, flow, out_dir, panel
        )
        logger.info("Placeholder 报告已保存: %s", paths["md"])
        return 0
    logger.info("K 线 %d 条, 最新 %s, 收盘 %.2f",
                len(kline), kline["date"].iloc[-1].strftime("%Y-%m-%d"),
                kline["close"].iloc[-1])

    # 2. 实时行情
    snapshot = df_mod.fetch_realtime_snapshot(SHENHUA)
    if snapshot.price <= 0:
        # 用 K 线最后一条兜底
        last = kline.iloc[-1]
        snapshot.price = float(last["close"])
        snapshot.open = float(last["open"])
        snapshot.high = float(last["high"])
        snapshot.low = float(last["low"])
        snapshot.pre_close = float(last.get("close", 0))
        logger.warning("实时行情为空，使用 K 线最后一日数据")
    else:
        # 用实时价替换 K 线最后一日的 close（盘中更准）
        kline.loc[kline.index[-1], "close"] = snapshot.price
        if snapshot.high > 0:
            kline.loc[kline.index[-1], "high"] = max(
                float(kline.loc[kline.index[-1], "high"]), snapshot.high
            )
        if snapshot.low > 0:
            kline.loc[kline.index[-1], "low"] = min(
                float(kline.loc[kline.index[-1], "low"]), snapshot.low
            )
        # 用实时快照补充 K 线最后一日的 turnover_pct / change_pct / change
        if snapshot.turnover_pct > 0:
            kline.loc[kline.index[-1], "turnover_pct"] = snapshot.turnover_pct
        if snapshot.change_pct != 0:
            kline.loc[kline.index[-1], "change_pct"] = snapshot.change_pct
            kline.loc[kline.index[-1], "change"] = snapshot.change

    # 自行计算 K 线中缺的 change_pct / change（新浪源没有）
    if "change_pct" in kline.columns and kline["change_pct"].isna().any():
        kline["change_pct"] = kline["close"].pct_change() * 100
    if "change" in kline.columns and kline["change"].isna().any():
        kline["change"] = kline["close"].diff()
    # 换手率只在实时数据有, 历史行用 "-" 占位
    if "turnover_pct" not in kline.columns:
        kline["turnover_pct"] = None

    # 3. 基本面
    fin = df_mod.fetch_individual_info(SHENHUA)

    # 4. 资金流
    flow = df_mod.fetch_fund_flow(SHENHUA)

    # 4.5 行业对比
    panel = df_mod.fetch_industry_panel(SHENHUA)

    # 5. 技术指标
    logger.info("计算技术指标 (akquant)...")
    indicators = ind_mod.compute_all(kline)

    # 6. 报告生成
    report_md = rg.build_report(slot_id, snapshot, kline, indicators, fin, flow, panel)
    paths = rg.save_report(
        report_md, slot_id, snapshot, kline, indicators, fin, flow, out_dir, panel
    )
    logger.info("报告已保存:")
    for k, v in paths.items():
        logger.info("  %s -> %s", k, v)
    return 0


def list_slots() -> None:
    print("可用时段:")
    for s in TIME_SLOTS:
        print(f"  {s['id']:14s}  {s['label']:6s}  cron(UTC)={s['cron_utc']:18s}  {s['focus']}")


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="中国神华智能分析")
    p.add_argument("--slot", "-s", choices=[s["id"] for s in TIME_SLOTS] + ["all"],
                   default="post_close", help="分析时段, all 表示跑全部 5 个")
    p.add_argument("--list", "-l", action="store_true", help="列出所有时段")
    p.add_argument("--out", "-o", default=str(REPORTS_DIR), help="报告输出目录")
    args = p.parse_args(argv)

    if args.list:
        list_slots()
        return 0

    out_dir = Path(args.out)
    if args.slot == "all":
        rc = 0
        for s in TIME_SLOTS:
            r = run(s["id"], out_dir)
            rc = rc or r
        return rc
    return run(args.slot, out_dir)


if __name__ == "__main__":
    sys.exit(main())

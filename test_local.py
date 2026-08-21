"""
本地调试：使用 mock 数据验证整个流水线。
不依赖 akshare 网络，确认所有指标计算、报告生成都能正常工作。
"""
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from src import data_fetcher as df_mod
from src import indicators as ind_mod
from src import report_generator as rg
from src.config import SHENHUA
from datetime import datetime


def make_mock_kline(days: int = 400) -> pd.DataFrame:
    """生成模拟的 A 股 K 线数据"""
    np.random.seed(42)
    end = datetime(2026, 8, 20)
    dates = pd.bdate_range(end=end, periods=days)
    # 神华历史价格中枢约 35-45
    price = 35 + np.cumsum(np.random.normal(0.02, 0.5, days))
    price = np.clip(price, 30, 50)
    o = price + np.random.normal(0, 0.1, days)
    c = price + np.random.normal(0, 0.15, days)
    h = np.maximum(o, c) + np.abs(np.random.normal(0, 0.2, days))
    low = np.minimum(o, c) - np.abs(np.random.normal(0, 0.2, days))
    vol = np.random.randint(500_000, 5_000_000, days)
    amount = vol * price * 100  # 简化
    return pd.DataFrame({
        "date": dates,
        "open": o.round(2),
        "high": h.round(2),
        "low": low.round(2),
        "close": c.round(2),
        "volume": vol,
        "amount": amount,
        "amplitude": np.abs(h - low) / low * 100,
        "change_pct": np.random.normal(0, 1.5, days),
        "change": np.random.normal(0, 0.5, days),
        "turnover_pct": np.random.uniform(0.1, 1.5, days),
    })


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("=" * 60)
    print("Mock 测试：使用本地模拟数据验证流水线")
    print("=" * 60)

    kline = make_mock_kline(400)
    print(f"✓ K 线: {len(kline)} 条, "
          f"最新 {kline['date'].iloc[-1].strftime('%Y-%m-%d')}, "
          f"收盘 {kline['close'].iloc[-1]:.2f}")

    # 模拟实时行情
    snap = df_mod.MarketSnapshot(
        price=float(kline["close"].iloc[-1]),
        open=float(kline["open"].iloc[-1]),
        high=float(kline["high"].iloc[-1]),
        low=float(kline["low"].iloc[-1]),
        pre_close=float(kline["close"].iloc[-2]),
        change=float(kline["close"].iloc[-1] - kline["close"].iloc[-2]),
        change_pct=float((kline["close"].iloc[-1] / kline["close"].iloc[-2] - 1) * 100),
        volume=float(kline["volume"].iloc[-1]),
        amount=float(kline["amount"].iloc[-1]),
        turnover_pct=0.85,
        pe=12.5, pb=1.8,
        total_mv=6800.0, circ_mv=6800.0,
        as_of=datetime.now().isoformat(),
    )
    print(f"✓ 模拟行情: 价格 {snap.price:.2f}, 涨跌 {snap.change_pct:+.2f}%")

    # 模拟基本面
    fin = df_mod.FinancialSnapshot(
        industry="煤炭开采",
        main_business="煤炭、电力、铁路、港口、煤化工",
        list_date="2007-06-22",
        total_shares=198.69e8,
        circ_shares=198.69e8,
        pe_ttm=12.5, pb=1.8, ps_ttm=1.9, pcf=8.2,
        roe=14.5, gross_margin=38.2, net_margin=22.1, debt_ratio=27.5,
        revenue_latest=2.86e11, revenue_yoy=-1.2,
        net_profit_latest=5.6e10, net_profit_yoy=-2.8,
        eps_latest=2.81, div_yield=5.2,
        report_date="2025-06-30",
    )
    print(f"✓ 模拟基本面: ROE {fin.roe}%, 营收同比 {fin.revenue_yoy}%")

    # 模拟资金流
    flow = df_mod.FundFlowSnapshot(
        main_net_inflow=1.2e8,
        super_net_inflow=8e7,
        big_net_inflow=4e7,
        mid_net_inflow=-3e7,
        small_net_inflow=-9e7,
        north_net_inflow=2.5e7,
        margin_balance=18.5e8,
    )
    print(f"✓ 模拟资金流: 主力净流入 {flow.main_net_inflow/1e4:.0f} 万元")

    panel = {"sector_name": "煤炭开采", "sector_change_pct": 0.85,
             "sh_index_change_pct": 0.32}
    print(f"✓ 模拟行业: 煤炭板块 {panel['sector_change_pct']}%, "
          f"上证 {panel['sh_index_change_pct']}%")

    # 技术指标
    print("\n计算技术指标 (akquant)...")
    indicators = ind_mod.compute_all(kline)
    print(f"✓ 指标计算完成，共 {sum(len(v) for v in indicators.values())} 个")
    for cat, vs in indicators.items():
        sample = {k: round(v, 4) if isinstance(v, (int, float)) else v
                  for k, v in list(vs.items())[:3]}
        print(f"  [{cat}] {sample} ...")

    # 报告生成
    print("\n生成报告 (post_close)...")
    md = rg.build_report("post_close", snap, kline, indicators, fin, flow, panel)
    print(f"✓ 报告长度: {len(md)} 字符")
    out = Path("/tmp/test_reports")
    out.mkdir(exist_ok=True)
    paths = rg.save_report(md, "post_close", snap, kline, indicators, fin, flow,
                           out, panel)
    print(f"✓ 报告保存: {paths}")
    print("\n===== 报告预览 (前 80 行) =====")
    for line in md.split("\n")[:80]:
        print(line)
    print("\n... (更多内容见文件)")
    print(f"\n📄 完整报告: {paths['md']}")


if __name__ == "__main__":
    main()

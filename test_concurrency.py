"""
并发安全测试：6 只股票同时写 latest_{slot}.md, 全部段落必须完整保留。

模拟 analyzer.py 的并行调用模式 (ThreadPoolExecutor 跑 6 只股票),
验证 _write_latest_md 内部的锁能避免 read-modify-write 竞争。
"""
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src import data_fetcher as df_mod
from src import indicators as ind_mod
from src import report_generator as rg
from src.config import STOCKS


def make_mock_kline(seed: int, days: int = 200) -> "pd.DataFrame":
    import numpy as np
    import pandas as pd
    np.random.seed(seed)
    end = datetime(2026, 8, 20)
    dates = pd.bdate_range(end=end, periods=days)
    price = 35 + np.cumsum(np.random.normal(0.02, 0.5, days))
    o = price + np.random.normal(0, 0.1, days)
    c = price + np.random.normal(0, 0.15, days)
    h = np.maximum(o, c) + np.abs(np.random.normal(0, 0.2, days))
    low = np.minimum(o, c) - np.abs(np.random.normal(0, 0.2, days))
    vol = np.random.randint(500_000, 5_000_000, days)
    return pd.DataFrame({
        "date": dates, "open": o.round(2), "high": h.round(2),
        "low": low.round(2), "close": c.round(2), "volume": vol,
        "amount": vol * price * 100, "amplitude": 1.0,
        "change_pct": np.random.normal(0, 1.5, days),
        "change": np.random.normal(0, 0.5, days),
        "turnover_pct": np.random.uniform(0.1, 1.5, days),
    })


def make_snap(close: float, prev_close: float):
    return df_mod.MarketSnapshot(
        price=close, open=close, high=close + 0.1, low=close - 0.1,
        pre_close=prev_close, change=close - prev_close,
        change_pct=(close / prev_close - 1) * 100,
        volume=1_000_000.0, amount=close * 1_000_000 * 100,
        turnover_pct=0.85, pe=12.5, pb=1.8, total_mv=6800.0, circ_mv=6800.0,
        as_of=datetime.now().isoformat(),
    )


def make_fin():
    return df_mod.FinancialSnapshot(
        industry="测试行业", main_business="测试主业", list_date="2020-01-01",
        total_shares=100e8, circ_shares=100e8, pe_ttm=12.5, pb=1.8,
        ps_ttm=1.9, pcf=8.2, roe=14.5, gross_margin=38.2, net_margin=22.1,
        debt_ratio=27.5, revenue_latest=2.86e11, revenue_yoy=-1.2,
        net_profit_latest=5.6e10, net_profit_yoy=-2.8, eps_latest=2.81,
        div_yield=5.2, report_date="2025-06-30",
    )


def make_flow():
    return df_mod.FundFlowSnapshot(
        main_net_inflow=1.2e8, super_net_inflow=8e7, big_net_inflow=4e7,
        mid_net_inflow=-3e7, small_net_inflow=-9e7, north_net_inflow=2.5e7,
        margin_balance=18.5e8,
    )


def analyze_one(stock, slot_id, out_dir, seed):
    """单只股票的完整流程, 不联网, 直接调 save_report"""
    kline = make_mock_kline(seed)
    snap = make_snap(float(kline["close"].iloc[-1]),
                     float(kline["close"].iloc[-2]))
    fin = make_fin()
    flow = make_flow()
    panel = {"sector_name": "测试板块", "sector_change_pct": 0.85,
             "sh_index_change_pct": 0.32}
    indicators = ind_mod.compute_all(kline)
    md = rg.build_report(slot_id, snap, kline, indicators, fin, flow,
                         stock, panel)
    rg.save_report(md, slot_id, snap, kline, indicators, fin, flow,
                   stock, out_dir, panel)


def test_concurrent_writes():
    """6 只股票并发写, 所有段落都必须保留"""
    print("=" * 60)
    print("测试 1: 6 只股票并发写 latest_post_close.md")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="cn_stock_test_"))
    try:
        slot_id = "post_close"
        latest = tmp / f"latest_{slot_id}.md"

        with ThreadPoolExecutor(max_workers=len(STOCKS)) as ex:
            futures = {
                ex.submit(analyze_one, s, slot_id, tmp, i * 100 + 7): s
                for i, s in enumerate(STOCKS)
            }
            for fut in as_completed(futures):
                s = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    print(f"  ✗ {s.symbol} 失败: {e!r}")
                    return False

        assert latest.exists(), f"latest file missing: {latest}"
        content = latest.read_text(encoding="utf-8")

        # 校验: 每只股票都有一段 "## 📊 {name}（{symbol}）"
        missing = []
        for s in STOCKS:
            marker = f"## 📊 {s.name}（{s.symbol}）"
            if marker not in content:
                missing.append(s.symbol)
        if missing:
            print(f"  ✗ 缺失段落: {missing}")
            print("  latest_md 内容 (前 1500 字):")
            print(content[:1500])
            return False

        # 校验: 每只股票的 H1 也升级成了 H3
        # 出现次数 = STOCKS 数量
        h3_count = sum(
            1 for s in STOCKS
            if f"### {s.name}（{s.symbol}）" in content
        )
        if h3_count != len(STOCKS):
            print(f"  ✗ H3 标题只匹配 {h3_count}/{len(STOCKS)}")
            return False

        # 校验: 段间分隔符 (---) 出现次数合理
        sep_count = content.count("\n---\n")
        # 至少应该有 N-1 个 (N 段之间夹 N-1 个 ---)
        if sep_count < len(STOCKS) - 1:
            print(f"  ✗ 分隔符 --- 只有 {sep_count} 个, 期望 >= {len(STOCKS) - 1}")
            return False

        print(f"  ✓ 全部 {len(STOCKS)} 只股票段落完整保留")
        print(f"  ✓ {h3_count} 个 H3 标题 (升级自 H1)")
        print(f"  ✓ {sep_count} 个 --- 分隔符")
        print(f"  ✓ 文件大小 {len(content):,} 字节")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_idempotent_replace():
    """同一天内重复跑 (模拟同 slot 重跑), 必须不重复, 也不丢段落"""
    print("=" * 60)
    print("测试 2: 同 slot 二次运行, 每只股票只能出现一次")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="cn_stock_test_idem_"))
    try:
        slot_id = "noon"
        latest = tmp / f"latest_{slot_id}.md"

        # 第一轮
        with ThreadPoolExecutor(max_workers=len(STOCKS)) as ex:
            list(ex.map(
                lambda p: analyze_one(p[0], slot_id, tmp, p[1]),
                [(s, i * 100 + 7) for i, s in enumerate(STOCKS)],
            ))

        first_content = latest.read_text(encoding="utf-8")
        first_size = len(first_content)

        # 第二轮 (idempotent), 同一批股票, 不同 seed
        with ThreadPoolExecutor(max_workers=len(STOCKS)) as ex:
            list(ex.map(
                lambda p: analyze_one(p[0], slot_id, tmp, p[1]),
                [(s, i * 100 + 99) for i, s in enumerate(STOCKS)],
            ))

        second_content = latest.read_text(encoding="utf-8")
        second_size = len(second_content)

        # 每只股票应该只出现一次
        for s in STOCKS:
            marker = f"## 📊 {s.name}（{s.symbol}）"
            n = second_content.count(marker)
            if n != 1:
                print(f"  ✗ {s.symbol} 出现 {n} 次, 期望 1")
                return False

        # 两次大小差异应小于第一轮的大小 (允许 5% 波动, 因为内容有微差)
        # 不应出现 N 倍膨胀
        if second_size > first_size * 1.1:
            print(f"  ✗ 二次运行后文件膨胀: {first_size} -> {second_size}")
            return False

        print(f"  ✓ 二次运行后, 全部 {len(STOCKS)} 只股票仍只出现一次")
        print(f"  ✓ 文件大小稳定: {first_size} -> {second_size} 字节")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_concurrent_two_slots():
    """两个 slot 同时跑, 互不干扰 (锁是 per-slot)"""
    print("=" * 60)
    print("测试 3: 两个 slot 并行, latest_post_close / latest_noon 互不影响")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="cn_stock_test_2slots_"))
    try:
        # barrier 让两个 slot 的 6 个 worker 严格同时起跑, 最大化冲突
        barrier = threading.Barrier(len(STOCKS) * 2)

        def run_slot(slot_id, seed_offset):
            def worker(payload):
                stock, seed = payload
                barrier.wait()  # 等所有 worker 就位后同时开跑
                analyze_one(stock, slot_id, tmp, seed)
            with ThreadPoolExecutor(max_workers=len(STOCKS)) as ex:
                list(ex.map(worker,
                            [(s, i * 100 + seed_offset) for i, s in enumerate(STOCKS)]))

        t1 = threading.Thread(target=run_slot, args=("post_close", 1))
        t2 = threading.Thread(target=run_slot, args=("noon", 10000))
        t1.start(); t2.start()
        t1.join(); t2.join()

        for slot_id in ("post_close", "noon"):
            content = (tmp / f"latest_{slot_id}.md").read_text(encoding="utf-8")
            for s in STOCKS:
                marker = f"## 📊 {s.name}（{s.symbol}）"
                if marker not in content:
                    print(f"  ✗ latest_{slot_id}.md 缺失 {s.symbol}")
                    return False
            print(f"  ✓ latest_{slot_id}.md 包含全部 {len(STOCKS)} 只股票")

        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)

    results = []
    results.append(("concurrent_writes", test_concurrent_writes()))
    print()
    results.append(("idempotent_replace", test_idempotent_replace()))
    print()
    results.append(("concurrent_two_slots", test_concurrent_two_slots()))
    print()
    print("=" * 60)
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    print("=" * 60)
    sys.exit(0 if all(ok for _, ok in results) else 1)
